import os
from pathlib import Path

import pytest

from ha_satimage.config import (
    VALID_DRIVERS,
    AppConfig,
    ConfigStore,
    RegionConfig,
    SourceDefinition,
    SourcesConfig,
    StorageConfig,
    default_config,
    load_config,
    save_config,
)


def test_default_config_has_vienna_mallorca_and_europe():
    config = default_config()
    names = {r.name for r in config.regions}
    assert names == {"wien", "mallorca", "europe"}
    overview = config.region("europe")
    assert overview.borders is True
    assert (overview.width, overview.height) == (1400, 1280)
    assert overview.bounding_box().as_tuple() == (33, -25, 72, 45)
    assert overview.composite == "natural_color_raw_with_night_ir"


def test_region_bounding_box_uses_center_and_radius():
    region = RegionConfig(name="test", lat=48.2, lon=16.4, radius_km=100)
    bbox = region.bounding_box()
    assert bbox.lat_min < 48.2 < bbox.lat_max
    assert bbox.lon_min < 16.4 < bbox.lon_max


def test_region_rejects_source_missing_from_catalog():
    with pytest.raises(ValueError, match="unknown source"):
        AppConfig(regions=[RegionConfig(name="t", lat=0, lon=0, radius_km=10, source="nope")])


def test_default_catalog_covers_all_drivers():
    config = default_config()
    assert {e.driver for e in config.sources.catalog} == set(VALID_DRIVERS)
    assert [e.id for e in config.sources.catalog if e.enabled] == ["msg_seviri"]
    for region in config.regions:
        AppConfig(sources=config.sources, regions=[region.model_copy(update={"source": "dummy"})])


def test_catalog_rejects_unknown_driver_and_duplicate_ids():
    with pytest.raises(ValueError, match="driver"):
        SourceDefinition(id="x", driver="nope")
    with pytest.raises(ValueError, match="more than once"):
        SourcesConfig(catalog=[{"id": "a", "driver": "dummy"}, {"id": "a", "driver": "dummy"}])


def test_custom_catalog_entry_can_be_used_by_region():
    config = AppConfig(
        sources={"catalog": [{"id": "iodc", "driver": "msg_seviri", "collection": "EO:EUM:DAT:MSG:HRSEVIRI-IODC"}]},
        regions=[{"name": "r", "lat": 0, "lon": 60, "radius_km": 100, "source": "iodc"}],
    )
    assert config.sources.get("iodc").driver == "msg_seviri"
    assert config.sources.is_enabled("iodc")
    assert config.sources.collection_for("iodc") == "EO:EUM:DAT:MSG:HRSEVIRI-IODC"


def test_default_msg_seviri_uses_rapid_scan_and_legacy_collection_is_migrated():
    assert SourcesConfig().collection_for("msg_seviri") == "EO:EUM:DAT:MSG:MSG15-RSS"
    legacy = SourcesConfig(**{"active": ["msg_seviri"], "msg_collection": "EO:EUM:DAT:MSG:HRSEVIRI"})
    assert legacy.collection_for("msg_seviri") == "EO:EUM:DAT:MSG:HRSEVIRI"
    assert legacy.is_enabled("msg_seviri")
    assert "msg_collection" not in legacy.model_dump()


def test_legacy_active_list_is_migrated_to_catalog():
    sources = SourcesConfig(**{"active": ["mtg_fci"], "poll_interval_minutes": 10})
    enabled = {e.id for e in sources.catalog if e.enabled}
    assert enabled == {"mtg_fci"}
    assert "active" not in sources.model_dump()


def test_storage_path_must_be_absolute():
    with pytest.raises(ValueError, match="absolute"):
        StorageConfig(frames_dir="relative/path")
    assert StorageConfig(frames_dir="/mnt/data/ha_satimage/").frames_dir == "/mnt/data/ha_satimage"
    assert StorageConfig(frames_dir="").frames_dir == ""


def test_effective_max_frames_defaults_to_60_minutes():
    region = RegionConfig(name="test", lat=0, lon=0, radius_km=10)
    assert region.effective_max_frames(poll_interval_minutes=15) == 4
    assert region.effective_max_frames(poll_interval_minutes=60) == 1
    assert region.effective_max_frames(poll_interval_minutes=1) == 60


def test_history_minutes_controls_max_frames():
    config = default_config()
    config.history.history_minutes = 24 * 60
    # Rapid Scan delivers every 5 minutes -> 288 frames for 24 hours.
    assert config.max_frames_for(config.regions[0]) == 288


def test_cycle_per_source_from_collection_driver_or_override():
    config = default_config()
    sources = config.sources
    assert sources.cycle_for("msg_seviri") == 5  # Rapid Scan
    assert sources.cycle_for("msg_seviri_0deg") == 15
    assert sources.cycle_for("mtg_fci") == 10
    # Without a known cycle: general poll interval.
    sources.poll_interval_minutes = 7
    assert sources.cycle_for("dummy") == 7
    assert sources.cycle_for("does-not-exist") == 7
    # Unknown collection: driver's cycle; an explicit value takes precedence.
    fci = sources.get("mtg_fci")
    fci.collection = "EO:EUM:DAT:0999"
    assert sources.cycle_for("mtg_fci") == 10
    fci.cycle_minutes = 20
    assert sources.cycle_for("mtg_fci") == 20
    config.regions[0].source = "mtg_fci"
    config.history.history_minutes = 60
    assert config.max_frames_for(config.regions[0]) == 3


