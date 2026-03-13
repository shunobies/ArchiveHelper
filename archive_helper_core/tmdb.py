"""TMDB lookup and disc suggestion helpers."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import (
    _clean_query_for_tmdb,
    _cmd_stdout,
    _extract_year_hint,
    _is_probable_disc_hint,
    _normalize_disc_hint,
    _query_variants_from_hint,
    _row_quality_score,
    _title_similarity,
    _title_tokens,
    probe_disc_metadata,
    tmdb_movie_runtime_minutes,
    tmdb_search,
    tmdb_suggest_from_disc,
)

__all__ = [
    "tmdb_search",
    "tmdb_movie_runtime_minutes",
    "tmdb_suggest_from_disc",
    "probe_disc_metadata",
    "_cmd_stdout",
    "_extract_year_hint",
    "_title_tokens",
    "_clean_query_for_tmdb",
    "_normalize_disc_hint",
    "_is_probable_disc_hint",
    "_query_variants_from_hint",
    "_title_similarity",
    "_row_quality_score",
]
