from __future__ import annotations

from pathlib import Path
from typing import Any


class StatePersistenceController:
    def __init__(self, gui: Any) -> None:
        self.gui = gui

    def state_dir(self) -> Path:
        return self.gui.persistence.state_dir

    def load(self) -> None:
        self.gui._load_persisted_state_impl()

    def persist(self) -> None:
        self.gui._persist_state_impl()
