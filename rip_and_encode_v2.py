#!/usr/bin/env python3
"""rip_and_encode_v2.py

Python port of rip_and_encode_v2.sh.

This script orchestrates external tools:
- MakeMKV (makemkvcon) to rip DVDs to MKV files
- HandBrakeCLI to transcode MKVs to MP4
- ffprobe to read MKV metadata/duration/chapters
- optional ssh/scp for remote copy destinations (host:/path)

It aims to match the Bash v2 behavior:
- strict fail-safe (keep artifacts on error; resumable)
- overlap mode to encode in the background while you insert the next disc
- continuous mode to batch multiple titles before final copy/cleanup
- CSV-driven schedule mode to prompt for the next disc after eject

Notes:
- This is a thin orchestration layer; it does not replace MakeMKV/HandBrake/ffprobe.
- CSV parsing is intentionally simple: 4 columns, no quoting, no embedded commas.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import IO, Iterable, Optional


# ----------------------------
# Debian hints / deps
# ----------------------------


def _read_os_release() -> dict[str, str]:
    try:
        txt = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}

    out: dict[str, str] = {}
    for raw in txt.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"')
        out[k.strip()] = v
    return out


def _is_debian_like() -> bool:
    osr = _read_os_release()
    ident = (osr.get("ID") or "").lower()
    like = (osr.get("ID_LIKE") or "").lower()
    return ident in {"debian", "ubuntu"} or any(x in like for x in ("debian", "ubuntu"))


def _sudo_prefix() -> list[str]:
    # Non-interactive sudo; if a password is needed, we fail with a clear message.
    return ["sudo", "-n"]


def _run_root_cmd(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    if os.geteuid() == 0:
        return run_cmd(argv, check=check, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if shutil.which("sudo"):
        return run_cmd(_sudo_prefix() + argv, check=check, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    raise RuntimeError("This step requires root privileges. Re-run as root or install/configure sudo.")


def jellyfin_is_installed() -> bool:
    if shutil.which("jellyfin"):
        return True
    # Debian/Ubuntu: jellyfin is a package. dpkg is reliable when present.
    if shutil.which("dpkg"):
        cp = run_cmd(["dpkg", "-s", "jellyfin"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return cp.returncode == 0
    return False


def ensure_jellyfin_installed() -> None:
    """Best-effort Jellyfin install for Debian/Ubuntu.

    This is intentionally conservative: it only attempts a straightforward apt install.
    If the package isn't available, it prints guidance rather than guessing repo setup.
    """

    if jellyfin_is_installed():
        print("Jellyfin is already installed.")
        return

    if not _is_debian_like():
        raise RuntimeError(
            "Jellyfin is not installed and automatic install is only supported for Debian/Ubuntu in this script. "
            "Install Jellyfin manually for your distro, then re-run."
        )

    if not shutil.which("apt-get"):
        raise RuntimeError("apt-get not found; cannot auto-install Jellyfin on this system.")

    print("--------------------------------------------")
    print("Jellyfin setup")
    print("--------------------------------------------")
    print("Jellyfin was not detected. Attempting installation via apt...")

    # Update and install.
    try:
        _run_root_cmd(["apt-get", "update"], check=True)
        _run_root_cmd(["apt-get", "install", "-y", "jellyfin"], check=True)
    except subprocess.CalledProcessError as e:
        out = (e.stdout or "").strip()
        if "Unable to locate package jellyfin" in out or "E: Unable to locate package jellyfin" in out:
            raise RuntimeError(
                "Jellyfin package is not available in your current apt sources. "
                "Add the official Jellyfin repository for your distro, then re-run with --ensure-jellyfin."
            ) from e
        raise

    # Enable + start service if systemd is present.
    if shutil.which("systemctl"):
        try:
            _run_root_cmd(["systemctl", "enable", "--now", "jellyfin"], check=False)
        except Exception:
            pass

    print("Jellyfin installation complete.")

def debian_install_hint(cmd: str) -> str:
    hints = {
        "awk": "sudo apt-get update && sudo apt-get install -y gawk",
        "screen": "sudo apt-get update && sudo apt-get install -y screen",
        "eject": "sudo apt-get update && sudo apt-get install -y eject",
        "ffprobe": "sudo apt-get update && sudo apt-get install -y ffmpeg",
        "find": "sudo apt-get update && sudo apt-get install -y findutils",
        "grep": "sudo apt-get update && sudo apt-get install -y grep",
        "HandBrakeCLI": "sudo apt-get update && sudo apt-get install -y handbrake-cli",
        "makemkvcon": "Install MakeMKV (makemkvcon) from MakeMKV upstream or a trusted third-party Debian repo; it is often not in Debian main.",
        "ssh": "sudo apt-get update && sudo apt-get install -y openssh-client",
        "scp": "sudo apt-get update && sudo apt-get install -y openssh-client",
        "sed": "sudo apt-get update && sudo apt-get install -y sed",
        "sort": "sudo apt-get update && sudo apt-get install -y coreutils",
        "tr": "sudo apt-get update && sudo apt-get install -y coreutils",
        "wc": "sudo apt-get update && sudo apt-get install -y coreutils",
        "stdbuf": "sudo apt-get update && sudo apt-get install -y coreutils",
        "tee": "sudo apt-get update && sudo apt-get install -y coreutils",
        "date": "sudo apt-get update && sudo apt-get install -y coreutils",
        "id": "sudo apt-get update && sudo apt-get install -y coreutils",
    }
    return hints.get(cmd, f"Install the package that provides '{cmd}' (distribution-specific).")


def which_required(cmd: str) -> str:
    path = shutil.which(cmd)
    if not path:
        raise RuntimeError(f"Missing required command: {cmd}\nDebian hint: {debian_install_hint(cmd)}")
    return path


def check_deps(movies_dir: str, series_dir: str) -> int:
    deps = [
        "screen",
        "awk",
        "eject",
        "ffprobe",
        "find",
        "grep",
        "HandBrakeCLI",
        "makemkvcon",
        "sed",
        "sort",
        "stdbuf",
        "tr",
        "wc",
        "tee",
        "date",
        "id",
    ]
    if is_remote_dest(movies_dir) or is_remote_dest(series_dir):
        deps.extend(["ssh", "scp"])

    missing = False
    for cmd in deps:
        if not shutil.which(cmd):
            missing = True
            print(f"Missing: {cmd}", file=sys.stderr)
            print(f"  Debian hint: {debian_install_hint(cmd)}", file=sys.stderr)

    if missing:
        return 2

    print("All dependencies are present.")
    return 0


# ----------------------------
# Logging / tee
# ----------------------------


class Tee:
    def __init__(self, a: IO[str], b: IO[str]) -> None:
        self._a = a
        self._b = b
        self._lock = Lock()

    def write(self, s: str) -> int:
        with self._lock:
            self._a.write(s)
            self._a.flush()
            self._b.write(s)
            self._b.flush()
        return len(s)

    def flush(self) -> None:
        with self._lock:
            self._a.flush()
            self._b.flush()


# ----------------------------
# Remote helpers
# ----------------------------


def is_remote_dest(dest: str | None) -> bool:
    if not dest:
        return False
    if ":" not in dest:
        return False
    if dest.startswith("/") or dest.startswith("./") or dest.startswith("../"):
        return False
    if "://" in dest:
        return False
    return True


def remote_host_part(dest: str) -> str:
    return dest.split(":", 1)[0]


def remote_path_part(dest: str) -> str:
    return dest.split(":", 1)[1]


def run_cmd(
    argv: list[str],
    *,
    check: bool = True,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, stdout=stdout, stderr=stderr, text=text)


def remote_exec(dest: str, cmd: str) -> None:
    run_cmd(["ssh", "-o", "BatchMode=yes", remote_host_part(dest), cmd])


def remote_preflight_dir(dest: str) -> None:
    rpath = remote_path_part(dest)
    try:
        remote_exec(dest, f"mkdir -p -- '{rpath}'")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Remote mkdir failed for: {dest}\nHint: ensure SSH keys/auth are set up and the remote path is valid."
        ) from e

    try:
        remote_exec(dest, f"test -d -- '{rpath}' && test -w -- '{rpath}'")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Remote directory is not writable: {dest}\nHint: check remote permissions/ownership for '{rpath}'."
        ) from e


def remote_exists(dest: str, subpath: str) -> bool:
    base = remote_path_part(dest)
    try:
        remote_exec(dest, f"test -e -- '{base}/{subpath}'")
        return True
    except subprocess.CalledProcessError:
        return False


def remote_copy_dir_into(local_dir: Path, remote_dest: str) -> None:
    rpath = remote_path_part(remote_dest)
    run_cmd(["scp", "-r", str(local_dir), f"{remote_host_part(remote_dest)}:{rpath}/"])


# ----------------------------
# Simple-mode SSH config helpers
# ----------------------------


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


# ----------------------------
# Prompts
# ----------------------------


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


# ----------------------------
# Work dir safety / naming
# ----------------------------


def clean_title(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9 ]", "", s).replace(" ", "_")


def sanitize_title_for_dir(title_raw: str) -> str:
    # Match Bash v2: spaces -> underscores, '/' -> '_'
    return title_raw.replace(" ", "_").replace("/", "_")


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


# ----------------------------
# Extras NFO
# ----------------------------


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


# ----------------------------
# ffprobe helpers
# ----------------------------


def ffprobe_meta_title(f: Path) -> str:
    def _probe(tag: str) -> str:
        try:
            cp = run_cmd(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    f"format_tags={tag}",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(f),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            return (cp.stdout or "").strip()
        except Exception:
            return ""

    title_tag = _probe("title")
    desc_tag = _probe("description")
    return desc_tag or title_tag


def ffprobe_duration_seconds(f: Path) -> int:
    try:
        cp = run_cmd(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(f),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        raw = (cp.stdout or "0").splitlines()[0].strip()
        if re.fullmatch(r"\d+(\.\d+)?", raw):
            return int(raw.split(".")[0])
    except Exception:
        pass
    return 0


def ffprobe_chapter_count(f: Path) -> int:
    try:
        cp = run_cmd(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "chapters=chapter",
                "-of",
                "csv=p=0",
                str(f),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return len([ln for ln in (cp.stdout or "").splitlines() if ln.strip()])
    except Exception:
        return 0


def file_size_mb(f: Path) -> int:
    try:
        return int(f.stat().st_size // 1048576)
    except OSError:
        return 0


# ----------------------------
# MakeMKV wrapper
# ----------------------------


def run_makemkv_with_progress_to_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    argv = [
        "stdbuf",
        "-oL",
        "-eL",
        "makemkvcon",
        "mkv",
        "--progress=-stdout",
        "--decrypt",
        "--cache=128",
        "--minlength=300",
        "disc:0",
        "all",
        str(out_dir),
    ]

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None

    prgv_re = re.compile(r"^PRGV:(.*)$")
    for line in proc.stdout:
        m = prgv_re.match(line.strip())
        if m:
            # PRGV:current,total,...
            parts = re.split(r"[, ]+", m.group(1).strip())
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) > 0:
                pct = (int(parts[0]) / int(parts[1])) * 100
                sys.stdout.write(f"\rMakeMKV progress: {pct:5.1f}%")
                sys.stdout.flush()
            continue
        if line.startswith("PRGC:") or line.startswith("PRGT:"):
            continue
        print(line.rstrip())

    code = proc.wait()
    print()
    if code != 0:
        raise RuntimeError(f"MakeMKV failed with exit code {code}")


def find_mkvs_in_dir(dir_: Path) -> list[Path]:
    return sorted([p for p in dir_.rglob("*.mkv") if p.is_file()])


# ----------------------------
# Classification heuristics
# ----------------------------


EXTRA_DURATION_THRESHOLD = 1200  # 20 minutes
EXTRA_KEYWORDS_RE = re.compile(r"extra|deleted|featurette|behind|interview|trailer|bonus|promo", re.I)


@dataclass
class MovieAnalysis:
    main_mkv: Optional[Path]
    titlemap: dict[Path, str]
    duration: dict[Path, int]
    is_extra: dict[Path, bool]


def analyze_mkvs_for_movie_disc(mkvs: list[Path]) -> MovieAnalysis:
    titlemap: dict[Path, str] = {}
    duration: dict[Path, int] = {}
    is_extra: dict[Path, bool] = {}

    for f in mkvs:
        meta_title = ffprobe_meta_title(f)
        titlemap[f] = meta_title

        dur_s = ffprobe_duration_seconds(f)
        chapters = ffprobe_chapter_count(f)

        if dur_s <= 0:
            # Fallback: pick main by size; do not classify as extra.
            duration[f] = file_size_mb(f)
            is_extra[f] = False
            continue

        duration[f] = dur_s
        rule_duration = dur_s < EXTRA_DURATION_THRESHOLD
        rule_keyword = bool(EXTRA_KEYWORDS_RE.search(meta_title or ""))
        rule_chapters = chapters <= 2
        is_extra[f] = bool(rule_duration and (rule_keyword or rule_chapters))

    main_candidates = [f for f in duration.keys() if not is_extra.get(f, False)]
    main_mkv: Optional[Path] = None
    if main_candidates:
        main_mkv = max(main_candidates, key=lambda p: duration.get(p, 0))
    if not main_mkv and mkvs:
        main_mkv = max(mkvs, key=lambda p: p.stat().st_size if p.exists() else 0)

    return MovieAnalysis(main_mkv=main_mkv, titlemap=titlemap, duration=duration, is_extra=is_extra)


# ----------------------------
# Encode queue / locking
# ----------------------------


def encode_lock_path(out: Path) -> Path:
    return Path(str(out) + ".enc.lock")


def lock_is_stale_or_clear(lock: Path) -> bool:
    if not lock.exists():
        return True
    try:
        pid_s = lock.read_text(errors="ignore").splitlines()[0].strip()
    except Exception:
        pid_s = ""

    if pid_s.isdigit():
        pid = int(pid_s)
        try:
            os.kill(pid, 0)
            return False
        except OSError:
            pass

    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass
    return True


def create_lock_or_fail(lock: Path) -> None:
    lock_is_stale_or_clear(lock)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError as e:
        raise RuntimeError(f"Output is locked (encode in progress?): {lock}\nIf this is stale, remove: {lock}") from e


def hb_encode(input_: Path, output: Path, preset: str) -> None:
    hb_encode_with_progress(input_, output, preset)


def _ensure_log_dir(home: Path) -> Path:
    env = (os.environ.get("RIP_AND_ENCODE_LOG_DIR") or "").strip()
    if env:
        p = Path(env).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            return home

    p = home / ".archive_helper_for_jellyfin" / "logs"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return home


def _gzip_compress(src: Path, dst: Path) -> None:
    with src.open("rb") as f_in, gzip.open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def rotate_logs(log_dir: Path, *, keep: int = 30, compress: bool = True, exclude: Optional[Path] = None) -> None:
    try:
        logs = sorted(log_dir.glob("rip_and_encode_v2_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return

    excl = exclude.resolve() if exclude else None
    kept = 0
    now = time.time()
    min_age_s = 24 * 60 * 60  # only rotate logs older than 24 hours

    for p in logs:
        try:
            if excl and p.resolve() == excl:
                continue
        except Exception:
            pass

        kept += 1
        if kept <= keep:
            continue

        try:
            if (now - p.stat().st_mtime) < min_age_s:
                continue
        except Exception:
            continue

        try:
            if compress:
                gz = p.with_suffix(p.suffix + ".gz")
                if not gz.exists():
                    _gzip_compress(p, gz)
                p.unlink(missing_ok=True)
            else:
                p.unlink(missing_ok=True)
        except Exception:
            pass


def hb_encode_with_progress(input_: Path, output: Path, preset: str) -> None:
    """Run HandBrakeCLI while emitting progress as newline-delimited log lines."""

    args = ["HandBrakeCLI", "-i", str(input_), "-o", str(output), "--preset", preset]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=0,
    )

    assert proc.stdout is not None
    buf = ""
    last_pct_int: Optional[int] = None
    last_emit = 0.0

    def should_emit(line: str) -> bool:
        nonlocal last_pct_int, last_emit
        m = re.search(r"Encoding:.*?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", line)
        if not m:
            return True
        try:
            pct = float(m.group(1))
        except Exception:
            return True
        pct_i = int(pct)
        now = time.time()
        if last_pct_int is None or pct_i != last_pct_int or (now - last_emit) >= 2.0:
            last_pct_int = pct_i
            last_emit = now
            return True
        return False

    while True:
        ch = proc.stdout.read(1)
        if ch == "" and proc.poll() is not None:
            break
        if ch in ("\r", "\n"):
            line = buf.strip()
            buf = ""
            if line and should_emit(line):
                print(line)
            continue
        buf += ch

    tail = buf.strip()
    if tail and should_emit(tail):
        print(tail)

    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"HandBrakeCLI failed (exit {code}) for output: {output}")


WORKDIR_MARKER_NAME = ".rip_and_encode_v2_workdir"
DISC_MANIFEST_NAME = ".rip_and_encode_v2_disc_manifest.json"
DISC_MANIFEST_VERSION = 1


# ----------------------------
# Title context
# ----------------------------


@dataclass
class TitleContext:
    title_raw: str
    title: str
    year: str
    is_series: bool
    season: Optional[int]
    season_pad: str
    movie_multi_disc: bool

    work_dir: Path
    mkv_root: Path

    remote_movies: bool
    remote_series: bool

    output_movie_dir: Optional[Path]
    output_movie_main: Optional[Path]

    output_season_dir: Optional[Path]

    output_extras_dir: Path
    output_extras_nfo: Path


def setup_title_context(
    *,
    home: Path,
    home_base: Path,
    title_raw: str,
    year: str,
    is_series: bool,
    season: Optional[int],
    movie_multi_disc: bool,
    movies_dir: str,
    series_dir: str,
) -> TitleContext:
    title = sanitize_title_for_dir(title_raw)

    season_pad = ""
    if is_series:
        season_pad = f"{(season or 0):02d}"

    work_dir = home_base / f"{title} ({year})"
    mkv_root = work_dir / "MKVs"
    mkv_root.mkdir(parents=True, exist_ok=True)

    # Mark this directory as managed by this script so optional cleanup operations
    # can be conservative (avoid deleting unrelated folders under $HOME).
    try:
        (work_dir / WORKDIR_MARKER_NAME).touch(exist_ok=True)
    except Exception:
        pass

    remote_movies = is_remote_dest(movies_dir)
    remote_series = is_remote_dest(series_dir)

    output_movie_dir: Optional[Path] = None
    output_movie_main: Optional[Path] = None
    output_season_dir: Optional[Path] = None

    if is_series:
        if remote_series:
            output_season_dir = work_dir / "__series_stage" / title / f"Season {season_pad}"
        else:
            output_season_dir = Path(series_dir) / title / f"Season {season_pad}"
        output_extras_dir = output_season_dir / "Extras"
        output_extras_nfo = output_extras_dir / "extras.nfo"
        output_season_dir.mkdir(parents=True, exist_ok=True)
        output_extras_dir.mkdir(parents=True, exist_ok=True)
        init_extras_nfo(output_extras_nfo)
    else:
        if remote_movies:
            output_movie_dir = work_dir
        else:
            output_movie_dir = Path(movies_dir) / f"{title} ({year})"
        output_movie_main = output_movie_dir / f"{title}.mp4"
        output_extras_dir = output_movie_dir / "Extras"
        output_extras_nfo = output_extras_dir / "extras.nfo"
        output_movie_dir.mkdir(parents=True, exist_ok=True)
        output_extras_dir.mkdir(parents=True, exist_ok=True)
        init_extras_nfo(output_extras_nfo)

    return TitleContext(
        title_raw=title_raw,
        title=title,
        year=year,
        is_series=is_series,
        season=season,
        season_pad=season_pad,
        movie_multi_disc=movie_multi_disc,
        work_dir=work_dir,
        mkv_root=mkv_root,
        remote_movies=remote_movies,
        remote_series=remote_series,
        output_movie_dir=output_movie_dir,
        output_movie_main=output_movie_main,
        output_season_dir=output_season_dir,
        output_extras_dir=output_extras_dir,
        output_extras_nfo=output_extras_nfo,
    )


# ----------------------------
# Rip + process
# ----------------------------


def rip_disc_if_needed(disc_dir: Path, prompt_msg: str, wait_for_enter: bool = True) -> list[Path]:
    mkvs = find_mkvs_in_dir(disc_dir)
    if mkvs:
        print(f"Resume: found existing MKVs in {disc_dir}; skipping rip.")
        return mkvs

    print(prompt_msg)
    if wait_for_enter:
        input()

    run_makemkv_with_progress_to_dir(disc_dir)
    try:
        run_cmd(["eject", "/dev/sr0"], check=False)
    except Exception:
        pass

    mkvs = find_mkvs_in_dir(disc_dir)
    if not mkvs:
        raise RuntimeError(f"No MKVs found in {disc_dir} after rip step.")
    return mkvs


def _disc_manifest_path(disc_dir: Path) -> Path:
    return disc_dir / DISC_MANIFEST_NAME


def _load_disc_manifest(disc_dir: Path) -> Optional[dict]:
    p = _disc_manifest_path(disc_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="ignore") or "{}")
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("version") != DISC_MANIFEST_VERSION:
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    return data


def _write_disc_manifest(disc_dir: Path, manifest: dict) -> None:
    p = _disc_manifest_path(disc_dir)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def _manifest_outputs_complete(manifest: dict) -> bool:
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        return False
    for it in items:
        if not isinstance(it, dict):
            return False
        out_s = it.get("output")
        if not isinstance(out_s, str) or not out_s:
            return False
        if not Path(out_s).exists():
            return False
    return True


def _find_existing_series_episode_output(
    *,
    season_dir: Path,
    series_title: str,
    season_pad: str,
    clean_episode_title: str,
) -> Optional[Path]:
    if not clean_episode_title:
        return None
    if not season_dir.exists():
        return None
    try:
        pat = re.compile(
            r"^" + re.escape(series_title) + r" - S" + re.escape(season_pad) + r"E\d{2} - " + re.escape(clean_episode_title) + r"\.mp4$"
        )
        matches = sorted([p for p in season_dir.iterdir() if p.is_file() and pat.match(p.name)])
        return matches[0] if matches else None
    except Exception:
        return None


def _encode_extra_to_output_and_register(
    *,
    input_: Path,
    output: Path,
    extras_nfo: Path,
    overlap: bool,
    preset: str,
    submit_encode,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    stem = output.stem
    if overlap:
        append_extra_nfo_if_missing(extras_nfo, stem, output.name)
        submit_encode(input_, output, preset)
    else:
        if output.exists():
            print(f"Skipping encode (exists): {output}")
        else:
            hb_encode(input_, output, preset)
        append_extra_nfo_if_missing(extras_nfo, stem, output.name)


def unique_out_path(extras_dir: Path, stem: str, ext: str) -> Path:
    extras_dir.mkdir(parents=True, exist_ok=True)
    out = extras_dir / f"{stem}.{ext}"
    lock = encode_lock_path(out)
    lock_is_stale_or_clear(lock)

    if not out.exists() and not lock.exists():
        return out

    for i in range(2, 100):
        candidate = f"{stem}-{i:02d}"
        out = extras_dir / f"{candidate}.{ext}"
        lock = encode_lock_path(out)
        lock_is_stale_or_clear(lock)
        if not out.exists() and not lock.exists():
            return out

    raise RuntimeError(f"Could not find a free filename for: {extras_dir}/{stem}.{ext}")


def encode_extra_and_register(
    *,
    input_: Path,
    extras_dir: Path,
    extras_nfo: Path,
    base_name: str,
    overlap: bool,
    preset: str,
    submit_encode,
) -> None:
    out = unique_out_path(extras_dir, base_name, "mp4")
    stem = out.stem

    if overlap:
        append_extra_nfo_if_missing(extras_nfo, stem, out.name)
        submit_encode(input_, out, preset)
    else:
        if out.exists():
            print(f"Skipping encode (exists): {out}")
        else:
            hb_encode(input_, out, preset)
        append_extra_nfo_if_missing(extras_nfo, stem, out.name)


def process_movie_disc(
    *,
    ctx: TitleContext,
    disc_index: int,
    disc_dir: Path,
    overlap: bool,
    preset: str,
    submit_encode,
) -> None:
    mkvs = find_mkvs_in_dir(disc_dir)
    if not mkvs:
        raise RuntimeError(f"No MKVs found for movie disc {disc_index}.")

    assert ctx.output_movie_main is not None

    if disc_index == 1 and not ctx.output_movie_main.exists():
        analysis = analyze_mkvs_for_movie_disc(mkvs)
        if not analysis.main_mkv:
            raise RuntimeError("Could not determine main MKV for disc 1.")

        if overlap:
            submit_encode(analysis.main_mkv, ctx.output_movie_main, preset)
        else:
            if ctx.output_movie_main.exists():
                print(f"Skipping encode (exists): {ctx.output_movie_main}")
            else:
                hb_encode(analysis.main_mkv, ctx.output_movie_main, preset)

        for f in mkvs:
            if analysis.main_mkv and f == analysis.main_mkv:
                continue
            name = clean_title(analysis.titlemap.get(f, "Extra") or "Extra")
            encode_extra_and_register(
                input_=f,
                extras_dir=ctx.output_extras_dir,
                extras_nfo=ctx.output_extras_nfo,
                base_name=name,
                overlap=overlap,
                preset=preset,
                submit_encode=submit_encode,
            )
    else:
        disc_pad = f"{disc_index:02d}"
        prefix = f"Disc{disc_pad}_"
        for f in mkvs:
            meta = ffprobe_meta_title(f)
            base = prefix + clean_title(meta or "Extra")
            encode_extra_and_register(
                input_=f,
                extras_dir=ctx.output_extras_dir,
                extras_nfo=ctx.output_extras_nfo,
                base_name=base,
                overlap=overlap,
                preset=preset,
                submit_encode=submit_encode,
            )

    close_extras_nfo(ctx.output_extras_nfo)


def series_next_episode_number(season_dir: Path) -> int:
    if not season_dir.exists():
        return 1
    ep_nums: list[int] = []
    for name in season_dir.iterdir():
        m = re.search(r"E(\d{2})", name.name)
        if m:
            ep_nums.append(int(m.group(1)))
    return (max(ep_nums) if ep_nums else 0) + 1


def process_series_disc(
    *,
    ctx: TitleContext,
    disc_dir: Path,
    overlap: bool,
    preset: str,
    submit_encode,
) -> None:
    mkvs = find_mkvs_in_dir(disc_dir)
    if not mkvs:
        raise RuntimeError("No MKVs found for series disc.")

    assert ctx.output_season_dir is not None

    manifest = _load_disc_manifest(disc_dir)
    if manifest and _manifest_outputs_complete(manifest):
        print(f"Resume: disc already processed (manifest complete): {disc_dir}")
        close_extras_nfo(ctx.output_extras_nfo)
        return

    items: list[dict] = []
    if manifest:
        raw_items = manifest.get("items")
        if isinstance(raw_items, list):
            items = [it for it in raw_items if isinstance(it, dict)]

    if not items:
        # First time (or legacy runs): build a deterministic plan and persist it.
        ep_num = series_next_episode_number(ctx.output_season_dir)
        used_outputs: set[str] = set()
        for f in mkvs:
            meta_title = ffprobe_meta_title(f)
            dur = ffprobe_duration_seconds(f)
            chapters = ffprobe_chapter_count(f)

            rule_duration = dur > 0 and dur < EXTRA_DURATION_THRESHOLD
            rule_keyword = bool(EXTRA_KEYWORDS_RE.search(meta_title or ""))
            rule_chapters = chapters <= 2

            is_extra = bool(rule_duration and (rule_keyword or rule_chapters))
            clean = clean_title(meta_title or "")
            input_rel = str(f.relative_to(disc_dir))

            if is_extra:
                out = unique_out_path(ctx.output_extras_dir, clean or "Extra", "mp4")
                # Ensure no accidental duplicates in the plan.
                if str(out) in used_outputs:
                    out = unique_out_path(ctx.output_extras_dir, (clean or "Extra") + "-dup", "mp4")
                used_outputs.add(str(out))
                items.append({"type": "extra", "input_rel": input_rel, "output": str(out)})
                continue

            # If a previous legacy run already produced this episode output, reuse it.
            existing = _find_existing_series_episode_output(
                season_dir=ctx.output_season_dir,
                series_title=ctx.title,
                season_pad=ctx.season_pad,
                clean_episode_title=clean,
            )
            if existing is not None:
                out = existing
            else:
                out = ctx.output_season_dir / f"{ctx.title} - S{ctx.season_pad}E{ep_num:02d} - {clean}.mp4"
                ep_num += 1

            if str(out) in used_outputs:
                # Extremely defensive: fall back to allocating a new episode number.
                out = ctx.output_season_dir / f"{ctx.title} - S{ctx.season_pad}E{ep_num:02d} - {clean}.mp4"
                ep_num += 1
            used_outputs.add(str(out))
            items.append({"type": "episode", "input_rel": input_rel, "output": str(out)})

        manifest = {
            "version": DISC_MANIFEST_VERSION,
            "kind": "series",
            "title": ctx.title,
            "year": ctx.year,
            "season": ctx.season_pad,
            "disc_dir": str(disc_dir),
            "items": items,
            "created_at": int(time.time()),
        }
        try:
            _write_disc_manifest(disc_dir, manifest)
        except Exception:
            pass

    # Execute plan (encode only missing outputs).
    for it in items:
        try:
            input_rel = it.get("input_rel")
            out_s = it.get("output")
            kind = it.get("type")
            if not isinstance(input_rel, str) or not isinstance(out_s, str) or not out_s:
                continue
            input_path = (disc_dir / input_rel).resolve()
            out = Path(out_s)
            if out.exists():
                continue
            if kind == "extra":
                _encode_extra_to_output_and_register(
                    input_=input_path,
                    output=out,
                    extras_nfo=ctx.output_extras_nfo,
                    overlap=overlap,
                    preset=preset,
                    submit_encode=submit_encode,
                )
            else:
                if overlap:
                    submit_encode(input_path, out, preset)
                else:
                    if out.exists():
                        print(f"Skipping encode (exists): {out}")
                    else:
                        hb_encode(input_path, out, preset)
        except Exception:
            raise

    close_extras_nfo(ctx.output_extras_nfo)


# ----------------------------
# CSV schedule
# ----------------------------


def trim_ws(s: str) -> str:
    return s.strip()


def is_bool_yn(s: str) -> bool:
    return s.strip().lower() in {"y", "n", "yes", "no", "true", "false"}


def normalize_bool_yn(s: str) -> str:
    v = s.strip().lower()
    if v in {"y", "yes", "true"}:
        return "y"
    if v in {"n", "no", "false"}:
        return "n"
    return ""


@dataclass
class ScheduleRow:
    kind: str  # "movie" or "series"
    name: str
    year: str
    third: str  # MultiDisc y/n OR season integer string
    disc: int
    line: int


def load_csv_schedule(file: Path) -> list[ScheduleRow]:
    rows: list[ScheduleRow] = []

    for n, raw in enumerate(file.read_text(errors="ignore").splitlines(), start=1):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue

        # Skip common header rows.
        if re.match(r"^\s*(movie|series)?\s*name\s*,\s*year\s*,", line, flags=re.I):
            continue

        parts = [trim_ws(p) for p in line.split(",")]
        if len(parts) != 4 or any(p == "" for p in parts[:4]):
            raise RuntimeError(
                f"CSV parse error at line {n}: expected exactly 4 comma-separated columns\n  Line: {line}"
            )

        name, year, third, disc_s = parts

        if not re.fullmatch(r"\d{4}", year):
            raise RuntimeError(f"CSV validation error at line {n}: year must be 4 digits\n  Line: {line}")

        if not disc_s.isdigit() or int(disc_s) < 1:
            raise RuntimeError(
                f"CSV validation error at line {n}: disc must be an integer >= 1\n  Line: {line}"
            )
        disc = int(disc_s)

        if is_bool_yn(third):
            kind = "movie"
            third_n = normalize_bool_yn(third)
            if not third_n:
                raise RuntimeError(
                    f"CSV validation error at line {n}: MultiDisc must be y/n\n  Line: {line}"
                )
            third = third_n
        else:
            kind = "series"
            if not third.isdigit() or int(third) < 1:
                raise RuntimeError(
                    f"CSV validation error at line {n}: season must be an integer >= 1\n  Line: {line}"
                )

        rows.append(ScheduleRow(kind=kind, name=name, year=year, third=third, disc=disc, line=n))

    if not rows:
        raise RuntimeError(f"CSV schedule is empty: {file}")

    return rows


def csv_disc_prompt_for_row(r: ScheduleRow) -> str:
    if r.kind == "movie":
        if r.third == "y":
            return f"Insert: Movie '{r.name} ({r.year})' Disc {r.disc} (MultiDisc=y). Press Enter when ready."
        return f"Insert: Movie '{r.name} ({r.year})' Disc {r.disc}. Press Enter when ready."
    return f"Insert: Series '{r.name} ({r.year})' Season {r.third} Disc {r.disc}. Press Enter when ready."


def csv_next_up_note(next_row: ScheduleRow) -> None:
    print(f"Next up: {csv_disc_prompt_for_row(next_row)}")


# ----------------------------
# Finalize (remote sync / cleanup)
# ----------------------------


def rm_mkvs_tree_if_allowed(home: Path, work_dir: Path, mkv_root: Path, keep_mkvs: bool) -> None:
    if keep_mkvs:
        print(f"--keep-mkvs: leaving MKVs intact: {mkv_root}")
        return
    if not is_safe_work_dir(home, work_dir):
        raise RuntimeError(f"Refusing to remove MKV tree; unsafe WORK_DIR: {work_dir}")
    if mkv_root.exists():
        shutil.rmtree(mkv_root)


def rm_work_dir_if_allowed(home: Path, work_dir: Path, keep_mkvs: bool) -> None:
    if keep_mkvs:
        print(f"--keep-mkvs: leaving WORK_DIR intact: {work_dir}")
        return
    if not is_safe_work_dir(home, work_dir):
        raise RuntimeError(f"Refusing to remove WORK_DIR; unsafe WORK_DIR: {work_dir}")
    if work_dir.exists():
        shutil.rmtree(work_dir)


def remote_sync_series_season(remote_base: str, title: str, local_season_dir: Path) -> None:
    remote_root = remote_path_part(remote_base)
    remote_exec(remote_base, f"mkdir -p -- '{remote_root}/{title}'")
    print(f"Copying season folder to remote: {remote_base}/{title}")
    run_cmd(["scp", "-r", str(local_season_dir), f"{remote_host_part(remote_base)}:{remote_root}/{title}/"])


def remote_sync_movie_folder(remote_base: str, title: str, year: str, local_movie_dir: Path) -> None:
    if remote_exists(remote_base, f"{title} ({year})"):
        raise RuntimeError(f"Remote destination already exists: {remote_base}/{title} ({year})")
    print(f"Copying movie folder to remote: {remote_base}")
    remote_copy_dir_into(local_movie_dir, remote_base)


# ----------------------------
# Screen + fail-safe
# ----------------------------


class FailSafe:
    def __init__(self) -> None:
        self.failed = False
        self.keep_mkvs_force = False

    def mark_failed(self) -> None:
        self.failed = True
        self.keep_mkvs_force = True


# ----------------------------
# CLI
# ----------------------------


def usage_text() -> str:
    return (
        "Interactive script that rips DVDs with MakeMKV and transcodes with HandBrakeCLI.\n"
        "This script is intentionally built for Debian; other distributions may require\n"
        "changes to package names, device paths, and dependencies.\n\n"
        "On startup, the script launches a named GNU screen session (if not already in\n"
        "one) and writes a log file under ~/.archive_helper_for_jellyfin/logs (fallback: $HOME).\n\n"
        "DVD encryption note (libdvdread / libdvdcss):\n"
        "  If you see messages like:\n"
        "    'libdvdread: Encrypted DVD support unavailable' / 'No css library available'\n"
        "  it usually means your system lacks libdvdcss. MakeMKV can often still rip\n"
        "  discs, but ffmpeg/ffprobe-based tools may show warnings or fail to read some\n"
        "  encrypted sources.\n\n"
        "  Debian hint (optional):\n"
        "    sudo apt-get update\n"
        "    sudo apt-get install -y libdvd-pkg\n"
        "    sudo dpkg-reconfigure libdvd-pkg\n\n"
        "CSV schedule format (simple, 4 columns, no embedded commas):\n\n"
        "  Movie rows:\n"
        "    Movie Name, Year, MultiDisc(y/n), DiscNumber\n"
        "    Example:\n"
        "      The Matrix, 1999, n, 1\n"
        "      The Lord of the Rings - The Two Towers, 2002, y, 1\n"
        "      The Lord of the Rings - The Two Towers, 2002, y, 2\n\n"
        "    Notes:\n"
        "      - If MultiDisc is y, disc 2+ are treated as 'extras-only'.\n"
        "      - DiscNumber must be an integer (1, 2, 3, ...).\n\n"
        "  Series rows:\n"
        "    Series Name, Year, SeasonNumber, DiscNumber\n"
        "    Example:\n"
        "      Firefly, 2002, 01, 1\n"
        "      Firefly, 2002, 01, 2\n\n"
        "    Notes:\n"
        "      - SeasonNumber must be an integer.\n"
        "      - Episode numbering follows CSV order; list discs in the order you want.\n\n"
        "General CSV rules:\n"
        "  - One row per disc.\n"
        "  - Titles must NOT contain commas.\n"
        "  - MultiDisc must be y/n (not 1/0) to avoid ambiguity with Season 1.\n"
        "  - Blank lines and lines starting with # are ignored.\n"
        "  - Windows CRLF is supported.\n"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rip_and_encode_v2.py",
        formatter_class=argparse.RawTextHelpFormatter,
        description=usage_text(),
    )

    p.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")
    p.add_argument("--simple", action="store_true", help="Guided mode (safe defaults)")
    p.add_argument("--keep-mkvs", "-k", action="store_true", help="Do not delete MKVs and do not delete the WORK_DIR")

    p.add_argument(
        "--cleanup-mkvs",
        action="store_true",
        help="Delete leftover MKVs under managed work directories in $HOME (safe, opt-in)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="When used with --cleanup-mkvs, only list what would be deleted",
    )

    p.add_argument(
        "--overlap",
        action="store_true",
        help="Overlap ripping with encoding (encode in background; allows next disc insertion sooner)",
    )
    p.add_argument(
        "--encode-jobs",
        type=int,
        default=int(os.environ.get("ENCODE_JOBS", "1")),
        help="Max concurrent HandBrake encodes when using --overlap (default: 1)",
    )
    p.add_argument(
        "--continuous",
        action="store_true",
        help="Process multiple titles back-to-back (implies --overlap); copy/cleanup after all encodes finish",
    )

    p.add_argument("--check-deps", action="store_true", help="Check dependencies and print Debian install hints")

    p.add_argument(
        "--ensure-jellyfin",
        action="store_true",
        help="If Jellyfin is not installed on this machine, attempt to install and enable it (Debian/Ubuntu only; requires root or passwordless sudo).",
    )

    p.add_argument(
        "--preset",
        nargs="?",
        const="__LIST__",
        default=os.environ.get("HB_PRESET_DVD", "") or "HQ 1080p30 Surround",
        help="HandBrake preset name; if omitted, list presets and exit",
    )

    p.add_argument("--movies-dir", default="/storage/Movies", help="Movies output directory (default: /storage/Movies)")
    p.add_argument("--series-dir", default="/storage/Series", help="Series output directory (default: /storage/Series)")

    p.add_argument("--csv", dest="csv_file", default="", help="Drive continuous mode from a CSV schedule (implies --continuous)")

    return p.parse_args(argv)


def list_handbrake_presets() -> None:
    which_required("HandBrakeCLI")
    run_cmd(["HandBrakeCLI", "--preset-list"], check=True)


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except Exception:
                    pass
    except Exception:
        return 0
    return total


def _human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v = float(max(0, n))
    for u in units:
        if v < 1024.0 or u == units[-1]:
            if u == "B":
                return f"{int(v)} {u}"
            return f"{v:.1f} {u}"
        v /= 1024.0
    return f"{int(n)} B"


def cleanup_mkvs(home: Path, *, dry_run: bool) -> int:
    """Conservatively remove MKVs under script work directories.

    Work dirs are created as direct children of $HOME named "<Title> (<Year>)".
    To avoid deleting unrelated folders, we only consider directories that either:
      - include a marker file created by this script, OR
      - appear to be legacy script output (Extras/extras.nfo or __series_stage).
    """

    candidates: list[tuple[Path, Path, bool]] = []
    try:
        children = list(home.iterdir())
    except Exception as e:
        print(f"ERROR: unable to read home directory: {e}", file=sys.stderr)
        return 2

    for work_dir in children:
        if not work_dir.is_dir():
            continue

        mkv_root = work_dir / "MKVs"
        if not mkv_root.is_dir():
            continue

        marker = work_dir / WORKDIR_MARKER_NAME
        legacy_hint = (work_dir / "Extras" / "extras.nfo").exists() or (work_dir / "__series_stage").exists()
        if not marker.exists() and not legacy_hint:
            continue

        if not is_safe_work_dir(home, work_dir):
            continue

        candidates.append((work_dir, mkv_root, marker.exists()))

    if not candidates:
        print("No managed MKV folders found to clean.")
        print("Hint: only work directories created by this script are eligible for cleanup.")
        return 0

    print("MKV cleanup candidates:")
    total_bytes = 0
    for work_dir, mkv_root, has_marker in sorted(candidates, key=lambda t: str(t[0])):
        size_b = _dir_size_bytes(mkv_root)
        total_bytes += size_b
        tag = "managed" if has_marker else "legacy"
        print(f"  - {mkv_root} ({_human_bytes(size_b)}) [{tag}]")

    print(f"Total candidates: {len(candidates)}")
    print(f"Total size: {_human_bytes(total_bytes)}")

    if dry_run:
        print("Dry run: nothing deleted.")
        return 0

    deleted = 0
    for work_dir, mkv_root, _has_marker in candidates:
        try:
            shutil.rmtree(mkv_root)
            deleted += 1
        except Exception as e:
            print(f"ERROR: failed to remove {mkv_root}: {e}", file=sys.stderr)

    print(f"Deleted MKV folders: {deleted}")
    return 0


# ----------------------------
# Main
# ----------------------------


def main(argv: list[str]) -> int:
    ns = parse_args(argv)

    if ns.cleanup_mkvs:
        home = Path.home()
        return cleanup_mkvs(home, dry_run=bool(ns.dry_run))

    # Normalize implied flags.
    if ns.continuous:
        ns.overlap = True
    if ns.csv_file:
        ns.continuous = True
        ns.overlap = True

    if ns.encode_jobs < 1 and ns.overlap:
        print("--encode-jobs must be >= 1", file=sys.stderr)
        return 2

    if ns.preset == "__LIST__":
        list_handbrake_presets()
        return 0

    if ns.check_deps:
        return check_deps(ns.movies_dir, ns.series_dir)

    csv_path: Optional[Path] = None
    if ns.csv_file:
        csv_path = Path(ns.csv_file)
        if not csv_path.exists():
            print(f"CSV file not found: {csv_path}", file=sys.stderr)
            return 2
        if not os.access(csv_path, os.R_OK):
            print(f"CSV file is not readable: {csv_path}", file=sys.stderr)
            return 2

    home = Path.home()
    home_base = home

    # Screen bootstrap.
    if not os.environ.get("RIP_AND_ENCODE_IN_SCREEN") and not os.environ.get("STY"):
        if not shutil.which("screen"):
            print("Missing required command: screen", file=sys.stderr)
            print(f"Debian hint: {debian_install_hint('screen')}", file=sys.stderr)
            return 127

        env = os.environ.copy()
        env["RIP_AND_ENCODE_IN_SCREEN"] = "1"
        cmd = ["screen", "-S", "rip_and_encode_v2", sys.executable, str(Path(__file__).resolve()), *argv]
        os.execvpe(cmd[0], cmd, env)

    log_dir = _ensure_log_dir(home_base)
    log_file = log_dir / f"rip_and_encode_v2_{time.strftime('%Y%m%d_%H%M%S')}.log"
    try:
        rotate_logs(log_dir, keep=30, compress=True, exclude=log_file)
    except Exception:
        pass
    log_fh = log_file.open("a", encoding="utf-8")

    sys.stdout = Tee(sys.__stdout__, log_fh)  # type: ignore[assignment]
    sys.stderr = Tee(sys.__stderr__, log_fh)  # type: ignore[assignment]

    print(f"Log file: {log_file}")

    if ns.ensure_jellyfin:
        try:
            ensure_jellyfin_installed()
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            print("Hint: re-run as root (sudo) or configure passwordless sudo for apt/systemctl.", file=sys.stderr)
            return 2

    failsafe = FailSafe()

    def _handle_sigint(signum, frame):
        failsafe.mark_failed()
        print("\nInterrupted. Leaving files in place for resume.", file=sys.stderr)
        print(f"Log: {log_file}", file=sys.stderr)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    # Fail fast deps.
    try:
        which_required("screen")
        for cmd in ["awk", "eject", "ffprobe", "find", "grep", "HandBrakeCLI", "makemkvcon", "sed", "sort", "stdbuf", "tr", "wc", "tee", "date", "id"]:
            which_required(cmd)
        if is_remote_dest(ns.movies_dir) or is_remote_dest(ns.series_dir):
            which_required("ssh")
            which_required("scp")
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 127

    # Simple mode: optional remote copy prompt (only if both dirs not overridden).
    keep_mkvs = bool(ns.keep_mkvs or ns.simple)
    if ns.simple and ns.movies_dir == "/storage/Movies" and ns.series_dir == "/storage/Series":
        copy_remote = prompt_yes_no("Copy files to a remote Jellyfin server over SSH? (y/n): ")
        if copy_remote == "y":
            ensure_simple_ssh_host(home, "jellyfin")
            ns.movies_dir = "jellyfin:/storage/Movies"
            ns.series_dir = "jellyfin:/storage/Series"

    # Storage preflight.
    def storage_preflight(dest: str, label: str) -> None:
        if is_remote_dest(dest):
            remote_preflight_dir(dest)
        else:
            p = Path(dest)
            if not p.is_dir():
                raise RuntimeError(f"{label} directory not found: {dest}")
            if not os.access(p, os.W_OK):
                raise RuntimeError(f"{label} directory is not writable: {dest}")

    try:
        storage_preflight(ns.movies_dir, "Movies")
        storage_preflight(ns.series_dir, "Series")
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    # Encode submission.
    executor = None
    futures: list = []

    encode_stats_lock = Lock()
    encode_queued = 0
    encode_started = 0
    encode_finished = 0

    if ns.overlap:
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=ns.encode_jobs)

    def submit_encode(input_: Path, output: Path, preset: str) -> None:
        nonlocal encode_queued, encode_started, encode_finished
        if output.exists():
            print(f"Skipping encode (exists): {output}")
            return

        with encode_stats_lock:
            encode_queued += 1
            queued_now = encode_queued

        if not ns.overlap:
            print(f"Queued encode: {output.name}")
            with encode_stats_lock:
                encode_started += 1
                started_now = encode_started
                queued_snap = encode_queued
            print(f"HandBrake start: {started_now}/{queued_snap}: {output.name}")
            hb_encode(input_, output, preset)
            with encode_stats_lock:
                encode_finished += 1
                finished_now = encode_finished
                queued_snap2 = encode_queued
            print(f"HandBrake done: {finished_now}/{queued_snap2}: {output.name}")
            return

        assert executor is not None
        lock = encode_lock_path(output)
        create_lock_or_fail(lock)

        def _job() -> None:
            nonlocal encode_started, encode_finished, encode_queued
            with encode_stats_lock:
                encode_started += 1
                started_now = encode_started
                queued_snap = encode_queued
            print(f"HandBrake start: {started_now}/{queued_snap}: {output.name}")

            proc = subprocess.Popen(
                ["HandBrakeCLI", "-i", str(input_), "-o", str(output), "--preset", preset],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=0,
            )
            try:
                lock.write_text(str(proc.pid) + "\n", encoding="utf-8")
            except Exception:
                pass

            # Convert HandBrake carriage-return progress into newline-friendly lines.
            assert proc.stdout is not None
            buf = ""
            last_pct_int: Optional[int] = None
            last_emit = 0.0

            def should_emit(line: str) -> bool:
                nonlocal last_pct_int, last_emit
                m = re.search(r"Encoding:.*?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", line)
                if not m:
                    return True
                try:
                    pct = float(m.group(1))
                except Exception:
                    return True
                pct_i = int(pct)
                now = time.time()
                if last_pct_int is None or pct_i != last_pct_int or (now - last_emit) >= 2.0:
                    last_pct_int = pct_i
                    last_emit = now
                    return True
                return False

            while True:
                ch = proc.stdout.read(1)
                if ch == "" and proc.poll() is not None:
                    break
                if ch in ("\r", "\n"):
                    line = buf.strip()
                    buf = ""
                    if line and should_emit(line):
                        print(line)
                    continue
                buf += ch

            tail = buf.strip()
            if tail and should_emit(tail):
                print(tail)

            code = proc.wait()
            try:
                lock.unlink(missing_ok=True)
            except Exception:
                pass
            if code != 0:
                raise RuntimeError(f"HandBrakeCLI failed (exit {code}) for output: {output}")

            with encode_stats_lock:
                encode_finished += 1
                finished_now = encode_finished
                queued_snap2 = encode_queued
            print(f"HandBrake done: {finished_now}/{queued_snap2}: {output.name}")

        futures.append(executor.submit(_job))
        print(f"Queued encode: {output.name}")

    # Batch tracking for finalize.
    batch_seen: set[str] = set()
    batch: list[TitleContext] = []

    def batch_key(ctx: TitleContext) -> str:
        if ctx.is_series:
            return f"S|{ctx.title}|{ctx.year}|{ctx.season_pad}"
        return f"M|{ctx.title}|{ctx.year}"

    def batch_add_once(ctx: TitleContext) -> None:
        key = batch_key(ctx)
        if key in batch_seen:
            return
        batch_seen.add(key)
        batch.append(ctx)

    if ns.continuous:
        print("Continuous mode enabled: keep feeding discs; copy/cleanup runs after all encodes finish.")

    try:
        if csv_path:
            schedule = load_csv_schedule(csv_path)
            print(f"CSV schedule loaded: {len(schedule)} discs")

            csv_next_confirmed = False
            for idx, row in enumerate(schedule):
                # Apply row to globals.
                if row.kind == "movie":
                    is_series = False
                    season = None
                    movie_multi = (row.third == "y")
                else:
                    is_series = True
                    season = int(row.third)
                    movie_multi = False

                ctx = setup_title_context(
                    home=home,
                    home_base=home_base,
                    title_raw=row.name,
                    year=row.year,
                    is_series=is_series,
                    season=season,
                    movie_multi_disc=movie_multi,
                    movies_dir=ns.movies_dir,
                    series_dir=ns.series_dir,
                )
                batch_add_once(ctx)

                disc_dir = ctx.mkv_root / f"Disc{row.disc:02d}"
                prompt_msg = csv_disc_prompt_for_row(row)

                mkvs = rip_disc_if_needed(disc_dir, prompt_msg, wait_for_enter=not csv_next_confirmed)
                csv_next_confirmed = False

                if ctx.is_series:
                    process_series_disc(ctx=ctx, disc_dir=disc_dir, overlap=ns.overlap, preset=ns.preset, submit_encode=submit_encode)
                else:
                    process_movie_disc(ctx=ctx, disc_index=row.disc, disc_dir=disc_dir, overlap=ns.overlap, preset=ns.preset, submit_encode=submit_encode)

                if idx + 1 < len(schedule):
                    csv_next_up_note(schedule[idx + 1])
                    print("When the next disc is inserted, press Enter to start ripping... ")
                    input()
                    csv_next_confirmed = True
        else:
            # Interactive.
            title_raw = prompt_nonempty("Enter title (Movie or Series name): ")
            year = prompt_year("Enter release year: ")
            is_series = prompt_yes_no("Is this a series? (y/n): ") == "y"

            season: Optional[int] = None
            if is_series:
                season = prompt_int("Enter season number: ")

            movie_multi = False
            if not is_series:
                movie_multi = prompt_yes_no("Is this a multi-disc movie (disc 2+ are extras)? (y/n): ") == "y"

            ctx = setup_title_context(
                home=home,
                home_base=home_base,
                title_raw=title_raw,
                year=year,
                is_series=is_series,
                season=season,
                movie_multi_disc=movie_multi,
                movies_dir=ns.movies_dir,
                series_dir=ns.series_dir,
            )

            while True:
                disc_index = 1
                while True:
                    disc_dir = ctx.mkv_root / f"Disc{disc_index:02d}"
                    rip_disc_if_needed(disc_dir, "Insert disc now (then press Enter)", wait_for_enter=True)

                    if ctx.is_series:
                        process_series_disc(ctx=ctx, disc_dir=disc_dir, overlap=ns.overlap, preset=ns.preset, submit_encode=submit_encode)
                        nxt = input("Insert next disc for this season? (y/n): ").strip().lower() or "n"
                        if nxt != "y":
                            break
                        disc_index += 1
                        continue

                    process_movie_disc(ctx=ctx, disc_index=disc_index, disc_dir=disc_dir, overlap=ns.overlap, preset=ns.preset, submit_encode=submit_encode)
                    if ctx.movie_multi_disc:
                        nxt = prompt_yes_no("Rip another disc for this movie (extras only)? (y/n): ")
                        if nxt != "y":
                            break
                        disc_index += 1
                        continue
                    break

                batch_add_once(ctx)

                if not ns.continuous:
                    break

                more = prompt_yes_no("Start another title now? (y/n): ")
                if more != "y":
                    break

                # Next title
                title_raw = prompt_nonempty("Enter title (Movie or Series name): ")
                year = prompt_year("Enter release year: ")
                is_series = prompt_yes_no("Is this a series? (y/n): ") == "y"
                season = prompt_int("Enter season number: ") if is_series else None
                movie_multi = False
                if not is_series:
                    movie_multi = prompt_yes_no("Is this a multi-disc movie (disc 2+ are extras)? (y/n): ") == "y"

                ctx = setup_title_context(
                    home=home,
                    home_base=home_base,
                    title_raw=title_raw,
                    year=year,
                    is_series=is_series,
                    season=season,
                    movie_multi_disc=movie_multi,
                    movies_dir=ns.movies_dir,
                    series_dir=ns.series_dir,
                )

        # Wait for encodes.
        if executor:
            from concurrent.futures import as_completed

            for fut in as_completed(futures):
                fut.result()

        # Finalize: copy/cleanup.
        for ctx in batch:
            print(f"Finalizing: {ctx.title} ({ctx.year})")
            if ctx.is_series:
                if ctx.remote_series:
                    assert ctx.output_season_dir is not None
                    remote_sync_series_season(ns.series_dir, ctx.title, ctx.output_season_dir)
            else:
                if ctx.remote_movies:
                    assert ctx.output_movie_dir is not None
                    remote_sync_movie_folder(ns.movies_dir, ctx.title, ctx.year, ctx.output_movie_dir)

            rm_mkvs_tree_if_allowed(home, ctx.work_dir, ctx.mkv_root, keep_mkvs)
            rm_work_dir_if_allowed(home, ctx.work_dir, keep_mkvs)

        print("Processing complete.")
        return 0

    except KeyboardInterrupt:
        failsafe.mark_failed()
        return 130
    except Exception as e:
        failsafe.mark_failed()
        keep_mkvs = True
        print("\nERROR: " + str(e), file=sys.stderr)
        print(f"Log: {log_file}", file=sys.stderr)
        print("Recovery tips:", file=sys.stderr)
        print("  - Re-run with --keep-mkvs to prevent cleanup", file=sys.stderr)
        print("  - If MKVs exist in WORK_DIR, the script will resume and transcode", file=sys.stderr)
        return 2
    finally:
        if executor:
            executor.shutdown(wait=False, cancel_futures=False)
        if failsafe.failed:
            print("\nExiting after an error. Files were left in place (safe mode).", file=sys.stderr)
            print(f"Log: {log_file}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
