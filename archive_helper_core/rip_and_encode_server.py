#!/usr/bin/env python3
"""Compatibility shim for legacy imports and CLI execution.

The implementation now lives in package-oriented modules plus
`archive_helper_core._legacy_rip_and_encode_server`.
"""

from __future__ import annotations

import sys

from archive_helper_core._legacy_rip_and_encode_server import *  # noqa: F401,F403
from archive_helper_core.cli import main, parse_args, usage_text


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
