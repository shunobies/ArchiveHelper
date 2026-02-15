from __future__ import annotations

EXEC_MODE_REMOTE = "remote"  # rip+encode on server (current behavior)
EXEC_MODE_LOCAL_RIP_ONLY = "local_rip_only"  # rip locally, encode on server (planned)
EXEC_MODE_LOCAL_RIP_ENCODE = "local_rip_encode"  # rip+encode locally, upload results (planned)

REMOTE_SCRIPT_RUN_PATH = "~/.archive_helper_for_jellyfin/rip_and_encode.py"


def ssh_target(user: str, host: str) -> str:
    if not host.strip():
        return ""
    if "@" in host:
        return host.strip()
    if user.strip():
        return f"{user.strip()}@{host.strip()}"
    return host.strip()


def build_ssh_base_args(target: str, port: str, keyfile: str) -> list[str]:
    args = ["ssh", "-tt"]
    if port.strip():
        args += ["-p", port.strip()]
    if keyfile.strip():
        args += ["-i", keyfile.strip()]
    args.append(target)
    return args


def build_scp_base_args(port: str, keyfile: str) -> list[str]:
    args = ["scp"]
    if port.strip():
        args += ["-P", port.strip()]
    if keyfile.strip():
        args += ["-i", keyfile.strip()]
    return args


def normalize_remote_script_path(remote_script: str) -> str:
    s = (remote_script or "").strip()
    if not s:
        return "rip_and_encode.py"
    if "/" in s or s.startswith("~"):
        return s
    return f"~/{s}"


def exec_mode_label(mode: str) -> str:
    m = (mode or "").strip()
    if m == EXEC_MODE_LOCAL_RIP_ONLY:
        return "Rip locally (encode on server)"
    if m == EXEC_MODE_LOCAL_RIP_ENCODE:
        return "Rip + encode locally (upload results)"
    return "Rip + encode on server (remote)"
