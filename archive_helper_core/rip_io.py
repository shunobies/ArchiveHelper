"""Disc ripping and recovery IO helpers."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import (
    _run_ddrescue_iso_recovery,
    _run_dvdbackup_recovery,
    _run_vobcopy_recovery,
    find_mkvs_in_dir,
    map_selected_title_indexes_to_mkvs,
    rip_disc_if_needed,
    run_makemkv_with_progress_to_dir,
)

__all__ = [
    "run_makemkv_with_progress_to_dir",
    "_run_ddrescue_iso_recovery",
    "_run_dvdbackup_recovery",
    "_run_vobcopy_recovery",
    "find_mkvs_in_dir",
    "map_selected_title_indexes_to_mkvs",
    "rip_disc_if_needed",
]
