#!/usr/bin/env python3
"""rip_and_encode_v2_gui.py

Tkinter GUI for rip_and_encode_v2.py (Option B: full workflow).

Goal:
- Run on Windows/macOS/Linux.
- Hide terminal usage from the operator.
- Connect to the rip host over SSH, upload a generated CSV schedule (or use a selected CSV),
  run rip_and_encode_v2.py in --csv mode, and drive the "press Enter to continue" prompts.

Requirements implemented:
- Collapsible log window (collapsed by default).
- Progress bar + step label driven by parsing the log output.

Assumptions:
- The ripping/transcoding tools (makemkvcon, HandBrakeCLI, ffprobe, eject) live on the remote host.
- The remote host has rip_and_encode_v2.py available at the provided remote path.
- The client has OpenSSH `ssh` and `scp` available (Windows 10+ usually does).

This GUI does not implement interactive (non-CSV) mode; instead it always drives rip_and_encode_v2.py
using --csv for determinism.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import pickle
import queue
import re
import stat
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import keyring  # type: ignore

    KEYRING_AVAILABLE = True
except Exception:
    KEYRING_AVAILABLE = False

try:
    import paramiko  # type: ignore

    PARAMIKO_AVAILABLE = True
except Exception:
    PARAMIKO_AVAILABLE = False

try:
    from tkinter import BOTH, END, LEFT, RIGHT, X, BooleanVar, IntVar, StringVar, Tk, filedialog, messagebox
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    TK_AVAILABLE = True
except ModuleNotFoundError:
    TK_AVAILABLE = False


class Tooltip:
    """Simple hover tooltip for Tk/ttk widgets (no external dependencies)."""

    def __init__(self, widget, text: str, *, delay_ms: int = 650) -> None:
        self.widget = widget
        self.text = (text or "").strip()
        self.delay_ms = int(delay_ms)
        self._after_id = None
        self._tip = None

        if not self.text:
            return

        try:
            widget.bind("<Enter>", self._on_enter, add=True)
            widget.bind("<Leave>", self._on_leave, add=True)
            widget.bind("<ButtonPress>", self._on_leave, add=True)
        except Exception:
            pass

    def _on_enter(self, _event=None) -> None:
        if not self.text:
            return
        try:
            if self._after_id is None:
                self._after_id = self.widget.after(self.delay_ms, self._show)
        except Exception:
            pass

    def _on_leave(self, _event=None) -> None:
        try:
            if self._after_id is not None:
                try:
                    self.widget.after_cancel(self._after_id)
                except Exception:
                    pass
                self._after_id = None
        finally:
            self._hide()

    def _show(self) -> None:
        self._after_id = None
        if self._tip is not None:
            return

        try:
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except Exception:
            return

        try:
            win = __import__("tkinter").Toplevel(self.widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{x}+{y}")
            lbl = ttk.Label(win, text=self.text, padding=(8, 5))
            lbl.pack()
            self._tip = win
        except Exception:
            self._tip = None

    def _hide(self) -> None:
        if self._tip is None:
            return
        try:
            self._tip.destroy()
        except Exception:
            pass
        self._tip = None


@dataclass
class UiState:
    running: bool = False
    waiting_for_enter: bool = False
    total_titles: int = 0
    finalized_titles: int = 0
    makemkv_phase: str = ""  # "analyze" | "process" | ""
    last_makemkv_total_pct: float = 0.0
    encode_queued: int = 0
    encode_started: int = 0
    encode_finished: int = 0
    encode_active_label: str = ""

    eta_phase: str = ""  # e.g., "makemkv" | "handbrake" | ""
    eta_last_pct: float = 0.0
    eta_last_ts: float = 0.0
    eta_rate_ewma: float = 0.0  # pct per second

    run_started_ts: float = 0.0


MAKE_MKV_PROGRESS_RE = re.compile(r"MakeMKV progress:\s*([0-9]+\.[0-9]+)%")
MAKEMKV_CURRENT_PROGRESS_RE = re.compile(r"Current progress\s*-\s*([0-9]{1,3})%")
MAKEMKV_TOTAL_PROGRESS_RE = re.compile(r"Total progress\s*-\s*([0-9]{1,3})%")
MAKEMKV_OPERATION_RE = re.compile(r"^Current operation:\s*(.+)$")
MAKEMKV_ACTION_RE = re.compile(r"^Current action:\s*(.+)$")

HB_PROGRESS_RE = re.compile(r"Encoding:.*?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%")
HB_TASK_RE = re.compile(r"^\[[0-9]{2}:[0-9]{2}:[0-9]{2}\]\s*Starting Task:\s*(.+)$")

HB_START_RE = re.compile(r"^HandBrake start:\s*(\d+)\s*/\s*(\d+):\s*(.+)$")
HB_DONE_RE = re.compile(r"^HandBrake done:\s*(\d+)\s*/\s*(\d+):\s*(.+)$")

PROMPT_INSERT_RE = re.compile(r"Insert: ")
PROMPT_NEXT_DISC_RE = re.compile(r"When the next disc is inserted, press Enter to start ripping\.\.\.")
FINALIZING_RE = re.compile(r"^Finalizing: ")
CSV_LOADED_RE = re.compile(r"^CSV schedule loaded:\s*(\d+)\s*discs")
ERROR_RE = re.compile(r"^ERROR:")
MAKEMKV_ACCESS_ERROR_RE = re.compile(r"Failed to get full access to drive")


def _write_csv_rows(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _csv_rows_from_manual(kind: str, name: str, year: str, season: str, start_disc: int, total_discs: int) -> list[str]:
    if "," in name:
        raise ValueError("Title must not contain commas (CSV constraint).")

    rows: list[str] = []
    if kind == "movie":
        md = "y" if total_discs > 1 else "n"
        for d in range(start_disc, total_discs + 1):
            rows.append(f"{name}, {year}, {md}, {d}")
    else:
        if not season.strip().isdigit() or int(season.strip()) < 1:
            raise ValueError("Season must be an integer >= 1.")
        s = str(int(season.strip())).zfill(2)
        for d in range(start_disc, total_discs + 1):
            rows.append(f"{name}, {year}, {s}, {d}")
    return rows


def _ssh_target(user: str, host: str) -> str:
    if not host.strip():
        return ""
    if "@" in host:
        return host.strip()
    if user.strip():
        return f"{user.strip()}@{host.strip()}"
    return host.strip()


def _build_ssh_base_args(target: str, port: str, keyfile: str) -> list[str]:
    args = ["ssh", "-tt"]
    if port.strip():
        args += ["-p", port.strip()]
    if keyfile.strip():
        args += ["-i", keyfile.strip()]
    args.append(target)
    return args


def _build_scp_base_args(port: str, keyfile: str) -> list[str]:
    args = ["scp"]
    if port.strip():
        args += ["-P", port.strip()]
    if keyfile.strip():
        args += ["-i", keyfile.strip()]
    return args


def _normalize_remote_script_path(remote_script: str) -> str:
    s = (remote_script or "").strip()
    if not s:
        return "rip_and_encode_v2.py"
    if "/" in s or s.startswith("~"):
        return s
    return f"~/{s}"


REMOTE_SCRIPT_RUN_PATH = "~/.archive_helper_for_jellyfin/rip_and_encode_v2.py"


if TK_AVAILABLE:

    class RipGui:
        def __init__(self, root: Tk) -> None:
            self.root = root
            self.root.title("Archive Helper for Jellyfin")
            self.state = UiState()

            self._main_thread_ident = threading.get_ident()
            self._hostkey_logged: set[str] = set()
            self._hostkey_lock = threading.Lock()

            self._replay_stop = threading.Event()
            self._replay_mode = False

            self.proc: subprocess.Popen[str] | None = None
            self.ssh_client = None
            self.ssh_channel = None
            self.reader_thread: threading.Thread | None = None
            self.ui_queue: queue.Queue[tuple[str, str]] = queue.Queue()

            # Auto-reconnect runtime state (tail remote log; send input via screen).
            self.tail_proc: subprocess.Popen[str] | None = None
            self.tail_client = None
            self.tail_channel = None

            self.run_target: str = ""
            self.run_port: str = ""
            self.run_keyfile: str = ""
            self.run_password: str = ""
            self.run_screen_name: str = ""
            self.run_log_path: str = ""

            self._stop_requested = threading.Event()
            self._done_emitted = False

            # Connection
            self.var_host = StringVar(value="")
            self.var_user = StringVar(value="")
            self.var_port = StringVar(value="")
            self.var_key = StringVar(value="")
            self.var_password = StringVar(value="")

            # Script settings
            self.var_movies_dir = StringVar(value="/storage/Movies")
            self.var_series_dir = StringVar(value="/storage/Series")
            self.var_preset = StringVar(value="HQ 1080p30 Surround")
            self.var_ensure_jellyfin = BooleanVar(value=False)

            self._presets_loading = False
            self._presets_loaded = False

            # Mode
            self.var_mode = StringVar(value="manual")
            self.var_csv_path = StringVar(value="")

            self.var_kind = StringVar(value="movie")
            self.var_title = StringVar(value="")
            self.var_year = StringVar(value="")
            self.var_season = StringVar(value="1")
            self.var_start_disc = IntVar(value=1)
            self.var_disc_count = IntVar(value=1)

            # Status / progress
            self.var_step = StringVar(value="Idle")
            self.var_prompt = StringVar(value="")
            self.var_eta = StringVar(value="")
            self.var_elapsed = StringVar(value="")

            self._load_persisted_state()

            self._build_ui()
            self.var_kind.trace_add("write", lambda *_: self._refresh_kind())
            self._refresh_kind()
            self._poll_ui_queue()

            # Periodic clock update (elapsed time label).
            self._tick_elapsed()

            # Keyboard shortcut: when a disc prompt appears, Enter triggers Continue.
            try:
                self.root.bind("<Return>", self._on_return_key)
                self.root.bind("<KP_Enter>", self._on_return_key)
            except Exception:
                pass

            self.root.protocol("WM_DELETE_WINDOW", self._on_close)

            # Try to populate HandBrake presets once connection details are provided.
            for v in (self.var_host, self.var_user, self.var_port, self.var_key, self.var_password):
                v.trace_add("write", lambda *_: self._maybe_load_presets_async())
            self._maybe_load_presets_async()

        def _build_ui(self) -> None:
            main = ttk.Frame(self.root, padding=10)
            main.pack(fill=BOTH, expand=True)
            self.main_frame = main

            header = ttk.Frame(main)
            header.pack(fill=X, pady=(0, 10))
            self._build_logo(header)

            # Connection frame
            conn = ttk.LabelFrame(main, text="Connection (SSH)", padding=10)
            conn.pack(fill=X)

            row = ttk.Frame(conn)
            row.pack(fill=X)
            ttk.Label(row, text="Host:").pack(side=LEFT)
            ent_host = ttk.Entry(row, textvariable=self.var_host, width=28)
            ent_host.pack(side=LEFT, padx=5)
            Tooltip(ent_host, "SSH host or IP address of the server.")
            ttk.Label(row, text="User:").pack(side=LEFT)
            ent_user = ttk.Entry(row, textvariable=self.var_user, width=16)
            ent_user.pack(side=LEFT, padx=5)
            Tooltip(ent_user, "SSH username on the server (example: jellyfin).")
            ttk.Label(row, text="Port:").pack(side=LEFT)
            ent_port = ttk.Entry(row, textvariable=self.var_port, width=6)
            ent_port.pack(side=LEFT, padx=5)
            Tooltip(ent_port, "SSH port (leave blank for default 22).")

            row2 = ttk.Frame(conn)
            row2.pack(fill=X, pady=(6, 0))
            ttk.Label(row2, text="Key file (optional):").pack(side=LEFT)
            ent_key = ttk.Entry(row2, textvariable=self.var_key, width=40)
            ent_key.pack(side=LEFT, padx=5)
            Tooltip(ent_key, "Optional: path to an SSH private key. If empty, password auth is used.")
            btn_key = ttk.Button(row2, text="Browse", command=self._browse_key)
            btn_key.pack(side=LEFT)
            Tooltip(btn_key, "Pick an SSH private key file.")

            row2b = ttk.Frame(conn)
            row2b.pack(fill=X, pady=(6, 0))
            ttk.Label(row2b, text="Password (required if no key):").pack(side=LEFT)
            ent_pw = ttk.Entry(row2b, textvariable=self.var_password, width=40, show="*")
            ent_pw.pack(side=LEFT, padx=5)
            Tooltip(ent_pw, "SSH password (required if you are not using a key file).")

            row3 = ttk.Frame(conn)
            row3.pack(fill=X, pady=(6, 0))
            # Intentionally blank: details are shown in Help to keep the UI clean.

            # Settings frame
            settings = ttk.LabelFrame(main, text="Run settings", padding=10)
            settings.pack(fill=X, pady=(10, 0))

            s0 = ttk.Frame(settings)
            s0.pack(fill=X)
            self.chk_ensure_jellyfin = ttk.Checkbutton(
                s0,
                text="Install Jellyfin if missing",
                variable=self.var_ensure_jellyfin,
            )
            self.chk_ensure_jellyfin.pack(side=LEFT)
            Tooltip(
                self.chk_ensure_jellyfin,
                "If Jellyfin is not installed on the server, try to install it (Debian/Ubuntu; requires sudo).",
            )

            s1 = ttk.Frame(settings)
            s1.pack(fill=X)
            ttk.Label(s1, text="Movies dir:").pack(side=LEFT)
            ent_movies = ttk.Entry(s1, textvariable=self.var_movies_dir, width=30)
            ent_movies.pack(side=LEFT, padx=5)
            Tooltip(ent_movies, "Output folder on the server for movies (example: /storage/Movies).")
            ttk.Label(s1, text="Series dir:").pack(side=LEFT)
            ent_series = ttk.Entry(s1, textvariable=self.var_series_dir, width=30)
            ent_series.pack(side=LEFT, padx=5)
            Tooltip(ent_series, "Output folder on the server for series (example: /storage/Series).")

            s2 = ttk.Frame(settings)
            s2.pack(fill=X, pady=(6, 0))
            ttk.Label(s2, text="HandBrake preset:").pack(side=LEFT)
            self.cbo_preset = ttk.Combobox(s2, textvariable=self.var_preset, width=33, state="normal")
            self.cbo_preset.pack(side=LEFT, padx=5)
            Tooltip(self.cbo_preset, "HandBrake preset name on the server (loaded from HandBrakeCLI --preset-list).")

            # Mode frame
            mode = ttk.LabelFrame(main, text="Schedule", padding=10)
            mode.pack(fill=X, pady=(10, 0))

            mtop = ttk.Frame(mode)
            mtop.pack(fill=X)
            ttk.Radiobutton(mtop, text="Manual", variable=self.var_mode, value="manual", command=self._refresh_mode).pack(side=LEFT)
            ttk.Radiobutton(mtop, text="CSV file", variable=self.var_mode, value="csv", command=self._refresh_mode).pack(side=LEFT, padx=10)

            self.manual_frame = ttk.Frame(mode)
            self.manual_frame.pack(fill=X, pady=(6, 0))

            r1 = ttk.Frame(self.manual_frame)
            r1.pack(fill=X)
            ttk.Label(r1, text="Type:").pack(side=LEFT)
            cbo_kind = ttk.Combobox(r1, textvariable=self.var_kind, values=["movie", "series"], state="readonly", width=8)
            cbo_kind.pack(side=LEFT, padx=5)
            Tooltip(cbo_kind, "Choose whether this schedule is a movie or a series.")
            ttk.Label(r1, text="Title:").pack(side=LEFT)
            ent_title = ttk.Entry(r1, textvariable=self.var_title, width=30)
            ent_title.pack(side=LEFT, padx=5)
            Tooltip(ent_title, "Title used for folder/filename naming (commas are not allowed in CSV schedules).")
            ttk.Label(r1, text="Year:").pack(side=LEFT)
            ent_year = ttk.Entry(r1, textvariable=self.var_year, width=6)
            ent_year.pack(side=LEFT, padx=5)
            Tooltip(ent_year, "4-digit release year (example: 2008).")

            self.season_row = ttk.Frame(self.manual_frame)
            self.season_row.pack(fill=X, pady=(6, 0))
            ttk.Label(self.season_row, text="Season (series):").pack(side=LEFT)
            ent_season = ttk.Entry(self.season_row, textvariable=self.var_season, width=6)
            ent_season.pack(side=LEFT, padx=5)
            Tooltip(ent_season, "Season number (used when Type is series).")

            r2 = ttk.Frame(self.manual_frame)
            r2.pack(fill=X, pady=(6, 0))
            ttk.Label(r2, text="Current disc in drive:").pack(side=LEFT)
            sp_start = ttk.Spinbox(r2, from_=1, to=20, textvariable=self.var_start_disc, width=4)
            sp_start.pack(side=LEFT, padx=5)
            Tooltip(sp_start, "Disc number currently in the drive (for resume).")
            ttk.Label(r2, text="Total discs:").pack(side=LEFT, padx=(14, 0))
            sp_total = ttk.Spinbox(r2, from_=1, to=20, textvariable=self.var_disc_count, width=4)
            sp_total.pack(side=LEFT, padx=5)
            Tooltip(sp_total, "Total number of discs for this job.")

            self.csv_frame = ttk.Frame(mode)

            c1 = ttk.Frame(self.csv_frame)
            c1.pack(fill=X, pady=(6, 0))
            ttk.Label(c1, text="CSV file:").pack(side=LEFT)
            ent_csv = ttk.Entry(c1, textvariable=self.var_csv_path, width=50)
            ent_csv.pack(side=LEFT, padx=5)
            Tooltip(ent_csv, "Local CSV schedule file to upload to the server (4 columns; no embedded commas).")
            btn_csv = ttk.Button(c1, text="Browse", command=self._browse_csv)
            btn_csv.pack(side=LEFT)
            Tooltip(btn_csv, "Pick a CSV schedule file.")

            self._refresh_mode()
            self._refresh_kind()

            # Status/progress
            status = ttk.LabelFrame(main, text="Status", padding=10)
            status.pack(fill=X, pady=(10, 0))

            status_top = ttk.Frame(status)
            status_top.pack(fill=X)
            ttk.Label(status_top, textvariable=self.var_step).pack(side=LEFT)
            ttk.Label(status_top, textvariable=self.var_eta).pack(side=RIGHT)
            ttk.Label(status_top, textvariable=self.var_elapsed).pack(side=RIGHT, padx=(0, 12))

            self.progress = ttk.Progressbar(status, orient="horizontal", mode="determinate", maximum=100)
            self.progress.pack(fill=X, pady=(6, 0))

            ttk.Label(status, textvariable=self.var_prompt, foreground="#444").pack(anchor="w", pady=(6, 0))

            # Controls
            controls = ttk.Frame(main)
            controls.pack(fill=X, pady=(10, 0))

            self.btn_start = ttk.Button(controls, text="Start", command=self.start)
            self.btn_start.pack(side=LEFT)
            Tooltip(self.btn_start, "Start the job on the server (runs inside a screen session).")

            self.btn_continue = ttk.Button(controls, text="Continue", command=self.send_enter, state="disabled")
            self.btn_continue.pack(side=LEFT, padx=8)
            Tooltip(self.btn_continue, "Send Enter to the server when prompted to insert the next disc.")

            self.btn_stop = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
            self.btn_stop.pack(side=LEFT)
            Tooltip(self.btn_stop, "Stop the current run and return the UI to idle.")

            self.btn_cleanup = ttk.Button(controls, text="Cleanup", command=self.cleanup_mkvs)
            self.btn_cleanup.pack(side=LEFT, padx=8)
            Tooltip(self.btn_cleanup, "Optionally delete leftover MKVs on the server (safe and confirmed).")

            self.btn_toggle_log = ttk.Button(controls, text="Show Log", command=self.toggle_log)
            self.btn_toggle_log.pack(side=RIGHT)

            self.btn_help = ttk.Button(controls, text="Help", command=self.show_help)
            self.btn_help.pack(side=RIGHT, padx=(0, 8))

            # Collapsible log (collapsed by default)
            self.log_visible = False
            self.log_frame = ttk.Frame(main)
            # not packed yet

            self.log_text = ScrolledText(self.log_frame, height=18)
            self.log_text.pack(fill=BOTH, expand=True)
            self.log_text.configure(state="disabled")

            # Default collapsed
            self._set_log_visible(False)

        def show_help(self) -> None:
            # Use a scrollable window (smaller font) instead of a tall messagebox.
            win = __import__("tkinter").Toplevel(self.root)
            win.title("Help")
            win.transient(self.root)
            try:
                win.grab_set()
            except Exception:
                pass

            # Size: keep smaller than the main window, but reasonable by default.
            try:
                self.root.update_idletasks()
                mw = max(600, int(self.root.winfo_width() * 0.85))
                mh = max(420, int(self.root.winfo_height() * 0.85))
                w = min(760, mw)
                h = min(560, mh)
            except Exception:
                w, h = 720, 520

            win.geometry(f"{w}x{h}")

            container = ttk.Frame(win, padding=10)
            container.pack(fill=BOTH, expand=True)

            ttk.Label(container, text="Archive Helper for Jellyfin", font=("TkDefaultFont", 12, "bold")).pack(
                anchor="w", pady=(0, 6)
            )

            text = __import__("tkinter").Text(
                container,
                wrap="word",
                font=("TkDefaultFont", 9),
                height=1,
                borderwidth=1,
                relief="solid",
            )
            scroll = ttk.Scrollbar(container, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=scroll.set)

            scroll.pack(side=RIGHT, fill="y")
            text.pack(side=LEFT, fill=BOTH, expand=True)

            # Styling tags (keep default colors).
            text.tag_configure("h1", font=("TkDefaultFont", 11, "bold"), spacing1=10, spacing3=6)
            text.tag_configure("h2", font=("TkDefaultFont", 10, "bold"), spacing1=8, spacing3=2)
            text.tag_configure("p", font=("TkDefaultFont", 9), spacing1=0, spacing3=6)
            text.tag_configure("num", font=("TkDefaultFont", 9, "bold"), lmargin1=0, lmargin2=0, spacing1=2, spacing3=2)
            text.tag_configure(
                "bullet",
                font=("TkDefaultFont", 9),
                lmargin1=18,
                lmargin2=36,
                spacing1=1,
                spacing3=1,
            )
            text.tag_configure(
                "subbullet",
                font=("TkDefaultFont", 9),
                lmargin1=36,
                lmargin2=54,
                spacing1=1,
                spacing3=1,
            )
            text.tag_configure("example", font=("TkDefaultFont", 9), lmargin1=36, lmargin2=36, spacing1=0, spacing3=4)
            text.tag_configure("link", foreground="#1a0dab", underline=True)

            def add_line(s: str, tag: str = "p") -> None:
                text.insert("end", s + "\n", tag)

            def add_blank() -> None:
                text.insert("end", "\n")

            def add_bullets(items: list[str], indent: str = "bullet") -> None:
                for it in items:
                    add_line("• " + it, indent)

            def add_link(label: str, url: str, *, indent_tag: str = "p") -> None:
                label_s = (label or "").strip()
                url_s = (url or "").strip()
                if not label_s or not url_s:
                    return

                tag = f"link_{abs(hash(url_s))}"
                text.tag_configure(tag, foreground="#1a0dab", underline=True)

                def _open(_event=None) -> None:
                    try:
                        webbrowser.open(url_s)
                    except Exception:
                        pass

                try:
                    text.tag_bind(tag, "<Button-1>", _open)
                    text.tag_bind(tag, "<Enter>", lambda _e: text.configure(cursor="hand2"))
                    text.tag_bind(tag, "<Leave>", lambda _e: text.configure(cursor=""))
                except Exception:
                    pass

                text.insert("end", label_s + "\n", (indent_tag, tag))

            # Content (structured for readable formatting)
            add_line("Overview: What is this app and why does it exist?", "h1")
            add_blank()
            add_line(
                "Archive Helper for Jellyfin is a helper tool that makes it easier to rip DVDs or Blu-rays on a Linux server and "
                "organize them so Jellyfin can automatically recognize and display them.",
                "p",
            )
            add_blank()
            add_line(
                "Instead of manually logging into a server, running commands, and watching terminal output, this app:",
                "p",
            )
            add_blank()
            add_bullets(
                [
                    "Connects to your rip server for you",
                    "Uploads the scripts it needs",
                    "Starts the ripping and encoding process",
                    "Shows you progress",
                    "Tells you when it's time to change discs",
                ]
            )
            add_blank()
            add_line("Think of it as a remote control for a dedicated ripping machine.", "p")

            add_blank()
            add_line("What this app does (step by step)", "h1")
            add_blank()

            add_line("1. Connects to your rip server using SSH", "num")
            add_bullets(
                [
                    "SSH is a secure way to remotely control another computer",
                    "This app uses it so you don't have to open a terminal yourself",
                ],
                "subbullet",
            )
            add_blank()

            add_line("2. Uploads a schedule", "num")
            add_bullets(
                [
                    "The schedule tells the server what you are ripping (movie or TV series)",
                    "It can be entered manually or loaded from a CSV file",
                ],
                "subbullet",
            )
            add_blank()

            add_line("3. Uploads or updates the rip script", "num")
            add_bullets(
                [
                    "This is the script that runs MakeMKV and HandBrake on the server",
                    "If you already used this app before, it keeps it up to date automatically",
                ],
                "subbullet",
            )
            add_blank()

            add_line("4. Runs the ripping and encoding workflow", "num")
            add_bullets(
                [
                    "MakeMKV copies the disc to the server",
                    "HandBrake converts the video into a Jellyfin-friendly format",
                ],
                "subbullet",
            )
            add_blank()

            add_line("5. Shows progress and disc swap prompts", "num")
            add_bullets(
                [
                    "You'll see what step it's on",
                    "When multiple discs are needed, the app tells you when to insert the next one",
                ],
                "subbullet",
            )

            add_blank()
            add_line("Connection (SSH)", "h1")
            add_blank()
            add_line("This section tells the app how to reach your rip server.", "p")

            add_blank()
            add_line("Host", "h2")
            add_line("The IP address or hostname of the rip server", "p")
            add_line("Examples:", "p")
            add_line("192.168.1.10", "example")
            add_line("media-server.local", "example")
            add_line("This is the machine where the disc drive and ripping software live", "p")

            add_blank()
            add_line("User", "h2")
            add_line("The Linux username on the rip server", "p")
            add_line("This is usually the same name you use when logging into the server directly", "p")

            add_blank()
            add_line("Port", "h2")
            add_line("The SSH port used by the server", "p")
            add_line("Almost always 22", "p")
            add_line("You usually don't need to change this", "p")

            add_blank()
            add_line("Key file (optional)", "h2")
            add_line("An SSH private key file", "p")
            add_line("Allows logging in without typing a password", "p")
            add_line("Recommended for advanced users, but not required", "p")

            add_blank()
            add_line("Password", "h2")
            add_line("Required only if you are not using a key file", "p")
            add_line("This is the password for the Linux user above", "p")
            add_line("The app does not display or store it in plain text", "p")

            add_blank()
            add_line("Run settings", "h1")
            add_blank()
            add_line("These settings control where files go and how they are encoded.", "p")

            add_blank()
            add_line("Install Jellyfin if missing", "h2")
            add_line("If checked, the app will try to:", "p")
            add_bullets(["Install Jellyfin", "Enable and start the Jellyfin service"], "bullet")
            add_blank()
            add_line("This requires the server user to have sudo (administrator) access", "p")
            add_line("Safe to leave unchecked if Jellyfin is already installed", "p")

            add_blank()
            add_line("Movies dir", "h2")
            add_line("Folder on the server where movies will be saved", "p")
            add_line("Example:", "p")
            add_line("/storage/Movies", "example")
            add_line("Jellyfin should already be configured to scan this folder", "p")

            add_blank()
            add_line("Series dir", "h2")
            add_line("Folder on the server where TV series will be saved", "p")
            add_line("Example:", "p")
            add_line("/storage/Series", "example")

            add_blank()
            add_line("HandBrake preset", "h2")
            add_line("The name of the HandBrake preset used for encoding", "p")
            add_line("This must exactly match a preset available on the server", "p")
            add_line("Example:", "p")
            add_line("HQ 1080p30 Surround", "example")
            add_line("You can change this later without reinstalling anything", "p")

            add_blank()
            add_line("Schedule", "h1")
            add_blank()
            add_line("The schedule tells the app what you want to rip.", "p")
            add_line("You can use Manual mode or a CSV file.", "p")

            add_blank()
            add_line("Manual Schedule", "h2")
            add_line("Use this if you are ripping one movie or one season at a time.", "p")

            add_blank()
            add_line("Type", "h2")
            add_line("Choose:", "p")
            add_bullets(["Movie → single film", "Series → TV show"], "bullet")

            add_blank()
            add_line("Title", "h2")
            add_line("The name of the movie or TV series", "p")
            add_line("This is used to name folders and files", "p")
            add_line("Example:", "p")
            add_line("The Matrix", "example")

            add_blank()
            add_line("Year", "h2")
            add_line("Release year of the movie or series", "p")
            add_line("Helps Jellyfin match the correct metadata", "p")
            add_line("Example:", "p")
            add_line("1999", "example")

            add_blank()
            add_line("Season (series only)", "h2")
            add_line("Season number for TV series", "p")
            add_line("Example:", "p")
            add_line("1", "example")

            add_blank()
            add_line("Total discs", "h2")
            add_line("How many discs are part of this job", "p")
            add_line("Examples:", "p")
            add_bullets(["Movie with bonus disc → 2", "TV season with 4 DVDs → 4"], "bullet")
            add_line("The app will prompt you when it's time to insert the next disc", "p")

            add_blank()
            add_line("Current disc in drive", "h2")
            add_line("Which disc is currently in the drive when you press Start", "p")
            add_line("Use this if you are resuming in the middle of a multi-disc set", "p")

            add_blank()
            add_line("CSV Schedule", "h2")
            add_line("Use this if you want to:", "p")
            add_bullets(["Queue multiple movies or seasons", "Run unattended batches", "Reuse schedules later"], "bullet")
            add_blank()
            add_line("You select an existing CSV file, and the app will process each entry in order.", "p")

            add_blank()
            add_line("Buttons", "h1")
            add_blank()
            add_line("Start", "h2")
            add_bullets(
                [
                    "Begins the ripping and encoding process",
                    "Uploads scripts and schedule to the server",
                    "Starts processing the first disc",
                ],
                "bullet",
            )
            add_blank()
            add_line("Continue", "h2")
            add_bullets(["Click this after inserting the next disc", "Used when ripping multiple discs in one job"], "bullet")
            add_blank()
            add_line("Stop", "h2")
            add_bullets(["Cancels the current job", "Safely stops processing on the server"], "bullet")
            add_blank()
            add_line("Show Log", "h2")
            add_bullets(
                [
                    "Displays detailed output from the server",
                    "Useful for troubleshooting or curiosity",
                    "Safe to ignore if everything is working",
                ],
                "bullet",
            )

            add_blank()
            add_line("Additional notes (important)", "h1")
            add_blank()
            add_line("The rip server must have:", "p")
            add_bullets(["Python 3", "MakeMKV", "HandBrakeCLI"], "bullet")
            add_blank()
            add_line("These are not installed automatically unless you explicitly enable it", "p")
            add_blank()
            add_line("All output files are saved on the server, not on this computer", "p")
            add_line("This app remembers your settings between runs", "p")
            add_line("Advanced users can run the rip script directly on the server for full control", "p")

            add_blank()
            add_line("Credits", "h1")
            add_blank()
            add_line("Created by ChatGPT 5.2", "p")
            add_line("Conceptualized and designed by Alex Autrey", "p")
            add_blank()
            add_line("Websites (open-source and core dependencies)", "h2")
            add_link("Jellyfin: https://jellyfin.org/", "https://jellyfin.org/")
            add_link("MakeMKV: https://www.makemkv.com/", "https://www.makemkv.com/")
            add_link("Python: https://www.python.org/", "https://www.python.org/")
            add_link("HandBrake: https://handbrake.fr/", "https://handbrake.fr/")
            add_link("FFmpeg / ffprobe: https://ffmpeg.org/", "https://ffmpeg.org/")
            add_link("GNU Screen: https://www.gnu.org/software/screen/", "https://www.gnu.org/software/screen/")
            add_link("OpenSSH: https://www.openssh.com/", "https://www.openssh.com/")
            add_link("Paramiko (Python SSH library): https://www.paramiko.org/", "https://www.paramiko.org/")
            add_link("keyring (Python): https://pypi.org/project/keyring/", "https://pypi.org/project/keyring/")

            text.configure(state="disabled")

            btns = ttk.Frame(win, padding=(10, 0, 10, 10))
            btns.pack(fill=X)
            ttk.Button(btns, text="Close", command=win.destroy).pack(side=RIGHT)

        def _build_logo(self, parent: ttk.Frame) -> None:
            """Draw a simple, original mark + title using a Canvas.

            This avoids external image dependencies (Pillow) and keeps the GUI
            easily downloadable/runnable.
            """

            c = ttk.Frame(parent)
            c.pack(fill=X)

            canvas = None
            try:
                canvas = __import__("tkinter").Canvas(c, width=360, height=54, highlightthickness=0)
            except Exception:
                canvas = None

            if canvas is not None:
                canvas.pack(side=LEFT)

                # Colors: neutral + cool accent (not Jellyfin-branded artwork).
                bg = "#f3f4f6"  # light neutral
                accent = "#2563eb"  # blue
                dark = "#111827"  # near-black

                canvas.configure(bg=bg)

                # Archive box (stylized document/box) with a lid.
                x0, y0 = 10, 10
                w, h = 34, 30
                canvas.create_rectangle(x0, y0 + 6, x0 + w, y0 + h, outline=dark, width=2, fill=bg)
                canvas.create_line(x0, y0 + 12, x0 + w, y0 + 12, fill=dark, width=2)
                canvas.create_line(x0 + 6, y0 + 6, x0 + 6, y0 + h, fill=dark, width=2)

                # Play triangle (media) inside the box.
                canvas.create_polygon(
                    x0 + 14,
                    y0 + 16,
                    x0 + 14,
                    y0 + 26,
                    x0 + 26,
                    y0 + 21,
                    outline=accent,
                    fill=accent,
                    width=1,
                )

                # Small "helper" sparkle.
                canvas.create_line(x0 + 40, y0 + 10, x0 + 48, y0 + 10, fill=accent, width=2)
                canvas.create_line(x0 + 44, y0 + 6, x0 + 44, y0 + 14, fill=accent, width=2)

                # Text
                canvas.create_text(
                    70,
                    18,
                    text="Archive Helper",
                    anchor="w",
                    fill=dark,
                    font=("TkDefaultFont", 14, "bold"),
                )
                canvas.create_text(
                    70,
                    38,
                    text="for Jellyfin",
                    anchor="w",
                    fill=dark,
                    font=("TkDefaultFont", 10),
                )
            else:
                ttk.Label(parent, text="Archive Helper for Jellyfin", font=("TkDefaultFont", 14, "bold")).pack(
                    anchor="w"
                )

        def _state_dir(self) -> Path:
            # Keep it simple and dependency-free.
            # Linux/macOS: ~/.archive_helper_for_jellyfin
            # Windows: uses the user's home directory as well.
            return Path.home() / ".archive_helper_for_jellyfin"

        def _state_path(self) -> Path:
            return self._state_dir() / "state.pkl"

        def _known_hosts_path(self) -> Path:
            return self._state_dir() / "known_hosts"

        def _ssh_common_opts(self) -> list[str]:
            # Avoid interactive terminal prompts (host key confirmation / password prompts).
            # Password-based auth is handled via Paramiko.
            kh = self._known_hosts_path()
            kh.parent.mkdir(parents=True, exist_ok=True)
            return [
                "-o",
                f"UserKnownHostsFile={str(kh)}",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "BatchMode=yes",
                "-o",
                "NumberOfPasswordPrompts=0",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "LogLevel=ERROR",
            ]

        def _log_threadsafe(self, message: str) -> None:
            if threading.get_ident() == self._main_thread_ident:
                self._append_log(message)
            else:
                self.ui_queue.put(("log", message))

        def _target_host(self, target: str) -> str:
            # target is typically "user@host" or "host".
            host = (target or "").strip()
            if "@" in host:
                host = host.split("@", 1)[1]
            host = host.strip()
            if host.startswith("[") and host.endswith("]"):
                host = host[1:-1]
            return host

        def _maybe_log_host_key_acceptance(self, target: str, port: str) -> None:
            """Log the host key that will be auto-accepted.

            For OpenSSH key-based mode, we best-effort use ssh-keyscan + ssh-keygen to show
            the fingerprint(s) we are about to accept, and we store them in the app-specific
            known_hosts file.
            """

            host = self._target_host(target)
            if not host:
                return
            p = (port or "").strip() or "22"
            key = f"openssh:{host}:{p}"

            with self._hostkey_lock:
                if key in self._hostkey_logged:
                    return
                self._hostkey_logged.add(key)

            if shutil.which("ssh-keyscan") is None or shutil.which("ssh-keygen") is None:
                self._log_threadsafe(
                    f"(Info) Auto-accepting SSH host key for {host}:{p}. "
                    "(ssh-keyscan/ssh-keygen not found; cannot display fingerprint.)\n"
                )
                return

            kh = self._known_hosts_path()
            kh.parent.mkdir(parents=True, exist_ok=True)

            # If we already have an entry, display its fingerprint(s).
            try:
                existing = subprocess.run(
                    ["ssh-keygen", "-F", host, "-f", str(kh)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                if existing.returncode == 0 and (existing.stdout or "").strip():
                    fps = subprocess.run(
                        ["ssh-keygen", "-lf", str(kh), "-F", host],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    out = (fps.stdout or "").strip()
                    if out:
                        self._log_threadsafe(f"(Info) SSH known_hosts entry for {host}:{p}:\n{out}\n")
                    else:
                        self._log_threadsafe(f"(Info) SSH known_hosts entry already present for {host}:{p}.\n")
                    return
            except Exception:
                # Continue to scanning below.
                pass

            # Scan and append (hashed) host keys, then display fingerprints.
            try:
                scan = subprocess.run(
                    ["ssh-keyscan", "-H", "-p", str(p), "-T", "5", host],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                scan_out = (scan.stdout or "").strip()
                if not scan_out:
                    self._log_threadsafe(
                        f"(Info) Auto-accepting SSH host key for {host}:{p}. "
                        "(Unable to scan fingerprint with ssh-keyscan.)\n"
                    )
                    return

                # Store what we're accepting in app-specific known_hosts.
                with kh.open("a", encoding="utf-8") as f:
                    f.write(scan_out + "\n")
                try:
                    os.chmod(kh, 0o600)
                except Exception:
                    pass

                fps = subprocess.run(
                    ["ssh-keygen", "-lf", "-"],
                    input=scan_out + "\n",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                fp_out = (fps.stdout or "").strip()
                if fp_out:
                    self._log_threadsafe(
                        f"(Info) Auto-accepted SSH host key fingerprint(s) for {host}:{p}:\n{fp_out}\n"
                    )
                else:
                    self._log_threadsafe(f"(Info) Auto-accepted SSH host key for {host}:{p}.\n")
            except Exception as e:
                self._log_threadsafe(
                    f"(Info) Auto-accepting SSH host key for {host}:{p}. "
                    f"(Could not display fingerprint: {e})\n"
                )

        def _maybe_log_paramiko_host_key(self, host: str, port: int, client) -> None:
            key = f"paramiko:{host}:{port}"
            with self._hostkey_lock:
                if key in self._hostkey_logged:
                    return
                self._hostkey_logged.add(key)

            try:
                transport = client.get_transport()
                if transport is None:
                    return
                pkey = transport.get_remote_server_key()
                if pkey is None:
                    return

                # Prefer a SHA256 fingerprint similar to OpenSSH output.
                raw = pkey.asbytes()
                sha256 = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
                self._log_threadsafe(
                    f"(Info) Auto-accepted SSH host key for {host}:{port} (Paramiko): "
                    f"{pkey.get_name()} SHA256:{sha256}\n"
                )
            except Exception:
                # Keep it best-effort only.
                pass

        def _ssh_args(self, target: str, port: str, keyfile: str, *, tty: bool = True) -> list[str]:
            self._maybe_log_host_key_acceptance(target, port)
            args = ["ssh"]
            if tty:
                args.append("-tt")
            if port.strip():
                args += ["-p", port.strip()]
            if keyfile.strip():
                args += ["-i", keyfile.strip()]
            args += self._ssh_common_opts()
            args.append(target)
            return args

        def _scp_args(self, target: str, port: str, keyfile: str) -> list[str]:
            self._maybe_log_host_key_acceptance(target, port)
            args = ["scp"]
            if port.strip():
                args += ["-P", port.strip()]
            if keyfile.strip():
                args += ["-i", keyfile.strip()]
            args += self._ssh_common_opts()
            return args

        def _keyring_id(self) -> str:
            host = (self.var_host.get() or "").strip()
            user = (self.var_user.get() or "").strip()
            port = (self.var_port.get() or "").strip() or "22"
            return f"{user}@{host}:{port}" if user else f"{host}:{port}"

        def _load_persisted_state(self) -> None:
            p = self._state_path()
            if p.exists():
                try:
                    data = pickle.loads(p.read_bytes())
                    if isinstance(data, dict):
                        self.var_host.set(str(data.get("host", self.var_host.get())))
                        self.var_user.set(str(data.get("user", self.var_user.get())))
                        self.var_port.set(str(data.get("port", self.var_port.get())))
                        self.var_key.set(str(data.get("key", self.var_key.get())))

                        self.var_movies_dir.set(str(data.get("movies_dir", self.var_movies_dir.get())))
                        self.var_series_dir.set(str(data.get("series_dir", self.var_series_dir.get())))
                        self.var_preset.set(str(data.get("preset", self.var_preset.get())))
                        self.var_ensure_jellyfin.set(bool(data.get("ensure_jellyfin", self.var_ensure_jellyfin.get())))

                        self.var_mode.set(str(data.get("mode", self.var_mode.get())))
                        self.var_csv_path.set(str(data.get("csv_path", self.var_csv_path.get())))
                        self.var_kind.set(str(data.get("kind", self.var_kind.get())))
                        self.var_title.set(str(data.get("title", self.var_title.get())))
                        self.var_year.set(str(data.get("year", self.var_year.get())))
                        self.var_season.set(str(data.get("season", self.var_season.get())))
                        self.var_start_disc.set(int(data.get("start_disc", int(self.var_start_disc.get()))))
                        self.var_disc_count.set(int(data.get("disc_count", int(self.var_disc_count.get()))))
                except Exception:
                    # Ignore corrupt state; user can re-enter values.
                    pass

            # Load password from OS keychain if available.
            if KEYRING_AVAILABLE:
                try:
                    pw = keyring.get_password("ArchiveHelperForJellyfin", self._keyring_id())
                    if pw:
                        self.var_password.set(pw)
                except Exception:
                    pass

        def _persist_state(self) -> None:
            data: dict[str, Any] = {
                "host": self.var_host.get(),
                "user": self.var_user.get(),
                "port": self.var_port.get(),
                "key": self.var_key.get(),
                "movies_dir": self.var_movies_dir.get(),
                "series_dir": self.var_series_dir.get(),
                "preset": self.var_preset.get(),
                "ensure_jellyfin": bool(self.var_ensure_jellyfin.get()),
                "mode": self.var_mode.get(),
                "csv_path": self.var_csv_path.get(),
                "kind": self.var_kind.get(),
                "title": self.var_title.get(),
                "year": self.var_year.get(),
                "season": self.var_season.get(),
                "start_disc": int(self.var_start_disc.get()),
                "disc_count": int(self.var_disc_count.get()),
            }

            sd = self._state_dir()
            sd.mkdir(parents=True, exist_ok=True)
            p = self._state_path()
            p.write_bytes(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))
            try:
                os.chmod(p, 0o600)
            except Exception:
                pass

            # Store password securely in OS keychain when possible.
            if KEYRING_AVAILABLE:
                try:
                    pw = (self.var_password.get() or "").strip()
                    if pw:
                        keyring.set_password("ArchiveHelperForJellyfin", self._keyring_id(), pw)
                    else:
                        keyring.delete_password("ArchiveHelperForJellyfin", self._keyring_id())
                except Exception:
                    pass

        def _on_close(self) -> None:
            # Best-effort stop and persist state.
            try:
                self._persist_state()
            except Exception:
                pass
            try:
                if self.state.running:
                    self.stop()
            except Exception:
                pass
            self.root.destroy()

        def _refresh_mode(self) -> None:
            if self.var_mode.get() == "csv":
                self.manual_frame.pack_forget()
                self.csv_frame.pack(fill=X, pady=(6, 0))
            else:
                self.csv_frame.pack_forget()
                self.manual_frame.pack(fill=X, pady=(6, 0))
            self._refresh_kind()

        def _refresh_kind(self) -> None:
            # Only relevant when manual schedule is selected.
            if not hasattr(self, "season_row"):
                return
            if self.var_mode.get() != "manual":
                return

            if self.var_kind.get() == "series":
                if not self.season_row.winfo_ismapped():
                    self.season_row.pack(fill=X, pady=(6, 0))
            else:
                if self.season_row.winfo_ismapped():
                    self.season_row.pack_forget()

        def _browse_key(self) -> None:
            p = filedialog.askopenfilename(title="Select SSH private key")
            if p:
                self.var_key.set(p)

        def _browse_csv(self) -> None:
            p = filedialog.askopenfilename(title="Select schedule CSV", filetypes=[("CSV", "*.csv"), ("All", "*")])
            if p:
                self.var_csv_path.set(p)

        def toggle_log(self) -> None:
            self._set_log_visible(not self.log_visible)

        def _set_log_visible(self, visible: bool) -> None:
            self.log_visible = visible
            if visible:
                self.log_frame.pack(fill=BOTH, expand=True, pady=(10, 0))
                self.btn_toggle_log.configure(text="Hide Log")
            else:
                self.log_frame.pack_forget()
                self.btn_toggle_log.configure(text="Show Log")

        def _append_log(self, line: str) -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert(END, line)
            self.log_text.see(END)
            self.log_text.configure(state="disabled")

        def _tick_elapsed(self) -> None:
            try:
                if self.state.running and self.state.run_started_ts > 0:
                    elapsed_s = max(0, int(time.time() - self.state.run_started_ts))
                    h = elapsed_s // 3600
                    m = (elapsed_s % 3600) // 60
                    s = elapsed_s % 60
                    self.var_elapsed.set(f"Elapsed: {h:02d}:{m:02d}:{s:02d}")
                else:
                    self.var_elapsed.set("")
            except Exception:
                pass
            self.root.after(500, self._tick_elapsed)

        def _poll_ui_queue(self) -> None:
            try:
                while True:
                    kind, payload = self.ui_queue.get_nowait()
                    if kind == "log":
                        self._append_log(payload)
                        self._parse_for_progress(payload)
                    elif kind == "presets":
                        try:
                            presets = [p for p in payload.split("\n") if p.strip()]
                            self._apply_presets(presets)
                        finally:
                            self._presets_loading = False
                    elif kind == "jellyfin":
                        self._apply_jellyfin_installed(payload.strip() == "1")
                    elif kind == "done":
                        self._on_done(payload)
            except queue.Empty:
                pass
            self.root.after(100, self._poll_ui_queue)

        def _apply_jellyfin_installed(self, installed: bool) -> None:
            try:
                if installed:
                    self.var_ensure_jellyfin.set(False)
                    if hasattr(self, "chk_ensure_jellyfin"):
                        self.chk_ensure_jellyfin.configure(state="disabled")
                else:
                    if hasattr(self, "chk_ensure_jellyfin"):
                        # Only enable if UI isn't currently running.
                        self.chk_ensure_jellyfin.configure(state=("disabled" if self.state.running else "normal"))
            except Exception:
                pass

        def _set_inputs_enabled(self, enabled: bool) -> None:
            """Enable/disable all user-editable fields while a run is active."""

            if not hasattr(self, "main_frame"):
                return

            keep_enabled: set[Any] = {
                getattr(self, "btn_stop", None),
                getattr(self, "btn_continue", None),
                getattr(self, "btn_toggle_log", None),
                getattr(self, "btn_help", None),
                getattr(self, "progress", None),
            }

            def walk(w) -> list[Any]:
                out: list[Any] = []
                try:
                    kids = list(w.winfo_children())
                except Exception:
                    return out
                for c in kids:
                    out.append(c)
                    out.extend(walk(c))
                return out

            for w in walk(self.main_frame):
                if w in keep_enabled:
                    continue
                try:
                    cls = w.winfo_class()
                except Exception:
                    cls = ""

                # Most interactive widgets are Ttk widgets.
                if cls in {
                    "TEntry",
                    "TCombobox",
                    "TSpinbox",
                    "TCheckbutton",
                    "TRadiobutton",
                    "TButton",
                }:
                    try:
                        w.configure(state=("normal" if enabled else "disabled"))
                    except Exception:
                        pass

            # Restore the intended control states.
            try:
                self.btn_start.configure(state=("normal" if enabled else "disabled"))
            except Exception:
                pass
            try:
                self.btn_cleanup.configure(state=("normal" if enabled else "disabled"))
            except Exception:
                pass

            # If Jellyfin is installed, keep the checkbox disabled.
            try:
                if hasattr(self, "chk_ensure_jellyfin") and str(self.chk_ensure_jellyfin["state"]) != "disabled":
                    self.chk_ensure_jellyfin.configure(state=("normal" if enabled else "disabled"))
            except Exception:
                pass

        def _apply_presets(self, presets: list[str]) -> None:
            if not presets:
                return
            if not hasattr(self, "cbo_preset"):
                return

            # Preserve current value; update dropdown choices.
            unique: list[str] = []
            seen: set[str] = set()
            for p in presets:
                p2 = p.strip()
                if not p2:
                    continue
                if p2 in seen:
                    continue
                seen.add(p2)
                unique.append(p2)

            try:
                self.cbo_preset.configure(values=unique)
                self._presets_loaded = True
            except Exception:
                pass

        def _maybe_load_presets_async(self) -> None:
            # Only attempt once (unless user edits connection info before first load completes).
            if self._presets_loaded or self._presets_loading:
                return

            host = (self.var_host.get() or "").strip()
            if not host:
                return

            target = _ssh_target(self.var_user.get(), host)
            if not target:
                return

            port = (self.var_port.get() or "").strip()
            keyfile = (self.var_key.get() or "").strip()
            password = (self.var_password.get() or "").strip()

            # If password auth is needed, we can't proceed without Paramiko.
            if password and not PARAMIKO_AVAILABLE:
                return

            # For key-based auth, OpenSSH must exist.
            if not password and (shutil.which("ssh") is None):
                return

            self._presets_loading = True

            def _work() -> None:
                try:
                    try:
                        # Best-effort remote Jellyfin check; if installed, disable the checkbox.
                        check = (
                            "if command -v jellyfin >/dev/null 2>&1; then echo yes; "
                            "elif command -v dpkg >/dev/null 2>&1 && dpkg -s jellyfin >/dev/null 2>&1; then echo yes; "
                            "else echo no; fi"
                        )
                        codej, outj = self._remote_run(target, port, keyfile, password, check)
                        if codej == 0 and (outj or "").strip().endswith("yes"):
                            self.ui_queue.put(("jellyfin", "1"))
                        elif codej == 0 and (outj or "").strip().endswith("no"):
                            self.ui_queue.put(("jellyfin", "0"))
                    except Exception:
                        pass

                    presets = self._fetch_remote_handbrake_presets(target, port, keyfile, password)
                    if not presets:
                        self.ui_queue.put(
                            (
                                "log",
                                "(Info) HandBrake preset list not available. "
                                "HandBrakeCLI may be missing on the server, or SSH auth failed. "
                                "You can still type a preset name manually.\n",
                            )
                        )
                    self.ui_queue.put(("presets", "\n".join(presets)))
                except Exception as e:
                    # Don't interrupt the user; just log.
                    self.ui_queue.put(("log", f"(Info) Could not load HandBrake presets: {e}\n"))
                    self.ui_queue.put(("presets", ""))

            threading.Thread(target=_work, daemon=True).start()

        def _fetch_remote_handbrake_presets(self, target: str, port: str, keyfile: str, password: str) -> list[str]:
            """Fetch the remote HandBrake preset list.

            Uses OpenSSH for key-based auth and Paramiko for password-based auth.
            Returns a list of preset name candidates.
            """

            cmd = "HandBrakeCLI --preset-list"
            precheck = "command -v HandBrakeCLI >/dev/null 2>&1"
            out = ""

            if password:
                client = self._connect_paramiko(target, port, keyfile, password)
                try:
                    # Some servers only add HandBrakeCLI to PATH for interactive shells.
                    # Try non-interactive login shell first, then fall back to interactive login shell.
                    shell_prefix = "bash -lc "
                    code, _ = self._exec_paramiko(client, shell_prefix + shlex.quote(precheck))
                    if code != 0:
                        shell_prefix = "bash -lic "
                        code, _ = self._exec_paramiko(client, shell_prefix + shlex.quote(precheck))
                        if code != 0:
                            self._log_threadsafe(
                                "(Info) HandBrakeCLI not found on the server; preset list cannot be loaded.\n"
                            )
                            return []
                        self._log_threadsafe(
                            "(Info) Loading HandBrake presets using an interactive shell (PATH differs for non-interactive SSH).\n"
                        )

                    code, out = self._exec_paramiko(client, shell_prefix + shlex.quote(cmd))
                    if code != 0:
                        self._log_threadsafe(
                            "(Info) Failed to run HandBrakeCLI --preset-list on the server; preset list cannot be loaded.\n"
                        )
                        return []
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                ssh_base = self._ssh_args(target, port, keyfile, tty=False)
                res = subprocess.run(
                    ssh_base + ["bash", "-lc", shlex.quote(precheck)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                if res.returncode != 0:
                    # Fall back to interactive login shell to match many users' manual SSH sessions.
                    res_i = subprocess.run(
                        ssh_base + ["bash", "-lic", shlex.quote(precheck)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    if res_i.returncode != 0:
                        detail = (res.stdout or "").strip() or (res_i.stdout or "").strip()
                        if detail:
                            self._log_threadsafe(
                                "(Info) HandBrakeCLI precheck failed on the server; preset list cannot be loaded:\n"
                                + detail
                                + "\n"
                            )
                        else:
                            self._log_threadsafe(
                                "(Info) HandBrakeCLI not found on the server; preset list cannot be loaded.\n"
                            )
                        return []
                    self._log_threadsafe(
                        "(Info) Loading HandBrake presets using an interactive shell (PATH differs for non-interactive SSH).\n"
                    )
                    use_interactive = True
                else:
                    use_interactive = False

                res2 = subprocess.run(
                    ssh_base
                    + (["bash", "-lic", shlex.quote(cmd)] if use_interactive else ["bash", "-lc", shlex.quote(cmd)]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                if res2.returncode != 0:
                    detail = (res2.stdout or "").strip()
                    if detail:
                        self._log_threadsafe(
                            "(Info) Failed to run HandBrakeCLI --preset-list on the server:\n" + detail + "\n"
                        )
                    else:
                        self._log_threadsafe(
                            "(Info) Failed to run HandBrakeCLI --preset-list on the server.\n"
                        )
                    return []
                out = res2.stdout or ""

            # Parse preset list output.
            # Typical format:
            #   General/
            #       Very Fast 1080p30
            #           Small H.264 ...
            # We want ONLY the "preset name" lines (usually indented, but indentation varies by build).
            presets: list[str] = []
            for raw in out.splitlines():
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue

                # Preserve indentation for classification.
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

                # Preset name lines are indented a small amount (often 4 spaces), but not the deeper
                # description lines (often 8+ spaces). Accept tabs too.
                if 2 <= indent_len <= 6:
                    name = s.strip()
                    if not name:
                        continue
                    # Defensive: ignore any stray category-like lines.
                    if name.endswith("/"):
                        continue
                    presets.append(name)

            # If the command ran but parsing yielded nothing, log a small snippet for troubleshooting.
            if not presets and out.strip():
                snippet_lines = [ln.rstrip("\r\n") for ln in out.splitlines() if ln.strip()][:20]
                if snippet_lines:
                    self._log_threadsafe(
                        "(Info) HandBrake preset list command ran, but no presets were parsed. "
                        "First lines of output:\n" + "\n".join(snippet_lines) + "\n"
                    )

            # De-dup while preserving order.
            seen: set[str] = set()
            unique: list[str] = []
            for p in presets:
                if p in seen:
                    continue
                seen.add(p)
                unique.append(p)
            return unique

        def _parse_for_progress(self, text_chunk: str) -> None:
            # text_chunk is typically a single line (with trailing \n)
            line = text_chunk.rstrip("\n")

            # MakeMKV raw status lines (as seen in example_log.log)
            m = MAKEMKV_OPERATION_RE.match(line)
            if m:
                op = m.group(1).strip()
                if self.state.waiting_for_enter:
                    self.state.waiting_for_enter = False
                    self.var_prompt.set("")
                    self.btn_continue.configure(state="disabled")

                if re.search(r"analy", op, flags=re.IGNORECASE):
                    self.state.makemkv_phase = "analyze"
                    self.var_step.set("Analyzing (MakeMKV): " + op)
                else:
                    self.state.makemkv_phase = "process"
                    self.var_step.set("Ripping (MakeMKV): " + op)

                self._eta_reset("makemkv")
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)
                return

            m = MAKEMKV_ACTION_RE.match(line)
            if m:
                act = m.group(1).strip()
                if self.state.waiting_for_enter:
                    self.state.waiting_for_enter = False
                    self.var_prompt.set("")
                    self.btn_continue.configure(state="disabled")

                if re.search(r"analy", act, flags=re.IGNORECASE):
                    self.state.makemkv_phase = "analyze"
                    self.var_step.set("Analyzing (MakeMKV): " + act)
                else:
                    self.state.makemkv_phase = "process"
                    self.var_step.set("Ripping (MakeMKV): " + act)

                self._eta_reset("makemkv")
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)
                return

            m = MAKEMKV_TOTAL_PROGRESS_RE.search(line)
            if m:
                try:
                    pct = float(m.group(1))
                except ValueError:
                    pct = 0.0
                if self.state.waiting_for_enter:
                    self.state.waiting_for_enter = False
                    self.var_prompt.set("")
                    self.btn_continue.configure(state="disabled")

                self.state.last_makemkv_total_pct = pct
                phase = self.state.makemkv_phase or "process"
                self.var_step.set("Analyzing (MakeMKV)" if phase == "analyze" else "Ripping (MakeMKV)")
                self.progress.configure(mode="determinate")
                self.progress.stop()
                self.progress["value"] = max(0.0, min(100.0, pct))

                self._eta_update("makemkv", pct)
                return

            m = MAKEMKV_CURRENT_PROGRESS_RE.search(line)
            if m:
                try:
                    pct = float(m.group(1))
                except ValueError:
                    pct = 0.0
                if self.state.waiting_for_enter:
                    self.state.waiting_for_enter = False
                    self.var_prompt.set("")
                    self.btn_continue.configure(state="disabled")

                # Prefer Total progress for driving the bar; Current progress is per-operation.
                phase = self.state.makemkv_phase or "process"
                self.var_step.set("Analyzing (MakeMKV)" if phase == "analyze" else "Ripping (MakeMKV)")
                if self.state.last_makemkv_total_pct > 0.0:
                    self.progress.configure(mode="determinate")
                    self.progress.stop()
                    self.progress["value"] = max(0.0, min(100.0, self.state.last_makemkv_total_pct))

                    self._eta_update("makemkv", self.state.last_makemkv_total_pct)
                else:
                    self.progress.configure(mode="determinate")
                    self.progress.stop()
                    self.progress["value"] = max(0.0, min(100.0, pct))

                    self._eta_update("makemkv", pct)
                return

            if MAKEMKV_ACCESS_ERROR_RE.search(line):
                self.var_step.set("Error")
                self.progress.stop()
                self.progress.configure(mode="indeterminate")
                return

            # HandBrake task markers
            m = HB_TASK_RE.match(line)
            if m:
                task = m.group(1).strip()
                # Encoding percent lines will switch it back to determinate.
                self.var_step.set("HandBrake: " + task)
                self._eta_reset("handbrake")
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)
                return

            m = HB_START_RE.match(line)
            if m:
                self.state.encode_started = int(m.group(1))
                self.state.encode_queued = max(self.state.encode_queued, int(m.group(2)))
                self.state.encode_active_label = m.group(3).strip()
                self.var_step.set(
                    f"Encoding (HandBrake) {self.state.encode_started} of {max(1, self.state.encode_queued)}"
                )
                self._eta_reset("handbrake")
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)
                return

            m = HB_DONE_RE.match(line)
            if m:
                self.state.encode_finished = int(m.group(1))
                self.state.encode_queued = max(self.state.encode_queued, int(m.group(2)))
                self.var_step.set(
                    f"Encoding (HandBrake) {min(self.state.encode_finished, self.state.encode_queued)} of {max(1, self.state.encode_queued)}"
                )
                self._eta_reset("handbrake")
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)
                return

            m = MAKE_MKV_PROGRESS_RE.search(line)
            if m:
                if self.state.waiting_for_enter:
                    self.state.waiting_for_enter = False
                    self.var_prompt.set("")
                    self.btn_continue.configure(state="disabled")
                self.var_step.set("Ripping (MakeMKV)")
                self.progress.configure(mode="determinate")
                self.progress.stop()
                self.progress["value"] = float(m.group(1))
                return

            m = HB_PROGRESS_RE.search(line)
            if m:
                try:
                    pct = float(m.group(1))
                except ValueError:
                    pct = 0.0
                if self.state.encode_queued > 0:
                    self.var_step.set(
                        f"Encoding (HandBrake) {max(1, self.state.encode_started)} of {self.state.encode_queued}"
                    )
                else:
                    self.var_step.set("Encoding (HandBrake)")
                self.progress.configure(mode="determinate")
                self.progress.stop()
                self.progress["value"] = max(0.0, min(100.0, pct))

                self._eta_update("handbrake", pct)
                return

            # Prompts can land mid-line depending on remote buffering; use search(), not match().
            if PROMPT_INSERT_RE.search(line) or PROMPT_NEXT_DISC_RE.search(line):
                self.state.waiting_for_enter = True
                self.var_step.set("Waiting for disc")
                shown = line
                if "Press Enter" in shown:
                    shown = shown.replace("Press Enter", "Click Continue (or press Enter)")
                self.var_prompt.set(shown)
                self.btn_continue.configure(state="normal")
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)
                return

            if line.startswith("Queued encode:"):
                self.state.encode_queued += 1
                self.var_step.set(f"Encoding (queued) {self.state.encode_queued}")
                self._eta_reset("handbrake")
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)
                return

            if FINALIZING_RE.match(line):
                self.state.finalized_titles += 1
                self.var_step.set("Finalizing")
                if self.state.waiting_for_enter:
                    self.state.waiting_for_enter = False
                    self.var_prompt.set("")
                    self.btn_continue.configure(state="disabled")
                if self.state.total_titles > 0:
                    self.progress.configure(mode="determinate")
                    self.progress.stop()
                    self.progress["value"] = (self.state.finalized_titles / self.state.total_titles) * 100
                else:
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(10)
                return

            if line.startswith("Processing complete."):
                self.var_step.set("Done")
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress["value"] = 100
                self.var_eta.set("")
                if self.state.waiting_for_enter:
                    self.state.waiting_for_enter = False
                self.var_prompt.set("")
                self.btn_continue.configure(state="disabled")
                if not self._done_emitted:
                    self._done_emitted = True
                    self.ui_queue.put(("done", "ok"))
                return

            if ERROR_RE.match(line):
                self.var_step.set("Error")
                self.progress.stop()
                self.progress.configure(mode="indeterminate")
                self.var_eta.set("")
                return

            # Attempt to infer title count from the schedule we generate / upload.
            if line.startswith("CSV schedule loaded:"):
                # We don't compute percent from disc count; finalize is by title.
                return

        def _on_return_key(self, _event=None) -> None:
            if not self.state.waiting_for_enter:
                return
            try:
                if str(self.btn_continue["state"]) != "normal":
                    return
            except Exception:
                pass
            self.send_enter()

        def _remote_run(self, target: str, port: str, keyfile: str, password: str, cmd: str) -> tuple[int, str]:
            """Run a short remote command and capture output."""

            if password:
                client = self._connect_paramiko(target, port, keyfile, password)
                try:
                    return self._exec_paramiko(client, "bash -lc " + shlex.quote(cmd))
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass

            ssh_base = self._ssh_args(target, port, keyfile, tty=False)
            res = subprocess.run(
                ssh_base + ["bash", "-lc", shlex.quote(cmd)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return res.returncode, res.stdout or ""

        def _screen_exists(self) -> bool:
            if not self.run_screen_name:
                return False
            code, _out = self._remote_run(
                self.run_target,
                self.run_port,
                self.run_keyfile,
                self.run_password,
                f"screen -S {shlex.quote(self.run_screen_name)} -Q select .",
            )
            return code == 0

        def _screen_stuff(self, payload: str) -> None:
            if not self.run_screen_name:
                return
            # payload is a bash $'..' string like $'\n' or $'\003'
            cmd = f"screen -S {shlex.quote(self.run_screen_name)} -p 0 -X stuff {payload}"
            self._remote_run(self.run_target, self.run_port, self.run_keyfile, self.run_password, cmd)

        def _find_latest_remote_log(self) -> str:
            cmd = (
                "for i in $(seq 1 25); do "
                "f=$(ls -t \"$HOME\"/.archive_helper_for_jellyfin/logs/rip_and_encode_v2_*.log \"$HOME\"/rip_and_encode_v2_*.log 2>/dev/null | head -n1); "
                "if [ -n \"$f\" ]; then echo \"$f\"; exit 0; fi; "
                "sleep 0.2; "
                "done; exit 1"
            )
            code, out = self._remote_run(self.run_target, self.run_port, self.run_keyfile, self.run_password, cmd)
            if code != 0:
                raise ValueError("Unable to locate remote log file after starting the job.")
            return (out or "").strip().splitlines()[-1].strip()

        def _start_tail(self) -> None:
            if not self.run_log_path:
                raise ValueError("Missing remote log path.")
            tail_cmd = f"tail -n +1 -F {shlex.quote(self.run_log_path)}"

            # Close any existing tail first.
            self._stop_tail(quiet=True)

            if self.run_password:
                self.tail_client = self._connect_paramiko(self.run_target, self.run_port, self.run_keyfile, self.run_password)
                chan = self.tail_client.get_transport().open_session()
                chan.get_pty()
                chan.exec_command("bash -lc " + shlex.quote(tail_cmd))
                self.tail_channel = chan
                self.tail_proc = None
            else:
                ssh_base = self._ssh_args(self.run_target, self.run_port, self.run_keyfile, tty=False)
                ssh_cmd = ssh_base + ["bash", "-lc", shlex.quote(tail_cmd)]
                self.tail_proc = subprocess.Popen(
                    ssh_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self.tail_channel = None
                self.tail_client = None

        def _stop_tail(self, *, quiet: bool = False) -> None:
            try:
                if self.tail_channel is not None:
                    try:
                        self.tail_channel.close()
                    except Exception:
                        pass
                if self.tail_client is not None:
                    try:
                        self.tail_client.close()
                    except Exception:
                        pass
            finally:
                self.tail_channel = None
                self.tail_client = None

            if self.tail_proc is not None:
                try:
                    if self.tail_proc.poll() is None:
                        self.tail_proc.terminate()
                except Exception:
                    pass
                self.tail_proc = None

            if not quiet:
                self._append_log("(Info) Stopped log tail.\n")

        def _on_done(self, payload: str) -> None:
            # Ensure any background tail process is stopped.
            try:
                self._stop_requested.set()
                self._stop_tail(quiet=True)
            except Exception:
                pass

            self.state.running = False
            self.state.waiting_for_enter = False
            self.state.makemkv_phase = ""
            self.state.last_makemkv_total_pct = 0.0
            self.state.encode_queued = 0
            self.state.encode_started = 0
            self.state.encode_finished = 0
            self.state.encode_active_label = ""
            self.state.eta_phase = ""
            self.state.eta_last_pct = 0.0
            self.state.eta_last_ts = 0.0
            self.state.eta_rate_ewma = 0.0
            self.state.run_started_ts = 0.0
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.btn_continue.configure(state="disabled")
            self.var_prompt.set("")
            self.var_eta.set("")
            self.var_elapsed.set("")

            self._set_inputs_enabled(True)

            # Clear run context.
            self.run_screen_name = ""
            self.run_log_path = ""

            if payload == "ok":
                messagebox.showinfo("Complete", "Processing complete.")
            else:
                messagebox.showerror("Stopped", payload)

        def cleanup_mkvs(self) -> None:
            if self.state.running:
                messagebox.showerror("Error", "Cleanup is disabled while a job is running. Stop the job first.")
                return

            try:
                target, remote_script, port, keyfile = self._validate()
                self._persist_state()
                password = (self.var_password.get() or "").strip()

                # Ensure the remote script exists (bootstrap upload if needed).
                remote_script = self._ensure_remote_script(target, port, keyfile, remote_script)

                self._append_log("Starting MKV cleanup preview (dry run)...\n")
                preview_cmd = " ".join(
                    shlex.quote(p)
                    for p in [
                        "python3",
                        remote_script,
                        "--cleanup-mkvs",
                        "--dry-run",
                    ]
                )
                code, out = self._remote_run(target, port, keyfile, password, preview_cmd)
                if out:
                    self._append_log(out.rstrip() + "\n")
                if code != 0:
                    raise ValueError("Cleanup preview failed.")

                # Count candidate lines (best-effort). The server emits: "  - <path> (...)".
                candidates = 0
                for line in (out or "").splitlines():
                    if line.startswith("  - "):
                        candidates += 1

                if candidates <= 0:
                    messagebox.showinfo("Cleanup", "No managed MKVs were found to clean.")
                    return

                confirm = messagebox.askyesno(
                    "Cleanup",
                    f"This will delete {candidates} MKV folder(s) from the remote host.\n\n"
                    "This does not delete your final MP4s in Movies/Series.\n\n"
                    "Continue?",
                )
                if not confirm:
                    self._append_log("Cleanup cancelled.\n")
                    return

                self._append_log("Running MKV cleanup...\n")
                run_cmd = " ".join(
                    shlex.quote(p)
                    for p in [
                        "python3",
                        remote_script,
                        "--cleanup-mkvs",
                    ]
                )
                code2, out2 = self._remote_run(target, port, keyfile, password, run_cmd)
                if out2:
                    self._append_log(out2.rstrip() + "\n")
                if code2 != 0:
                    raise ValueError("Cleanup failed.")

                messagebox.showinfo("Cleanup", "Cleanup complete.")
            except Exception as e:
                messagebox.showerror("Error", str(e))

        def _validate(self) -> tuple[str, str, str, str]:
            target = _ssh_target(self.var_user.get(), self.var_host.get())
            if not target:
                raise ValueError("Host is required.")

            keyfile = self.var_key.get()
            password = (self.var_password.get() or "").strip()

            # If a key file is provided, ensure OpenSSH will accept it (Linux/macOS enforce strict perms).
            kf = (keyfile or "").strip()
            if kf and os.name == "posix":
                try:
                    st = os.stat(kf)
                    mode = stat.S_IMODE(st.st_mode)
                    if (mode & 0o077) != 0:
                        raise ValueError(
                            "SSH key file permissions are too open, so OpenSSH will ignore the key.\n\n"
                            f"Key file: {kf}\n"
                            f"Current mode: {oct(mode)}\n\n"
                            "Fix on this machine:\n"
                            f"  chmod 600 {shlex.quote(kf)}\n"
                            "  chmod 700 ~/.ssh\n"
                        )
                except FileNotFoundError:
                    raise ValueError(f"SSH key file not found: {kf}")
                except PermissionError:
                    raise ValueError(f"Cannot read SSH key file: {kf}")
                except ValueError:
                    raise
                except Exception:
                    # Best-effort only; don't block non-standard setups.
                    pass

            # If no key file is provided, require a password.
            if not (keyfile or "").strip() and not password:
                raise ValueError("Password is required when no SSH key file is provided.")

            # Password auth requires Paramiko (system ssh cannot accept a password non-interactively).
            if password and not PARAMIKO_AVAILABLE:
                raise ValueError(
                    "Password-based SSH requires the 'paramiko' package. Install it and try again (pip install paramiko)."
                )

            # For key-based connections we can keep using OpenSSH; for password-based we'll use Paramiko.
            if not password:
                if shutil.which("ssh") is None:
                    raise ValueError("OpenSSH 'ssh' was not found on this machine.")
                if shutil.which("scp") is None:
                    raise ValueError("OpenSSH 'scp' was not found on this machine.")

            return target, REMOTE_SCRIPT_RUN_PATH, self.var_port.get(), keyfile

        def _parse_target(self, target: str) -> tuple[str, str]:
            # target is either "user@host" or "host"
            if "@" in target:
                user, host = target.split("@", 1)
                return user, host
            return self.var_user.get().strip(), target

        def _connect_paramiko(self, target: str, port: str, keyfile: str, password: str):
            if not PARAMIKO_AVAILABLE:
                raise ValueError("Paramiko is not available.")

            user, host = self._parse_target(target)
            if not user:
                raise ValueError("User is required for password-based SSH.")

            p = int(port.strip() or "22")
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            kf = keyfile.strip()
            if kf:
                client.connect(hostname=host, port=p, username=user, key_filename=kf, password=password or None)
            else:
                client.connect(hostname=host, port=p, username=user, password=password)

            self._maybe_log_paramiko_host_key(host, p, client)

            return client

        def _ensure_remote_python3(self, target: str, port: str, keyfile: str, password: str) -> None:
            """Ensure python3 exists on the remote host.

            We do not attempt to install python3 automatically (would require sudo and is distro-specific);
            we provide a clear error so the operator can install it.
            """

            check_cmd = "command -v python3 >/dev/null 2>&1"
            if password:
                client = self._connect_paramiko(target, port, keyfile, password)
                try:
                    code, _out = self._exec_paramiko(client, "bash -lc " + shlex.quote(check_cmd))
                    if code != 0:
                        raise ValueError(
                            "Remote host is missing python3. Install Python 3 on the remote host and try again."
                        )
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                ssh_base = self._ssh_args(target, port, keyfile, tty=False)
                res = subprocess.run(
                    ssh_base + ["bash", "-lc", shlex.quote(check_cmd)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                if res.returncode != 0:
                    raise ValueError("Remote host is missing python3. Install Python 3 on the remote host and try again.")

        def _ensure_remote_dir(self, target: str, port: str, keyfile: str, password: str, remote_dir: str) -> None:
            mkdir_cmd = "mkdir -p " + shlex.quote(remote_dir)
            if password:
                client = self._connect_paramiko(target, port, keyfile, password)
                try:
                    code, out = self._exec_paramiko(client, "bash -lc " + shlex.quote(mkdir_cmd))
                    if code != 0:
                        raise ValueError("Failed to create remote directory: " + out.strip())
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                ssh_base = self._ssh_args(target, port, keyfile, tty=False)
                res = subprocess.run(
                    ssh_base + ["bash", "-lc", shlex.quote(mkdir_cmd)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                if res.returncode != 0:
                    raise ValueError("Failed to create remote directory: " + (res.stdout or "").strip())

        def _remote_abs_path_paramiko(self, client, run_path: str) -> str:
            # Paramiko SFTP does not expand '~'. Convert to an absolute path.
            s = (run_path or "").strip()
            if not s.startswith("~"):
                return s
            code, out = self._exec_paramiko(client, "bash -lc " + shlex.quote("echo $HOME"))
            if code != 0:
                raise ValueError("Unable to determine remote home directory.")
            home = (out or "").strip().splitlines()[-1].strip()
            return s.replace("~", home, 1)

        def _remote_home_ssh(self, target: str, port: str, keyfile: str) -> str:
            ssh_base = self._ssh_args(target, port, keyfile, tty=False)
            res = subprocess.run(
                ssh_base + ["bash", "-lc", shlex.quote("echo $HOME")],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if res.returncode != 0:
                raise ValueError("Unable to determine remote home directory: " + ((res.stdout or "").strip()))
            home = (res.stdout or "").strip().splitlines()[-1].strip()
            if not home:
                raise ValueError("Unable to determine remote home directory.")
            return home

        def _remote_abs_path_ssh(self, target: str, port: str, keyfile: str, run_path: str) -> str:
            s = (run_path or "").strip()
            if not s.startswith("~"):
                return s
            home = self._remote_home_ssh(target, port, keyfile)
            return s.replace("~", home, 1)

        def _exec_paramiko(self, client, command: str) -> tuple[int, str]:
            stdin, stdout, stderr = client.exec_command(command)
            out = (stdout.read() or b"").decode("utf-8", errors="replace")
            err = (stderr.read() or b"").decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            return code, out + err

        def _sftp_put(self, client, local_path: str, remote_path: str) -> None:
            sftp = client.open_sftp()
            try:
                sftp.put(local_path, remote_path)
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass

        def _ensure_remote_script(self, target: str, port: str, keyfile: str, remote_script: str) -> str:
            """Ensure the rip script exists on the remote; upload it if missing.

            This keeps the GUI runnable for users who don't have the script pre-installed
            on the remote host.
            """
            normalized = _normalize_remote_script_path(remote_script)
            password = (self.var_password.get() or "").strip()

            # Ensure python3 exists and our remote directory is present.
            self._ensure_remote_python3(target, port, keyfile, password)

            # Use an absolute remote directory path (don't rely on '~' expansion).
            if password:
                client = self._connect_paramiko(target, port, keyfile, password)
                try:
                    remote_dir = self._remote_abs_path_paramiko(client, "~/.archive_helper_for_jellyfin")
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                remote_dir = self._remote_abs_path_ssh(target, port, keyfile, "~/.archive_helper_for_jellyfin")

            self._ensure_remote_dir(target, port, keyfile, password, remote_dir)

            local_script = Path(__file__).resolve().parent / "rip_and_encode_v2.py"
            if not local_script.exists():
                raise ValueError(f"Local script not found: {local_script}")

            # Always upload so the remote host matches the GUI's version.
            self._append_log(f"Uploading rip script to remote ({normalized})...\n")
            if password:
                client = self._connect_paramiko(target, port, keyfile, password)
                try:
                    abs_path = self._remote_abs_path_paramiko(client, normalized)
                    self._sftp_put(client, str(local_script), abs_path)
                    return abs_path
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                abs_path = self._remote_abs_path_ssh(target, port, keyfile, normalized)
                scp_args = self._scp_args(target, port, keyfile)
                try:
                    res = subprocess.run(
                        scp_args + [str(local_script), f"{target}:{abs_path}"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=True,
                    )
                    if res.stdout:
                        self._append_log(res.stdout)
                except subprocess.CalledProcessError as e:
                    detail = ((e.stdout or "").strip())
                    raise ValueError(
                        "Failed to upload rip script to the remote host.\n\n"
                        f"Target: {target}\n"
                        f"Remote path: {abs_path}\n\n"
                        + (detail if detail else "(No additional details.)")
                    )
                return abs_path

        def start(self) -> None:
            if self.state.running:
                return

            try:
                target, remote_script, port, keyfile = self._validate()
                self._persist_state()
                password = (self.var_password.get() or "").strip()

                # Ensure the remote script exists (bootstrap upload if needed).
                remote_script = self._ensure_remote_script(target, port, keyfile, remote_script)

                # Build local CSV schedule (manual or selected CSV).
                local_csv = None
                if self.var_mode.get() == "csv":
                    p = self.var_csv_path.get().strip()
                    if not p:
                        raise ValueError("CSV file path is required.")
                    local_csv = Path(p)
                    if not local_csv.exists():
                        raise ValueError(f"CSV file not found: {local_csv}")
                else:
                    title = self.var_title.get().strip()
                    year = self.var_year.get().strip()
                    if not title:
                        raise ValueError("Title is required.")
                    if not re.fullmatch(r"\d{4}", year):
                        raise ValueError("Year must be 4 digits.")

                    kind = self.var_kind.get()
                    total_discs = int(self.var_disc_count.get())
                    start_disc = int(self.var_start_disc.get())
                    if total_discs < 1:
                        raise ValueError("Total discs must be >= 1.")
                    if start_disc < 1 or start_disc > total_discs:
                        raise ValueError("Current disc must be between 1 and Total discs.")

                    rows = _csv_rows_from_manual(
                        kind=kind,
                        name=title,
                        year=year,
                        season=self.var_season.get(),
                        start_disc=start_disc,
                        total_discs=total_discs,
                    )

                    tmp = Path(tempfile.gettempdir()) / f"rip_and_encode_gui_{int(time.time())}.csv"
                    _write_csv_rows(tmp, rows)
                    local_csv = tmp

                    # Best-effort title counting for finalize progress.
                    self.state.total_titles = 1
                    self.state.finalized_titles = 0

                assert local_csv is not None

                # Upload CSV to remote.
                remote_csv = f"/tmp/rip_and_encode_schedule_{int(time.time())}.csv"
                self._append_log("Uploading schedule via SCP...\n")
                if password:
                    client = self._connect_paramiko(target, port, keyfile, password)
                    try:
                        self._sftp_put(client, str(local_csv), remote_csv)
                    finally:
                        try:
                            client.close()
                        except Exception:
                            pass
                else:
                    scp_args = self._scp_args(target, port, keyfile)
                    scp_cmd = scp_args + [str(local_csv), f"{target}:{remote_csv}"]
                    try:
                        res = subprocess.run(
                            scp_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            check=True,
                        )
                        if res.stdout:
                            self._append_log(res.stdout)
                    except subprocess.CalledProcessError as e:
                        detail = ((e.stdout or "").strip())
                        raise ValueError(
                            "Failed to upload schedule to the remote host.\n\n"
                            f"Target: {target}\n"
                            f"Remote path: {remote_csv}\n\n"
                            + (detail if detail else "(No additional details.)")
                        )

                # Build remote command.
                cmd_parts = [
                    "RIP_AND_ENCODE_IN_SCREEN=1",
                    "python3",
                    remote_script,
                    "--csv",
                    remote_csv,
                    "--movies-dir",
                    self.var_movies_dir.get().strip(),
                    "--series-dir",
                    self.var_series_dir.get().strip(),
                    "--preset",
                    self.var_preset.get().strip(),
                ]

                if self.var_ensure_jellyfin.get():
                    cmd_parts += ["--ensure-jellyfin"]

                # Defaults: always overlap with 1 encode job, and always keep MKVs (fail-safe).
                cmd_parts += ["--overlap", "--encode-jobs", "1", "--keep-mkvs"]

                remote_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

                # Start remote job in a detached screen session so we can reconnect/tail logs.
                self._append_log("Starting remote job (screen)...\n")

                # Store run context for reconnect.
                self.run_target = target
                self.run_port = port
                self.run_keyfile = keyfile
                self.run_password = password
                self.run_screen_name = f"archive_helper_for_jellyfin_{int(time.time())}"
                self.run_log_path = ""
                self._stop_requested.clear()
                self._done_emitted = False

                # Ensure screen exists.
                code, out = self._remote_run(target, port, keyfile, password, "command -v screen >/dev/null 2>&1")
                if code != 0:
                    raise ValueError("Remote host is missing 'screen'. Install it and try again.\n" + (out or "").strip())

                screen_cmd = (
                    f"screen -S {shlex.quote(self.run_screen_name)} -dm "
                    f"bash -lc {shlex.quote(remote_cmd)}"
                )
                code, out = self._remote_run(target, port, keyfile, password, screen_cmd)
                if code != 0:
                    raise ValueError("Failed to start remote job in screen: " + (out or "").strip())

                # Find the log file path and begin tailing it.
                self.run_log_path = self._find_latest_remote_log()
                self._append_log(f"(Info) Following remote log: {self.run_log_path}\n")
                self._start_tail()

                # Clear legacy direct-stream handles (we tail logs instead).
                self.proc = None
                self.ssh_channel = None
                self.ssh_client = None

                self.state.running = True
                self.state.waiting_for_enter = False
                self.state.makemkv_phase = ""
                self.state.last_makemkv_total_pct = 0.0
                self.state.encode_queued = 0
                self.state.encode_started = 0
                self.state.encode_finished = 0
                self.state.encode_active_label = ""
                self.state.eta_phase = ""
                self.state.eta_last_pct = 0.0
                self.state.eta_last_ts = 0.0
                self.state.eta_rate_ewma = 0.0
                self.state.run_started_ts = time.time()
                self.var_step.set("Running")
                self.var_prompt.set("")
                self.var_eta.set("")
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)

                self.btn_start.configure(state="disabled")
                self.btn_stop.configure(state="normal")
                self.btn_continue.configure(state="disabled")

                self._set_inputs_enabled(False)

                self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
                self.reader_thread.start()

            except Exception as e:
                messagebox.showerror("Error", str(e))

        def _reader_loop(self) -> None:
            backoff = 1.0

            while self.state.running and not self._stop_requested.is_set():
                # OpenSSH tail path
                if self.run_password == "":
                    if self.tail_proc is None:
                        try:
                            self._start_tail()
                        except Exception as e:
                            self.ui_queue.put(("log", f"(Info) Failed to start log tail: {e}\n"))
                            time.sleep(min(backoff, 10.0))
                            backoff = min(backoff * 2.0, 30.0)
                            continue

                    assert self.tail_proc is not None
                    assert self.tail_proc.stdout is not None

                    try:
                        for line in self.tail_proc.stdout:
                            if self._stop_requested.is_set() or not self.state.running:
                                break
                            self.ui_queue.put(("log", line))

                        if self._stop_requested.is_set() or not self.state.running:
                            break

                        code = self.tail_proc.wait()
                        self.ui_queue.put(("log", f"(Info) Disconnected from server (tail exit {code}). Reconnecting...\n"))
                    except Exception as e:
                        self.ui_queue.put(("log", f"(Info) Lost connection while reading log: {e}. Reconnecting...\n"))

                    # Cleanup and attempt reconnect.
                    self._stop_tail(quiet=True)
                    if not self._screen_exists():
                        self.ui_queue.put(("done", "Lost connection and the remote job is no longer running."))
                        return
                    time.sleep(min(backoff, 10.0))
                    backoff = min(backoff * 2.0, 30.0)
                    continue

                # Paramiko tail path
                if self.tail_channel is None:
                    try:
                        self._start_tail()
                    except Exception as e:
                        self.ui_queue.put(("log", f"(Info) Failed to start log tail: {e}\n"))
                        time.sleep(min(backoff, 10.0))
                        backoff = min(backoff * 2.0, 30.0)
                        continue

                assert self.tail_channel is not None
                buf = ""
                try:
                    while self.state.running and not self._stop_requested.is_set():
                        if self.tail_channel.recv_ready():
                            data = self.tail_channel.recv(4096)
                            if not data:
                                break
                            chunk = data.decode("utf-8", errors="replace")
                            buf += chunk
                            while "\n" in buf:
                                line, buf = buf.split("\n", 1)
                                self.ui_queue.put(("log", line + "\n"))

                        if self.tail_channel.exit_status_ready():
                            break
                        time.sleep(0.03)

                    if buf:
                        self.ui_queue.put(("log", buf))

                    self.ui_queue.put(("log", "(Info) Disconnected from server. Reconnecting...\n"))
                except Exception as e:
                    self.ui_queue.put(("log", f"(Info) Lost connection while reading log: {e}. Reconnecting...\n"))

                # Cleanup and attempt reconnect.
                self._stop_tail(quiet=True)
                if not self._screen_exists():
                    self.ui_queue.put(("done", "Lost connection and the remote job is no longer running."))
                    return
                time.sleep(min(backoff, 10.0))
                backoff = min(backoff * 2.0, 30.0)

            # Stop requested.
            return

        def send_enter(self) -> None:
            try:
                if self.run_screen_name:
                    # Send Enter into the remote screen session.
                    self._screen_stuff("$'\\n'")
                else:
                    # Fallback to legacy direct-stdin mode.
                    if self.proc and self.proc.stdin:
                        self.proc.stdin.write("\n")
                        self.proc.stdin.flush()
                    elif self.ssh_channel is not None:
                        self.ssh_channel.send("\n")
                    else:
                        return
                self.state.waiting_for_enter = False
                self.btn_continue.configure(state="disabled")
                self.var_prompt.set("")
                self.var_eta.set("")
                self.var_step.set("Running")
            except Exception:
                pass

        def _eta_reset(self, phase: str) -> None:
            # Reset when we switch phases or go indeterminate.
            self.state.eta_phase = phase
            self.state.eta_last_pct = 0.0
            self.state.eta_last_ts = 0.0
            self.state.eta_rate_ewma = 0.0
            self.var_eta.set("")

        def _eta_update(self, phase: str, pct: float) -> None:
            # Conservative ETA: require increasing progress and a stable rate.
            try:
                pct = float(pct)
            except Exception:
                return
            pct = max(0.0, min(100.0, pct))

            now = time.time()
            if self.state.eta_phase != phase:
                self._eta_reset(phase)
                self.state.eta_last_pct = pct
                self.state.eta_last_ts = now
                return

            if self.state.eta_last_ts <= 0.0:
                self.state.eta_last_pct = pct
                self.state.eta_last_ts = now
                return

            dt = now - self.state.eta_last_ts
            if dt <= 0.4:
                return

            dp = pct - self.state.eta_last_pct
            # Ignore non-forward movement.
            if dp <= 0.0:
                self.state.eta_last_ts = now
                self.state.eta_last_pct = pct
                return

            rate = dp / dt  # pct per second
            # EWMA smoothing.
            alpha = 0.25
            if self.state.eta_rate_ewma <= 0.0:
                self.state.eta_rate_ewma = rate
            else:
                self.state.eta_rate_ewma = (alpha * rate) + ((1.0 - alpha) * self.state.eta_rate_ewma)

            self.state.eta_last_ts = now
            self.state.eta_last_pct = pct

            if self.state.eta_rate_ewma <= 0.01:
                self.var_eta.set("")
                return

            remaining_pct = max(0.0, 100.0 - pct)
            eta_s = remaining_pct / self.state.eta_rate_ewma

            # Hide clearly bogus numbers.
            if eta_s < 1.0 or eta_s > (12 * 60 * 60):
                self.var_eta.set("")
                return

            mins = int(eta_s // 60)
            secs = int(eta_s % 60)
            if mins >= 60:
                hrs = mins // 60
                mins = mins % 60
                self.var_eta.set(f"ETA {hrs}h {mins:02d}m")
            else:
                self.var_eta.set(f"ETA {mins}m {secs:02d}s")

        def stop(self) -> None:
            if self._replay_mode and self.state.running:
                self._replay_stop.set()
                return

            self._stop_requested.set()

            # Stop remote job if we launched it in screen.
            if self.run_screen_name:
                try:
                    self._screen_stuff("$'\\003'")
                    time.sleep(0.2)
                    self._remote_run(
                        self.run_target,
                        self.run_port,
                        self.run_keyfile,
                        self.run_password,
                        f"screen -S {shlex.quote(self.run_screen_name)} -X quit",
                    )
                except Exception:
                    pass
                try:
                    self._stop_tail(quiet=True)
                except Exception:
                    pass
                return

            if self.ssh_channel is not None:
                try:
                    self.ssh_channel.send("\x03")
                except Exception:
                    pass
                try:
                    self.ssh_channel.close()
                except Exception:
                    pass
                return

            if not self.proc:
                return
            try:
                # Send Ctrl-C over the PTY, then fall back to terminate.
                if self.proc.stdin:
                    self.proc.stdin.write("\x03")
                    self.proc.stdin.flush()
            except Exception:
                pass

            def _kill_later() -> None:
                time.sleep(1.5)
                try:
                    if self.proc and self.proc.poll() is None:
                        self.proc.terminate()
                except Exception:
                    pass

            threading.Thread(target=_kill_later, daemon=True).start()

        def start_replay(self, log_path: str) -> None:
            if self.state.running:
                return

            p = Path(log_path)
            if not p.exists():
                raise ValueError(f"Log file not found: {p}")

            self._replay_mode = True
            self._replay_stop.clear()

            self.state.running = True
            self.state.waiting_for_enter = False
            self.var_step.set("Replaying log")
            self.var_prompt.set("")
            self.progress.configure(mode="indeterminate")
            self.progress.start(10)

            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.btn_continue.configure(state="disabled")

            def _replay() -> None:
                try:
                    with p.open("r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, start=1):
                            if self._replay_stop.is_set():
                                self.ui_queue.put(("done", "Stopped"))
                                return
                            self.ui_queue.put(("log", line))

                            # Yield periodically so Tk stays responsive, but keep replay fast.
                            if i % 250 == 0:
                                time.sleep(0.001)
                    self.ui_queue.put(("done", "ok"))
                except Exception as e:
                    self.ui_queue.put(("done", str(e)))

            self.reader_thread = threading.Thread(target=_replay, daemon=True)
            self.reader_thread.start()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--replay-log",
        dest="replay_log",
        default="",
        help="Replay a local log file (no SSH) to test progress parsing.",
    )
    args = parser.parse_args()

    if not TK_AVAILABLE:
        print("Tkinter is not available in this Python environment.")
        print("Linux hint (Debian/Ubuntu): sudo apt-get update && sudo apt-get install -y python3-tk")
        print("macOS hint: use the official python.org installer (includes Tk) or install tk via Homebrew.")
        print("Windows hint: Python from python.org typically includes Tkinter.")
        return 2

    root = Tk()
    try:
        gui = RipGui(root)
        if args.replay_log:
            gui.start_replay(args.replay_log)
        root.mainloop()
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
