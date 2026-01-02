from __future__ import annotations

from archive_helper_gui.remote_exec import RemoteExecutor


def fetch_handbrake_presets(
    remote: RemoteExecutor,
    *,
    target: str,
    port: str,
    keyfile: str,
    password: str,
) -> list[str]:
    """Fetch HandBrake preset names from the remote host.

    Uses RemoteExecutor for OpenSSH/Paramiko; tries non-interactive shell first and
    falls back to interactive shell if HandBrakeCLI is not found.
    """

    precheck = "command -v HandBrakeCLI >/dev/null 2>&1"
    cmd = "HandBrakeCLI --preset-list"

    # Some servers only add HandBrakeCLI to PATH for interactive shells.
    code, out = remote.run_bash(target, port, keyfile, password, precheck, interactive=False)
    use_interactive = False
    if code != 0:
        code_i, out_i = remote.run_bash(target, port, keyfile, password, precheck, interactive=True)
        if code_i != 0:
            detail = (out or "").strip() or (out_i or "").strip()
            if detail:
                remote.log(
                    "(Info) HandBrakeCLI precheck failed on the server; preset list cannot be loaded:\n"
                    + detail
                    + "\n"
                )
            else:
                remote.log("(Info) HandBrakeCLI not found on the server; preset list cannot be loaded.\n")
            return []

        remote.log(
            "(Info) Loading HandBrake presets using an interactive shell (PATH differs for non-interactive SSH).\n"
        )
        use_interactive = True

    code2, out2 = remote.run_bash(target, port, keyfile, password, cmd, interactive=use_interactive)
    if code2 != 0:
        detail = (out2 or "").strip()
        if detail:
            remote.log("(Info) Failed to run HandBrakeCLI --preset-list on the server:\n" + detail + "\n")
        else:
            remote.log("(Info) Failed to run HandBrakeCLI --preset-list on the server.\n")
        return []

    out_text = out2 or ""

    presets: list[str] = []
    for raw in out_text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue

        s = line.lstrip(" \t")
        indent_len = len(line) - len(s)

        # Skip common noise / warnings.
        if s.startswith("[") or s.startswith("Cannot load"):
            continue
        if s == "HandBrake has exited.":
            continue

        # Skip category headers like "General/" (no indent, ends with '/').
        if not line.startswith(" ") and s.endswith("/"):
            continue

        # Preset name lines are indented a small amount; description lines are indented deeper.
        if 2 <= indent_len <= 6:
            name = s.strip()
            if not name:
                continue
            if name.endswith("/"):
                continue
            presets.append(name)

    if not presets and out_text.strip():
        snippet_lines = [ln.rstrip("\r\n") for ln in out_text.splitlines() if ln.strip()][:20]
        if snippet_lines:
            remote.log(
                "(Info) HandBrake preset list command ran, but no presets were parsed. "
                "First lines of output:\n" + "\n".join(snippet_lines) + "\n"
            )

    seen: set[str] = set()
    unique: list[str] = []
    for p in presets:
        if p in seen:
            continue
        seen.add(p)
        unique.append(p)

    return unique
