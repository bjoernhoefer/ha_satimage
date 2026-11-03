"""scripts/ha_measure.sh against stubbed host tools (no root, no Docker).

Regression for the first measurement on nzbpi: host disk counters above
2^31 bytes showed 0 MiB (mawk %d), a repeated vmstat header turned the
iowait maximum into 0 %, and a missing /api/diagnostics printed a traceback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

VMSTAT = """#!/bin/bash
cat <<'OUT'
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st gu
 1  0      0 100000 1000 200000    0    0     5     5  100  200  5  2 90 50  0  0
 1  0      0 100000 1000 200000    0    0     5     5  100  200  5  2 90  2  0  0
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st gu
 1  1      0 100000 1000 200000    0    0     5     5  100  200  5  2 80 12  0  0
 0  0      0 100000 1000 200000    0    0     5     5  100  200  5  2 90  4  0  0
OUT
"""


def _diskstats(read_sectors: int, written_sectors: int) -> str:
    fields = lambda r, w: f"0 0 {r} 0 0 0 {w} 0 0 0 0 0 0 0 0 0 0"
    return (
        f" 179       0 mmcblk0 {fields(read_sectors, written_sectors)}\n"
        f" 259       0 nvme0n1 {fields(read_sectors * 2, written_sectors * 2)}\n"
        f"   8       0 sda {fields(1, 1)}\n"
    )


def test_measure_reports_large_counters_iowait_and_missing_diagnostics(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "vmstat": VMSTAT,
        "journalctl": "#!/bin/bash\necho 'mmc0: Card stuck being busy! __mmc_poll_for_busy'\n",
        "curl": "#!/bin/bash\necho '<html>not json</html>'\n",
        # Grows the counters after the "sleep" so the interval has a difference.
        "sleep": f"#!/bin/bash\ncp {tmp_path}/diskstats.after {tmp_path}/diskstats\n"
                 f"cp {tmp_path}/io.after {tmp_path}/cg/io.stat\n",
        "awk": f"#!/bin/bash\nexec {shutil.which('mawk') or shutil.which('awk')} \"$@\"\n",
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    base = 5_000_000_000 // 512  # > 2^31 bytes
    (tmp_path / "diskstats").write_text(_diskstats(base, base))
    (tmp_path / "diskstats.after").write_text(_diskstats(base + 2048 * 10, base + 2048 * 3))
    cg = tmp_path / "cg"
    cg.mkdir()
    gib = 1 << 30
    (cg / "io.stat").write_text(f"179:0 rbytes={5 * gib} wbytes={3 * gib} rios=1\n")
    (tmp_path / "io.after").write_text(f"179:0 rbytes={5 * gib + 20 * 2**20} wbytes={3 * gib} rios=2\n")
    (cg / "memory.current").write_text(str(700 * 2**20))
    (cg / "memory.peak").write_text(str(1626 * 2**20))
    (cg / "memory.max").write_text("805306368")
    (cg / "memory.events").write_text("oom 0\noom_kill 3\n")

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/ha_measure.sh"), "ha_satellite", "0"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
             "DISKSTATS": str(tmp_path / "diskstats"), "CGROUP_DIR": str(cg)},
        capture_output=True, text=True, timeout=60,
    )
    out = result.stdout
    assert result.returncode == 0, result.stderr + out
    assert "Traceback" not in out + result.stderr
    # Boot average (50) skipped, repeated header ignored: 2, 12, 4.
    assert "iowait: avg 6.0%, max 12% (vmstat, 3 samples" in out
    assert "mmcblk0      read     10.0  written      3.0" in out
    assert "nvme0n1      read     20.0  written      6.0" in out
    assert "sda" not in out
    assert "179:0    read     20.0  written      0.0" in out
    assert "peak in the interval" in out
    assert "Kernel 'Card stuck being busy' messages: 1" in out
    assert "(not available - version without /api/diagnostics)" in out
