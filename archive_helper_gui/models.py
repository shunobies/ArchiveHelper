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

    # Most recent disc prompt (used so CSV mode can show which disc is next even
    # when the script prints a generic "next disc" message afterward).
    next_disc_prompt: str = ""

    eta_phase: str = ""  # e.g., "makemkv" | "handbrake" | ""
    # Baseline used for average-rate ETA smoothing within a phase.
    eta_start_pct: float = 0.0
    eta_start_ts: float = 0.0
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
