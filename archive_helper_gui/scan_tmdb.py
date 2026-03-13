from __future__ import annotations

from typing import Any


class ScanTmdbController:
    def __init__(self, gui: Any) -> None:
        self.gui = gui

    def build_v2_schedule_from_panel(self):
        return self.gui._build_v2_schedule_from_panel_impl()
