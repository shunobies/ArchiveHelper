from __future__ import annotations

from types import SimpleNamespace

import pytest

import rip_and_encode_gui as gui_mod


@pytest.mark.skipif(not gui_mod.TK_AVAILABLE, reason="Tk not available")
def test_setup_gate_delegation() -> None:
    gui = object.__new__(gui_mod.RipGui)
    called = {"gate": False, "complete": False}

    gui.ui_build = SimpleNamespace(
        apply_setup_gate=lambda: called.__setitem__("gate", True),
        is_setup_complete=lambda: called.__setitem__("complete", True) or True,
    )

    gui._apply_setup_gate()
    assert called["gate"] is True
    assert gui._is_setup_complete() is True
    assert called["complete"] is True


@pytest.mark.skipif(not gui_mod.TK_AVAILABLE, reason="Tk not available")
def test_schedule_building_delegation_for_manual_and_csv_paths() -> None:
    gui = object.__new__(gui_mod.RipGui)
    sentinel = ["row"]
    gui.scan_tmdb = SimpleNamespace(build_v2_schedule_from_panel=lambda: sentinel)

    assert gui._build_v2_schedule_from_panel() == sentinel


@pytest.mark.skipif(not gui_mod.TK_AVAILABLE, reason="Tk not available")
def test_start_stop_and_log_toggle_delegation() -> None:
    gui = object.__new__(gui_mod.RipGui)
    calls: list[str] = []
    gui.run_orchestration = SimpleNamespace(
        start=lambda: calls.append("start"),
        stop=lambda: calls.append("stop"),
        start_remote_job=lambda *a, **k: "remote",
        start_replay=lambda p: calls.append(f"replay:{p}"),
    )
    gui.logging_progress = SimpleNamespace(
        toggle_log=lambda: calls.append("toggle"),
        set_log_visible=lambda v: calls.append(f"visible:{v}"),
        append_log=lambda l: calls.append(f"log:{l}"),
        trim_log=lambda **kw: calls.append(f"trim:{kw['max_lines']}"),
        poll_ui_queue=lambda: calls.append("poll"),
        parse_for_progress=lambda t: calls.append(f"progress:{t}"),
    )

    gui.start()
    gui.stop()
    gui.toggle_log()
    gui._set_log_visible(True)
    gui._append_log("line")
    gui._trim_log(max_lines=20)
    gui._poll_ui_queue()
    gui._parse_for_progress("chunk")
    assert gui._start_remote_job("x") == "remote"
    gui.start_replay("demo.log")

    assert calls == [
        "start",
        "stop",
        "toggle",
        "visible:True",
        "log:line",
        "trim:20",
        "poll",
        "progress:chunk",
        "replay:demo.log",
    ]
