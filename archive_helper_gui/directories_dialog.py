from __future__ import annotations

from typing import Callable


def open_directories_settings_dialog(
    *,
    root,
    movies_dir_var,
    series_dir_var,
    books_dir_var,
    music_dir_var,
    local_dest_var,
    validate_directories: Callable[[], object],
    persist_state: Callable[[], None],
    modal: bool = False,
    next_label: str = "Close",
) -> "object":
    import tkinter as tk
    from tkinter import BOTH, LEFT, X, filedialog, messagebox, ttk

    from .tooltip import Tooltip

    win = tk.Toplevel(root)
    win.title("Settings: Directories")
    win.resizable(False, False)

    if modal:
        try:
            win.transient(root)
            win.grab_set()
        except Exception:
            pass

    frm = ttk.Frame(win, padding=10)
    frm.pack(fill=BOTH, expand=True)

    dirs = ttk.LabelFrame(frm, text="Directories", padding=10)
    dirs.pack(fill=X)

    r1 = ttk.Frame(dirs)
    r1.pack(fill=X)
    ttk.Label(r1, text="Movies dir:").pack(side=LEFT)
    ent_movies = ttk.Entry(r1, textvariable=movies_dir_var, width=40)
    ent_movies.pack(side=LEFT, padx=5)
    Tooltip(ent_movies, "Output folder on the server for movies (example: /storage/Movies).")

    r2 = ttk.Frame(dirs)
    r2.pack(fill=X, pady=(6, 0))
    ttk.Label(r2, text="Series dir:").pack(side=LEFT)
    ent_series = ttk.Entry(r2, textvariable=series_dir_var, width=40)
    ent_series.pack(side=LEFT, padx=5)
    Tooltip(ent_series, "Output folder on the server for series (example: /storage/Series).")

    r5 = ttk.Frame(dirs)
    r5.pack(fill=X, pady=(6, 0))
    ttk.Label(r5, text="Local destination:").pack(side=LEFT)
    ent_local = ttk.Entry(r5, textvariable=local_dest_var, width=45)
    ent_local.pack(side=LEFT, padx=5)
    Tooltip(
        ent_local,
        "Local staging directory for local rip modes (Rip locally...).\n"
        "The app will create per-title subfolders here.",
    )

    def _browse_local() -> None:
        try:
            p = filedialog.askdirectory(title="Select local destination")
        except Exception:
            p = ""
        if p:
            try:
                local_dest_var.set(p)
            except Exception:
                pass

    btn_local = ttk.Button(r5, text="Browse", command=_browse_local)
    btn_local.pack(side=LEFT)
    Tooltip(btn_local, "Pick a local folder used as staging for local rip modes.")

    r3 = ttk.Frame(dirs)
    r3.pack(fill=X, pady=(6, 0))
    ttk.Label(r3, text="Books dir:").pack(side=LEFT)
    ent_books = ttk.Entry(r3, textvariable=books_dir_var, width=40)
    ent_books.pack(side=LEFT, padx=5)
    Tooltip(ent_books, "(Future) Output folder on the server for books (example: /storage/Books).")

    r4 = ttk.Frame(dirs)
    r4.pack(fill=X, pady=(6, 0))
    ttk.Label(r4, text="Music dir:").pack(side=LEFT)
    ent_music = ttk.Entry(r4, textvariable=music_dir_var, width=40)
    ent_music.pack(side=LEFT, padx=5)
    Tooltip(ent_music, "(Future) Output folder on the server for music (example: /storage/Music).")

    btns = ttk.Frame(frm)
    btns.pack(fill=X, pady=(10, 0))

    def _close() -> None:
        try:
            if modal:
                validate_directories()
            persist_state()
        except Exception as e:
            if modal:
                messagebox.showerror("Directories", str(e))
                return
        try:
            win.destroy()
        except Exception:
            pass

    try:
        win.protocol("WM_DELETE_WINDOW", _close)
    except Exception:
        pass

    ttk.Button(btns, text=next_label, command=_close).pack(side=tk.RIGHT)

    try:
        ent_movies.focus_set()
    except Exception:
        pass

    return win
