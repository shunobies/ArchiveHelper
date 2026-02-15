#!/usr/bin/env python3
"""Thin entrypoint for the server rip/encode workflow.

Implementation lives in archive_helper_core.rip_and_encode_server to keep this
script small and easier to maintain.
"""

from __future__ import annotations

from archive_helper_core.rip_and_encode_server import main, sanitize_title_for_dir

__all__ = ["main", "sanitize_title_for_dir"]


if __name__ == "__main__":
    raise SystemExit(main())
