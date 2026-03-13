"""Encoding, subtitle extraction, and encode lock helpers."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import (
    _ensure_log_dir,
    _gzip_compress,
    _normalize_subtitle_language,
    _preset_target_height,
    _refresh_mp4_quality_metadata,
    _resolution_label_from_height,
    _subtitle_output_path,
    create_lock_or_fail,
    encode_lock_path,
    extract_external_subtitles,
    ffprobe_subtitle_streams,
    handbrake_subtitle_args,
    hb_encode,
    hb_encode_with_progress,
    lock_is_stale_or_clear,
    rotate_logs,
)

__all__ = [
    "hb_encode",
    "hb_encode_with_progress",
    "extract_external_subtitles",
    "handbrake_subtitle_args",
    "ffprobe_subtitle_streams",
    "_normalize_subtitle_language",
    "_subtitle_output_path",
    "_preset_target_height",
    "_resolution_label_from_height",
    "_refresh_mp4_quality_metadata",
    "encode_lock_path",
    "lock_is_stale_or_clear",
    "create_lock_or_fail",
    "_ensure_log_dir",
    "_gzip_compress",
    "rotate_logs",
]
