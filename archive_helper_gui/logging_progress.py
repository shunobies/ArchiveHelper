from __future__ import annotations

from typing import Any


class LoggingProgressController:
    def __init__(self, gui: Any) -> None:
        self.gui = gui

    def toggle_log(self) -> None:
        self.gui._toggle_log_impl()

    def set_log_visible(self, visible: bool) -> None:
        self.gui._set_log_visible_impl(visible)

    def append_log(self, line: str) -> None:
        self.gui._append_log_impl(line)

    def trim_log(self, *, max_lines: int) -> None:
        self.gui._trim_log_impl(max_lines=max_lines)

    def poll_ui_queue(self) -> None:
        self.gui._poll_ui_queue_impl()

    def parse_for_progress(self, text_chunk: str) -> None:
        self.gui._parse_for_progress_impl(text_chunk)
