import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_helper_gui.parser import parse_for_progress


class _Var:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _Button:
    def __init__(self) -> None:
        self.state = "disabled"

    def configure(self, **kwargs) -> None:
        if "state" in kwargs:
            self.state = kwargs["state"]


class _Progress:
    def __init__(self) -> None:
        self.mode = None
        self.value = 0

    def configure(self, **kwargs) -> None:
        if "mode" in kwargs:
            self.mode = kwargs["mode"]

    def start(self, *_args, **_kwargs) -> None:
        return

    def stop(self) -> None:
        return

    def __setitem__(self, key, value):
        if key == "value":
            self.value = value


class _Gui:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            waiting_for_enter=False,
            next_disc_prompt="",
            last_makemkv_total_pct=0.0,
            makemkv_phase="",
            disc_total_selected_titles=0,
            disc_completed_titles=0,
            disc_failed_titles=0,
            current_disc_id="",
            encode_queued=0,
            encode_started=0,
            encode_finished=0,
            encode_active_label="",
            finalized_titles=0,
            total_titles=0,
        )
        self.var_step = _Var()
        self.var_prompt = _Var()
        self.var_eta = _Var()
        self.btn_continue = _Button()
        self.progress = _Progress()
        self.ui_queue = SimpleNamespace(put=lambda _x: None)
        self._done_emitted = False

    def _eta_reset(self, *_args, **_kwargs) -> None:
        return

    def _eta_update(self, *_args, **_kwargs) -> None:
        return


def test_disc_prompt_legacy_insert_format_enables_continue() -> None:
    gui = _Gui()

    parse_for_progress(gui, "Insert: Movie 'X (2000)' Disc 1. Press Enter when ready.")

    assert gui.state.waiting_for_enter is True
    assert gui.btn_continue.state == "normal"
    assert gui.var_prompt.value.startswith("Please insert:")
    assert "Click Continue (or press Enter)" in gui.var_prompt.value


def test_disc_prompt_v2_insert_disc_format_enables_continue() -> None:
    gui = _Gui()

    parse_for_progress(gui, "Insert disc 1 (disc-1) now (then press Enter)")

    assert gui.state.waiting_for_enter is True
    assert gui.btn_continue.state == "normal"
    assert "Insert disc 1 (disc-1) now" in gui.var_prompt.value
    assert "Click Continue (or press Enter)" in gui.var_prompt.value
