"""Audible metadata/index and audiobook sync workflow helpers."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import (
    _extract_meta_author,
    _find_cover_image,
    _load_audible_library_index,
    _normalize_key,
    _render_book_nfo,
    _run_audible_sync,
    _safe_path_component,
    _sanitize_audible_name,
    _unique_path,
    run_audiobook_workflow,
)

__all__ = [
    "run_audiobook_workflow",
    "_load_audible_library_index",
    "_run_audible_sync",
    "_sanitize_audible_name",
    "_normalize_key",
    "_extract_meta_author",
    "_render_book_nfo",
    "_safe_path_component",
    "_unique_path",
    "_find_cover_image",
]
