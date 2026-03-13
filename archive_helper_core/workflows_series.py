"""Series workflows, ordering hints, and episode planning helpers."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import (
    _episode_hint_from_text,
    _find_existing_series_episode_output,
    _natural_key,
    _series_plan_order,
    _source_title_order_hint,
    _source_title_order_hint_from_meta,
    _source_title_order_hint_from_name,
    process_series_disc,
    series_next_episode_number,
)

__all__ = [
    "process_series_disc",
    "series_next_episode_number",
    "_series_plan_order",
    "_episode_hint_from_text",
    "_source_title_order_hint",
    "_source_title_order_hint_from_name",
    "_source_title_order_hint_from_meta",
    "_natural_key",
    "_find_existing_series_episode_output",
]
