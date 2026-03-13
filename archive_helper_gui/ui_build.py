from __future__ import annotations

from typing import Any


class UiBuildController:
    """UI-oriented helpers kept separate from the main RipGui facade."""

    def __init__(self, gui: Any) -> None:
        self.gui = gui

    def apply_setup_gate(self) -> None:
        g = self.gui
        ready = g._connection_ready() and g._directories_ready()
        if hasattr(g, "btn_start"):
            g.btn_start.configure(state=("normal" if ready and not g.state.running else "disabled"))
        if hasattr(g, "lbl_setup_status"):
            g.lbl_setup_status.configure(
                text=("Setup complete" if ready else "Setup required: open Settings")
            )

    def is_setup_complete(self) -> bool:
        return self.gui._connection_ready() and self.gui._directories_ready()
