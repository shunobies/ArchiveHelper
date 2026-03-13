from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_helper_core import cli
from archive_helper_core import manifest as manifest_mod
from archive_helper_core import workflows_series as series_mod
from archive_helper_core.rip_io import map_selected_title_indexes_to_mkvs
import archive_helper_core._legacy_rip_and_encode_server as legacy


def test_title_mapping_prefers_explicit_title_indexes(tmp_path: Path) -> None:
    mkv_a = tmp_path / "movie_title_t03.mkv"
    mkv_b = tmp_path / "movie_title_t01.mkv"
    mkv_c = tmp_path / "movie_title_t07.mkv"
    for p in (mkv_a, mkv_b, mkv_c):
        p.write_text("x", encoding="utf-8")

    mapped, missing = map_selected_title_indexes_to_mkvs(
        mkvs=[mkv_a, mkv_b, mkv_c],
        selected_indexes=[1, 3, 9],
    )

    assert mapped[1] == mkv_b
    assert mapped[3] == mkv_a
    assert missing == [9]


def test_manifest_salvage_recovers_valid_items_from_corrupt_payload() -> None:
    raw = '{"junk": 1} trash {"source_title_index": 2, "output": "/tmp/t2.mp4", "input_rel": "x"} trailing'

    salvaged = manifest_mod._salvage_disc_manifest_from_text(raw)

    assert salvaged is not None
    assert salvaged["version"] == legacy.DISC_MANIFEST_VERSION
    assert salvaged["needs_revalidation"] is True
    assert salvaged["items"] == [
        {
            "source_title_index": 2,
            "output": "/tmp/t2.mp4",
            "input_rel": "x",
            "state": "ripped",
            "last_error": "",
        }
    ]


def test_series_plan_order_is_deterministic_with_episode_hints(monkeypatch) -> None:
    mkvs = [Path("title_t10.mkv"), Path("title_t03.mkv"), Path("title_t02.mkv")]

    titles = {
        "title_t10.mkv": "Episode 03",
        "title_t03.mkv": "Episode 01",
        "title_t02.mkv": "Episode 02",
    }
    monkeypatch.setattr(legacy, "ffprobe_meta_title", lambda path: titles[path.name])

    ordered = series_mod._series_plan_order(mkvs)

    assert [p.name for p in ordered] == ["title_t03.mkv", "title_t02.mkv", "title_t10.mkv"]


def test_cli_normalization_implies_overlap_for_csv() -> None:
    rc = cli.main(["--csv", "schedule.csv", "--encode-jobs", "0", "--preset", "HQ 1080p30 Surround"])
    assert rc == 2


def test_parse_args_parity_for_tmdb_defaults() -> None:
    ns = cli.parse_args(["--tmdb-search", "Aliens"])
    assert ns.tmdb_search == "Aliens"
    assert ns.tmdb_media_type == "movie"
    assert ns.tmdb_limit == 8
