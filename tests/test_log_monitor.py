"""Hourly log monitor: workflow contract, log fetch on the host, analysis and issues."""

import base64
import gzip
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/log-monitor.yml"
REPO = "bjoernhoefer/ha_satimage"
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)

spec = importlib.util.spec_from_file_location("log_monitor", ROOT / "scripts/log_monitor.py")
lm = importlib.util.module_from_spec(spec)
sys.modules["log_monitor"] = lm
spec.loader.exec_module(lm)


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def kb():
    return lm.parse_kb((ROOT / "PROBLEM-SOLUTIONS.md").read_text())


def encode(entries):
    return base64.b64encode(gzip.compress(json.dumps({"entries": entries}).encode())).decode()


def entry(message, level="ERROR", ts=NOW - timedelta(minutes=10), logger="ha_satimage.scheduler", id=1):
    return {"id": id, "ts": ts.isoformat(), "level": level, "logger": logger, "message": message}


# --------------------------------------------------------------------------
# Workflow contract


def test_runs_hourly_and_manually(workflow):
    triggers = workflow[True]
    assert triggers["schedule"] == [{"cron": "17 * * * *"}]
    assert triggers["workflow_dispatch"]["inputs"]["targets"]["options"] == [
        "configured", "pi", "azure", "both",
    ]
    assert workflow["permissions"] == {"contents": "read"}


@pytest.mark.parametrize(
    "job,labels",
    [
        ("collect-pi", ["self-hosted", "linux", "ARM64", "deploy-satellite"]),
        ("collect-azure", ["self-hosted", "linux", "X64", "deploy-satellite-azure"]),
    ],
)
def test_host_jobs_run_no_repository_code(workflow, job, labels):
    collect = workflow["jobs"][job]
    assert collect["runs-on"] == labels
    assert collect["permissions"] == {}
    assert all("uses" not in step for step in collect["steps"])
    assert "${{" not in collect["steps"][0]["run"]
    assert collect["steps"][0]["run"] == workflow["jobs"]["collect-pi"]["steps"][0]["run"]
    assert set(collect["outputs"]) == {"status", "detail", "version", "logs"}


def test_analyze_job_permissions_and_inputs(workflow):
    analyze = workflow["jobs"]["analyze"]
    assert analyze["runs-on"] == "ubuntu-latest"
    assert analyze["permissions"] == {"contents": "read", "issues": "write", "models": "read"}
    assert analyze["concurrency"]["group"] == "log-monitor-analyze"
    env = analyze["steps"][1]["env"]
    for target in ("PI", "AZURE"):
        for name in ("RESULT", "STATUS", "DETAIL", "VERSION", "LOGS"):
            assert f"LOG_MONITOR_{target}_{name}" in env
    assert env["COPILOT_ASSIGN_TOKEN"] == "${{ secrets.COPILOT_ASSIGN_TOKEN }}"
    assert "${{" not in analyze["steps"][1]["run"]


def run_step(tmp_path, step, stubs=None, **env):
    for name, script in (stubs or {}).items():
        executable = tmp_path / name
        executable.write_text("#!/bin/bash\n" + script)
        executable.chmod(0o755)
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "GITHUB_OUTPUT": str(tmp_path / "output"), **env},
        capture_output=True, text=True,
    )


def outputs(tmp_path):
    result = {}
    for line in (tmp_path / "output").read_text().splitlines():
        key, _, value = line.partition("=")
        result[key] = value
    return result


@pytest.mark.parametrize(
    "requested,monitor,deploy,expected",
    [
        ("", "", "", ("true", "false")),
        ("configured", "", "azure", ("false", "true")),
        ("configured", "both", "pi", ("true", "true")),
        ("configured", "none", "both", ("false", "false")),
        ("azure", "pi", "", ("false", "true")),
        ("both", "", "", ("true", "true")),
    ],
)
def test_target_resolution(tmp_path, workflow, requested, monitor, deploy, expected):
    result = run_step(
        tmp_path, workflow["jobs"]["targets"]["steps"][0],
        REQUESTED=requested, MONITOR_TARGETS=monitor, DEPLOY_TARGETS=deploy,
    )
    assert result.returncode == 0, result.stderr
    assert (outputs(tmp_path)["pi"], outputs(tmp_path)["azure"]) == expected


