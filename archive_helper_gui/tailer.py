from __future__ import annotations

import shlex
import subprocess
import time


def start_tail(gui, *, from_start: bool = True, tail_lines: int = 2000) -> None:
    ctx = gui._get_run_ctx()
    if not ctx.log_path:
        raise ValueError("Missing remote log path.")

    if from_start:
        tail_cmd = f"tail -n +1 -F {shlex.quote(ctx.log_path)}"
    else:
        tail_cmd = f"tail -n {int(tail_lines)} -F {shlex.quote(ctx.log_path)}"

    stop_tail(gui, quiet=True)

    if ctx.password:
        gui.tail_client = gui._connect_paramiko(ctx.target, ctx.port, ctx.keyfile, ctx.password)
        chan = gui.tail_client.get_transport().open_session()
        chan.get_pty()
        chan.exec_command("bash -lc " + shlex.quote(tail_cmd))
        gui.tail_channel = chan
        gui.tail_proc = None
    else:
        ssh_base = gui._ssh_args(ctx.target, ctx.port, ctx.keyfile, tty=False)
        ssh_cmd = ssh_base + ["bash", "-lc", shlex.quote(tail_cmd)]
        gui.tail_proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        gui.tail_channel = None
        gui.tail_client = None


def stop_tail(gui, *, quiet: bool = False) -> None:
    try:
        if getattr(gui, "tail_channel", None) is not None:
            try:
                gui.tail_channel.close()
            except Exception:
                pass
        if getattr(gui, "tail_client", None) is not None:
            try:
                gui.tail_client.close()
            except Exception:
                pass
    finally:
        gui.tail_channel = None
        gui.tail_client = None

    if getattr(gui, "tail_proc", None) is not None:
        try:
            if gui.tail_proc.poll() is None:
                gui.tail_proc.terminate()
        except Exception:
            pass
        gui.tail_proc = None

    if not quiet:
        gui._append_log("(Info) Stopped log tail.\n")


def reader_loop(gui) -> None:
    backoff = 1.0

    while gui.state.running and not gui._stop_requested.is_set():
        if gui.run_ctx is None:
            gui.ui_queue.put(("done", "Lost connection and the remote job is no longer running."))
            return

        ctx = gui.run_ctx

        # OpenSSH tail path
        if ctx.password == "":
            if gui.tail_proc is None:
                try:
                    start_tail(gui)
                except Exception as e:
                    gui.ui_queue.put(("log", f"(Info) Failed to start log tail: {e}\n"))
                    time.sleep(min(backoff, 10.0))
                    backoff = min(backoff * 2.0, 30.0)
                    continue

            assert gui.tail_proc is not None
            assert gui.tail_proc.stdout is not None

            try:
                for line in gui.tail_proc.stdout:
                    if gui._stop_requested.is_set() or not gui.state.running:
                        break
                    gui.ui_queue.put(("log", line))

                if gui._stop_requested.is_set() or not gui.state.running:
                    break

                code = gui.tail_proc.wait()
                gui.ui_queue.put(("log", f"(Info) Disconnected from server (tail exit {code}). Reconnecting...\n"))
            except Exception as e:
                gui.ui_queue.put(("log", f"(Info) Lost connection while reading log: {e}. Reconnecting...\n"))

            stop_tail(gui, quiet=True)
            if not gui._screen_exists():
                gui.ui_queue.put(("done", "Lost connection and the remote job is no longer running."))
                return
            time.sleep(min(backoff, 10.0))
            backoff = min(backoff * 2.0, 30.0)
            continue

        # Paramiko tail path
        if gui.tail_channel is None:
            try:
                start_tail(gui)
            except Exception as e:
                gui.ui_queue.put(("log", f"(Info) Failed to start log tail: {e}\n"))
                time.sleep(min(backoff, 10.0))
                backoff = min(backoff * 2.0, 30.0)
                continue

        assert gui.tail_channel is not None
        buf = ""
        try:
            while gui.state.running and not gui._stop_requested.is_set():
                if gui.tail_channel.recv_ready():
                    data = gui.tail_channel.recv(4096)
                    if not data:
                        break
                    chunk = data.decode("utf-8", errors="replace")
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        gui.ui_queue.put(("log", line + "\n"))

                if gui.tail_channel.exit_status_ready():
                    break
                time.sleep(0.03)

            if buf:
                gui.ui_queue.put(("log", buf))

            gui.ui_queue.put(("log", "(Info) Disconnected from server. Reconnecting...\n"))
        except Exception as e:
            gui.ui_queue.put(("log", f"(Info) Lost connection while reading log: {e}. Reconnecting...\n"))

        stop_tail(gui, quiet=True)
        if not gui._screen_exists():
            gui.ui_queue.put(("done", "Lost connection and the remote job is no longer running."))
            return
        time.sleep(min(backoff, 10.0))
        backoff = min(backoff * 2.0, 30.0)

    return
