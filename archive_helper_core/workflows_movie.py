"""Movie-oriented workflow orchestration."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import (
    _encode_extra_to_output_and_register,
    analyze_mkvs_for_movie_disc,
    encode_extra_and_register,
    movie_disc_has_main_feature,
    process_movie_disc,
    process_multi_movie_disc,
    setup_title_context,
    unique_out_path,
)

__all__ = [
    "process_movie_disc",
    "process_multi_movie_disc",
    "movie_disc_has_main_feature",
    "analyze_mkvs_for_movie_disc",
    "_encode_extra_to_output_and_register",
    "encode_extra_and_register",
    "setup_title_context",
    "unique_out_path",
]
