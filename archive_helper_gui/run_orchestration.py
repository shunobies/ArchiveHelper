from __future__ import annotations

from typing import Any


class RunOrchestrationController:
    def __init__(self, gui: Any) -> None:
        self.gui = gui

    def start(self) -> None:
        self.gui.start_impl()

    def stop(self) -> None:
        self.gui.stop_impl()

    def start_replay(self, log_path: str) -> None:
        self.gui.start_replay_impl(log_path)

    def start_remote_job(self, *args: Any, **kwargs: Any) -> Any:
        return self.gui._start_remote_job_impl(*args, **kwargs)
