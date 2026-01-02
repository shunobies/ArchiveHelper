from __future__ import annotations


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
            ttk = __import__("tkinter.ttk").ttk
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
