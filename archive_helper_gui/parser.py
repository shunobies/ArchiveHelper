from __future__ import annotations

import re

from archive_helper_gui.log_patterns import (
    ERROR_RE,
    FINALIZING_RE,
    HB_DONE_RE,
    HB_PROGRESS_RE,
    HB_START_RE,
    HB_TASK_RE,
    MAKEMKV_ACCESS_ERROR_RE,
    MAKEMKV_ACTION_RE,
    MAKEMKV_CURRENT_PROGRESS_RE,
    MAKEMKV_OPERATION_RE,
    MAKEMKV_TOTAL_PROGRESS_RE,
    MAKE_MKV_PROGRESS_RE,
    PROMPT_INSERT_RE,
    PROMPT_LOW_DISK_RE,
    PROMPT_NEXT_DISC_RE,
)


def parse_for_progress(gui, text_chunk: str) -> None:
    """Parse a log line and update GUI progress/UI state.

    This intentionally takes the GUI object so we can keep behavior identical while
    moving the large parsing state machine out of the entrypoint.
    """

    line = (text_chunk or "").rstrip("\n")

    def _format_disc_prompt(raw: str) -> str:
        shown = (raw or "").strip()
        if shown.lower().startswith("next up:"):
            shown = shown.split(":", 1)[1].strip()
        if shown.startswith("Insert:"):
            shown = "Please " + shown[0].lower() + shown[1:]
        shown = re.sub(r"\bpress\s+enter\b", "Click Continue (or press Enter)", shown, flags=re.I)
        return shown

    # MakeMKV raw status lines
    m = MAKEMKV_OPERATION_RE.match(line)
    if m:
        op = m.group(1).strip()
        if gui.state.waiting_for_enter:
            gui.state.waiting_for_enter = False
            gui.var_prompt.set("")
            gui.btn_continue.configure(state="disabled")
            gui.state.next_disc_prompt = ""

        if re.search(r"analy", op, flags=re.IGNORECASE):
            gui.state.makemkv_phase = "analyze"
            gui.var_step.set("Analyzing (MakeMKV): " + op)
        else:
            gui.state.makemkv_phase = "process"
            gui.var_step.set("Ripping (MakeMKV): " + op)

        gui._eta_reset("makemkv")
        gui.progress.configure(mode="indeterminate")
        gui.progress.start(10)
        return

    m = MAKEMKV_ACTION_RE.match(line)
    if m:
        act = m.group(1).strip()
        if gui.state.waiting_for_enter:
            gui.state.waiting_for_enter = False
            gui.var_prompt.set("")
            gui.btn_continue.configure(state="disabled")
            gui.state.next_disc_prompt = ""

        if re.search(r"analy", act, flags=re.IGNORECASE):
            gui.state.makemkv_phase = "analyze"
            gui.var_step.set("Analyzing (MakeMKV): " + act)
        else:
            gui.state.makemkv_phase = "process"
            gui.var_step.set("Ripping (MakeMKV): " + act)

        gui._eta_reset("makemkv")
        gui.progress.configure(mode="indeterminate")
        gui.progress.start(10)
        return

    m = MAKEMKV_TOTAL_PROGRESS_RE.search(line)
    if m:
        try:
            pct = float(m.group(1))
        except ValueError:
            pct = 0.0
        if gui.state.waiting_for_enter:
            gui.state.waiting_for_enter = False
            gui.var_prompt.set("")
            gui.btn_continue.configure(state="disabled")
            gui.state.next_disc_prompt = ""

        gui.state.last_makemkv_total_pct = pct
        phase = gui.state.makemkv_phase or "process"
        gui.var_step.set("Analyzing (MakeMKV)" if phase == "analyze" else "Ripping (MakeMKV)")
        gui.progress.configure(mode="determinate")
        gui.progress.stop()
        gui.progress["value"] = max(0.0, min(100.0, pct))

        gui._eta_update("makemkv", pct)
        return

    m = MAKEMKV_CURRENT_PROGRESS_RE.search(line)
    if m:
        try:
            pct = float(m.group(1))
        except ValueError:
            pct = 0.0
        if gui.state.waiting_for_enter:
            gui.state.waiting_for_enter = False
            gui.var_prompt.set("")
            gui.btn_continue.configure(state="disabled")
            gui.state.next_disc_prompt = ""

        phase = gui.state.makemkv_phase or "process"
        gui.var_step.set("Analyzing (MakeMKV)" if phase == "analyze" else "Ripping (MakeMKV)")
        if gui.state.last_makemkv_total_pct > 0.0:
            gui.progress.configure(mode="determinate")
            gui.progress.stop()
            gui.progress["value"] = max(0.0, min(100.0, gui.state.last_makemkv_total_pct))

            gui._eta_update("makemkv", gui.state.last_makemkv_total_pct)
        else:
            gui.progress.configure(mode="determinate")
            gui.progress.stop()
            gui.progress["value"] = max(0.0, min(100.0, pct))

            gui._eta_update("makemkv", pct)
        return

    if MAKEMKV_ACCESS_ERROR_RE.search(line):
        gui.var_step.set("Error")
        gui.progress.stop()
        gui.progress.configure(mode="indeterminate")
        return

    # HandBrake task markers
    m = HB_TASK_RE.match(line)
    if m:
        task = m.group(1).strip()
        gui.var_step.set("HandBrake: " + task)
        gui._eta_reset("handbrake")
        gui.progress.configure(mode="indeterminate")
        gui.progress.start(10)
        return

    m = HB_START_RE.match(line)
    if m:
        gui.state.encode_started = int(m.group(1))
        # Use the server-reported total (authoritative) to avoid UI drift.
        gui.state.encode_queued = int(m.group(2))
        gui.state.encode_active_label = m.group(3).strip()
        gui.var_step.set(f"Encoding (HandBrake) {gui.state.encode_started} of {max(1, gui.state.encode_queued)}")
        gui._eta_reset("handbrake")
        gui.progress.configure(mode="indeterminate")
        gui.progress.start(10)
        return

    m = HB_DONE_RE.match(line)
    if m:
        gui.state.encode_finished = int(m.group(1))
        # Use the server-reported total (authoritative) to avoid UI drift.
        gui.state.encode_queued = int(m.group(2))
        gui.var_step.set(
            f"Encoding (HandBrake) {min(gui.state.encode_finished, gui.state.encode_queued)} of {max(1, gui.state.encode_queued)}"
        )
        gui._eta_reset("handbrake")
        gui.progress.configure(mode="indeterminate")
        gui.progress.start(10)
        return

    m = MAKE_MKV_PROGRESS_RE.search(line)
    if m:
        if gui.state.waiting_for_enter:
            gui.state.waiting_for_enter = False
            gui.var_prompt.set("")
            gui.btn_continue.configure(state="disabled")
            gui.state.next_disc_prompt = ""
        gui.var_step.set("Ripping (MakeMKV)")
        gui.progress.configure(mode="determinate")
        gui.progress.stop()
        gui.progress["value"] = float(m.group(1))
        return

    m = HB_PROGRESS_RE.search(line)
    if m:
        try:
            pct = float(m.group(1))
        except ValueError:
            pct = 0.0
        if gui.state.encode_queued > 0:
            gui.var_step.set(f"Encoding (HandBrake) {max(1, gui.state.encode_started)} of {gui.state.encode_queued}")
        else:
            gui.var_step.set("Encoding (HandBrake)")
        gui.progress.configure(mode="determinate")
        gui.progress.stop()
        gui.progress["value"] = max(0.0, min(100.0, pct))

        gui._eta_update("handbrake", pct)
        return

    if PROMPT_INSERT_RE.search(line) or PROMPT_NEXT_DISC_RE.search(line):
        gui.state.waiting_for_enter = True
        gui.var_step.set("Waiting for disc")

        # Remember the last concrete disc prompt so CSV mode can keep displaying it
        # even when the script prints a generic "next disc" line afterward.
        if PROMPT_INSERT_RE.search(line):
            gui.state.next_disc_prompt = line

        shown_raw = line
        if PROMPT_NEXT_DISC_RE.search(line):
            prev = (getattr(gui.state, "next_disc_prompt", "") or "").strip()
            if prev:
                shown_raw = prev

        gui.var_prompt.set(_format_disc_prompt(shown_raw))
        gui.btn_continue.configure(state="normal")
        gui.progress.configure(mode="indeterminate")
        gui.progress.start(10)
        return

    if PROMPT_LOW_DISK_RE.search(line):
        gui.state.waiting_for_enter = True
        gui.var_step.set("Paused (low disk space)")
        shown = line
        if "Press Enter" in shown:
            shown = shown.replace("Press Enter", "Click Continue (or press Enter)")
        gui.var_prompt.set(shown)
        gui.btn_continue.configure(state="normal")
        gui.progress.configure(mode="indeterminate")
        gui.progress.start(10)
        return

    if line.startswith("Queued encode:"):
        gui.state.encode_queued += 1
        gui.var_step.set(f"Encoding (queued) {gui.state.encode_queued}")
        gui._eta_reset("handbrake")
        gui.progress.configure(mode="indeterminate")
        gui.progress.start(10)
        return

    if FINALIZING_RE.match(line):
        gui.state.finalized_titles += 1
        gui.var_step.set("Finalizing")
        if gui.state.waiting_for_enter:
            gui.state.waiting_for_enter = False
            gui.var_prompt.set("")
            gui.btn_continue.configure(state="disabled")
        if gui.state.total_titles > 0:
            gui.progress.configure(mode="determinate")
            gui.progress.stop()
            gui.progress["value"] = (gui.state.finalized_titles / gui.state.total_titles) * 100
        else:
            gui.progress.configure(mode="indeterminate")
            gui.progress.start(10)
        return

    if line.startswith("Processing complete."):
        gui.var_step.set("Done")
        gui.progress.stop()
        gui.progress.configure(mode="determinate")
        gui.progress["value"] = 100
        gui.var_eta.set("")
        if gui.state.waiting_for_enter:
            gui.state.waiting_for_enter = False
        gui.var_prompt.set("")
        gui.btn_continue.configure(state="disabled")
        if not gui._done_emitted:
            gui._done_emitted = True
            gui.ui_queue.put(("done", "ok"))
        return

    if ERROR_RE.match(line):
        gui.var_step.set("Error")
        gui.progress.stop()
        gui.progress.configure(mode="indeterminate")
        gui.var_eta.set("")
        return

    # CSV schedule line exists, but we don't compute percent from disc count.
    if line.startswith("CSV schedule loaded:"):
        return
