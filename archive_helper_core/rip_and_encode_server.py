#!/usr/bin/env python3
"""rip_and_encode.py

Python port of the original rip_and_encode shell workflow.

This script orchestrates external tools:
- MakeMKV (makemkvcon) to rip DVDs to MKV files
- HandBrakeCLI to transcode MKVs to MP4/MKV
- ffprobe to read MKV metadata/duration/chapters
- optional ssh/scp for remote copy destinations (host:/path)

It aims to match the original behavior:
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
import difflib
import getpass
import gzip
import html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import IO, Callable, Iterable, Optional

from archive_helper_core.schedule_csv import (
    ScheduleRow,
    csv_disc_prompt_for_row,
    csv_next_up_note,
    load_csv_schedule,
)


def tmdb_search(*, api_key: str, query: str, year: str = "", media_type: str = "movie", limit: int = 8) -> list[dict[str, str]]:
    api_key_s = (api_key or "").strip()
    query_s = (query or "").strip()
    if not api_key_s:
        raise RuntimeError("TMDB API key is required.")
    if not query_s:
        raise RuntimeError("TMDB query is required.")

    mt = (media_type or "movie").strip().lower()
    if mt not in {"movie", "tv", "multi"}:
        raise RuntimeError("TMDB media type must be 'movie', 'tv', or 'multi'.")

    try:
        lim = max(1, min(20, int(limit)))
    except Exception:
        lim = 8

    params: dict[str, str] = {
        "api_key": api_key_s,
        "query": query_s,
        "include_adult": "false",
        "page": "1",
    }
    year_s = (year or "").strip()
    if year_s and re.fullmatch(r"\d{4}", year_s) and mt != "multi":
        if mt == "movie":
            params["year"] = year_s
        else:
            params["first_air_date_year"] = year_s

    url = f"https://api.themoviedb.org/3/search/{mt}?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "ArchiveHelper/1.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = (e.read() or b"").decode("utf-8", errors="replace").strip()
        if e.code == 401:
            raise RuntimeError("TMDB rejected the API key (HTTP 401).")
        raise RuntimeError(f"TMDB search failed (HTTP {e.code}). {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"TMDB network error: {e}")

    try:
        data = json.loads(payload)
    except Exception:
        raise RuntimeError("TMDB returned invalid JSON.")

    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    results: list[dict[str, str]] = []
    for it in items[:lim]:
        if not isinstance(it, dict):
            continue
        result_mt = str(it.get("media_type") or mt).strip().lower()
        if result_mt == "person":
            continue
        if result_mt not in {"movie", "tv"}:
            result_mt = "tv" if it.get("name") and not it.get("title") else "movie"
        name = str(it.get("title") or it.get("name") or "").strip()
        if not name:
            continue
        date_raw = str(it.get("release_date") or it.get("first_air_date") or "").strip()
        match_year = date_raw[:4] if re.fullmatch(r"\d{4}(-\d{2}-\d{2})?", date_raw) else ""
        overview = str(it.get("overview") or "").strip()
        results.append(
            {
                "id": str(it.get("id") or ""),
                "media_type": result_mt,
                "title": name,
                "year": match_year,
                "original_title": str(it.get("original_title") or it.get("original_name") or "").strip(),
                "popularity": str(it.get("popularity") or ""),
                "overview": overview,
            }
        )

    return results


def tmdb_movie_runtime_minutes(*, api_key: str, title: str, year: str = "") -> Optional[int]:
    """Best-effort lookup of a movie runtime (in minutes) from TMDB.

    Returns None if no runtime can be determined.
    """

    api_key_s = (api_key or "").strip()
    title_s = (title or "").strip()
    if not api_key_s or not title_s:
        return None

    try:
        matches = tmdb_search(api_key=api_key_s, query=title_s, year=year, media_type="movie", limit=6)
    except Exception:
        return None

    for match in matches:
        tmdb_id = str(match.get("id") or "").strip()
        if not tmdb_id.isdigit():
            continue

        url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?" + urllib.parse.urlencode({"api_key": api_key_s})
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "ArchiveHelper/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
            data = json.loads(payload)
        except Exception:
            continue

        runtime = data.get("runtime") if isinstance(data, dict) else None
        try:
            runtime_i = int(runtime)
        except Exception:
            runtime_i = 0
        if runtime_i > 0:
            return runtime_i

    return None


def _cmd_stdout(argv: list[str], *, timeout_s: int = 8) -> str:
    try:
        cp = run_cmd(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout_s)
        return (cp.stdout or "").strip()
    except Exception:
        return ""


def _extract_year_hint(text: str) -> str:
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text or "")
    return m.group(1) if m else ""


def _title_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    stop = {"the", "a", "an", "and", "of", "edition", "extended", "unrated", "remastered", "cut"}
    return {t for t in cleaned.split() if len(t) >= 2 and t not in stop}


def _clean_query_for_tmdb(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    s = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", s)
    s = re.sub(r"\b(?:1080p|2160p|720p|480p|x264|x265|h264|h265|hevc|hdr10?\+?|uhd|web[- ]?dl|webrip|blu[- ]?ray|brrip|dvdrip|remux|proper|repack)\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:disc|disk|dvd|bd|cd|title|copy|backup|retail|ntsc|pal|r1|r2|r3)\b", " ", s, flags=re.I)
    s = re.sub(r"[_\.\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return s


def _normalize_disc_hint(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    s = re.sub(r"[_\.]+", " ", s)
    s = re.sub(r"\b(?:1080p|2160p|720p|480p|x264|x265|h264|h265|hevc|hdr|uhd|web[- ]?dl|blu[- ]?ray|brrip|dvdrip)\b", " ", s, flags=re.I)
    s = re.sub(r"\b(disc|disk|dvd|video_ts|vol|volume|title|copy)\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return s


def _is_probable_disc_hint(text: str) -> bool:
    s = (text or "").strip().strip("\"'")
    if len(s) < 2 or len(s) > 160:
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    if re.fullmatch(r"[\d\W_]+", s):
        return False
    if s.lower().startswith(("http://", "https://")):
        return False
    if s.count("/") >= 2 or s.count("\\") >= 2:
        return False
    return True


def _query_variants_from_hint(text: str) -> list[str]:
    """Build likely TMDB query variants from a raw disc label/title."""
    raw = (text or "").strip()
    if not _is_probable_disc_hint(raw):
        return []

    variants: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        c = re.sub(r"\s+", " ", (candidate or "").strip(" -_\"'"))
        if len(c) < 2:
            return
        key = c.lower()
        if key not in seen:
            seen.add(key)
            variants.append(c)

    norm = _normalize_disc_hint(raw)
    _add(raw)
    _add(norm)
    _add(_clean_query_for_tmdb(raw))

    for base in [raw, norm]:
        stripped = re.sub(r"\b(?:disc|disk|dvd|cd|part)\s*[-_:#]*\s*\d{1,2}\b", "", base, flags=re.I)
        stripped = re.sub(r"\b(?:copy|backup|retail|r1|r2|nts[cp]|pal)\b", "", stripped, flags=re.I)
        _add(_normalize_disc_hint(stripped))

        # Labels often include extra detail after separators; try the title-first fragment.
        first_fragment = re.split(r"\s(?:[-–—:|]|/)+\s", stripped, maxsplit=1)[0]
        _add(_normalize_disc_hint(first_fragment))
        _add(_clean_query_for_tmdb(first_fragment))

    # If the hint still has many words, include a compact first-N-word variant.
    compact = _clean_query_for_tmdb(norm)
    words = compact.split()
    if len(words) >= 5:
        _add(" ".join(words[:4]))

    return variants


def _title_similarity(a: str, b: str) -> float:
    aa = re.sub(r"[^a-z0-9]+", " ", (a or "").lower()).strip()
    bb = re.sub(r"[^a-z0-9]+", " ", (b or "").lower()).strip()
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    return difflib.SequenceMatcher(None, aa, bb).ratio()


def _row_quality_score(*, query: str, row: dict[str, str], year_hint: str = "") -> float:
    title = str(row.get("title") or "")
    original = str(row.get("original_title") or "")
    similarity = max(_title_similarity(query, title), _title_similarity(query, original))
    score = similarity

    q_tokens = _title_tokens(query)
    t_tokens = _title_tokens(title)
    o_tokens = _title_tokens(original)
    overlap_t = (len(q_tokens & t_tokens) / len(q_tokens)) if q_tokens else 0.0
    overlap_o = (len(q_tokens & o_tokens) / len(q_tokens)) if q_tokens else 0.0
    overlap = max(overlap_t, overlap_o)
    score += 0.35 * overlap

    if q_tokens and (q_tokens <= t_tokens or q_tokens <= o_tokens):
        score += 0.15

    row_year = str(row.get("year") or "")
    if year_hint and row_year:
        if year_hint == row_year:
            score += 0.2
        else:
            try:
                if abs(int(year_hint) - int(row_year)) <= 1:
                    score += 0.08
            except Exception:
                pass

    try:
        popularity = float(str(row.get("popularity") or "0"))
    except Exception:
        popularity = 0.0
    score += min(0.15, popularity / 800.0)
    return score


def probe_disc_metadata(*, disc_device: str = "/dev/sr0") -> dict[str, object]:
    raw_hints: list[str] = []

    blkid = _cmd_stdout(["blkid", "-o", "export", disc_device])
    if blkid:
        for ln in blkid.splitlines():
            if ln.startswith("LABEL="):
                raw_hints.append(ln.split("=", 1)[1].strip())

    if shutil.which("isoinfo"):
        iso = _cmd_stdout(["isoinfo", "-d", "-i", disc_device])
        for ln in iso.splitlines():
            if "Volume id:" in ln:
                raw_hints.append(ln.split("Volume id:", 1)[1].strip())

    if shutil.which("lsdvd"):
        lsdvd_out = _cmd_stdout(["lsdvd", disc_device])
        for ln in lsdvd_out.splitlines():
            m = re.search(r"Disc Title:\s*(.+)", ln, flags=re.I)
            if m:
                raw_hints.append(m.group(1).strip())

    if shutil.which("makemkvcon"):
        mkv_info = _cmd_stdout(["makemkvcon", "-r", "--noscan", "info", "disc:0"], timeout_s=12)
        for ln in mkv_info.splitlines():
            if not ln.startswith("CINFO:"):
                continue
            vals = re.findall(r'"([^\"]+)"', ln)
            for v in vals:
                if len(v.strip()) >= 3:
                    raw_hints.append(v.strip())

    hints: list[str] = []
    seen_hints: set[str] = set()
    for raw in raw_hints:
        if not _is_probable_disc_hint(raw):
            continue
        n = _normalize_disc_hint(raw)
        if len(n) < 2:
            continue
        k = n.lower()
        if k in seen_hints:
            continue
        seen_hints.add(k)
        hints.append(n)

    queries: list[str] = []
    seen_queries: set[str] = set()
    for raw in raw_hints:
        if not _is_probable_disc_hint(raw):
            continue
        for q in _query_variants_from_hint(raw):
            key = q.lower()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            queries.append(q)
            if len(queries) >= 8:
                break
        if len(queries) >= 8:
            break

    if not queries:
        queries = hints[:6]

    year_hint = ""
    for q in queries:
        year_hint = _extract_year_hint(q)
        if year_hint:
            break

    return {"device": disc_device, "hints": hints, "queries": queries, "year_hint": year_hint}


def tmdb_suggest_from_disc(*, api_key: str, disc_device: str = "/dev/sr0", media_type: str = "auto", limit: int = 8) -> dict[str, object]:
    mt = (media_type or "auto").strip().lower()
    if mt not in {"auto", "movie", "tv"}:
        raise RuntimeError("TMDB disc media type must be 'auto', 'movie', or 'tv'.")

    meta = probe_disc_metadata(disc_device=disc_device)
    queries = [str(q) for q in (meta.get("queries") or []) if str(q).strip()]
    year_hint = str(meta.get("year_hint") or "")
    if not queries:
        return {**meta, "results": []}

    # For auto mode, multi search tends to recover noisy disc labels better;
    # keep direct movie/tv probes as a fallback.
    search_types = ["multi", "movie", "tv"] if mt == "auto" else [mt]
    dedup: dict[str, tuple[float, dict[str, str]]] = {}

    expanded_queries: list[str] = []
    seen_queries: set[str] = set()
    for q in queries:
        for variant in (q, re.sub(r"\b(19\d{2}|20\d{2})\b", "", q).strip()):
            key = variant.lower()
            if len(variant) < 2 or key in seen_queries:
                continue
            seen_queries.add(key)
            expanded_queries.append(variant)
        if len(expanded_queries) >= 10:
            break

    for q in expanded_queries:
        for search_mt in search_types:
            year_attempts = [year_hint] if year_hint else [""]
            if year_hint:
                year_attempts.append("")
            for y in year_attempts:
                try:
                    rows = tmdb_search(api_key=api_key, query=q, year=y, media_type=search_mt, limit=max(5, min(16, limit)))
                except Exception:
                    continue
                for row in rows:
                    key = f"{row.get('media_type','')}:{row.get('id','')}"
                    if not row.get("id"):
                        key = f"{row.get('media_type','')}:{(row.get('title') or '').lower()}:{row.get('year') or ''}"
                    score = _row_quality_score(query=q, row=row, year_hint=year_hint)
                    existing = dedup.get(key)
                    if existing is None or score > existing[0]:
                        dedup[key] = (score, row)
                    if len(dedup) >= 40:
                        break
                if len(dedup) >= 40:
                    break
            if len(dedup) >= 40:
                break
        if len(dedup) >= 40:
            break

    ranked = [pair for pair in dedup.values()]
    ranked.sort(key=lambda x: x[0], reverse=True)
    results = [row for _, row in ranked[: max(1, min(20, int(limit)))]]
    return {**meta, "results": results}


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


def _has_passwordless_sudo() -> bool:
    if os.geteuid() == 0:
        return True
    if not shutil.which("sudo"):
        return False
    cp = run_cmd(["sudo", "-n", "true"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cp.returncode == 0


def _makemkvcon_is_snap() -> bool:
    path = shutil.which("makemkvcon") or ""
    if "/snap/" in path:
        return True
    # Some installs may expose makemkvcon via PATH symlinks; check snap presence too.
    if shutil.which("snap"):
        cp = run_cmd(["snap", "list", "makemkv"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return cp.returncode == 0
    return False


def _parse_snap_connections_table(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("interface") and "plug" in line.lower() and "slot" in line.lower():
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        interface = parts[0]
        plug = parts[1]
        slot = parts[2]
        notes = parts[3] if len(parts) > 3 else ""
        rows.append({"interface": interface, "plug": plug, "slot": slot, "notes": notes})
    return rows


def maybe_ensure_makemkv_snap_interfaces() -> None:
    """Best-effort MakeMKV Snap access improvements.

    When MakeMKV is installed as a Snap, AppArmor confinement can block access to
    /dev/sg* (SCSI generic) devices unless certain interfaces are connected.

    This function:
    - detects a Snap-based MakeMKV
    - inspects `snap connections makemkv`
    - attempts to connect a small set of low-risk interfaces when sudo is non-interactive
    - otherwise prints the exact commands to run manually

    It never hard-fails the run.
    """

    if not _makemkvcon_is_snap():
        return
    if not shutil.which("snap"):
        print(
            "(Info) MakeMKV appears to be installed via Snap, but the `snap` command was not found. "
            "Skipping Snap interface checks.",
            file=sys.stderr,
        )
        return

    cp = run_cmd(["snap", "connections", "makemkv"], check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = (cp.stdout or "")
    if cp.returncode != 0:
        print(
            "(Info) Unable to read `snap connections makemkv`; skipping Snap interface checks.\n" + out.strip(),
            file=sys.stderr,
        )
        return

    rows = _parse_snap_connections_table(out)
    # Map plug name (makemkv:<plug>) -> slot
    plug_slot: dict[str, str] = {}
    for r in rows:
        plug = r.get("plug", "")
        slot = r.get("slot", "")
        if plug.startswith("makemkv:"):
            plug_slot[plug.split(":", 1)[1]] = slot

    # Only auto-connect conservative interfaces.
    auto_plugs = ["optical-write", "removable-media"]
    optional_plugs = ["process-control"]

    missing_auto = [p for p in auto_plugs if p in plug_slot and plug_slot.get(p, "-") == "-"]
    missing_optional = [p for p in optional_plugs if p in plug_slot and plug_slot.get(p, "-") == "-"]

    if not missing_auto and not missing_optional:
        return

    print("--------------------------------------------")
    print("MakeMKV (Snap) device access")
    print("--------------------------------------------")
    print(
        "MakeMKV is installed via Snap. Some Snap interfaces may need to be connected so MakeMKV can fully access the optical drive (often via /dev/sg*)."
    )

    if missing_auto:
        if _has_passwordless_sudo():
            for plug in missing_auto:
                print(f"(Info) Connecting Snap interface: makemkv:{plug}")
                try:
                    cp2 = _run_root_cmd(["snap", "connect", f"makemkv:{plug}"], check=False)
                    msg = (cp2.stdout or "").strip()
                    if msg:
                        print(msg)
                except Exception as e:
                    print(f"(Warn) Unable to connect makemkv:{plug}: {e}", file=sys.stderr)
        else:
            print("(Info) Passwordless sudo is not available, so the script will not change Snap connections automatically.")
            print("Run these commands manually on the server, then retry:")
            for plug in missing_auto:
                print(f"  sudo snap connect makemkv:{plug}")

    if missing_optional:
        print("(Info) Optional (advanced) Snap interfaces detected but not connected:")
        for plug in missing_optional:
            print(f"  makemkv:{plug}")
        print("Only enable these if you understand the security impact. Example:")
        for plug in missing_optional:
            print(f"  sudo snap connect makemkv:{plug}")


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
        "ffmpeg": "sudo apt-get update && sudo apt-get install -y ffmpeg",
        "find": "sudo apt-get update && sudo apt-get install -y findutils",
        "grep": "sudo apt-get update && sudo apt-get install -y grep",
        "HandBrakeCLI": "sudo apt-get update && sudo apt-get install -y handbrake-cli",
        "makemkvcon": "Install MakeMKV (makemkvcon) from MakeMKV upstream or a trusted third-party Debian repo; it is often not in Debian main.",
        "ddrescue": "sudo apt-get update && sudo apt-get install -y gddrescue",
        "dvdbackup": "sudo apt-get update && sudo apt-get install -y dvdbackup",
        "vobcopy": "sudo apt-get update && sudo apt-get install -y vobcopy",
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




def log_fallback_dependency_status() -> None:
    """Log availability of fallback tools used for optical recovery stages."""
    fallback_tools = ["ddrescue", "dvdbackup", "vobcopy"]
    missing: list[str] = []

    print("Fallback dependency check:")
    for cmd in fallback_tools:
        path = shutil.which(cmd)
        if path:
            print(f"  - {cmd}: available ({path})")
        else:
            missing.append(cmd)
            print(f"  - {cmd}: MISSING")
            print(f"    Debian hint: {debian_install_hint(cmd)}")

    if missing:
        print(
            "Fallback note: missing tools above will be skipped when fallback stages run; "
            "install them to enable full recovery attempts."
        )
    else:
        print("Fallback note: all fallback tools are installed.")


def check_deps(movies_dir: str, series_dir: str) -> int:
    deps = [
        "screen",
        "awk",
        "eject",
        "ffprobe",
        "ffmpeg",
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

    print("All required dependencies are present.")
    log_fallback_dependency_status()
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


class MakeMKVError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = int(code)


def run_makemkv_with_progress_to_dir(out_dir: Path, *, cache_mb: int = 128, source: str = "disc:0") -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        cache_mb = int(cache_mb)
    except Exception:
        cache_mb = 512
    cache_mb = max(16, min(8192, cache_mb))

    argv = [
        "stdbuf",
        "-oL",
        "-eL",
        "makemkvcon",
        "mkv",
        "--progress=-stdout",
        "--decrypt",
        f"--cache={cache_mb}",
        "--minlength=300",
        str(source),
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
        raise MakeMKVError(int(code), f"MakeMKV failed with exit code {code}")


def _run_ddrescue_iso_recovery(*, disc_dir: Path, disc_device: str = "/dev/sr0") -> Optional[Path]:
    if not shutil.which("ddrescue"):
        print("Fallback: ddrescue not available; skipping ddrescue stage.")
        return None

    fallback_dir = disc_dir / "_fallback"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    iso_path = fallback_dir / "recovered_disc.iso"
    map_path = fallback_dir / "recovered_disc.map"

    def _fmt_bytes(size: int) -> str:
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        val = float(max(0, size))
        for u in units:
            if val < 1024.0 or u == units[-1]:
                if u == "B":
                    return f"{int(val)} {u}"
                return f"{val:.1f} {u}"
            val /= 1024.0
        return "0 B"

    def _run_ddrescue_pass(argv: list[str], *, pass_label: str) -> int:
        proc = subprocess.Popen(argv)
        started = time.monotonic()
        while True:
            try:
                return proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                elapsed = int(time.monotonic() - started)
                mins, secs = divmod(elapsed, 60)
                try:
                    iso_size = iso_path.stat().st_size
                    size_msg = _fmt_bytes(iso_size)
                except OSError:
                    size_msg = "unknown"
                print(
                    f"Fallback: ddrescue {pass_label} still running "
                    f"(elapsed {mins:02d}:{secs:02d}, recovered size {size_msg})."
                )

    print("Fallback: running ddrescue image recovery (pass 1/2, no retries).")
    pass1_code = _run_ddrescue_pass(
        ["ddrescue", "-f", "-n", disc_device, str(iso_path), str(map_path)],
        pass_label="pass 1/2",
    )
    if pass1_code != 0:
        print(f"Fallback: ddrescue pass 1/2 exited with code {pass1_code}.")

    print("Fallback: running ddrescue image recovery (pass 2/2, retry bad sectors).")
    pass2_code = _run_ddrescue_pass(
        ["ddrescue", "-f", "-d", "-r3", disc_device, str(iso_path), str(map_path)],
        pass_label="pass 2/2",
    )
    if pass2_code != 0:
        print(f"Fallback: ddrescue pass 2/2 exited with code {pass2_code}.")

    if iso_path.exists() and iso_path.stat().st_size > 0:
        print(f"Fallback: ddrescue produced image: {iso_path}")
        return iso_path

    print("Fallback: ddrescue did not produce a usable image.")
    return None


def _run_dvdbackup_recovery(*, disc_dir: Path, disc_device: str = "/dev/sr0") -> Optional[Path]:
    if not shutil.which("dvdbackup"):
        print("Fallback: dvdbackup not available; skipping dvdbackup stage.")
        return None

    fallback_dir = disc_dir / "_fallback" / "dvdbackup"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    print("Fallback: running dvdbackup full-disc copy.")
    cp = run_cmd(
        ["dvdbackup", "-i", disc_device, "-M", "-o", str(fallback_dir)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print((cp.stdout or "").rstrip())

    # dvdbackup typically creates <output>/<disc_label>/VIDEO_TS.
    candidates = sorted(fallback_dir.rglob("VIDEO_TS"))
    for cand in candidates:
        if cand.is_dir():
            print(f"Fallback: dvdbackup produced VIDEO_TS: {cand}")
            return cand

    print("Fallback: dvdbackup did not produce a usable VIDEO_TS folder.")
    return None


def _run_vobcopy_recovery(*, disc_dir: Path, disc_device: str = "/dev/sr0") -> Optional[Path]:
    if not shutil.which("vobcopy"):
        print("Fallback: vobcopy not available; skipping vobcopy stage.")
        return None

    fallback_dir = disc_dir / "_fallback" / "vobcopy"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    print("Fallback: running vobcopy extraction.")
    cp = run_cmd(
        ["vobcopy", "-i", disc_device, "-m", "-o", str(fallback_dir)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print((cp.stdout or "").rstrip())

    candidates = sorted(fallback_dir.rglob("VIDEO_TS"))
    for cand in candidates:
        if cand.is_dir():
            print(f"Fallback: vobcopy produced VIDEO_TS: {cand}")
            return cand

    # Some vobcopy runs produce .vob files directly in output folder.
    if any(fallback_dir.rglob("*.VOB")) or any(fallback_dir.rglob("*.vob")):
        print(f"Fallback: vobcopy produced VOB files in: {fallback_dir}")
        return fallback_dir

    print("Fallback: vobcopy did not produce usable VOB/VIDEO_TS output.")
    return None


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


def handbrake_subtitle_args(mode: str) -> list[str]:
    mode_s = (mode or "preset").strip().lower()
    if mode_s == "soft":
        # Keep subtitle tracks selectable (best compatibility with MKV outputs).
        return ["--all-subtitles", "--subtitle-default=none"]
    if mode_s in {"none", "external"}:
        return ["--subtitle=none"]
    return []


def ffprobe_subtitle_streams(path: Path) -> list[dict]:
    try:
        cp = run_cmd(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_streams",
                "-print_format",
                "json",
                str(path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return []

    streams = data.get("streams") if isinstance(data, dict) else None
    return [st for st in (streams or []) if isinstance(st, dict)]


def _normalize_subtitle_language(raw: str) -> str:
    code = (raw or "").strip().lower()
    # Jellyfin-friendly, short language tags: use two-letter ISO-ish prefix.
    # Examples: eng -> en, en_us -> en, fra -> fr.
    letters = re.sub(r"[^a-z]", "", code)
    if len(letters) >= 2:
        return letters[:2]
    return "und"


def _subtitle_output_path(base: Path, ext: str) -> Path:
    out = Path(str(base) + ext)
    if not out.exists():
        return out
    for i in range(2, 100):
        cand = Path(str(base) + f".{i:02d}" + ext)
        if not cand.exists():
            return cand
    return out


def extract_external_subtitles(input_: Path, video_output: Path) -> None:
    streams = ffprobe_subtitle_streams(input_)
    if not streams:
        print(f"Subtitle extraction done: {input_.name} (no subtitle streams found)")
        return

    out_dir = video_output.parent
    stem = video_output.stem
    total_streams = len(streams)
    extracted_ok = 0
    extracted_failed = 0

    print(f"Subtitle extraction start: {input_.name} ({total_streams} streams)")

    for idx, st in enumerate(streams, start=1):
        stream_index = st.get("index")
        if not isinstance(stream_index, int):
            continue

        codec = str(st.get("codec_name") or "").strip().lower()
        tags = st.get("tags") if isinstance(st.get("tags"), dict) else {}
        lang = _normalize_subtitle_language(str(tags.get("language") or "und"))

        base = out_dir / f"{stem}.{lang}"

        if codec in {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}:
            out = _subtitle_output_path(base, ".srt")
            cmd = [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-i",
                str(input_),
                "-map",
                f"0:{stream_index}",
                "-c:s",
                "srt",
                str(out),
            ]
        elif codec == "dvd_subtitle":
            out = _subtitle_output_path(base, ".idx")
            cmd = [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-i",
                str(input_),
                "-map",
                f"0:{stream_index}",
                "-c:s",
                "copy",
                "-f",
                "vobsub",
                str(out),
            ]
        else:
            out = _subtitle_output_path(base, ".mks")
            cmd = [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-i",
                str(input_),
                "-map",
                f"0:{stream_index}",
                "-c:s",
                "copy",
                "-f",
                "matroska",
                str(out),
            ]

        print(f"Subtitle extraction progress: {idx}/{total_streams}: {input_.name} stream {stream_index} -> {out.name}")
        cp = run_cmd(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if cp.returncode != 0:
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            extracted_failed += 1
            print(f"Warning: subtitle extraction failed for stream {stream_index} ({codec or 'unknown'})")
            continue

        extracted_ok += 1

    print(f"Subtitle extraction done: {input_.name} ({extracted_ok} succeeded, {extracted_failed} failed)")


def hb_encode(input_: Path, output: Path, preset: str, *, subtitle_mode: str = "preset") -> None:
    hb_encode_with_progress(input_, output, preset, subtitle_mode=subtitle_mode)


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
        candidates: list[Path] = []
        for pat in ("rip_and_encode_*.log", "rip_and_encode_v2_*.log"):
            try:
                candidates.extend(list(log_dir.glob(pat)))
            except Exception:
                pass
        logs = sorted(set(candidates), key=lambda p: p.stat().st_mtime, reverse=True)
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


def hb_encode_with_progress(input_: Path, output: Path, preset: str, *, subtitle_mode: str = "preset") -> None:
    """Run HandBrakeCLI while emitting progress as newline-delimited log lines."""

    args = ["HandBrakeCLI", "-i", str(input_), "-o", str(output), "--preset", preset]
    args.extend(handbrake_subtitle_args(subtitle_mode))
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
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


WORKDIR_MARKER_NAME = ".rip_and_encode_workdir"
WORKDIR_MARKER_NAMES = (WORKDIR_MARKER_NAME, ".rip_and_encode_v2_workdir")

DISC_MANIFEST_NAME = ".rip_and_encode_disc_manifest.json"
DISC_MANIFEST_NAMES = (DISC_MANIFEST_NAME, ".rip_and_encode_v2_disc_manifest.json")
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
    output_ext: str,
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
        output_movie_main = output_movie_dir / f"{title}.{output_ext}"
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


def rip_disc_if_needed(
    disc_dir: Path,
    prompt_msg: str,
    *,
    wait_for_enter: bool = True,
    makemkv_cache_mb: int = 128,
    validate_rip: Optional[Callable[[list[Path]], tuple[bool, str]]] = None,
) -> list[Path]:
    def _has_acceptable_rip() -> tuple[bool, str]:
        mkvs_now = find_mkvs_in_dir(disc_dir)
        if not mkvs_now:
            return False, "No MKVs found after rip attempt."
        if validate_rip is None:
            return True, ""
        try:
            ok, reason = validate_rip(mkvs_now)
            return bool(ok), (reason or "")
        except Exception as e:
            # Validation itself should not abort a run; continue with current output.
            return True, f"Rip validation check failed unexpectedly ({e}); continuing with current output."

    existing_mkvs = find_mkvs_in_dir(disc_dir)
    direct_error: Optional[Exception] = None

    if existing_mkvs:
        print(f"Resume: found existing MKVs in {disc_dir}; validating rip before encode.")
        acceptable, reason = _has_acceptable_rip()
        if acceptable:
            return existing_mkvs
        print(f"Resume: existing rip failed validation; entering fallback flow: {reason}")
        direct_error = RuntimeError(reason or "Existing MKVs failed validation.")
    else:
        print(prompt_msg)
        if wait_for_enter:
            input()

        auto_retry_used = False
        while True:
            try:
                run_makemkv_with_progress_to_dir(disc_dir, cache_mb=makemkv_cache_mb)
                direct_error = None
                break
            except MakeMKVError as e:
                # Exit code 11 commonly corresponds to a transient "Failed to open disc"
                # (disc not fully ready, drive hiccup, tray state). Treat it as recoverable.
                if int(getattr(e, "code", -1)) == 11:
                    if not auto_retry_used:
                        auto_retry_used = True
                        print("MakeMKV could not open the disc (exit 11). Retrying once in 8 seconds...")
                        try:
                            run_cmd(["eject", "-t", "/dev/sr0"], check=False)
                        except Exception:
                            pass
                        time.sleep(8)
                        continue

                    print("MakeMKV could not open the disc (exit 11).")
                    print("Check the disc/drive and press Enter to retry (or Ctrl-C / Stop to abort).")
                    input()
                    continue
                direct_error = e
                break

    if direct_error is None:
        acceptable, reason = _has_acceptable_rip()
        if not acceptable:
            direct_error = RuntimeError(reason or "Rip output did not pass validation.")

    if direct_error is not None:
        print(f"Fallback: direct MakeMKV rip failed: {direct_error}")
        print("Fallback: stage 1/3: ddrescue image recovery + MakeMKV from recovered image.")
        recovered_iso = _run_ddrescue_iso_recovery(disc_dir=disc_dir)
        if recovered_iso is not None:
            try:
                run_makemkv_with_progress_to_dir(
                    disc_dir,
                    cache_mb=makemkv_cache_mb,
                    source=f"file:{recovered_iso}",
                )
            except Exception as e:
                print(f"Fallback: MakeMKV from ddrescue image failed: {e}")
            else:
                print("Fallback: success from ddrescue image.")

        acceptable, reason = _has_acceptable_rip()
        if not acceptable:
            print(f"Fallback: stage 1 output still not acceptable: {reason}")
            print("Fallback: stage 2/3: dvdbackup structure copy + MakeMKV from VIDEO_TS.")
            dvdbackup_source = _run_dvdbackup_recovery(disc_dir=disc_dir)
            if dvdbackup_source is not None:
                try:
                    run_makemkv_with_progress_to_dir(
                        disc_dir,
                        cache_mb=makemkv_cache_mb,
                        source=f"file:{dvdbackup_source}",
                    )
                except Exception as e:
                    print(f"Fallback: MakeMKV from dvdbackup output failed: {e}")
                else:
                    print("Fallback: success from dvdbackup output.")

        acceptable, reason = _has_acceptable_rip()
        if not acceptable:
            print(f"Fallback: stage 2 output still not acceptable: {reason}")
            print("Fallback: stage 3/3: vobcopy extraction + MakeMKV from fallback output.")
            vobcopy_source = _run_vobcopy_recovery(disc_dir=disc_dir)
            if vobcopy_source is not None:
                try:
                    run_makemkv_with_progress_to_dir(
                        disc_dir,
                        cache_mb=makemkv_cache_mb,
                        source=f"file:{vobcopy_source}",
                    )
                except Exception as e:
                    print(f"Fallback: MakeMKV from vobcopy output failed: {e}")
                else:
                    print("Fallback: success from vobcopy output.")

        acceptable, reason = _has_acceptable_rip()
        if not acceptable:
            raise RuntimeError(
                "Fallback: all recovery stages failed "
                f"(direct_makemkv, ddrescue, dvdbackup, vobcopy). Last reason: {reason}"
            )
    try:
        run_cmd(["eject", "/dev/sr0"], check=False)
    except Exception:
        pass

    mkvs = find_mkvs_in_dir(disc_dir)
    if not mkvs:
        raise RuntimeError(f"No MKVs found in {disc_dir} after rip step.")
    return mkvs

def _disc_manifest_path(disc_dir: Path) -> Path:
    for name in DISC_MANIFEST_NAMES:
        p = disc_dir / name
        if p.exists():
            return p
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
    output_ext: str,
) -> Optional[Path]:
    if not clean_episode_title:
        return None
    if not season_dir.exists():
        return None
    try:
        pat = re.compile(
            r"^" + re.escape(series_title) + r" - S" + re.escape(season_pad) + r"E\d{2} - " + re.escape(clean_episode_title) + r"\." + re.escape(output_ext) + r"$"
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
    subtitle_mode: str,
    submit_encode,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    stem = output.stem
    if subtitle_mode == "external":
        extract_external_subtitles(input_, output)

    if overlap:
        append_extra_nfo_if_missing(extras_nfo, stem, output.name)
        submit_encode(input_, output, preset, subtitle_mode)
    else:
        if output.exists():
            print(f"Skipping encode (exists): {output}")
        else:
            hb_encode(input_, output, preset, subtitle_mode=subtitle_mode)
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
    output_ext: str,
    subtitle_mode: str,
    submit_encode,
) -> None:
    out = unique_out_path(extras_dir, base_name, output_ext)
    stem = out.stem

    if subtitle_mode == "external":
        extract_external_subtitles(input_, out)

    if overlap:
        append_extra_nfo_if_missing(extras_nfo, stem, out.name)
        submit_encode(input_, out, preset, subtitle_mode)
    else:
        if out.exists():
            print(f"Skipping encode (exists): {out}")
        else:
            hb_encode(input_, out, preset, subtitle_mode=subtitle_mode)
        append_extra_nfo_if_missing(extras_nfo, stem, out.name)


def process_movie_disc(
    *,
    ctx: TitleContext,
    disc_index: int,
    disc_dir: Path,
    overlap: bool,
    preset: str,
    output_ext: str,
    subtitle_mode: str,
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

        if subtitle_mode == "external":
            extract_external_subtitles(analysis.main_mkv, ctx.output_movie_main)

        if overlap:
            submit_encode(analysis.main_mkv, ctx.output_movie_main, preset, subtitle_mode)
        else:
            if ctx.output_movie_main.exists():
                print(f"Skipping encode (exists): {ctx.output_movie_main}")
            else:
                hb_encode(analysis.main_mkv, ctx.output_movie_main, preset, subtitle_mode=subtitle_mode)

        for f in _series_plan_order(mkvs):
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
                output_ext=output_ext,
                subtitle_mode=subtitle_mode,
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
                output_ext=output_ext,
                subtitle_mode=subtitle_mode,
                submit_encode=submit_encode,
            )

    close_extras_nfo(ctx.output_extras_nfo)


def movie_disc_has_main_feature(
    *,
    mkvs: list[Path],
    tmdb_runtime_min: Optional[int] = None,
) -> tuple[bool, str]:
    """Heuristic check that disc output contains a plausible main movie title."""

    if not mkvs:
        return False, "No MKVs were produced."

    durations = [ffprobe_duration_seconds(f) for f in mkvs]
    valid_durations = [d for d in durations if d > 0]
    if not valid_durations:
        return True, "Could not read MKV durations; skipping movie-runtime validation."

    longest = max(valid_durations)

    if tmdb_runtime_min and tmdb_runtime_min > 0:
        expected_s = int(tmdb_runtime_min) * 60
        # Allow director's/extended cuts and PAL/NTSC drift while still catching
        # obvious extras-only rips.
        min_ok_s = max(1800, int(expected_s * 0.55))
        if longest < min_ok_s:
            return (
                False,
                f"Longest ripped title is {longest // 60} min, well below TMDB runtime "
                f"({tmdb_runtime_min} min).",
            )
        return True, ""

    # No TMDB runtime available: very conservative guard for obvious misses.
    if longest < 2400:
        return False, f"Longest ripped title is only {longest // 60} min; likely extras-only rip."

    return True, ""


def series_next_episode_number(season_dir: Path) -> int:
    if not season_dir.exists():
        return 1
    ep_nums: list[int] = []
    for name in season_dir.iterdir():
        m = re.search(r"E(\d{2})", name.name)
        if m:
            ep_nums.append(int(m.group(1)))
    return (max(ep_nums) if ep_nums else 0) + 1


def _natural_key(text: str) -> list[object]:
    parts = re.split(r"(\d+)", text or "")
    key: list[object] = []
    for p in parts:
        if not p:
            continue
        if p.isdigit():
            key.append(int(p))
        else:
            key.append(p.lower())
    return key


def _episode_hint_from_text(text: str) -> Optional[int]:
    s = (text or "").strip()
    if not s:
        return None

    patterns = (
        r"\bS\d{1,2}E(\d{1,3})\b",
        r"\bE(?:P(?:ISODE)?)?\s*[-_. ]?(\d{1,3})\b",
        r"\b(\d{1,2})x(\d{1,3})\b",
        r"\bPART\s*(\d{1,3})\b",
    )
    for pat in patterns:
        m = re.search(pat, s, flags=re.I)
        if not m:
            continue
        try:
            if len(m.groups()) >= 2 and m.group(2):
                n = int(m.group(2))
            else:
                n = int(m.group(1))
            if 1 <= n <= 999:
                return n
        except Exception:
            continue
    return None


def _source_title_order_hint(path: Path) -> Optional[int]:
    stem = path.stem
    for pat in (r"(?:^|[^a-z])t(?:itle)?[_\- ]?(\d{1,3})(?:[^a-z]|$)", r"\btitle[_\- ]?(\d{1,3})\b"):
        m = re.search(pat, stem, flags=re.I)
        if not m:
            continue
        try:
            n = int(m.group(1))
            if 0 <= n <= 999:
                return n
        except Exception:
            continue
    return None


def _series_plan_order(mkvs: list[Path]) -> list[Path]:
    """Return MKVs in a more episode-friendly order.

    Priority:
    1) episode number parsed from ffprobe metadata title
    2) MakeMKV-style title index hinted in filename (e.g. title_t02.mkv)
    3) natural filename order
    """

    def _key(path: Path) -> tuple[int, int, int, list[object]]:
        meta_title = ffprobe_meta_title(path)
        ep_hint = _episode_hint_from_text(meta_title or "")
        source_hint = _source_title_order_hint(path)
        return (
            0 if ep_hint is not None else 1,
            ep_hint if ep_hint is not None else 10_000,
            source_hint if source_hint is not None else 10_000,
            _natural_key(path.name),
        )

    return sorted(mkvs, key=_key)


def process_series_disc(
    *,
    ctx: TitleContext,
    disc_dir: Path,
    overlap: bool,
    preset: str,
    output_ext: str,
    subtitle_mode: str,
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
        for f in _series_plan_order(mkvs):
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
                out = unique_out_path(ctx.output_extras_dir, clean or "Extra", output_ext)
                # Ensure no accidental duplicates in the plan.
                if str(out) in used_outputs:
                    out = unique_out_path(ctx.output_extras_dir, (clean or "Extra") + "-dup", output_ext)
                used_outputs.add(str(out))
                items.append({"type": "extra", "input_rel": input_rel, "output": str(out)})
                continue

            # If a previous legacy run already produced this episode output, reuse it.
            existing = _find_existing_series_episode_output(
                season_dir=ctx.output_season_dir,
                series_title=ctx.title,
                season_pad=ctx.season_pad,
                clean_episode_title=clean,
                output_ext=output_ext,
            )
            if existing is not None:
                out = existing
            else:
                out = ctx.output_season_dir / f"{ctx.title} - S{ctx.season_pad}E{ep_num:02d} - {clean}.{output_ext}"
                ep_num += 1

            if str(out) in used_outputs:
                # Extremely defensive: fall back to allocating a new episode number.
                out = ctx.output_season_dir / f"{ctx.title} - S{ctx.season_pad}E{ep_num:02d} - {clean}.{output_ext}"
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
                    subtitle_mode=subtitle_mode,
                    submit_encode=submit_encode,
                )
            else:
                if subtitle_mode == "external":
                    extract_external_subtitles(input_path, out)

                if overlap:
                    submit_encode(input_path, out, preset, subtitle_mode)
                else:
                    if out.exists():
                        print(f"Skipping encode (exists): {out}")
                    else:
                        hb_encode(input_path, out, preset, subtitle_mode=subtitle_mode)
        except Exception:
            raise

    close_extras_nfo(ctx.output_extras_nfo)


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
        "\n"
        "CD music mode:\n"
        "  - Use --disc-type cd to rip audio CDs with abcde.\n"
        "  - Metadata lookup uses MusicBrainz/CDDB via abcde (album/artist/tracks).\n"
        "  - Output is arranged for Jellyfin as: Artist/Album (Year)/01 - Track.ext\n"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rip_and_encode.py",
        formatter_class=argparse.RawTextHelpFormatter,
        description=usage_text(),
    )

    p.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")
    p.add_argument("--simple", action="store_true", help="Guided mode (safe defaults)")
    p.add_argument("--keep-mkvs", "-k", action="store_true", help="Do not delete MKVs and do not delete the WORK_DIR")

    p.add_argument(
        "--disc-type",
        choices=["dvd", "bluray", "cd"],
        default="dvd",
        help="Disc type hint for ripping. 'bluray' increases MakeMKV cache (1024MB vs 512MB); 'cd' uses abcde for music CDs.",
    )

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

    p.add_argument(
        "--output-container",
        choices=["mp4", "mkv"],
        default=(os.environ.get("HB_OUTPUT_CONTAINER", "mp4") or "mp4").lower(),
        help="Output container extension for encoded files (default: mp4)",
    )

    p.add_argument(
        "--subtitle-mode",
        choices=["preset", "soft", "external", "none"],
        default=(os.environ.get("HB_SUBTITLE_MODE", "external") or "external").lower(),
        help=(
            "Subtitle behavior: preset=use preset defaults, "
            "soft=embed subtitle tracks without default selection, "
            "external=extract subtitles from source MKV to sidecar files with ffmpeg, "
            "none=drop subtitles"
        ),
    )

    p.add_argument("--movies-dir", default="/storage/Movies", help="Movies output directory (default: /storage/Movies)")
    p.add_argument("--series-dir", default="/storage/Series", help="Series output directory (default: /storage/Series)")
    p.add_argument("--books-dir", default="/storage/Books", help="Audiobook output directory (default: /storage/Books)")
    p.add_argument("--music-dir", default="/storage/Music", help="Music output directory (default: /storage/Music)")
    p.add_argument("--cd-artist", default="", help="Optional artist override hint for CD logging/organization")
    p.add_argument("--cd-album", default="", help="Optional album override hint for CD logging/organization")
    p.add_argument("--cd-year", default="", help="Optional 4-digit album year for CD folder naming")

    p.add_argument(
        "--audiobook-workflow",
        action="store_true",
        help="Run audiobook post-processing: clean Audible-style names, generate book.nfo, and optionally run tagbooks.sh.",
    )
    p.add_argument(
        "--audible-sync",
        action="store_true",
        help="Before audiobook post-processing, run an external Audible downloader command (credentials are not persisted).",
    )
    p.add_argument(
        "--audible-download-cmd",
        default=os.environ.get("AUDIBLE_DOWNLOAD_CMD", ""),
        help="Shell command to sync/download Audible books. Supports {books_dir} placeholder; credentials are passed via AUDIBLE_USERNAME/AUDIBLE_PASSWORD env vars.",
    )
    p.add_argument("--audible-username", default="", help="Audible login username/email (optional; prompted if missing when --audible-sync is used)")
    p.add_argument("--audible-password", default="", help="Audible login password (optional; prompted if missing when --audible-sync is used)")
    p.add_argument("--audible-locale", default="us", help="Audible marketplace locale hint for downloader command (default: us)")
    p.add_argument("--audible-library-json", default="", help="Optional Audible metadata export JSON used to enrich generated book.nfo files")
    p.add_argument(
        "--skip-tagbooks",
        action="store_true",
        help="When used with --audiobook-workflow, do not run tagbooks.sh after generating NFO files.",
    )
    p.add_argument("--tagbooks-script", default="", help="Path to tagbooks.sh (defaults to ./tagbooks.sh or <books-dir>/tagbooks.sh)")

    p.add_argument("--csv", dest="csv_file", default="", help="Drive continuous mode from a CSV schedule (implies --continuous)")

    p.add_argument(
        "--no-disc-prompts",
        action="store_true",
        help=(
            "In CSV mode, do not pause between discs for manual disc insertion. "
            "Useful when discs were ripped elsewhere and MKVs already exist in the per-disc folders."
        ),
    )

    p.add_argument("--tmdb-search", default="", help="Search TMDB and print JSON results (non-interactive utility mode)")
    p.add_argument(
        "--tmdb-suggest-from-disc",
        action="store_true",
        help="Probe the inserted disc with Linux tools and return TMDB suggestions as JSON.",
    )
    p.add_argument("--tmdb-api-key", default="", help="TMDB API key used with --tmdb-search")
    p.add_argument("--disc-device", default="/dev/sr0", help="Optical disc device path used by --tmdb-suggest-from-disc")
    p.add_argument("--tmdb-year", default="", help="Optional 4-digit year hint used with --tmdb-search")
    p.add_argument(
        "--tmdb-media-type",
        choices=["movie", "tv"],
        default="movie",
        help="TMDB search media type for --tmdb-search (default: movie)",
    )
    p.add_argument("--tmdb-limit", type=int, default=8, help="Max TMDB matches to return with --tmdb-search (default: 8)")
    p.add_argument(
        "--tmdb-disc-media-type",
        choices=["auto", "movie", "tv"],
        default="auto",
        help="TMDB type filter for --tmdb-suggest-from-disc (default: auto)",
    )

    return p.parse_args(argv)


def _format_gb(bytes_: int) -> float:
    try:
        return float(bytes_) / (1024.0 ** 3)
    except Exception:
        return 0.0


def _disk_targets_for_run(*, home_base: Path, movies_dir: str, series_dir: str) -> list[Path]:
    targets: list[Path] = [home_base]
    if movies_dir and not is_remote_dest(movies_dir):
        targets.append(Path(movies_dir))
    if series_dir and not is_remote_dest(series_dir):
        targets.append(Path(series_dir))
    return targets


def _dedupe_paths_by_device(paths: list[Path]) -> list[Path]:
    seen: set[int] = set()
    unique: list[Path] = []
    for p in paths:
        try:
            dev = os.stat(p).st_dev
        except Exception:
            dev = None
        if dev is None:
            unique.append(p)
            continue
        if dev in seen:
            continue
        seen.add(dev)
        unique.append(p)
    return unique


def pause_if_low_disk_space(*, paths: list[Path], min_free_gb: int = 20) -> None:
    """Pause the run if free space is below the threshold on any relevant filesystem.

    Intended for long CSV runs: prevents silently filling disks mid-run.
    """

    try:
        min_free_gb = int(min_free_gb)
    except Exception:
        min_free_gb = 20
    min_free_gb = max(1, min_free_gb)

    targets = _dedupe_paths_by_device([p for p in (paths or []) if p])
    if not targets:
        return

    while True:
        low: list[tuple[Path, float]] = []
        for p in targets:
            try:
                usage = shutil.disk_usage(p)
                free_gb = _format_gb(int(usage.free))
            except Exception:
                continue
            if free_gb < float(min_free_gb):
                low.append((p, free_gb))

        if not low:
            return

        print("Low disk space detected. Please free up space on the server.")
        for p, free_gb in low:
            print(f"  - {p}: {free_gb:.1f} GB free (need >= {min_free_gb} GB)")
        print(
            "Low disk space: free up space and Press Enter to retry.\n"
            "\n"
            "Note: this script does NOT auto-clean while a job is running, because\n"
            "background encodes may still be reading MKVs from the work directory.\n"
            "To run managed cleanup, stop the job and use the GUI Cleanup button\n"
            "(or run rip_and_encode.py --cleanup-mkvs separately).\n"
            "\n"
            "CSV resume note: after cleanup, re-running the same CSV will skip movie\n"
            "discs whose output files already exist; for series, you may still need\n"
            "to remove completed rows if you cleaned the work directory."
        )
        input()


def _encode_lock_active_for_output(out: Path) -> bool:
    lock = encode_lock_path(out)
    return not lock_is_stale_or_clear(lock)


def _movie_disc_outputs_exist(ctx: "TitleContext", disc_index: int, output_ext: str) -> bool:
    """Best-effort check to skip re-ripping discs on CSV restart.

    Conservative: only skips when we see expected outputs and there is no active
    encode lock suggesting the output is still being written.
    """

    try:
        disc_index = int(disc_index)
    except Exception:
        return False
    if disc_index < 1:
        return False

    # Disc 1: main movie output is deterministic.
    if disc_index == 1:
        out = ctx.output_movie_main
        if out is None or not out.exists():
            return False
        if _encode_lock_active_for_output(out):
            return False
        return True

    # Disc 2+: extras-only outputs have a deterministic prefix.
    extras_dir = ctx.output_extras_dir
    if not extras_dir.exists():
        return False
    prefix = f"Disc{disc_index:02d}_"
    try:
        for p in extras_dir.glob(prefix + f"*.{output_ext}"):
            if p.is_file() and not _encode_lock_active_for_output(p):
                return True
    except Exception:
        return False
    return False


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


def run_cd_rip_with_abcde(*, music_dir: str, artist_hint: str = "", album_hint: str = "", year_hint: str = "") -> int:
    if not shutil.which("abcde"):
        raise RuntimeError("Missing required command for CD workflow: abcde")

    out_root = Path(music_dir).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    year = (year_hint or "").strip()
    if year and not re.fullmatch(r"\d{4}", year):
        year = ""

    album_expr = "${ALBUMFILE}"
    if year:
        album_expr = f"${{ALBUMFILE}} ({year})"

    conf_lines = [
        "CDDBMETHOD=musicbrainz,cddb",
        "OUTPUTTYPE=flac",
        "PADTRACKS=y",
        f"OUTPUTDIR={shlex.quote(str(out_root))}",
        f"OUTPUTFORMAT='${{ARTISTFILE}}/{album_expr}/${{TRACKNUM}} - ${{TRACKFILE}}'",
        f"VAOUTPUTFORMAT='Various Artists/{album_expr}/${{TRACKNUM}} - ${{ARTISTFILE}} - ${{TRACKFILE}}'",
    ]

    with tempfile.TemporaryDirectory(prefix="archive_helper_abcde_") as td:
        conf_path = Path(td) / "abcde_archive_helper.conf"
        conf_path.write_text("\n".join(conf_lines) + "\n", encoding="utf-8")

        print("CD workflow: ripping with abcde (MusicBrainz/CDDB metadata lookup).")
        print("Jellyfin naming target: Artist/Album (Year)/01 - Track.flac")
        if artist_hint:
            print(f"CD artist hint: {artist_hint}")
        if album_hint:
            print(f"CD album hint: {album_hint}")
        if year:
            print(f"CD year hint: {year}")

        cp = subprocess.run(
            ["abcde", "-c", str(conf_path), "-o", "flac"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        out = (cp.stdout or "").strip()
        if out:
            print(out)
        if cp.returncode != 0:
            raise RuntimeError(f"abcde failed with exit code {cp.returncode}")

    print(f"CD workflow complete. Music written under: {out_root}")
    return 0




def _sanitize_audible_name(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    s = re.sub(r"\(\s*unabridged\s*\)", "", s, flags=re.I)
    s = re.sub(r"\[[A-Z0-9]{8,}\]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return s


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _extract_meta_author(entry: dict[str, object]) -> str:
    authors = entry.get("authors")
    if isinstance(authors, list):
        vals = []
        for a in authors:
            if isinstance(a, dict):
                name = str(a.get("name") or "").strip()
                if name:
                    vals.append(name)
            elif isinstance(a, str) and a.strip():
                vals.append(a.strip())
        if vals:
            return ", ".join(vals)
    if isinstance(authors, str):
        return authors.strip()
    return str(entry.get("author") or "").strip()


def _load_audible_library_index(path: str) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError("Audible metadata JSON must be an array or an object with an 'items' array.")

    index: dict[str, dict[str, str]] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        asin = str(row.get("asin") or "").strip()
        author = _extract_meta_author(row)
        if not title and not asin:
            continue

        meta = {
            "title": title,
            "author": author,
            "summary": str(row.get("summary") or row.get("description") or "").strip(),
            "language": str(row.get("language") or "").strip(),
            "publisher": str(row.get("publisher") or "").strip(),
            "year": str(row.get("year") or row.get("release_year") or "").strip(),
            "genre": str(row.get("genre") or "").strip(),
            "sorttitle": str(row.get("sort_title") or "").strip(),
            "series_name": "",
            "series_number": "",
            "collection": "",
        }

        series = row.get("series")
        if isinstance(series, dict):
            meta["series_name"] = str(series.get("name") or "").strip()
            meta["series_number"] = str(series.get("sequence") or series.get("number") or "").strip()
        elif isinstance(series, list) and series:
            first = series[0]
            if isinstance(first, dict):
                meta["series_name"] = str(first.get("name") or "").strip()
                meta["series_number"] = str(first.get("sequence") or first.get("number") or "").strip()

        collection = row.get("collection")
        if isinstance(collection, dict):
            meta["collection"] = str(collection.get("name") or "").strip()
        elif isinstance(collection, str):
            meta["collection"] = collection.strip()

        keys = {_normalize_key(title), _normalize_key(_sanitize_audible_name(title)), _normalize_key(asin)}
        for k in keys:
            if k:
                index[k] = meta
    return index


def _render_book_nfo(*, title: str, cover_name: str, meta: dict[str, str]) -> str:
    def x(v: str) -> str:
        return html.escape((v or "").strip())

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<book>',
        f'  <title>{x(title)}</title>',
    ]
    for k in ("author", "genre", "year", "summary", "language", "publisher", "sorttitle"):
        v = (meta.get(k) or "").strip()
        if v:
            lines.append(f'  <{k}>{x(v)}</{k}>')
    sn = (meta.get("series_name") or "").strip()
    si = (meta.get("series_number") or "").strip()
    if sn or si:
        lines.append("  <series>")
        if sn:
            lines.append(f'    <name>{x(sn)}</name>')
        if si:
            lines.append(f'    <number>{x(si)}</number>')
        lines.append("  </series>")
    coll = (meta.get("collection") or "").strip()
    if coll:
        lines.append(f'  <collection>{x(coll)}</collection>')
    lines.append(f'  <thumb>{x(cover_name)}</thumb>')
    lines.append('</book>')
    return "\n".join(lines) + "\n"


def _run_audible_sync(*, books_dir: Path, command_template: str, username: str, password: str, locale: str) -> None:
    cmd_tpl = (command_template or "").strip()
    if not cmd_tpl:
        raise RuntimeError("--audible-sync requires --audible-download-cmd (or AUDIBLE_DOWNLOAD_CMD env var).")

    if not username:
        username = input("Audible email/username: ").strip()
    if not password:
        password = getpass.getpass("Audible password: ").strip()

    command = cmd_tpl.format(books_dir=shlex.quote(str(books_dir)))
    env = os.environ.copy()
    env["AUDIBLE_USERNAME"] = username
    env["AUDIBLE_PASSWORD"] = password
    env["AUDIBLE_LOCALE"] = (locale or "us").strip() or "us"

    print("Starting Audible library sync...")
    cp = subprocess.run(["bash", "-lc", command], env=env, check=False)
    env.pop("AUDIBLE_USERNAME", None)
    env.pop("AUDIBLE_PASSWORD", None)
    if cp.returncode != 0:
        raise RuntimeError(f"Audible sync command failed with exit code {cp.returncode}")


def _safe_path_component(value: str, *, fallback: str) -> str:
    v = _sanitize_audible_name(value)
    v = re.sub(r'[\\/:*?"<>|]', " ", v)
    v = re.sub(r"\s+", " ", v).strip(" .")
    return v or fallback


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.name
    i = 2
    while True:
        candidate = path.with_name(f"{stem} ({i})")
        if not candidate.exists():
            return candidate
        i += 1


def _find_cover_image(dir_path: Path) -> Path | None:
    for name in ["cover.jpg", "cover.jpeg", "folder.jpg", "Folder.jpg", "thumb.jpg"]:
        p = dir_path / name
        if p.exists() and p.is_file():
            return p
    return None


def run_audiobook_workflow(
    *,
    books_dir: str,
    metadata_json: str = "",
    run_tagging: bool = True,
    tagbooks_script: str = "",
    audible_sync: bool = False,
    audible_download_cmd: str = "",
    audible_username: str = "",
    audible_password: str = "",
    audible_locale: str = "us",
) -> int:
    root = Path(books_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    library_root = root / "Audiobooks"
    library_root.mkdir(parents=True, exist_ok=True)

    if audible_sync:
        _run_audible_sync(
            books_dir=root,
            command_template=audible_download_cmd,
            username=(audible_username or "").strip(),
            password=(audible_password or "").strip(),
            locale=audible_locale,
        )

    meta_idx = _load_audible_library_index(metadata_json) if metadata_json else {}

    m4b_files = sorted(root.rglob("*.m4b"))
    m4b_files = [p for p in m4b_files if p.is_file()]
    if not m4b_files:
        print(f"No .m4b files found under: {root}")

    created = 0
    renamed = 0
    moved = 0

    for m4b in m4b_files:
        if not m4b.exists():
            continue

        src_parent = m4b.parent
        clean_file_name = _sanitize_audible_name(m4b.stem)
        if clean_file_name and clean_file_name != m4b.stem:
            new_file = m4b.with_name(clean_file_name + m4b.suffix)
            if not new_file.exists():
                m4b.rename(new_file)
                m4b = new_file
                renamed += 1

        title_guess = _sanitize_audible_name(src_parent.name) or _sanitize_audible_name(m4b.stem) or src_parent.name
        asin_match = re.search(r"\[([A-Z0-9]{8,})\]", src_parent.name + " " + m4b.stem)
        asin = asin_match.group(1) if asin_match else ""
        meta = meta_idx.get(_normalize_key(asin)) or meta_idx.get(_normalize_key(title_guess)) or {}

        title = (meta.get("title") or title_guess or m4b.stem).strip()
        author = (meta.get("author") or "Unknown Author").strip() or "Unknown Author"
        year = (meta.get("year") or "").strip()
        year = year if re.fullmatch(r"\d{4}", year) else ""

        author_dir_name = _safe_path_component(author, fallback="Unknown Author")
        title_dir_name = _safe_path_component(title, fallback="Unknown Title")
        if year:
            title_dir_name = f"{title_dir_name} ({year})"

        target_dir = library_root / author_dir_name / title_dir_name
        target_dir = _unique_path(target_dir) if target_dir.exists() and m4b.parent.resolve() != target_dir.resolve() else target_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        target_file = target_dir / (_safe_path_component(title, fallback="Unknown Title") + ".m4b")
        if target_file.exists() and m4b.resolve() != target_file.resolve():
            suffix = 2
            base = _safe_path_component(title, fallback="Unknown Title")
            while True:
                cand = target_dir / f"{base} ({suffix}).m4b"
                if not cand.exists():
                    target_file = cand
                    break
                suffix += 1

        if m4b.resolve() != target_file.resolve():
            m4b.rename(target_file)
            m4b = target_file
            moved += 1

        src_cover = _find_cover_image(src_parent)
        if src_cover is not None:
            dest_cover = target_dir / "cover.jpg"
            if src_cover.resolve() != dest_cover.resolve() and not dest_cover.exists():
                shutil.copy2(src_cover, dest_cover)

        cover = _find_cover_image(target_dir)
        cover_name = cover.name if cover else "cover.jpg"

        nfo_path = target_dir / "book.nfo"
        nfo_path.write_text(
            _render_book_nfo(title=title, cover_name=cover_name, meta=meta),
            encoding="utf-8",
        )
        created += 1

    print(f"Audiobook workflow: library root is {library_root}")
    print(f"Audiobook workflow: generated {created} book.nfo file(s).")
    if renamed:
        print(f"Audiobook workflow: renamed {renamed} file name(s).")
    if moved:
        print(f"Audiobook workflow: moved {moved} audiobook file(s) into Jellyfin structure.")

    if run_tagging:
        script = (tagbooks_script or "").strip()
        if not script:
            candidate = root / "tagbooks.sh"
            if candidate.exists():
                script = str(candidate)
            else:
                candidate = Path.cwd() / "tagbooks.sh"
                if candidate.exists():
                    script = str(candidate)
        if script:
            print(f"Running tagbook metadata pass: {script}")
            cp = subprocess.run(["bash", str(Path(script).expanduser())], cwd=str(library_root), check=False)
            if cp.returncode != 0:
                raise RuntimeError(f"tagbooks script failed with exit code {cp.returncode}")
        else:
            print("Skipping tag pass: tagbooks.sh not found. Use --tagbooks-script to specify it.")

    print("Audiobook workflow complete.")
    return 0

def cleanup_mkvs(home: Path, *, dry_run: bool, movies_dir: str, series_dir: str) -> int:
    """Conservatively remove *work directories* created by this script.

    Work dirs are created as direct children of $HOME named "<Title> (<Year>)".

    Safety rails:
    - Only directories that contain a MKVs/ subfolder are even considered.
    - Candidates must either include a marker file created by this script, OR
      appear to be legacy script output (Extras/extras.nfo or __series_stage).
    - Candidates must be exactly one path segment under $HOME.

    IMPORTANT: this deletes the entire work directory (including MKVs/ and any
    staging/log artifacts) but it does not delete anything in Jellyfin final
    storage directories.
    """

    # Best-effort guard to avoid deleting user-configured final storage
    # directories if they happen to live under $HOME.
    exclude_roots: list[Path] = []
    for raw in (movies_dir, series_dir):
        if not raw:
            continue
        if is_remote_dest(raw):
            continue
        try:
            exclude_roots.append(Path(raw).resolve())
        except Exception:
            pass

    candidates: list[tuple[Path, bool, int]] = []
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

        marker_paths = [work_dir / name for name in WORKDIR_MARKER_NAMES]
        has_marker = any(p.exists() for p in marker_paths)
        legacy_hint = (work_dir / "Extras" / "extras.nfo").exists() or (work_dir / "__series_stage").exists()
        if not has_marker and not legacy_hint:
            continue

        if not is_safe_work_dir(home, work_dir):
            continue

        # Never delete anything that is (or sits under) the configured local
        # Movies/Series directories.
        try:
            resolved = work_dir.resolve()
            if any(str(resolved) == str(root) or str(resolved).startswith(str(root) + os.sep) for root in exclude_roots):
                continue
        except Exception:
            continue

        # Compute size for reporting (best-effort).
        size_b = _dir_size_bytes(work_dir)
        candidates.append((work_dir, has_marker, size_b))

    if not candidates:
        print("No managed MKV folders found to clean.")
        print("Hint: only work directories created by this script are eligible for cleanup.")
        return 0

    print("Work directory cleanup candidates:")
    total_bytes = 0
    for work_dir, has_marker, size_b in sorted(candidates, key=lambda t: str(t[0])):
        total_bytes += size_b
        tag = "managed" if has_marker else "legacy"
        print(f"  - {work_dir} ({_human_bytes(size_b)}) [{tag}]")

    print(f"Total candidates: {len(candidates)}")
    print(f"Total size: {_human_bytes(total_bytes)}")

    if dry_run:
        print("Dry run: nothing deleted.")
        return 0

    deleted = 0
    failures = 0
    for work_dir, _has_marker, _size_b in candidates:
        try:
            shutil.rmtree(work_dir)
            deleted += 1
        except Exception as e:
            failures += 1
            print(f"ERROR: failed to remove {work_dir}: {e}", file=sys.stderr)

    print(f"Deleted work directories: {deleted}")
    if failures:
        print(f"Failures: {failures}", file=sys.stderr)
        return 1
    return 0


# ----------------------------
# Main
# ----------------------------


def main(argv: list[str]) -> int:
    ns = parse_args(argv)

    if ns.tmdb_suggest_from_disc:
        try:
            payload = tmdb_suggest_from_disc(
                api_key=ns.tmdb_api_key,
                disc_device=ns.disc_device,
                media_type=ns.tmdb_disc_media_type,
                limit=ns.tmdb_limit,
            )
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    if ns.tmdb_search:
        try:
            matches = tmdb_search(
                api_key=ns.tmdb_api_key,
                query=ns.tmdb_search,
                year=ns.tmdb_year,
                media_type=ns.tmdb_media_type,
                limit=ns.tmdb_limit,
            )
            print(json.dumps({"query": ns.tmdb_search, "results": matches}, ensure_ascii=False))
            return 0
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    if ns.cleanup_mkvs:
        home = Path.home()
        return cleanup_mkvs(home, dry_run=bool(ns.dry_run), movies_dir=ns.movies_dir, series_dir=ns.series_dir)

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

    if ns.audiobook_workflow:
        try:
            return run_audiobook_workflow(
                books_dir=ns.books_dir,
                metadata_json=ns.audible_library_json,
                run_tagging=(not ns.skip_tagbooks),
                tagbooks_script=ns.tagbooks_script,
                audible_sync=bool(ns.audible_sync),
                audible_download_cmd=ns.audible_download_cmd,
                audible_username=ns.audible_username,
                audible_password=ns.audible_password,
                audible_locale=ns.audible_locale,
            )
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    if getattr(ns, "disc_type", "dvd") == "cd" and ns.csv_file:
        print("--csv is not used in CD mode. Start CD jobs from manual mode.", file=sys.stderr)
        return 2

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
        cmd = ["screen", "-S", "rip_and_encode", sys.executable, str(Path(__file__).resolve()), *argv]
        os.execvpe(cmd[0], cmd, env)

    log_dir = _ensure_log_dir(home_base)
    log_file = log_dir / f"rip_and_encode_{time.strftime('%Y%m%d_%H%M%S')}.log"
    try:
        rotate_logs(log_dir, keep=30, compress=True, exclude=log_file)
    except Exception:
        pass
    log_fh = log_file.open("a", encoding="utf-8")

    sys.stdout = Tee(sys.__stdout__, log_fh)  # type: ignore[assignment]
    sys.stderr = Tee(sys.__stderr__, log_fh)  # type: ignore[assignment]

    print(f"Log file: {log_file}")

    if getattr(ns, "disc_type", "dvd") == "cd":
        try:
            return run_cd_rip_with_abcde(
                music_dir=ns.music_dir,
                artist_hint=ns.cd_artist,
                album_hint=ns.cd_album,
                year_hint=ns.cd_year,
            )
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    if ns.ensure_jellyfin:
        try:
            ensure_jellyfin_installed()
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            print("Hint: re-run as root (sudo) or configure passwordless sudo for apt/systemctl.", file=sys.stderr)
            return 2

    failsafe = FailSafe()

    makemkv_cache_mb = 1024 if (getattr(ns, "disc_type", "dvd") == "bluray") else 512

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
        for cmd in ["awk", "eject", "ffprobe", "ffmpeg", "find", "grep", "HandBrakeCLI", "makemkvcon", "sed", "sort", "stdbuf", "tr", "wc", "tee", "date", "id"]:
            which_required(cmd)
        if is_remote_dest(ns.movies_dir) or is_remote_dest(ns.series_dir):
            which_required("ssh")
            which_required("scp")
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 127

    # If MakeMKV is installed via Snap, try to ensure the key interfaces are connected
    # (best-effort; does not fail the run).
    try:
        maybe_ensure_makemkv_snap_interfaces()
    except Exception:
        pass

    # Fallback tools are optional per stage but we log availability up front so users
    # understand which automatic recovery paths are active for this run.
    log_fallback_dependency_status()

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

    def submit_encode(input_: Path, output: Path, preset: str, subtitle_mode: str) -> None:
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
            hb_encode(input_, output, preset, subtitle_mode=subtitle_mode)
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
                ["HandBrakeCLI", "-i", str(input_), "-o", str(output), "--preset", preset, *handbrake_subtitle_args(subtitle_mode)],
                stdin=subprocess.DEVNULL,
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
    tmdb_runtime_cache: dict[tuple[str, str], Optional[int]] = {}

    def _movie_disc_validator_for_ctx(ctx: TitleContext, disc_index: int):
        if ctx.is_series or disc_index != 1:
            return None

        cache_key = (ctx.title_raw.strip().lower(), ctx.year.strip())

        def _validator(mkvs: list[Path]) -> tuple[bool, str]:
            runtime_min: Optional[int] = None
            if cache_key in tmdb_runtime_cache:
                runtime_min = tmdb_runtime_cache[cache_key]
            else:
                runtime_min = tmdb_movie_runtime_minutes(
                    api_key=getattr(ns, "tmdb_api_key", ""),
                    title=ctx.title_raw,
                    year=ctx.year,
                )
                tmdb_runtime_cache[cache_key] = runtime_min
                if runtime_min:
                    print(f"TMDB runtime check: expected runtime for '{ctx.title_raw}' is {runtime_min} min.")
                else:
                    print(f"TMDB runtime check: no runtime found for '{ctx.title_raw}'; using local heuristic only.")

            return movie_disc_has_main_feature(mkvs=mkvs, tmdb_runtime_min=runtime_min)

        return _validator

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

            csv_next_confirmed = bool(ns.no_disc_prompts)
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
                    output_ext=ns.output_container,
                )
                batch_add_once(ctx)

                disc_dir = ctx.mkv_root / f"Disc{row.disc:02d}"
                prompt_msg = csv_disc_prompt_for_row(row)

                # CSV restart support: if the work directory was cleaned but output files
                # already exist, skip re-ripping movie discs to avoid starting over.
                # (Series discs are not safely attributable without the per-disc manifest.)
                if (
                    row.kind == "movie"
                    and not find_mkvs_in_dir(disc_dir)
                    and _movie_disc_outputs_exist(ctx, row.disc, ns.output_container)
                    and not csv_next_confirmed
                ):
                    print(
                        f"Resume: outputs already exist; skipping rip/encode: {ctx.title} ({ctx.year}) disc {row.disc}"
                    )
                else:
                    disc_failed = False
                    try:
                        mkvs = rip_disc_if_needed(
                            disc_dir,
                            prompt_msg,
                            wait_for_enter=not csv_next_confirmed,
                            makemkv_cache_mb=makemkv_cache_mb,
                            validate_rip=_movie_disc_validator_for_ctx(ctx, row.disc),
                        )
                    except Exception as e:
                        print(
                            "ERROR: disc processing failed after fallback attempts; "
                            f"continuing queue for {ctx.title} ({ctx.year}) disc {row.disc}: {e}"
                        )
                        disc_failed = True
                    csv_next_confirmed = False

                    if (not disc_failed) and ctx.is_series:
                        process_series_disc(
                            ctx=ctx,
                            disc_dir=disc_dir,
                            overlap=ns.overlap,
                            preset=ns.preset,
                            output_ext=ns.output_container,
                            subtitle_mode=ns.subtitle_mode,
                            submit_encode=submit_encode,
                        )
                    elif not disc_failed:
                        process_movie_disc(
                            ctx=ctx,
                            disc_index=row.disc,
                            disc_dir=disc_dir,
                            overlap=ns.overlap,
                            preset=ns.preset,
                            output_ext=ns.output_container,
                            subtitle_mode=ns.subtitle_mode,
                            submit_encode=submit_encode,
                        )

                if idx + 1 < len(schedule):
                    # Before prompting for the next disc, ensure we have enough free space
                    # on the filesystem(s) where we are storing MKVs and writing encoded outputs.
                    pause_if_low_disk_space(
                        paths=_disk_targets_for_run(
                            home_base=home_base,
                            movies_dir=ns.movies_dir,
                            series_dir=ns.series_dir,
                        ),
                        min_free_gb=20,
                    )
                    if not ns.no_disc_prompts:
                        csv_next_up_note(schedule[idx + 1])
                        print("When the next disc is inserted, press Enter to start ripping... ")
                        input()
                        csv_next_confirmed = True
                    else:
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
                output_ext=ns.output_container,
            )

            while True:
                disc_index = 1
                while True:
                    disc_dir = ctx.mkv_root / f"Disc{disc_index:02d}"
                    rip_disc_if_needed(
                        disc_dir,
                        "Insert disc now (then press Enter)",
                        wait_for_enter=True,
                        makemkv_cache_mb=makemkv_cache_mb,
                        validate_rip=_movie_disc_validator_for_ctx(ctx, disc_index),
                    )

                    if ctx.is_series:
                        process_series_disc(ctx=ctx, disc_dir=disc_dir, overlap=ns.overlap, preset=ns.preset, output_ext=ns.output_container, subtitle_mode=ns.subtitle_mode, submit_encode=submit_encode)
                        nxt = input("Insert next disc for this season? (y/n): ").strip().lower() or "n"
                        if nxt != "y":
                            break
                        disc_index += 1
                        continue

                    process_movie_disc(ctx=ctx, disc_index=disc_index, disc_dir=disc_dir, overlap=ns.overlap, preset=ns.preset, output_ext=ns.output_container, subtitle_mode=ns.subtitle_mode, submit_encode=submit_encode)
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
                    output_ext=ns.output_container,
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