def test_effective_max_frames_can_be_overridden():
    region = RegionConfig(name="test", lat=0, lon=0, radius_km=10, max_frames=10)
    assert region.effective_max_frames(poll_interval_minutes=15) == 10


def test_save_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "config.yaml"
    config = default_config()
    save_config(config, path)

    loaded = load_config(path)
    assert loaded.regions[0].name == config.regions[0].name
    assert loaded.eumetsat.consumer_key == config.eumetsat.consumer_key


def test_load_config_returns_default_when_missing(tmp_path: Path):
    path = tmp_path / "does-not-exist.yaml"
    config = load_config(path)
    assert len(config.regions) == 3


@pytest.mark.parametrize("loader", [load_config, lambda path: ConfigStore(path).stored()])
def test_upgrade_adds_overview_once_and_preserves_existing_locations(tmp_path, loader):
    import yaml

    path = tmp_path / "config.yaml"
    raw = default_config().model_dump()
    raw.pop("europe_overview_added")
    raw["regions"] = raw["regions"][:2]
    path.write_text(yaml.safe_dump(raw))
    upgraded = loader(path)
    assert [r.model_dump() for r in upgraded.regions[:2]] == raw["regions"]
    assert upgraded.region("europe").borders is True
    assert yaml.safe_load(path.read_text())["europe_overview_added"] is True
    assert len(loader(path).regions) == 3
    upgraded.regions = upgraded.regions[:2]
    save_config(upgraded, path)
    assert loader(path).region("europe") is None


@pytest.mark.parametrize("reason", ["full", "exists", "disabled", "fci", "custom-source"])
def test_upgrade_respects_location_limit_names_and_sources(tmp_path, reason):
    import yaml

    from ha_satimage.config import DEFAULT_FCI_COMPOSITE

    path = tmp_path / "config.yaml"
    raw = default_config().model_dump()
    raw.pop("europe_overview_added")
    if reason != "exists":
        raw["regions"] = raw["regions"][:2]
    if reason == "full":
        raw["regions"] += [
            {**raw["regions"][0], "name": name} for name in ("berlin", "rome")
        ]
    if reason in ("disabled", "fci"):
        for entry in raw["sources"]["catalog"]:
            entry["enabled"] = reason == "fci" and entry["id"] == "mtg_fci"
    if reason == "custom-source":
        raw["sources"]["catalog"] = [
            {"id": "custom", "driver": "msg_seviri", "enabled": True}
        ]
        for region in raw["regions"]:
            region["source"] = "custom"
    path.write_text(yaml.safe_dump(raw))
    upgraded = load_config(path)
    assert [r.model_dump() for r in upgraded.regions[:len(raw["regions"])]] == raw["regions"]
    if reason in ("full", "disabled"):
        assert upgraded.region("europe") is None
    elif reason == "fci":
        assert upgraded.region("europe").source == "mtg_fci"
        assert upgraded.region("europe").composite == DEFAULT_FCI_COMPOSITE
    elif reason == "custom-source":
        assert upgraded.region("europe").source == "custom"
    else:
        assert len(upgraded.regions) == 3


@pytest.mark.parametrize("extent", [
    (-25, 72, 45, 33), (45, 33, -25, 72), (-181, 33, 45, 72),
    (-25, 33, 45, 91), (float("nan"), 33, 45, 72),
])
def test_region_rejects_invalid_extent(extent):
    with pytest.raises(ValueError, match="extent"):
        RegionConfig(name="test", lat=0, lon=0, radius_km=100, lonlat_extent=extent)


def test_env_overrides_take_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "config.yaml"
    config = default_config()
    config.eumetsat.consumer_key = "from-file"
    config.eumetsat.consumer_secret = "file-secret"
    save_config(config, path)

    monkeypatch.setenv("EUMETSAT_CONSUMER_KEY", "from-env")
    monkeypatch.setenv("EUMETSAT_CONSUMER_SECRET", "env-secret")

    loaded = load_config(path)
    assert loaded.eumetsat.consumer_key == "from-env"
    assert loaded.eumetsat.consumer_secret == "env-secret"


def test_env_override_not_persisted_back_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "config.yaml"
    config = default_config()
    config.eumetsat.consumer_key = "from-file"
    save_config(config, path)

    monkeypatch.setenv("EUMETSAT_CONSUMER_KEY", "from-env")
    store = ConfigStore(path)
    assert store.get().eumetsat.consumer_key == "from-env"

    # The file on disk still contains the original value.
    raw = path.read_text(encoding="utf-8")
    assert "from-file" in raw
    assert "from-env" not in raw


def test_masked_hides_secret_but_keeps_key():
    config = default_config()
    config.eumetsat.consumer_key = "abc"
    config.eumetsat.consumer_secret = "supersecretvalue"
    masked = config.masked()
    assert masked.eumetsat.consumer_key == "abc"
    assert masked.eumetsat.consumer_secret != "supersecretvalue"
    assert masked.eumetsat.consumer_secret.endswith("alue")


def test_config_store_update_persists(tmp_path: Path):
    path = tmp_path / "config.yaml"
    store = ConfigStore(path)
    config = store.get()
    config.sources.poll_interval_minutes = 30
    store.update(config)

    reloaded = ConfigStore(path)
    assert reloaded.get().sources.poll_interval_minutes == 30
