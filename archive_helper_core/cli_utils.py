from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def ssh_config_file(home: Path) -> Path:
    return home / ".ssh" / "config"


def ensure_ssh_dir(home: Path) -> None:
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home / ".ssh", 0o700)
    except OSError:
        pass


def ssh_config_has_host(cfg: Path, host: str) -> bool:
    if not cfg.exists():
        return False

    # Minimal parser: look for `Host <name>` lines and match tokens.
    for line in cfg.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("host "):
            tokens = line.split()[1:]
            if host in tokens:
                return True
    return False


def prompt_default(prompt: str, default: str) -> str:
    value = input(f"{prompt} [{default}]: ").replace("\r", "").strip()
    return value or default


def expand_tilde(home: Path, p: str) -> str:
    if p.startswith("~/"):
        return str(home / p[2:])
    return p


def append_ssh_host_block(cfg: Path, host: str, hostname: str, user: str, port: str, identityfile: str) -> None:
    ensure_ssh_dir(cfg.parent.parent)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.touch(exist_ok=True)
    try:
        os.chmod(cfg, 0o600)
    except OSError:
        pass

    with cfg.open("a", encoding="utf-8") as f:
        f.write(
            "\n"
            f"Host {host}\n"
            f"  HostName {hostname}\n"
            f"  User {user}\n"
            f"  Port {port}\n"
            f"  IdentityFile {identityfile}\n"
            "  IdentitiesOnly yes\n"
        )


def ensure_simple_ssh_host(home: Path, host: str) -> None:
    cfg = ssh_config_file(home)
    if ssh_config_has_host(cfg, host):
        return

    print("--------------------------------------------")
    print("SSH setup for remote Jellyfin copy")
    print("--------------------------------------------")
    print(f"No SSH config entry found for Host '{host}'.")
    print(f"We'll add one to {cfg}.")
    print()

    hostname = prompt_default("HostName (IP or DNS name)", "172.29.114.183")
    user = prompt_default("User", os.environ.get("USER", "shuno"))
    port = prompt_default("Port", "22")
    identityfile = prompt_default("IdentityFile (SSH key path)", "~/.ssh/id_ed25519")
    identityfile_expanded = expand_tilde(home, identityfile)

    append_ssh_host_block(cfg, host, hostname, user, port, identityfile_expanded)

    if not Path(identityfile_expanded).exists():
        print(f"Warning: IdentityFile does not exist yet: {identityfile_expanded}", file=sys.stderr)
        print(f"Debian hint: ssh-keygen -t ed25519 -f '{identityfile_expanded}'", file=sys.stderr)
        print("Then copy the public key to the server (example):", file=sys.stderr)
        print(f"  ssh-copy-id -i '{identityfile_expanded}.pub' {host}", file=sys.stderr)

    print()
    print(f"SSH config updated. You can test with: ssh {host}")


def prompt_nonempty(prompt: str) -> str:
    while True:
        v = input(prompt)
        if v.strip():
            return v
        print("Input cannot be empty. Try again.", file=sys.stderr)


def prompt_year(prompt: str) -> str:
    while True:
        v = input(prompt).strip()
        if re.fullmatch(r"\d{4}", v):
            return v
        print("Year must be a 4-digit number. Try again.", file=sys.stderr)


def prompt_yes_no(prompt: str) -> str:
    while True:
        v = input(prompt).strip().lower()
        if v in ("y", "n"):
            return v
        print("Please answer y or n.", file=sys.stderr)


def prompt_int(prompt: str) -> int:
    while True:
        v = input(prompt).strip()
        if re.fullmatch(r"\d+", v):
            return int(v)
        print("Please enter a number.", file=sys.stderr)


def clean_title(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9 ]", "", s).replace(" ", "_")


def sanitize_title_for_dir(title_raw: str) -> str:
    # Linux/shell-friendly folder name: avoid spaces/quotes and other special chars.
    # Goal: user can `cd` into folders without needing quoting.
    s = (title_raw or "").strip()
    if not s:
        return "Untitled"

    # Normalize obvious path separators.
    s = s.replace("/", "_").replace(os.sep, "_")

    # Replace anything outside a conservative allowlist.
    # Allowed: letters, digits, dot, underscore, dash, space (space -> underscore below).
    s = re.sub(r"[^A-Za-z0-9._\- ]+", "_", s)

    # Spaces -> underscores (consistent with the existing behavior).
    s = s.replace(" ", "_")

    # Collapse repeated separators and trim edges.
    s = re.sub(r"_+", "_", s)
    s = s.strip("._-_")
    return s or "Untitled"


def is_safe_work_dir(home: Path, work_dir: Path) -> bool:
    try:
        work_dir = work_dir.resolve()
        home = home.resolve()
    except OSError:
        return False

    if not str(work_dir).startswith(str(home) + os.sep):
        return False
    rel = str(work_dir)[len(str(home) + os.sep) :]
    if not rel:
        return False
    if "/" in rel or os.sep in rel:
        # must be exactly one segment under home
        return False
    if work_dir == home:
        return False
    return True


def init_extras_nfo(nfo: Path) -> None:
    if not nfo.exists():
        nfo.write_text("<extras>\n", encoding="utf-8")


def close_extras_nfo(nfo: Path) -> None:
    if not nfo.exists():
        return
    txt = nfo.read_text(errors="ignore")
    if "</extras>" not in txt:
        with nfo.open("a", encoding="utf-8") as f:
            f.write("</extras>\n")


def append_extra_nfo_if_missing(nfo: Path, title: str, filename: str) -> None:
    if nfo.exists() and f"<filename>{filename}</filename>" in nfo.read_text(errors="ignore"):
        return

    with nfo.open("a", encoding="utf-8") as f:
        f.write(
            "  <video>\n"
            f"    <title>{title}</title>\n"
            f"    <filename>{filename}</filename>\n"
            "  </video>\n"
        )


def _ffprobe_text(args: list[str]) -> str:
    try:
        cp = subprocess.run(
            args,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return cp.stdout or ""
    except Exception:
        return ""


def ffprobe_meta_title(f: Path) -> str:
    def _probe(tag: str) -> str:
        return _ffprobe_text(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                f"format_tags={tag}",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(f),
            ]
        ).strip()

    title_tag = _probe("title")
    desc_tag = _probe("description")
    return desc_tag or title_tag


def ffprobe_duration_seconds(f: Path) -> int:
    raw = (
        _ffprobe_text(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(f),
            ]
        )
        .splitlines()[:1]
    )
    first = raw[0].strip() if raw else "0"
    if re.fullmatch(r"\d+(\.\d+)?", first):
        return int(first.split(".")[0])
    return 0


def ffprobe_chapter_count(f: Path) -> int:
    out = _ffprobe_text(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "chapters=chapter",
            "-of",
            "csv=p=0",
            str(f),
        ]
    )
    return len([ln for ln in out.splitlines() if ln.strip()])


def file_size_mb(f: Path) -> int:
    try:
        return int(f.stat().st_size // 1048576)
    except OSError:
        return 0
