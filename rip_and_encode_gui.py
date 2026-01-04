#!/usr/bin/env python3
"""rip_and_encode_gui.py

Tkinter GUI for rip_and_encode.py (Option B: full workflow).

Goal:
- Run on Windows/macOS/Linux.
- Hide terminal usage from the operator.
- Connect to the rip host over SSH, upload a generated CSV schedule (or use a selected CSV),
    run rip_and_encode.py in --csv mode, and drive the "press Enter to continue" prompts.

Requirements implemented:
- Collapsible log window (collapsed by default).
- Progress bar + step label driven by parsing the log output.

Assumptions:
- The ripping/transcoding tools (makemkvcon, HandBrakeCLI, ffprobe, eject) live on the remote host.
- The remote host has rip_and_encode.py available at the provided remote path.
- The client has OpenSSH `ssh` and `scp` available (Windows 10+ usually does).

This GUI does not implement interactive (non-CSV) mode; instead it always drives rip_and_encode.py
using --csv for determinism.
"""

from __future__ import annotations

import argparse
import os
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
from pathlib import Path
from typing import Any, cast

from rip_and_encode import csv_disc_prompt_for_row, load_csv_schedule, sanitize_title_for_dir

from archive_helper_gui.log_patterns import (
    CSV_LOADED_RE,
    ERROR_RE,
    FINALIZING_RE,
    HB_DONE_RE,
    HB_PROGRESS_RE,
    HB_START_RE,
    HB_TASK_RE,
    MAKEMKV_ACCESS_ERROR_RE,
    MAKEMKV_ACTION_RE,
    MAKEMKV_CURRENT_PROGRESS_RE,
    MAKEMKV_OPERATION_RE,
    MAKEMKV_TOTAL_PROGRESS_RE,
    MAKE_MKV_PROGRESS_RE,
    PROMPT_INSERT_RE,
    PROMPT_NEXT_DISC_RE,
)
from archive_helper_gui.models import ConnectionInfo, RunContext, UiState
from archive_helper_gui.parser import parse_for_progress
from archive_helper_gui.handbrake_presets import fetch_handbrake_presets
from archive_helper_gui.connection_dialog import open_connection_settings_dialog
from archive_helper_gui.directories_dialog import open_directories_settings_dialog
from archive_helper_gui.help_dialog import show_help_dialog
from archive_helper_gui.persistence import PersistenceStore
from archive_helper_gui.remote_exec import RemoteExecutor
from archive_helper_gui.schedule import csv_rows_from_manual, write_csv_rows
from archive_helper_gui.tailer import reader_loop as tailer_reader_loop
from archive_helper_gui.tailer import start_tail as tailer_start_tail
from archive_helper_gui.tailer import stop_tail as tailer_stop_tail
from archive_helper_gui.tooltip import Tooltip

# Optional dependencies: define symbols on all paths so static analyzers (Pylance)
# don't report hundreds of "possibly unbound" errors.
keyring = None
try:
    import keyring as _keyring  # type: ignore

    keyring = _keyring
    KEYRING_AVAILABLE = True
except Exception:
    KEYRING_AVAILABLE = False

try:
    import paramiko  # type: ignore

    PARAMIKO_AVAILABLE = True
except Exception:
    PARAMIKO_AVAILABLE = False

try:
    from tkinter import (  # type: ignore
        BOTH as _TK_BOTH,
        END as _TK_END,
        LEFT as _TK_LEFT,
        RIGHT as _TK_RIGHT,
        X as _TK_X,
        BooleanVar as _TK_BooleanVar,
        IntVar as _TK_IntVar,
        Menu as _TK_Menu,
        StringVar as _TK_StringVar,
        Tk as _TK_Tk,
        Toplevel as _TK_Toplevel,
        filedialog as _TK_filedialog,
        messagebox as _TK_messagebox,
    )
    from tkinter import ttk as _TK_ttk  # type: ignore
    from tkinter.scrolledtext import ScrolledText as _TK_ScrolledText  # type: ignore

    BOTH = _TK_BOTH
    END = _TK_END
    LEFT = _TK_LEFT
    RIGHT = _TK_RIGHT
    X = _TK_X
    BooleanVar = _TK_BooleanVar
    IntVar = _TK_IntVar
    Menu = _TK_Menu
    StringVar = _TK_StringVar
    Tk = _TK_Tk
    Toplevel = _TK_Toplevel
    filedialog = _TK_filedialog
    messagebox = _TK_messagebox
    ttk = _TK_ttk
    ScrolledText = _TK_ScrolledText

    TK_AVAILABLE = True
except ModuleNotFoundError:
    TK_AVAILABLE = False

    # Define placeholders so references are always bound for type checking.
    # These are never used at runtime because the GUI exits early when TK_AVAILABLE is False.
    BOTH = cast(Any, "both")
    X = cast(Any, "x")
    LEFT = cast(Any, "left")
    RIGHT = cast(Any, "right")
    END = cast(Any, "end")

    StringVar = cast(Any, lambda *args, **kwargs: None)
    BooleanVar = cast(Any, lambda *args, **kwargs: None)
    IntVar = cast(Any, lambda *args, **kwargs: None)
    Menu = cast(Any, lambda *args, **kwargs: None)
    Tk = cast(Any, object)
    Toplevel = cast(Any, object)
    ttk = cast(Any, None)
    filedialog = cast(Any, None)
    messagebox = cast(Any, None)
    ScrolledText = cast(Any, object)



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
        return "rip_and_encode.py"
    if "/" in s or s.startswith("~"):
        return s
    return f"~/{s}"


REMOTE_SCRIPT_RUN_PATH = "~/.archive_helper_for_jellyfin/rip_and_encode.py"
EXEC_MODE_REMOTE = "remote"  # rip+encode on server (current behavior)
EXEC_MODE_LOCAL_RIP_ONLY = "local_rip_only"  # rip locally, encode on server (planned)
EXEC_MODE_LOCAL_RIP_ENCODE = "local_rip_encode"  # rip+encode locally, upload results (planned)

def _exec_mode_label(mode: str) -> str:
    m = (mode or "").strip()
    if m == EXEC_MODE_LOCAL_RIP_ONLY:
        return "Rip locally (encode on server)"
    if m == EXEC_MODE_LOCAL_RIP_ENCODE:
        return "Rip + encode locally (upload results)"
    return "Rip + encode on server (remote)"


