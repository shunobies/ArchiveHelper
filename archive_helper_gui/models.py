from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UiState:
    running: bool = False
    waiting_for_enter: bool = False
    total_titles: int = 0
    finalized_titles: int = 0
    makemkv_phase: str = ""  # "analyze" | "process" | ""
    last_makemkv_total_pct: float = 0.0
    encode_queued: int = 0
    encode_started: int = 0
    encode_finished: int = 0
    encode_active_label: str = ""

    eta_phase: str = ""  # e.g., "makemkv" | "handbrake" | ""
    eta_last_pct: float = 0.0
    eta_last_ts: float = 0.0
    eta_rate_ewma: float = 0.0  # pct per second

    run_started_ts: float = 0.0


@dataclass(frozen=True)
class ConnectionInfo:
    target: str
    remote_script: str
    port: str
    keyfile: str
    password: str


@dataclass
class RunContext:
    target: str = ""
    port: str = ""
    keyfile: str = ""
    password: str = ""
    screen_name: str = ""
    log_path: str = ""
    remote_start_epoch: int = 0