@pytest.mark.parametrize("configured", ["nas", "pi;id", "$(id)", "pi\nazure=x"])
def test_invalid_targets_fail(tmp_path, workflow, configured):
    result = run_step(tmp_path, workflow["jobs"]["targets"]["steps"][0],
                      REQUESTED="configured", MONITOR_TARGETS=configured)
    assert result.returncode != 0
    assert "::error::" in result.stdout


CURL_OK = """
url="${@: -1}"
out=""
while [[ $# -gt 0 ]]; do [[ "$1" == "-o" ]] && out="$2"; shift; done
echo "$url" >> "$GITHUB_OUTPUT.urls"
case "$url" in
  */api/version) echo '{"version": "1.12.0"}' ;;
  */api/logs*) cat "$LOG_FIXTURE" > "$out" ;;
esac
"""


def test_fetch_success(tmp_path, workflow):
    fixture = tmp_path / "logs.json"
    entries = [entry("Download msg_seviri failed: boom")]
    fixture.write_text(json.dumps({"entries": entries}))
    result = run_step(tmp_path, workflow["jobs"]["collect-pi"]["steps"][0],
                      {"curl": CURL_OK}, LOG_FIXTURE=str(fixture), PORT="")
    assert result.returncode == 0, result.stderr
    out = outputs(tmp_path)
    assert out["status"] == "ok"
    assert out["version"] == "1.12.0"
    assert lm.decode_logs(out["logs"]) == entries
    assert (tmp_path / "output.urls").read_text().splitlines() == [
        "http://127.0.0.1:6060/api/logs?limit=0", "http://127.0.0.1:6060/api/version",
    ]


def test_fetch_unreachable_is_reported_not_failed(tmp_path, workflow):
    result = run_step(
        tmp_path, workflow["jobs"]["collect-pi"]["steps"][0],
        {"curl": 'echo "curl: (7) Failed to connect to 127.0.0.1 port 6061" >&2\nexit 7\n'},
        PORT="6061",
    )
    assert result.returncode == 0, result.stderr
    out = outputs(tmp_path)
    assert out["status"] == "unreachable"
    assert "Failed to connect" in out["detail"]
    assert "logs" not in out


@pytest.mark.parametrize("port", ["0", "70000", "80;id", "abc"])
def test_fetch_rejects_invalid_port(tmp_path, workflow, port):
    result = run_step(tmp_path, workflow["jobs"]["collect-pi"]["steps"][0],
                      {"curl": "touch \"$GITHUB_OUTPUT.called\"\n"}, PORT=port)
    assert result.returncode != 0
    assert not (tmp_path / "output.called").exists()


# --------------------------------------------------------------------------
# Knowledge base


def test_knowledge_base_entries_are_valid(kb):
    ids = [item.id for item in kb]
    assert len(ids) >= 8 and len(set(ids)) == len(ids)
    text = (ROOT / "PROBLEM-SOLUTIONS.md").read_text()
    assert text.count("\n## PS-") == len(kb), "every PS entry needs a valid Pattern"
    for item in kb:
        for name in ("symptom", "cause", "solution", "fixed in"):
            assert item.fields.get(name), f"{item.id} lacks {name}"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("No EUMETSAT credentials configured (web UI or environment variables)", "PS-001"),
        ("Download msg_seviri failed: Data Store search failed: 401 Unauthorized", "PS-002"),
        ("No product in EO:EUM:DAT:0662 within the last 3 hours", "PS-003"),
        ("Rendering for vienna failed: Render process exceeded the memory budget of 576 MiB "
         "(RSS 600 MiB) and was stopped - paused until 20:30 UTC", "PS-004"),
        ("Render process killed by the kernel OOM killer (container memory limit); peak RSS 700 MiB", "PS-004"),
        ("Archive mallorca / mtg_fci_0665 / ir_sandwich 20261112T201923Z failed: Render process killed "
         "by the kernel OOM killer (container memory limit); peak RSS 1264 MiB - paused until 20:53 UTC", "PS-009"),
        ("Rendering exceeded the time limit of 300s", "PS-005"),
        ("Storage problem - downloads and renders paused: free space 100 MB", "PS-006"),
        ("Rendering for x failed: Composite 'airmass' could not be generated", "PS-007"),
        ("ha_satimage service unreachable on pi: refused", "PS-008"),
        ("Something completely new happened", None),
    ],
)
def test_knowledge_base_matches(kb, message, expected):
    found = lm.match_kb(kb, message)
    assert (found.id if found else None) == expected