if TK_AVAILABLE:

    class RipGui:
        def __init__(self, root: Any) -> None:
            self.root = root
            self.root.title("Archive Helper for Jellyfin")
            self.state = UiState()

            # Set to True right before destroying the Tk root to prevent
            # scheduled callbacks from attempting to open dialogs.
            self._closing = False

            self._main_thread_ident = threading.get_ident()
            self._replay_stop = threading.Event()
            self._replay_mode = False

            self._state_file_existed = False

            # Persisted last-run metadata (for reattach after GUI crash/power loss).
            self.last_run_host: str = ""
            self.last_run_user: str = ""
            self.last_run_port: str = ""
            self.last_run_screen_name: str = ""
            self.last_run_log_path: str = ""
            self.last_run_remote_start_epoch: int = 0

            self.proc: subprocess.Popen[str] | None = None
            self.ssh_client = None
            self.ssh_channel = None
            self.reader_thread: threading.Thread | None = None
            self.ui_queue: queue.Queue[tuple[str, str]] = queue.Queue()

            # Auto-reconnect runtime state (tail remote log; send input via screen).
            self.tail_proc: subprocess.Popen[str] | None = None
            self.tail_client = None
            self.tail_channel = None

            self.run_ctx: RunContext | None = None

            # Local rip mode runtime state.
            self._local_continue_event = threading.Event()
            self._local_waiting_for_continue = False
            self._local_stop_requested = threading.Event()
            self._local_proc: subprocess.Popen[str] | None = None
            self._local_thread: threading.Thread | None = None
            self._local_ripping_active = False

            # Stashed connection/script info for local mode handoff.
            self._local_cfg: ConnectionInfo | None = None
            self._local_remote_script: str = ""

            self._stop_requested = threading.Event()
            self._done_emitted = False
            self._done_handled = False

            # Connection
            self.var_host = StringVar(value="")
            self.var_user = StringVar(value="")
            self.var_port = StringVar(value="")
            self.var_key = StringVar(value="")
            self.var_password = StringVar(value="")

            # Remote executor (OpenSSH/Paramiko). Initialized after connection vars exist.
            self.remote = RemoteExecutor(
                state_dir=self._state_dir(),
                log=self._log_threadsafe,
                default_user_getter=lambda: (self.var_user.get() or "").strip(),
            )

            # Local persistence (state file + optional keyring password).
            self.persistence = PersistenceStore(
                state_dir=self._state_dir(),
                keyring_available=KEYRING_AVAILABLE,
                keyring_module=(keyring if KEYRING_AVAILABLE else None),
            )

            # Script settings
            self.var_movies_dir = StringVar(value="/storage/Movies")
            self.var_series_dir = StringVar(value="/storage/Series")
            self.var_books_dir = StringVar(value="/storage/Books")
            self.var_music_dir = StringVar(value="/storage/Music")
            self.var_preset = StringVar(value="HQ 1080p30 Surround")
            self.var_ensure_jellyfin = BooleanVar(value=False)
            self.var_disc_type = StringVar(value="dvd")

            self._connection_win: Any | None = None
            self._directories_win: Any | None = None

            self._presets_loading = False
            self._presets_loaded = False
            # Execution mode (where ripping/encoding happens). Default is current behavior: remote.
            self.var_exec_mode = StringVar(value=EXEC_MODE_REMOTE)
            self._exec_mode_was_loaded = False

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

            # If the user hasn't picked an execution mode yet, prompt once on startup.
            self._maybe_prompt_exec_mode_on_startup()

            self._apply_setup_gate()
            self.root.after(0, self._maybe_run_first_launch_setup)

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
            menubar = Menu(self.root)
            settings_menu = Menu(menubar, tearoff=0)
            # Keep references so we can disable settings editing while a run is active.
            self._settings_menu = settings_menu

            settings_menu.add_command(label="Connection...", command=self._open_connection_settings)
            self._settings_menu_connection_idx = settings_menu.index("end")

            settings_menu.add_command(label="Directories...", command=self._open_directories_settings)
            self._settings_menu_directories_idx = settings_menu.index("end")
            settings_menu.add_separator()
            settings_menu.add_checkbutton(
                label="Install Jellyfin if missing",
                variable=self.var_ensure_jellyfin,
                command=self._on_menu_setting_changed,
            )
            settings_menu.add_command(label="Rip mode...", command=self._open_exec_mode_settings)
            self._settings_menu_exec_mode_idx = settings_menu.index("end")
            menubar.add_cascade(label="Settings", menu=settings_menu)
            self.root.config(menu=menubar)

            main = ttk.Frame(self.root, padding=10)
            main.pack(fill=BOTH, expand=True)
            self.main_frame = main

            header = ttk.Frame(main)
            header.pack(fill=X, pady=(0, 10))
            self._build_logo(header)

            # Settings frame
            settings = ttk.LabelFrame(main, text="Run settings", padding=10)
            settings.pack(fill=X, pady=(10, 0))

            s1 = ttk.Frame(settings)
            s1.pack(fill=X)
            ttk.Label(s1, text="Disc type:").pack(side=LEFT)
            cbo_disc = ttk.Combobox(s1, textvariable=self.var_disc_type, values=["dvd", "bluray"], state="readonly", width=8)
            cbo_disc.pack(side=LEFT, padx=5)
            Tooltip(cbo_disc, "Select 'bluray' for Blu-ray discs (uses a larger MakeMKV cache: 1024MB vs 128MB).")

            s2 = ttk.Frame(settings)
            s2.pack(fill=X, pady=(6, 0))
            ttk.Label(s2, text="HandBrake preset:").pack(side=LEFT)
            self.cbo_preset = ttk.Combobox(s2, textvariable=self.var_preset, width=33, state="normal")
            self.cbo_preset.pack(side=LEFT, padx=5)
            Tooltip(self.cbo_preset, "HandBrake preset name on the server (loaded from HandBrakeCLI --preset-list).")

            note = ttk.Label(settings, text="Connection and output directories are set under Settings.")
            note.pack(anchor="w", pady=(6, 0))

            self.lbl_exec_mode = ttk.Label(settings, text=f"Rip mode: {_exec_mode_label(self.var_exec_mode.get())}")
            self.lbl_exec_mode.pack(anchor="w", pady=(2, 0))

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
            self.disc_row = r2
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
            Tooltip(self.btn_cleanup, "Optionally delete leftover work folders (temporary MKVs/staging) on the server (safe and confirmed).")

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
            show_help_dialog(self.root)

        def _build_logo(self, parent: Any) -> None:
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
            return self.persistence.state_path()

        def _known_hosts_path(self) -> Path:
            return self.remote.known_hosts_path

        def _ssh_common_opts(self) -> list[str]:
            return self.remote.ssh_common_opts()

        def _log_threadsafe(self, message: str) -> None:
            if threading.get_ident() == self._main_thread_ident:
                self._append_log(message)
            else:
                self.ui_queue.put(("log", message))

        def _ssh_args(self, target: str, port: str, keyfile: str, *, tty: bool = True) -> list[str]:
            return self.remote.ssh_args(target, port, keyfile, tty=tty)

        def _scp_args(self, target: str, port: str, keyfile: str) -> list[str]:
            return self.remote.scp_args(target, port, keyfile)

        def _keyring_id(self) -> str:
            host = (self.var_host.get() or "").strip()
            user = (self.var_user.get() or "").strip()
            port = (self.var_port.get() or "").strip() or "22"
            return f"{user}@{host}:{port}" if user else f"{host}:{port}"

        def _load_persisted_state(self) -> None:
            if self.persistence.state_file_exists():
                self._state_file_existed = True

            data = self.persistence.load_state_dict()
            if isinstance(data, dict):
                self.var_host.set(str(data.get("host", self.var_host.get())))
                self.var_user.set(str(data.get("user", self.var_user.get())))
                self.var_port.set(str(data.get("port", self.var_port.get())))
                self.var_key.set(str(data.get("key", self.var_key.get())))

                self.var_movies_dir.set(str(data.get("movies_dir", self.var_movies_dir.get())))
                self.var_series_dir.set(str(data.get("series_dir", self.var_series_dir.get())))
                self.var_books_dir.set(str(data.get("books_dir", self.var_books_dir.get())))
                self.var_music_dir.set(str(data.get("music_dir", self.var_music_dir.get())))
                self.var_preset.set(str(data.get("preset", self.var_preset.get())))
                self.var_ensure_jellyfin.set(bool(data.get("ensure_jellyfin", self.var_ensure_jellyfin.get())))
                self.var_disc_type.set(str(data.get("disc_type", self.var_disc_type.get())))
                if "exec_mode" in data:
                    self._exec_mode_was_loaded = True
                self.var_exec_mode.set(str(data.get("exec_mode", self.var_exec_mode.get())))

                self.var_mode.set(str(data.get("mode", self.var_mode.get())))
                self.var_csv_path.set(str(data.get("csv_path", self.var_csv_path.get())))
                self.var_kind.set(str(data.get("kind", self.var_kind.get())))
                self.var_title.set(str(data.get("title", self.var_title.get())))
                self.var_year.set(str(data.get("year", self.var_year.get())))
                self.var_season.set(str(data.get("season", self.var_season.get())))
                self.var_start_disc.set(int(data.get("start_disc", int(self.var_start_disc.get()))))
                self.var_disc_count.set(int(data.get("disc_count", int(self.var_disc_count.get()))))

                # Last-run (reattach) metadata.
                self.last_run_host = str(data.get("last_run_host", self.last_run_host))
                self.last_run_user = str(data.get("last_run_user", self.last_run_user))
                self.last_run_port = str(data.get("last_run_port", self.last_run_port))
                self.last_run_screen_name = str(data.get("last_run_screen_name", self.last_run_screen_name))
                self.last_run_log_path = str(data.get("last_run_log_path", self.last_run_log_path))
                try:
                    self.last_run_remote_start_epoch = int(
                        data.get("last_run_remote_start_epoch", int(self.last_run_remote_start_epoch))
                    )
                except Exception:
                    self.last_run_remote_start_epoch = 0

            pw = self.persistence.load_password(self._keyring_id())
            if pw:
                self.var_password.set(pw)

        def _persist_state(self) -> None:
            data: dict[str, Any] = {
                "host": self.var_host.get(),
                "user": self.var_user.get(),
                "port": self.var_port.get(),
                "key": self.var_key.get(),
                "movies_dir": self.var_movies_dir.get(),
                "series_dir": self.var_series_dir.get(),
                "books_dir": self.var_books_dir.get(),
                "music_dir": self.var_music_dir.get(),
                "preset": self.var_preset.get(),
                "ensure_jellyfin": bool(self.var_ensure_jellyfin.get()),
                "disc_type": self.var_disc_type.get(),
                "mode": self.var_mode.get(),
                "csv_path": self.var_csv_path.get(),
                "kind": self.var_kind.get(),
                "title": self.var_title.get(),
                "year": self.var_year.get(),
                "season": self.var_season.get(),
                "start_disc": int(self.var_start_disc.get()),
                "disc_count": int(self.var_disc_count.get()),
                "exec_mode": self.var_exec_mode.get(),

                # Last-run (reattach) metadata.
                "last_run_host": self.last_run_host,
                "last_run_user": self.last_run_user,
                "last_run_port": self.last_run_port,
                "last_run_screen_name": self.last_run_screen_name,
                "last_run_log_path": self.last_run_log_path,
                "last_run_remote_start_epoch": int(self.last_run_remote_start_epoch or 0),
            }

            self.persistence.save_state_dict(data)
            self.persistence.save_password(self._keyring_id(), (self.var_password.get() or ""))

            try:
                if hasattr(self, "lbl_exec_mode"):
                    self.lbl_exec_mode.configure(text=f"Rip mode: {_exec_mode_label(self.var_exec_mode.get())}")
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
                    # Give the user a safe choice: stop remote job, or leave it running
                    # so they can reattach later (useful if the GUI is being closed accidentally).
                    choice = messagebox.askyesnocancel(
                        "Quit",
                        "A job is currently running.\n\n"
                        "Yes: Stop the job, then quit.\n"
                        "No: Quit the GUI but leave the job running on the server (you can reattach later).\n"
                        "Cancel: Keep the GUI open.",
                    )
                    if choice is None:
                        return
                    if choice:
                        self.stop()
            except Exception:
                pass
            try:
                self._closing = True
            except Exception:
                pass
            try:
                self.root.destroy()
            except Exception:
                pass

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
                # Always hide Season row in CSV mode.
                self.season_row.pack_forget()
                return

            if self.var_kind.get() == "series":
                # Ensure consistent placement above the disc row.
                try:
                    self.season_row.pack(fill=X, pady=(6, 0), before=self.disc_row)
                except Exception:
                    self.season_row.pack(fill=X, pady=(6, 0))
            else:
                # Unconditionally hide so startup doesn't depend on Tk mapping state.
                self.season_row.pack_forget()

        def _browse_key(self) -> None:
            p = filedialog.askopenfilename(title="Select SSH private key")
            if p:
                self.var_key.set(p)

        def _on_menu_setting_changed(self) -> None:
            try:
                self._persist_state()
            except Exception:
                pass

        def _maybe_prompt_exec_mode_on_startup(self) -> None:
            # We only prompt once, when the setting hasn't been loaded from disk.
            # Default behavior remains remote rip+encode.
            if self._exec_mode_was_loaded:
                return
            try:
                self._open_exec_mode_settings(modal=True, title="Choose rip mode")
            except Exception:
                return

        def _open_exec_mode_settings(self, *, modal: bool = False, title: str = "Rip mode") -> None:
            if self.state.running:
                messagebox.showinfo("Rip mode", "You can only change rip mode when idle.")
                return

            win = Toplevel(self.root)
            win.title(title)
            win.resizable(False, False)
            try:
                win.transient(self.root)
            except Exception:
                pass

            container = ttk.Frame(win, padding=12)
            container.pack(fill=BOTH, expand=True)

            ttk.Label(
                container,
                text=(
                    "Where should ripping/encoding happen?\n\n"
                    "If you don't change this, the default is: rip + encode on the server (remote)."
                ),
                justify=LEFT,
            ).pack(anchor="w")

            choice = StringVar(value=(self.var_exec_mode.get() or EXEC_MODE_REMOTE))
            options = ttk.Frame(container)
            options.pack(fill=X, pady=(10, 0))

            ttk.Radiobutton(
                options,
                text="Rip + encode on server (remote)",
                variable=choice,
                value=EXEC_MODE_REMOTE,
            ).pack(anchor="w")

            ttk.Radiobutton(
                options,
                text="Rip locally, encode on server (planned)",
                variable=choice,
                value=EXEC_MODE_LOCAL_RIP_ONLY,
            ).pack(anchor="w", pady=(4, 0))

            ttk.Radiobutton(
                options,
                text="Rip + encode locally, upload results (planned)",
                variable=choice,
                value=EXEC_MODE_LOCAL_RIP_ENCODE,
            ).pack(anchor="w", pady=(4, 0))

            btns = ttk.Frame(container)
            btns.pack(fill=X, pady=(12, 0))

            def _cancel() -> None:
                try:
                    win.destroy()
                except Exception:
                    pass

            def _ok() -> None:
                self.var_exec_mode.set(choice.get() or EXEC_MODE_REMOTE)
                try:
                    self._persist_state()
                except Exception:
                    pass
                _cancel()

            ttk.Button(btns, text="Cancel", command=_cancel).pack(side=RIGHT)
            ttk.Button(btns, text="OK", command=_ok).pack(side=RIGHT, padx=(0, 8))

            if modal:
                try:
                    win.grab_set()
                except Exception:
                    pass
                try:
                    self.root.wait_window(win)
                except Exception:
                    pass

        def _open_connection_settings(self, *, modal: bool = False, next_label: str = "Close") -> None:
            try:
                if self._connection_win is not None and self._connection_win.winfo_exists():
                    self._connection_win.lift()
                    return
            except Exception:
                self._connection_win = None

            self._connection_win = open_connection_settings_dialog(
                root=self.root,
                host_var=self.var_host,
                user_var=self.var_user,
                port_var=self.var_port,
                key_var=self.var_key,
                password_var=self.var_password,
                browse_key=self._browse_key,
                validate=self._validate,
                persist_state=self._persist_state,
                modal=modal,
                next_label=next_label,
            )

        def _validate_directories(self) -> None:
            movies = (self.var_movies_dir.get() or "").strip()
            series = (self.var_series_dir.get() or "").strip()
            books = (self.var_books_dir.get() or "").strip()
            music = (self.var_music_dir.get() or "").strip()
            if not movies:
                raise ValueError("Movies dir is required.")
            if not series:
                raise ValueError("Series dir is required.")
            if not books:
                raise ValueError("Books dir is required.")
            if not music:
                raise ValueError("Music dir is required.")

        def _open_directories_settings(self, *, modal: bool = False, next_label: str = "Close") -> None:
            try:
                if self._directories_win is not None and self._directories_win.winfo_exists():
                    self._directories_win.lift()
                    return
            except Exception:
                self._directories_win = None

            self._directories_win = open_directories_settings_dialog(
                root=self.root,
                movies_dir_var=self.var_movies_dir,
                series_dir_var=self.var_series_dir,
                books_dir_var=self.var_books_dir,
                music_dir_var=self.var_music_dir,
                validate_directories=self._validate_directories,
                persist_state=self._persist_state,
                modal=modal,
                next_label=next_label,
            )

        def _connection_ready(self) -> bool:
            host = (self.var_host.get() or "").strip()
            user = (self.var_user.get() or "").strip()
            keyfile = (self.var_key.get() or "").strip()
            password = (self.var_password.get() or "").strip()
            if not host:
                return False
            if not keyfile and not password:
                return False
            if password and not user:
                return False
            return True

        def _directories_ready(self) -> bool:
            return all(
                (v.get() or "").strip()
                for v in (self.var_movies_dir, self.var_series_dir, self.var_books_dir, self.var_music_dir)
            )

        def _is_setup_complete(self) -> bool:
            return self._connection_ready() and self._directories_ready()

        def _apply_setup_gate(self) -> None:
            ready = self._is_setup_complete()
            try:
                if hasattr(self, "btn_start"):
                    self.btn_start.configure(state=("normal" if (ready and not self.state.running) else "disabled"))
            except Exception:
                pass
            try:
                if hasattr(self, "btn_cleanup"):
                    self.btn_cleanup.configure(state=("normal" if (ready and not self.state.running) else "disabled"))
            except Exception:
                pass

        def run_setup_wizard(self, *, force: bool = False) -> None:
            self._run_setup_wizard(force=force)

        def _maybe_run_first_launch_setup(self) -> None:
            if getattr(self, "_closing", False):
                return
            # Run on first launch OR whenever required settings are missing.
            if self._is_setup_complete():
                self._apply_setup_gate()
                # If a remote job is still running (GUI was closed/crashed), offer to reattach.
                self._maybe_offer_reattach()
                return

            # If the user already has a state file, they might have intentionally left settings blank.
            # Still block Start/Cleanup, but don't force popups unless it's truly the first run.
            if self._state_file_existed and not self._connection_ready():
                self._apply_setup_gate()
                return

            self._run_setup_wizard(force=False)

            # After setup, offer to reattach if we can.
            self._maybe_offer_reattach()

        def _last_run_matches_current_connection(self) -> bool:
            host = (self.var_host.get() or "").strip()
            user = (self.var_user.get() or "").strip()
            port = (self.var_port.get() or "").strip() or "22"
            if not self.last_run_host or not self.last_run_port:
                return False
            if host != self.last_run_host:
                return False
            if (self.last_run_user or "").strip() != user:
                return False
            if (self.last_run_port or "").strip() != port:
                return False
            return True

        def _remote_file_exists(self, path: str) -> bool:
            ctx = self._get_run_ctx()
            if not path:
                return False
            code, _out = self._remote_run(
                ctx.target,
                ctx.port,
                ctx.keyfile,
                ctx.password,
                f"test -f {shlex.quote(path)}",
            )
            return code == 0

        def _clear_last_run_metadata(self) -> None:
            self.last_run_screen_name = ""
            self.last_run_log_path = ""
            self.last_run_remote_start_epoch = 0
            self.last_run_host = ""
            self.last_run_user = ""
            self.last_run_port = ""
            try:
                self._persist_state()
            except Exception:
                pass

        def _maybe_offer_reattach(self) -> None:
            if getattr(self, "_closing", False):
                return
            # Only offer if we're idle and have enough info to safely target the right host.
            if self.state.running or self.run_ctx is not None:
                return
            if not self._is_setup_complete():
                return
            if not (self.last_run_screen_name or "").strip():
                return
            if not self._last_run_matches_current_connection():
                return

            try:
                cfg = self._validate()
            except Exception:
                return

            # Quick remote check: does the screen session still exist?
            try:
                code, _out = self._remote_run(
                    cfg.target,
                    cfg.port,
                    cfg.keyfile,
                    cfg.password,
                    f"screen -S {shlex.quote(self.last_run_screen_name)} -Q select .",
                )
            except Exception:
                return

            if code != 0:
                # Stale metadata (job completed or server rebooted). Clear it.
                self._clear_last_run_metadata()
                return

            try:
                do_reattach = messagebox.askyesno(
                    "Reattach",
                    "A remote job appears to still be running.\n\n"
                    f"Screen session: {self.last_run_screen_name}\n\n"
                    "Would you like to reattach and resume following progress?",
                )
            except Exception:
                # Can occur if the app is closing/destroyed while a scheduled
                # callback tries to show the prompt.
                return
            if not do_reattach:
                return

            self._reattach_to_existing_run(cfg)

        def _reattach_to_existing_run(self, cfg: ConnectionInfo) -> None:
            # Rebuild run context from persisted metadata.
            self._clear_log()
            self._append_log("(Info) Reattaching to existing remote job...\n")

            self.run_ctx = RunContext(
                target=cfg.target,
                port=cfg.port,
                keyfile=cfg.keyfile,
                password=cfg.password,
                screen_name=self.last_run_screen_name,
                log_path=(self.last_run_log_path or ""),
                remote_start_epoch=int(self.last_run_remote_start_epoch or 0),
            )

            # If remote start epoch is missing, capture now to scope log lookup.
            if not self.run_ctx.remote_start_epoch:
                try:
                    code_ts, out_ts = self._remote_run(cfg.target, cfg.port, cfg.keyfile, cfg.password, "date +%s")
                    if code_ts == 0:
                        self.run_ctx.remote_start_epoch = max(0, int((out_ts or "").strip().splitlines()[-1]) - 60)
                except Exception:
                    self.run_ctx.remote_start_epoch = 0

            # Validate log path (it can rotate or differ). If missing, re-discover.
            if self.run_ctx.log_path:
                if not self._remote_file_exists(self.run_ctx.log_path):
                    self.run_ctx.log_path = ""

            if not self.run_ctx.log_path:
                self.run_ctx.log_path = self._find_latest_remote_log()

            # Persist refreshed metadata so a later crash still reattaches cleanly.
            self.last_run_host = (self.var_host.get() or "").strip()
            self.last_run_user = (self.var_user.get() or "").strip()
            self.last_run_port = (self.var_port.get() or "").strip() or "22"
            self.last_run_screen_name = self.run_ctx.screen_name
            self.last_run_log_path = self.run_ctx.log_path
            self.last_run_remote_start_epoch = int(self.run_ctx.remote_start_epoch or 0)
            try:
                self._persist_state()
            except Exception:
                pass

            self._append_log(f"(Info) Following remote log: {self.run_ctx.log_path}\n")

            # Reattach: tail from end to avoid re-reading huge logs.
            self._stop_requested.clear()
            self._done_emitted = False
            self._done_handled = False
            self._start_tail(from_start=False)

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

        def _run_setup_wizard(self, *, force: bool = False) -> None:
            # Block Start/Cleanup until both steps are complete.
            self._apply_setup_gate()

            if force:
                # Always show both steps once for testing.
                self._open_connection_settings(modal=True, next_label="Next")
                try:
                    if self._connection_win is not None:
                        self.root.wait_window(self._connection_win)
                except Exception:
                    pass

                self._open_directories_settings(modal=True, next_label="Finish")
                try:
                    if self._directories_win is not None:
                        self.root.wait_window(self._directories_win)
                except Exception:
                    pass
            else:
                while not self._connection_ready():
                    self._open_connection_settings(modal=True, next_label="Next")
                    try:
                        if self._connection_win is not None:
                            self.root.wait_window(self._connection_win)
                    except Exception:
                        break
                    if self._connection_ready():
                        break

                while self._connection_ready() and not self._directories_ready():
                    self._open_directories_settings(modal=True, next_label="Finish")
                    try:
                        if self._directories_win is not None:
                            self.root.wait_window(self._directories_win)
                    except Exception:
                        break
                    if self._directories_ready():
                        break

            self._apply_setup_gate()

        def _browse_csv(self) -> None:
            p = filedialog.askopenfilename(title="Select schedule CSV", filetypes=[("CSV", "*.csv"), ("All", "*")])
            if p:
                self.var_csv_path.set(p)
        def _validate_csv_schedule_file(self, path: Path) -> int:
            """Validate the entire CSV schedule before starting a run.

            Mirrors the server-side rules (4 columns, no embedded commas, strict year/disc/season).
            Returns the number of parsed rows.
            """

            text = path.read_text(errors="ignore")
            rows = 0
            for n, raw in enumerate(text.splitlines(), start=1):
                line = raw.rstrip("\r")
                if not line.strip():
                    continue
                if line.lstrip().startswith("#"):
                    continue
                if re.match(r"^\s*(movie|series)?\s*name\s*,\s*year\s*,", line, flags=re.I):
                    continue

                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 4 or any(p == "" for p in parts[:4]):
                    raise ValueError(
                        f"CSV parse error at line {n}: expected exactly 4 comma-separated columns (no embedded commas)\n"
                        f"Line: {line}"
                    )

                _name, year, third, disc_s = parts

                if not re.fullmatch(r"\d{4}", year):
                    raise ValueError(f"CSV validation error at line {n}: year must be 4 digits\nLine: {line}")
                if not disc_s.isdigit() or int(disc_s) < 1:
                    raise ValueError(f"CSV validation error at line {n}: disc must be an integer >= 1\nLine: {line}")

                v = third.strip().lower()
                is_bool = v in {"y", "n", "yes", "no", "true", "false"}
                if is_bool:
                    if v not in {"y", "n", "yes", "no", "true", "false"}:
                        raise ValueError(f"CSV validation error at line {n}: MultiDisc must be y/n\nLine: {line}")
                else:
                    if not third.isdigit() or int(third) < 1:
                        raise ValueError(
                            f"CSV validation error at line {n}: season must be an integer >= 1\nLine: {line}"
                        )

                rows += 1

            if rows < 1:
                raise ValueError(f"CSV schedule is empty: {path}")
            return rows

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
            self._trim_log(max_lines=100)
            self.log_text.see(END)

        def _trim_log(self, *, max_lines: int) -> None:
            """Keep the log textbox bounded to avoid long-run UI slowdowns."""

            try:
                end_index = self.log_text.index("end-1c")  # e.g. "123.0"
                line_count = int(str(end_index).split(".", 1)[0])
            except Exception:
                return

            extra = line_count - int(max_lines)
            if extra <= 0:
                return

            try:
                # Delete whole lines from the start; keep the most recent max_lines.
                self.log_text.delete("1.0", f"{extra + 1}.0")
            except Exception:
                return
            self.log_text.configure(state="disabled")

        def _clear_log(self) -> None:
            try:
                self.log_text.configure(state="normal")
                self.log_text.delete("1.0", END)
                self.log_text.configure(state="disabled")
            except Exception:
                pass

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
                    elif kind == "local_wait":
                        # Worker thread requests an operator prompt (disc swap / continue gate).
                        self._local_continue_event.clear()
                        self._local_waiting_for_continue = True
                        self.state.waiting_for_enter = True
                        self.var_step.set("Waiting for disc")
                        self.var_prompt.set(payload)
                        try:
                            self.btn_continue.configure(state="normal")
                        except Exception:
                            pass
                        try:
                            self.progress.configure(mode="indeterminate")
                            self.progress.start(10)
                        except Exception:
                            pass
                    elif kind == "start_remote_encode":
                        # Local rip/upload completed; start the remote encode pass.
                        self._local_waiting_for_continue = False
                        self._local_ripping_active = False
                        try:
                            self.btn_continue.configure(state="disabled")
                        except Exception:
                            pass
                        self.var_prompt.set("")

                        cfg = self._local_cfg
                        remote_script = self._local_remote_script
                        remote_csv = (payload or "").strip()
                        if cfg is None or not remote_script or not remote_csv:
                            self._on_done("Local mode internal error: missing remote start parameters.")
                        else:
                            try:
                                self._start_remote_job(cfg, remote_script, remote_csv, extra_args=["--no-disc-prompts"])
                            except Exception as e:
                                self._on_done(str(e))
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
                else:
                    pass
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

            # Prevent changing Connection/Directories mid-run.
            try:
                menu = getattr(self, "_settings_menu", None)
                if menu is not None:
                    state = "normal" if enabled else "disabled"
                    idx_conn = getattr(self, "_settings_menu_connection_idx", None)
                    idx_dirs = getattr(self, "_settings_menu_directories_idx", None)
                    idx_mode = getattr(self, "_settings_menu_exec_mode_idx", None)
                    if idx_conn is not None:
                        menu.entryconfigure(idx_conn, state=state)
                    if idx_dirs is not None:
                        menu.entryconfigure(idx_dirs, state=state)
                    if idx_mode is not None:
                        menu.entryconfigure(idx_mode, state=state)
            except Exception:
                pass

            # Note: 'Install Jellyfin if missing' is now menu-driven only.

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
            return fetch_handbrake_presets(
                self.remote,
                target=target,
                port=port,
                keyfile=keyfile,
                password=password,
            )

        def _parse_for_progress(self, text_chunk: str) -> None:
            parse_for_progress(self, text_chunk)

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
            return self.remote.remote_run(target, port, keyfile, password, cmd)

        def _get_run_ctx(self) -> RunContext:
            if self.run_ctx is None:
                raise RuntimeError("No active run context.")
            return self.run_ctx

        def _screen_exists(self) -> bool:
            if self.run_ctx is None or not self.run_ctx.screen_name:
                return False
            ctx = self.run_ctx
            code, _out = self._remote_run(
                ctx.target,
                ctx.port,
                ctx.keyfile,
                ctx.password,
                f"screen -S {shlex.quote(ctx.screen_name)} -Q select .",
            )
            return code == 0

        def _screen_stuff(self, payload: str) -> None:
            if self.run_ctx is None or not self.run_ctx.screen_name:
                return
            ctx = self.run_ctx
            # payload is a bash $'..' string like $'\n' or $'\003'
            cmd = f"screen -S {shlex.quote(ctx.screen_name)} -p 0 -X stuff {payload}"
            self._remote_run(ctx.target, ctx.port, ctx.keyfile, ctx.password, cmd)

        def _find_latest_remote_log(self) -> str:
            ctx = self._get_run_ctx()
            min_ts = int(ctx.remote_start_epoch or 0)
            cmd = (
                "for i in $(seq 1 50); do "
                "f=$(ls -t "
                "\"$HOME\"/.archive_helper_for_jellyfin/logs/rip_and_encode_*.log "
                "\"$HOME\"/rip_and_encode_*.log "
                "\"$HOME\"/.archive_helper_for_jellyfin/logs/rip_and_encode_v2_*.log "
                "\"$HOME\"/rip_and_encode_v2_*.log "
                "2>/dev/null | head -n1); "
                "if [ -n \"$f\" ]; then "
                "  mt=$(stat -c %Y \"$f\" 2>/dev/null || echo 0); "
                f"  if [ \"$mt\" -ge {min_ts} ]; then echo \"$f\"; exit 0; fi; "
                "fi; "
                "sleep 0.2; "
                "done; exit 1"
            )
            code, out = self._remote_run(ctx.target, ctx.port, ctx.keyfile, ctx.password, cmd)
            if code != 0:
                raise ValueError("Unable to locate remote log file after starting the job.")
            return (out or "").strip().splitlines()[-1].strip()

        def _start_tail(self, *, from_start: bool = True, tail_lines: int = 2000) -> None:
            tailer_start_tail(self, from_start=from_start, tail_lines=tail_lines)

        def _stop_tail(self, *, quiet: bool = False) -> None:
            tailer_stop_tail(self, quiet=quiet)

        def _on_done(self, payload: str) -> None:
            if self._done_handled:
                return
            self._done_handled = True

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

            # Clear local-mode transient state.
            try:
                self._local_ripping_active = False
                self._local_waiting_for_continue = False
                self._local_cfg = None
                self._local_remote_script = ""
                self._local_stop_requested.clear()
                self._local_continue_event.clear()
            except Exception:
                pass

            # Clear run context.
            self.run_ctx = None

            # Clear reattach metadata for completed/stopped runs.
            self._clear_last_run_metadata()

            if payload == "ok":
                do_cleanup = messagebox.askyesno(
                    "Complete",
                    "Processing complete.\n\n"
                    "Would you like to cleanup the leftover MKVs / temporary work folders now?\n\n"
                    "This will not delete your final MP4s in the configured Movies/Series directories.",
                )
                if do_cleanup:
                    self.cleanup_mkvs()
                return

            # Any non-ok terminal state: show message and clear the visible log so a restart
            # does not look/feel stuck on previous errors.
            if str(payload).strip().lower() == "stopped":
                messagebox.showinfo("Stopped", "Stopped.")
            else:
                messagebox.showerror("Stopped", payload)
            self._clear_log()

        def cleanup_mkvs(self) -> None:
            if self.state.running:
                messagebox.showerror("Error", "Cleanup is disabled while a job is running. Stop the job first.")
                return

            if not self._is_setup_complete():
                self._run_setup_wizard(force=False)
                if not self._is_setup_complete():
                    return

            try:
                cfg = self._validate()
                self._persist_state()

                # Ensure the remote script exists (bootstrap upload if needed).
                remote_script = self._ensure_remote_script(cfg.target, cfg.port, cfg.keyfile, cfg.remote_script)

                self._append_log("Starting MKV cleanup preview (dry run)...\n")
                preview_cmd = " ".join(
                    shlex.quote(p)
                    for p in [
                        "python3",
                        remote_script,
                        "--cleanup-mkvs",
                        "--dry-run",
                        "--movies-dir",
                        self.var_movies_dir.get().strip(),
                        "--series-dir",
                        self.var_series_dir.get().strip(),
                    ]
                )
                code, out = self._remote_run(cfg.target, cfg.port, cfg.keyfile, cfg.password, preview_cmd)
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
                    f"This will delete {candidates} work folder(s) from the remote host.\n\n"
                    "This deletes temporary MKVs and other per-title staging artifacts under the user's home directory.\n"
                    "It does not delete your final MP4s in the configured Movies/Series directories.\n\n"
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
                        "--movies-dir",
                        self.var_movies_dir.get().strip(),
                        "--series-dir",
                        self.var_series_dir.get().strip(),
                    ]
                )
                code2, out2 = self._remote_run(cfg.target, cfg.port, cfg.keyfile, cfg.password, run_cmd)
                if out2:
                    self._append_log(out2.rstrip() + "\n")
                if code2 != 0:
                    raise ValueError("Cleanup failed.")

                messagebox.showinfo("Cleanup", "Cleanup complete.")
            except Exception as e:
                messagebox.showerror("Error", str(e))

        def _validate(self) -> ConnectionInfo:
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

            return ConnectionInfo(
                target=target,
                remote_script=REMOTE_SCRIPT_RUN_PATH,
                port=self.var_port.get(),
                keyfile=keyfile,
                password=password,
            )

        def _connect_paramiko(self, target: str, port: str, keyfile: str, password: str):
            return self.remote.connect_paramiko(target, port, keyfile, password)

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
            return self.remote.exec_paramiko(client, command)

        def _sftp_put(self, client, local_path: str, remote_path: str) -> None:
            self.remote.sftp_put(client, local_path, remote_path)

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

            script_dir = Path(__file__).resolve().parent
            local_script = script_dir / "rip_and_encode.py"
            if not local_script.exists():
                # Backward compatibility if the script is still named with v2.
                local_script = script_dir / "rip_and_encode_v2.py"
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

            exec_mode = (self.var_exec_mode.get() or EXEC_MODE_REMOTE).strip() or EXEC_MODE_REMOTE
            if exec_mode == EXEC_MODE_LOCAL_RIP_ENCODE:
                messagebox.showinfo(
                    "Rip mode",
                    "Rip + encode locally is planned but not implemented yet.\n\n"
                    "For now, please use: Rip locally (encode on server) or Rip + encode on server (remote).",
                )
                return

            if not self._is_setup_complete():
                self._run_setup_wizard(force=False)
                if not self._is_setup_complete():
                    return

            try:
                # Fresh run: clear the visible log so prior MakeMKV/ERROR lines don't confuse recovery.
                self._clear_log()

                cfg = self._validate()
                self._persist_state()

                # Ensure the remote script exists (bootstrap upload if needed).
                remote_script = self._ensure_remote_script(cfg.target, cfg.port, cfg.keyfile, cfg.remote_script)

                # Build local CSV schedule (manual or selected CSV).
                local_csv = None
                if self.var_mode.get() == "csv":
                    p = self.var_csv_path.get().strip()
                    if not p:
                        raise ValueError("CSV file path is required.")
                    local_csv = Path(p)
                    if not local_csv.exists():
                        raise ValueError(f"CSV file not found: {local_csv}")
                    # Preflight validate the entire CSV now so we fail fast with a helpful line number.
                    try:
                        _rows = self._validate_csv_schedule_file(local_csv)
                    except Exception as e:
                        raise ValueError(
                            "CSV validation failed. Fix the CSV and try again.\n\n" + str(e)
                        )
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

                    rows = csv_rows_from_manual(
                        kind=kind,
                        name=title,
                        year=year,
                        season=self.var_season.get(),
                        start_disc=start_disc,
                        total_discs=total_discs,
                    )

                    tmp = Path(tempfile.gettempdir()) / f"rip_and_encode_gui_{int(time.time())}.csv"
                    write_csv_rows(tmp, rows)
                    local_csv = tmp

                    # Best-effort title counting for finalize progress.
                    self.state.total_titles = 1
                    self.state.finalized_titles = 0

                assert local_csv is not None

                if exec_mode == EXEC_MODE_LOCAL_RIP_ONLY:
                    schedule = load_csv_schedule(local_csv)
                    self._begin_local_rip_only(cfg, remote_script, local_csv, schedule)
                    return

                remote_csv = self._upload_schedule_to_remote(cfg, local_csv)
                self._start_remote_job(cfg, remote_script, remote_csv)

                return

            except Exception as e:
                messagebox.showerror("Error", str(e))

        def _upload_schedule_to_remote(self, cfg: ConnectionInfo, local_csv: Path) -> str:
            remote_csv = f"/tmp/rip_and_encode_schedule_{int(time.time())}.csv"
            self._append_log("Uploading schedule via SCP...\n")

            if cfg.password:
                client = self._connect_paramiko(cfg.target, cfg.port, cfg.keyfile, cfg.password)
                try:
                    self._sftp_put(client, str(local_csv), remote_csv)
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
                return remote_csv

            scp_args = self._scp_args(cfg.target, cfg.port, cfg.keyfile)
            scp_cmd = scp_args + [str(local_csv), f"{cfg.target}:{shlex.quote(remote_csv)}"]
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
                    f"Target: {cfg.target}\n"
                    f"Remote path: {remote_csv}\n\n"
                    + (detail if detail else "(No additional details.)")
                )
            return remote_csv

        def _start_remote_job(
            self,
            cfg: ConnectionInfo,
            remote_script: str,
            remote_csv: str,
            *,
            extra_args: list[str] | None = None,
        ) -> None:
            extra = list(extra_args or [])

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
                "--disc-type",
                (self.var_disc_type.get().strip() or "dvd"),
                "--preset",
                self.var_preset.get().strip(),
            ]

            if self.var_ensure_jellyfin.get():
                cmd_parts += ["--ensure-jellyfin"]

            # Defaults: always overlap with 1 encode job, and always keep MKVs (fail-safe).
            cmd_parts += ["--overlap", "--encode-jobs", "1", "--keep-mkvs"]
            cmd_parts += extra

            remote_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

            self._append_log("Starting remote job (screen)...\n")

            self.run_ctx = RunContext(
                target=cfg.target,
                port=cfg.port,
                keyfile=cfg.keyfile,
                password=cfg.password,
                screen_name=f"archive_helper_for_jellyfin_{int(time.time())}",
                log_path="",
                remote_start_epoch=0,
            )

            # Persist run metadata immediately so a GUI crash/power loss can reattach.
            self.last_run_host = (self.var_host.get() or "").strip()
            self.last_run_user = (self.var_user.get() or "").strip()
            self.last_run_port = (self.var_port.get() or "").strip() or "22"
            self.last_run_screen_name = self.run_ctx.screen_name
            self.last_run_log_path = ""
            self.last_run_remote_start_epoch = 0
            try:
                self._persist_state()
            except Exception:
                pass

            self._stop_requested.clear()
            self._done_emitted = False
            self._done_handled = False

            code, out = self._remote_run(
                cfg.target,
                cfg.port,
                cfg.keyfile,
                cfg.password,
                "command -v screen >/dev/null 2>&1",
            )
            if code != 0:
                raise ValueError("Remote host is missing 'screen'. Install it and try again.\n" + (out or "").strip())

            screen_cmd = (
                f"screen -S {shlex.quote(self.run_ctx.screen_name)} -dm "
                f"bash -lc {shlex.quote(remote_cmd)}"
            )

            # Capture remote time so we can pick the correct (new) log file for this run.
            try:
                code_ts, out_ts = self._remote_run(cfg.target, cfg.port, cfg.keyfile, cfg.password, "date +%s")
                if code_ts == 0:
                    self.run_ctx.remote_start_epoch = max(0, int((out_ts or "").strip().splitlines()[-1]) - 1)
            except Exception:
                self.run_ctx.remote_start_epoch = 0

            code, out = self._remote_run(cfg.target, cfg.port, cfg.keyfile, cfg.password, screen_cmd)
            if code != 0:
                raise ValueError("Failed to start remote job in screen: " + (out or "").strip())

            self.run_ctx.log_path = self._find_latest_remote_log()
            self.last_run_log_path = self.run_ctx.log_path
            self.last_run_remote_start_epoch = int(self.run_ctx.remote_start_epoch or 0)
            try:
                self._persist_state()
            except Exception:
                pass

            self._append_log(f"(Info) Following remote log: {self.run_ctx.log_path}\n")
            self._start_tail()

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
            self.state.eta_start_pct = 0.0
            self.state.eta_start_ts = 0.0
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

        def _begin_local_rip_only(
            self,
            cfg: ConnectionInfo,
            remote_script: str,
            local_csv: Path,
            schedule: list,
        ) -> None:
            if self.state.running:
                raise RuntimeError("Already running.")

            self._append_log("Local rip mode: rip locally, upload MKVs, encode on server.\n")
            self._local_cfg = cfg
            self._local_remote_script = remote_script
            self._local_stop_requested.clear()
            self._local_continue_event.clear()
            self._local_waiting_for_continue = False
            self._local_ripping_active = True

            # Treat local ripping as a run so Stop/inputs behave consistently.
            self.state.running = True
            self.state.waiting_for_enter = False
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

            self._local_thread = threading.Thread(
                target=self._local_rip_only_worker,
                args=(cfg, remote_script, local_csv, schedule),
                daemon=True,
            )
            self._local_thread.start()

        def _local_rip_only_worker(
            self,
            cfg: ConnectionInfo,
            remote_script: str,
            local_csv: Path,
            schedule: list,
        ) -> None:
            try:
                code_home, out_home = self._remote_run(cfg.target, cfg.port, cfg.keyfile, cfg.password, "echo $HOME")
                if code_home != 0:
                    raise RuntimeError("Unable to determine remote $HOME: " + (out_home or "").strip())
                remote_home = (out_home or "").strip().splitlines()[-1].strip()
                if not remote_home:
                    raise RuntimeError("Unable to determine remote $HOME.")

                for row in schedule:
                    if self._local_stop_requested.is_set():
                        self.ui_queue.put(("done", "Stopped"))
                        return

                    title_s = sanitize_title_for_dir(getattr(row, "name", ""))
                    year_s = str(getattr(row, "year", "")).strip()
                    disc_i = int(getattr(row, "disc", 0) or 0)
                    if not title_s or not year_s or disc_i < 1:
                        raise RuntimeError("Invalid schedule row encountered during local rip.")

                    prompt = csv_disc_prompt_for_row(row)
                    prompt += "\n\nClick Continue to start ripping locally."
                    self.ui_queue.put(("local_wait", prompt))

                    # Wait for Continue (or Stop).
                    while not self._local_stop_requested.is_set():
                        if self._local_continue_event.wait(0.2):
                            break
                    if self._local_stop_requested.is_set():
                        self.ui_queue.put(("done", "Stopped"))
                        return
                    self._local_continue_event.clear()

                    local_disc_dir = (
                        self._state_dir()
                        / "local_rips"
                        / f"{title_s} ({year_s})"
                        / "MKVs"
                        / f"Disc{disc_i:02d}"
                    )
                    local_disc_dir.mkdir(parents=True, exist_ok=True)

                    existing = sorted([p for p in local_disc_dir.rglob("*.mkv") if p.is_file()])
                    if existing:
                        self.ui_queue.put(("log", f"(Local) Resume: found existing MKVs in {local_disc_dir}; skipping rip.\n"))
                    else:
                        self.ui_queue.put(("log", f"(Local) Ripping to: {local_disc_dir}\n"))
                        self._run_local_makemkv(local_disc_dir)

                    mkvs = sorted([p for p in local_disc_dir.rglob("*.mkv") if p.is_file()])
                    if not mkvs:
                        raise RuntimeError(f"No MKVs found after local rip: {local_disc_dir}")

                    remote_work_dir = f"{remote_home}/{title_s} ({year_s})"
                    remote_mkv_root = f"{remote_work_dir}/MKVs"
                    self._ensure_remote_dir(cfg.target, cfg.port, cfg.keyfile, cfg.password, remote_mkv_root)

                    self.ui_queue.put(("log", f"(Local) Uploading MKVs for Disc{disc_i:02d}...\n"))
                    self._upload_dir_to_remote_mkv_root(cfg, local_disc_dir, remote_mkv_root)

                # Upload schedule and kick off encode-only pass (no disc prompts).
                remote_csv = self._upload_schedule_to_remote(cfg, local_csv)
                self.ui_queue.put(("start_remote_encode", remote_csv))
            except Exception as e:
                msg = (str(e) or "").strip()
                if msg.lower() == "stopped":
                    self.ui_queue.put(("done", "Stopped"))
                else:
                    self.ui_queue.put(("done", f"Local rip failed: {e}"))

        def _upload_dir_to_remote_mkv_root(self, cfg: ConnectionInfo, local_disc_dir: Path, remote_mkv_root: str) -> None:
            # Copy DiscNN directory into remote MKVs/ (so remote gets MKVs/DiscNN/*)
            if cfg.password:
                client = self._connect_paramiko(cfg.target, cfg.port, cfg.keyfile, cfg.password)
                try:
                    abs_root = self._remote_abs_path_paramiko(client, remote_mkv_root)
                    # Ensure MKVs root exists (Paramiko path does not expand '~').
                    code, out = self._exec_paramiko(client, "bash -lc " + shlex.quote("mkdir -p -- " + shlex.quote(abs_root)))
                    if code != 0:
                        raise ValueError("Failed to create remote MKVs dir: " + (out or "").strip())

                    disc_name = local_disc_dir.name
                    abs_disc_dir = f"{abs_root.rstrip('/')}/{disc_name}"
                    code2, out2 = self._exec_paramiko(
                        client,
                        "bash -lc " + shlex.quote("mkdir -p -- " + shlex.quote(abs_disc_dir)),
                    )
                    if code2 != 0:
                        raise ValueError("Failed to create remote disc dir: " + (out2 or "").strip())

                    sftp = client.open_sftp()
                    try:
                        for p in sorted(local_disc_dir.rglob("*")):
                            if p.is_dir():
                                rel = p.relative_to(local_disc_dir)
                                rdir = f"{abs_disc_dir}/{str(rel).replace(os.sep, '/') }"
                                try:
                                    sftp.mkdir(rdir)
                                except Exception:
                                    pass
                                continue
                            if not p.is_file():
                                continue

                            rel = p.relative_to(local_disc_dir)
                            rpath = f"{abs_disc_dir}/{str(rel).replace(os.sep, '/') }"
                            # Ensure parent directories exist.
                            parent = rpath.rsplit("/", 1)[0]
                            try:
                                sftp.mkdir(parent)
                            except Exception:
                                pass
                            sftp.put(str(p), rpath)
                    finally:
                        try:
                            sftp.close()
                        except Exception:
                            pass
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
                return

            scp_args = self._scp_args(cfg.target, cfg.port, cfg.keyfile)
            dest = f"{cfg.target}:{shlex.quote(remote_mkv_root)}"
            try:
                res = subprocess.run(
                    scp_args + ["-r", str(local_disc_dir), dest],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=True,
                )
                if res.stdout:
                    self.ui_queue.put(("log", res.stdout))
            except subprocess.CalledProcessError as e:
                detail = ((e.stdout or "").strip())
                raise ValueError(
                    "Failed to upload MKVs to the remote host.\n\n"
                    f"Target: {cfg.target}\n"
                    f"Remote dir: {remote_mkv_root}\n\n"
                    + (detail if detail else "(No additional details.)")
                )

        def _run_local_makemkv(self, out_dir: Path, *, cache_mb: int = 128) -> None:
            out_dir.mkdir(parents=True, exist_ok=True)

            exe = None
            for cand in ["makemkvcon", "makemkvcon64", "makemkvcon.exe", "makemkvcon64.exe"]:
                exe = shutil.which(cand)
                if exe:
                    break
            if not exe:
                raise RuntimeError("MakeMKV not found. Install MakeMKV and ensure 'makemkvcon' is on PATH.")

            try:
                cache_mb = int(cache_mb)
            except Exception:
                cache_mb = 128
            cache_mb = max(16, min(8192, cache_mb))

            argv: list[str] = []
            if os.name != "nt" and shutil.which("stdbuf"):
                argv += ["stdbuf", "-oL", "-eL"]
            argv += [
                exe,
                "mkv",
                "--progress=-stdout",
                "--decrypt",
                f"--cache={cache_mb}",
                "--minlength=300",
                "disc:0",
                "all",
                str(out_dir),
            ]

            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            self._local_proc = proc
            assert proc.stdout is not None

            prgv_re = re.compile(r"^PRGV:(.*)$")
            last_emit = 0.0
            last_pct = -1.0
            try:
                for line in proc.stdout:
                    if self._local_stop_requested.is_set():
                        break
                    s = line.rstrip("\n")
                    m = prgv_re.match(s.strip())
                    if m:
                        parts = re.split(r"[, ]+", m.group(1).strip())
                        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) > 0:
                            pct = (int(parts[0]) / int(parts[1])) * 100.0
                            now = time.time()
                            if pct - last_pct >= 0.3 and (now - last_emit) >= 0.5:
                                last_pct = pct
                                last_emit = now
                                self.ui_queue.put(("log", f"MakeMKV progress: {pct:5.1f}%\n"))
                        continue

                    if s.startswith("PRGC:") or s.startswith("PRGT:"):
                        continue
                    self.ui_queue.put(("log", s + "\n"))
            finally:
                self._local_proc = None

            if self._local_stop_requested.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                raise RuntimeError("Stopped")

            code = proc.wait()
            if code != 0:
                raise RuntimeError(f"MakeMKV failed with exit code {code}")

        def _reader_loop(self) -> None:
            tailer_reader_loop(self)

        def send_enter(self) -> None:
            try:
                # Local mode: Continue acts as a gate between discs.
                if self._local_waiting_for_continue and self.state.running:
                    self._local_continue_event.set()
                    self._local_waiting_for_continue = False
                    self.state.waiting_for_enter = False
                    self.btn_continue.configure(state="disabled")
                    self.var_prompt.set("")
                    self.var_eta.set("")
                    self.var_step.set("Running")
                    return

                if self.run_ctx is not None and self.run_ctx.screen_name:
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
            self.state.eta_start_pct = 0.0
            self.state.eta_start_ts = 0.0
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
                self.state.eta_start_pct = pct
                self.state.eta_start_ts = now
                self.state.eta_last_pct = pct
                self.state.eta_last_ts = now
                return

            if self.state.eta_last_ts <= 0.0:
                self.state.eta_start_pct = pct
                self.state.eta_start_ts = now
                self.state.eta_last_pct = pct
                self.state.eta_last_ts = now
                return

            if self.state.eta_start_ts <= 0.0:
                self.state.eta_start_pct = self.state.eta_last_pct
                self.state.eta_start_ts = self.state.eta_last_ts

            dt = now - self.state.eta_last_ts
            if dt <= 0.4:
                return

            dp = pct - self.state.eta_last_pct
            # If progress isn't moving (or we only got a "current" update while total is
            # unchanged), gently decay the rate so the ETA doesn't get stuck optimistic.
            if dp <= 0.0:
                if self.state.eta_rate_ewma > 0.0:
                    # Very mild decay (~0.1% per second) so a stall slowly increases ETA.
                    decay = 0.999 ** max(0.0, dt)
                    self.state.eta_rate_ewma *= decay

                self.state.eta_last_ts = now
                self.state.eta_last_pct = pct

                # Still display an ETA if we have a prior rate estimate.
                rate_used = self.state.eta_rate_ewma
                if rate_used <= 0.01:
                    return

                remaining_pct = max(0.0, 100.0 - pct)
                eta_s = remaining_pct / rate_used
                if eta_s <= 30.0:
                    self.var_eta.set("ETA <1m")
                    return
                if eta_s > (12 * 60 * 60):
                    return

                mins = int(eta_s // 60)
                secs = int(eta_s % 60)
                if mins >= 60:
                    hrs = mins // 60
                    mins = mins % 60
                    self.var_eta.set(f"ETA {hrs}h {mins:02d}m")
                else:
                    self.var_eta.set(f"ETA {mins}m {secs:02d}s")
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

            # Blend EWMA with average-rate since phase start for stability.
            avg_rate = 0.0
            total_dt = now - self.state.eta_start_ts
            total_dp = pct - self.state.eta_start_pct
            if total_dt >= 5.0 and total_dp > 0.5:
                avg_rate = total_dp / total_dt

            rate_used = self.state.eta_rate_ewma
            if avg_rate > 0.0 and rate_used > 0.0:
                rate_used = 0.55 * rate_used + 0.45 * avg_rate
            elif avg_rate > 0.0:
                rate_used = avg_rate

            if rate_used <= 0.01:
                return

            remaining_pct = max(0.0, 100.0 - pct)
            eta_s = remaining_pct / rate_used

            # Avoid clearing the ETA on transient estimates; clamp instead.
            if eta_s <= 30.0:
                self.var_eta.set("ETA <1m")
                return
            if eta_s > (12 * 60 * 60):
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

            # Local rip-only phase (before remote encode starts).
            if self._local_ripping_active and self.run_ctx is None and self.state.running:
                self._local_stop_requested.set()
                # Unblock any pending Continue wait.
                try:
                    self._local_continue_event.set()
                except Exception:
                    pass
                try:
                    if self._local_proc is not None and self._local_proc.poll() is None:
                        self._local_proc.terminate()
                except Exception:
                    pass
                self._local_ripping_active = False
                self._on_done("Stopped")
                return

            # If we're not running, still reset UI to a known-good idle state.
            if not self.state.running:
                self._stop_requested.set()
                try:
                    self._stop_tail(quiet=True)
                except Exception:
                    pass
                self._on_done("Stopped")
                return

            self._stop_requested.set()

            # Stop remote job if we launched it in screen.
            if self.run_ctx is not None and self.run_ctx.screen_name:
                try:
                    self._screen_stuff("$'\\003'")
                    time.sleep(0.2)
                    ctx = self.run_ctx
                    self._remote_run(ctx.target, ctx.port, ctx.keyfile, ctx.password, f"screen -S {shlex.quote(ctx.screen_name)} -X quit")
                except Exception:
                    pass
                try:
                    self._stop_tail(quiet=True)
                except Exception:
                    pass

                # Ensure the UI transitions back to idle even if the reader thread never emits "done".
                self._on_done("Stopped")
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
                self._on_done("Stopped")
                return

            if not self.proc:
                self._on_done("Stopped")
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

            self._on_done("Stopped")

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
    parser.add_argument(
        "--force-setup",
        dest="force_setup",
        action="store_true",
        help="Force the first-launch Connection→Directories setup wizard on startup (testing).",
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
        if args.force_setup:
            root.after(0, lambda: gui.run_setup_wizard(force=True))
        if args.replay_log:
            gui.start_replay(args.replay_log)
        root.mainloop()
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
