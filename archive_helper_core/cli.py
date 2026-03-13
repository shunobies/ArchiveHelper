"""CLI parsing and top-level orchestration entrypoint."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import main, parse_args, usage_text

__all__ = ["usage_text", "parse_args", "main"]