def test_kb_anchor_matches_github_slug(kb):
    assert kb[0].anchor == "ps-001-eumetsat-credentials-missing"


# --------------------------------------------------------------------------
# Redaction and grouping


@pytest.mark.parametrize(
    "raw,secret",
    [
        ("token=abc123SECRET failed", "abc123SECRET"),
        ("Authorization: Bearer eyJhbGciOi.xyz", "eyJhbGciOi"),
        ("consumer_secret: 'ZZtop99'", "ZZtop99"),
        ("https://user:pw@example.org/x", "user:pw"),
        ("connect to 192.168.1.20 failed", "192.168.1.20"),
        ("mail bjoern@example.org", "bjoern@example.org"),
        ("key aB3dE5gH7jK9mN1pQ3sT5vX7z here", "aB3dE5gH7jK9mN1pQ3sT5vX7z"),
    ],
)
def test_redaction(raw, secret):
    assert secret not in lm.redact(raw)


def test_redaction_keeps_useful_text():
    text = "Composite 'natural_color_hrv_with_night_ir' failed for MSG4-SEVI-MSG15-0100-NA-20261110201241"
    assert lm.redact(text) == text


def test_fingerprint_ignores_numbers_and_timestamps():
    a = lm.fingerprint("x", "Region took 12.5 s at 2026-11-10T20:00:00Z\nTraceback...\nValueError: 3")
    b = lm.fingerprint("x", "Region took 99 s at 2026-11-11T08:15:00+00:00\nTraceback...\nValueError: 7")
    c = lm.fingerprint("x", "Region took 99 s\nTraceback...\nKeyError: 7")
    assert a == b != c


def test_collect_groups_filters_levels_window_and_groups_known(kb):
    target = lm.TargetLogs("pi", "ok", "1.12.0", entries=[
        entry("Rendering exceeded the time limit of 300s", id=1),
        entry("Rendering exceeded the time limit of 280s", id=2, logger="other"),
        entry("Just info", level="INFO", id=3),
        entry("Old error", ts=NOW - timedelta(hours=30), id=4),
        entry("New thing broke", level="CRITICAL", id=5),
    ])
    groups = lm.collect_groups([target], kb, NOW, timedelta(hours=24))
    assert len(groups["kb-ps-005"].occurrences) == 2
    unknown = [group for key, group in groups.items() if key != "kb-ps-005"]
    assert len(unknown) == 1 and unknown[0].kb is None
    assert unknown[0].title() == "[log-monitor] ha_satimage.scheduler: New thing broke"


def test_unreachable_target_becomes_an_error(kb):
    target = lm.TargetLogs("azure", "unreachable", detail="curl: (7) refused 10.0.0.4")
    groups = lm.collect_groups([target], kb, NOW, timedelta(hours=24))
    group = groups["unreachable-azure"]
    assert group.kb.id == "PS-008"
    assert "10.0.0.4" not in group.sample.message


