"""Dependency and platform checks for server workflows."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import (
    _has_passwordless_sudo,
    _is_debian_like,
    _makemkvcon_is_snap,
    _parse_snap_connections_table,
    _read_os_release,
    _run_root_cmd,
    _sudo_prefix,
    check_deps,
    debian_install_hint,
    ensure_jellyfin_installed,
    jellyfin_is_installed,
    log_fallback_dependency_status,
    maybe_ensure_makemkv_snap_interfaces,
    which_required,
)

__all__ = [
    "check_deps",
    "debian_install_hint",
    "which_required",
    "log_fallback_dependency_status",
    "jellyfin_is_installed",
    "ensure_jellyfin_installed",
    "maybe_ensure_makemkv_snap_interfaces",
    "_read_os_release",
    "_is_debian_like",
    "_sudo_prefix",
    "_run_root_cmd",
    "_has_passwordless_sudo",
    "_makemkvcon_is_snap",
    "_parse_snap_connections_table",
]
