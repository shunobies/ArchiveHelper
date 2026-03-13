from __future__ import annotations

from typing import Any


class ConnectionRuntimeController:
    def __init__(self, gui: Any) -> None:
        self.gui = gui

    def validate(self):
        return self.gui._validate_impl()