def test_read_targets(kb):
    env = {
        "LOG_MONITOR_PI_RESULT": "success", "LOG_MONITOR_PI_STATUS": "ok",
        "LOG_MONITOR_PI_VERSION": "1.12.0", "LOG_MONITOR_PI_LOGS": encode([entry("x")]),
        "LOG_MONITOR_AZURE_RESULT": "skipped",
    }
    targets, warnings = lm.read_targets(["pi", "azure"], env)
    assert [t.name for t in targets] == ["pi"] and targets[0].entries[0]["message"] == "x"
    assert warnings == []
    targets, warnings = lm.read_targets(["pi"], {"LOG_MONITOR_PI_RESULT": "failure"})
    assert targets == [] and "failure" in warnings[0]
    targets, warnings = lm.read_targets(
        ["pi"], {"LOG_MONITOR_PI_RESULT": "success", "LOG_MONITOR_PI_STATUS": "ok",
                 "LOG_MONITOR_PI_LOGS": "not base64!"})
    assert targets == [] and "not valid" in warnings[0]


# --------------------------------------------------------------------------
# Issue handling


class FakeGitHub:
    def __init__(self):
        self.issues = {}
        self.comments = []
        self.assigned = []
        self.labels = 0

    def monitor_issues(self):
        return [dict(issue) for issue in self.issues.values()]

    def ensure_labels(self):
        self.labels += 1

    def create_issue(self, title, body, labels):
        number = len(self.issues) + 1
        self.issues[number] = {"number": number, "title": title, "body": body,
                               "labels": labels, "state": "open"}
        return self.issues[number]

    def update_issue(self, number, **fields):
        self.issues[number].update(fields)
        return self.issues[number]

    def comment(self, number, body):
        self.comments.append((number, body))

    def assign_copilot(self, number, branch, instructions, token):
        self.assigned.append((number, branch, token))


def run(gh, kb, entries, now=NOW, **options):
    target = lm.TargetLogs("pi", "ok", "1.12.0", entries=entries)
    groups = lm.collect_groups([target], kb, now, timedelta(hours=24))
    opts = lm.Options(repo=REPO, copilot_token="tok", **options)
    return lm.process(groups, gh.monitor_issues(), gh, kb, {"pi": "1.12.0"}, opts)


def test_creates_issues_and_assigns_only_unknown_to_copilot(kb):
    gh = FakeGitHub()
    summary = run(gh, kb, [
        entry("Rendering exceeded the time limit of 300s", id=1),
        entry("Brand new @someone ``` failure", id=2),
    ])
    assert len(gh.issues) == 2 and gh.labels == 1
    known, unknown = gh.issues[1], gh.issues[2]
    assert known["labels"] == ["log-monitor", "known-problem"]
    assert "PS-005" in known["title"] and "**Solution:**" in known["body"]
    assert f"https://github.com/{REPO}/blob/main/PROBLEM-SOLUTIONS.md#ps-005-" in known["body"]
    assert unknown["labels"] == ["log-monitor", "needs-analysis"]
    assert "@someone" not in unknown["body"] and "``` failure" not in unknown["body"]
    assert lm.read_marker(unknown["body"])["last_seen"]["pi"]
    assert gh.assigned == [(2, "main", "tok")]
    assert any("created #2" in line for line in summary)


def test_rerun_does_not_report_twice_and_updates_counts(kb):
    gh = FakeGitHub()
    first = entry("Brand new failure", id=1)
    run(gh, kb, [first])
    body = gh.issues[1]["body"]
    assert run(gh, kb, [first]) == [] and gh.issues[1]["body"] == body
    later = entry("Brand new failure", id=2, ts=NOW - timedelta(minutes=2))
    summary = run(gh, kb, [first, later])
    assert summary == ["- updated #1: 1 new occurrence(s)"]
    marker = lm.read_marker(gh.issues[1]["body"])
    assert marker["count"] == 2
    assert "| Occurrences reported | 2 |" in gh.issues[1]["body"]
    assert len(gh.issues) == 1 and gh.comments == [] and len(gh.assigned) == 1


