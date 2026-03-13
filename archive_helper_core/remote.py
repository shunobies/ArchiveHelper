"""Remote destination and SSH copy helpers."""

from __future__ import annotations

from archive_helper_core._legacy_rip_and_encode_server import (
    append_ssh_host_block,
    ensure_simple_ssh_host,
    ensure_ssh_dir,
    expand_tilde,
    is_remote_dest,
    prompt_default,
    prompt_int,
    prompt_nonempty,
    prompt_year,
    prompt_yes_no,
    remote_copy_dir_into,
    remote_exec,
    remote_exists,
    remote_host_part,
    remote_path_part,
    remote_preflight_dir,
    run_cmd,
    ssh_config_file,
    ssh_config_has_host,
)

__all__ = [
    "is_remote_dest",
    "remote_host_part",
    "remote_path_part",
    "run_cmd",
    "remote_exec",
    "remote_preflight_dir",
    "remote_exists",
    "remote_copy_dir_into",
    "ssh_config_file",
    "ensure_ssh_dir",
    "ssh_config_has_host",
    "prompt_default",
    "expand_tilde",
    "append_ssh_host_block",
    "ensure_simple_ssh_host",
    "prompt_nonempty",
    "prompt_year",
    "prompt_yes_no",
    "prompt_int",
]
