from __future__ import annotations

from typing import Callable, Optional


def open_connection_settings_dialog(
    *,
    root,
    host_var,
    user_var,
    port_var,
    key_var,
    password_var,
    browse_key: Callable[[], None],
    validate: Callable[[], object],
    persist_state: Callable[[], None],
    modal: bool = False,
    next_label: str = "Close",
) -> "object":
    import tkinter as tk
    from tkinter import BOTH, LEFT, X, messagebox, ttk

    from .tooltip import Tooltip

    win = tk.Toplevel(root)
    win.title("Settings: Connection")
    win.resizable(False, False)

    if modal:
        try:
            win.transient(root)
            win.grab_set()
        except Exception:
            pass

    frm = ttk.Frame(win, padding=10)
    frm.pack(fill=BOTH, expand=True)

    conn = ttk.LabelFrame(frm, text="Connection (SSH)", padding=10)
    conn.pack(fill=X)

    row = ttk.Frame(conn)
    row.pack(fill=X)
    ttk.Label(row, text="Host:").pack(side=LEFT)
    ent_host = ttk.Entry(row, textvariable=host_var, width=28)
    ent_host.pack(side=LEFT, padx=5)
    Tooltip(ent_host, "SSH host or IP address of the server.")
    ttk.Label(row, text="User:").pack(side=LEFT)
    ent_user = ttk.Entry(row, textvariable=user_var, width=16)
    ent_user.pack(side=LEFT, padx=5)
    Tooltip(ent_user, "SSH username on the server (example: jellyfin).")
    ttk.Label(row, text="Port:").pack(side=LEFT)
    ent_port = ttk.Entry(row, textvariable=port_var, width=6)
    ent_port.pack(side=LEFT, padx=5)
    Tooltip(ent_port, "SSH port (leave blank for default 22).")

    row2 = ttk.Frame(conn)
    row2.pack(fill=X, pady=(6, 0))
    ttk.Label(row2, text="Key file (optional):").pack(side=LEFT)
    ent_key = ttk.Entry(row2, textvariable=key_var, width=40)
    ent_key.pack(side=LEFT, padx=5)
    Tooltip(ent_key, "Optional: path to an SSH private key. If empty, password auth is used.")
    btn_key = ttk.Button(row2, text="Browse", command=browse_key)
    btn_key.pack(side=LEFT)
    Tooltip(btn_key, "Pick an SSH private key file.")

    row2b = ttk.Frame(conn)
    row2b.pack(fill=X, pady=(6, 0))
    ttk.Label(row2b, text="Password (required if no key):").pack(side=LEFT)
    ent_pw = ttk.Entry(row2b, textvariable=password_var, width=40, show="*")
    ent_pw.pack(side=LEFT, padx=5)
    Tooltip(ent_pw, "SSH password (required if you are not using a key file).")

    btns = ttk.Frame(frm)
    btns.pack(fill=X, pady=(10, 0))

    def _close() -> None:
        try:
            if modal:
                validate()
            persist_state()
        except Exception as e:
            if modal:
                messagebox.showerror("Connection", str(e))
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
        ent_host.focus_set()
    except Exception:
        pass

    return win