def test_closed_issue_is_reopened_on_recurrence(kb):
    gh = FakeGitHub()
    run(gh, kb, [entry("Brand new failure", id=1, ts=NOW - timedelta(hours=2))])
    gh.issues[1].update(state="closed", state_reason="completed",
                        closed_at=(NOW - timedelta(hours=1)).isoformat())
    # Occurrence before closing (still in the buffer) does not reopen.
    late = entry("Brand new failure", id=2, ts=NOW - timedelta(minutes=90))
    assert run(gh, kb, [late]) == []
    recur = entry("Brand new failure", id=3, ts=NOW - timedelta(minutes=5))
    summary = run(gh, kb, [late, recur])
    assert gh.issues[1]["state"] == "open"
    assert summary == ["- reopened #1: 1 new occurrence(s)"]
    assert gh.comments and "occurred again" in gh.comments[0][1]
    assert len(gh.assigned) == 1


def test_not_planned_issue_stays_closed(kb):
    gh = FakeGitHub()
    run(gh, kb, [entry("Brand new failure", id=1, ts=NOW - timedelta(hours=2))])
    gh.issues[1].update(state="closed", state_reason="not_planned",
                        closed_at=(NOW - timedelta(hours=1)).isoformat())
    run(gh, kb, [entry("Brand new failure", id=2, ts=NOW - timedelta(minutes=5))])
    assert gh.issues[1]["state"] == "closed" and gh.comments == []
    assert lm.read_marker(gh.issues[1]["body"])["count"] == 2


def test_limits_new_issues_and_copilot_assignments(kb):
    gh = FakeGitHub()
    entries = [entry(f"Failure kind {chr(65 + i)}", id=i, logger=f"l{i}") for i in range(5)]
    summary = run(gh, kb, entries, max_new_issues=3, max_copilot=2)
    assert len(gh.issues) == 3 and len(gh.assigned) == 2
    assert sum("deferred" in line for line in summary) == 2
    run(gh, kb, entries, max_new_issues=3, max_copilot=2)
    assert len(gh.issues) == 5


def test_copilot_mode_none_and_dry_run(kb):
    gh = FakeGitHub()
    run(gh, kb, [entry("Brand new failure")], copilot_mode="none")
    assert len(gh.issues) == 1 and gh.assigned == []
    dry = FakeGitHub()
    summary = run(dry, kb, [entry("Other failure")], dry_run=True)
    assert dry.issues == {} and summary[0].startswith("- would create")


def test_main_end_to_end(tmp_path, monkeypatch, kb):
    gh = FakeGitHub()
    monkeypatch.setattr(lm, "GitHub", lambda repo: gh)
    summary_file = tmp_path / "summary.md"
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    env = {
        "LOG_MONITOR_PI_RESULT": "success", "LOG_MONITOR_PI_STATUS": "ok",
        "LOG_MONITOR_PI_VERSION": "1.12.0",
        "LOG_MONITOR_PI_LOGS": encode([entry("Rendering exceeded the time limit of 300s", ts=recent)]),
        "LOG_MONITOR_AZURE_RESULT": "success", "LOG_MONITOR_AZURE_STATUS": "unreachable",
        "LOG_MONITOR_AZURE_DETAIL": "curl: (7) refused",
        "GITHUB_STEP_SUMMARY": str(summary_file), "GH_TOKEN": "", "COPILOT_ASSIGN_TOKEN": "",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert lm.main(["--repo", REPO, "--kb", str(ROOT / "PROBLEM-SOLUTIONS.md")]) == 0
    titles = sorted(issue["title"] for issue in gh.issues.values())
    assert titles == [
        "[log-monitor] PS-005: Rendering exceeded the time limit",
        "[log-monitor] PS-008: Log monitor cannot reach the service",
    ]
    assert "## Log monitor" in summary_file.read_text()


def test_main_rejects_bad_arguments():
    assert lm.main(["--repo", "nope"]) == 2
    assert lm.main(["--repo", REPO, "--targets", "pi;id"]) == 2
