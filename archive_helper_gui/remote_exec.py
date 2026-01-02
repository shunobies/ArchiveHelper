from __future__ import annotations

import base64
import hashlib
import os
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

try:
    import paramiko  # type: ignore

    PARAMIKO_AVAILABLE = True
except Exception:
    PARAMIKO_AVAILABLE = False


class RemoteExecutor:
    """Remote execution and file transfer helpers.

    Designed to be GUI-agnostic: callers provide a log callback.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        log: Callable[[str], None],
        default_user_getter: Callable[[], str],
    ) -> None:
        self._state_dir = state_dir
        self._log = log
        self._default_user_getter = default_user_getter

        self._hostkey_logged: set[str] = set()
        self._hostkey_lock = threading.Lock()

    def log(self, message: str) -> None:
        self._log(message)

    @property
    def known_hosts_path(self) -> Path:
        return self._state_dir / "known_hosts"

    def ssh_common_opts(self) -> list[str]:
        kh = self.known_hosts_path
        kh.parent.mkdir(parents=True, exist_ok=True)
        return [
            "-o",
            f"UserKnownHostsFile={str(kh)}",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "BatchMode=yes",
            "-o",
            "NumberOfPasswordPrompts=0",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "LogLevel=ERROR",
        ]

    def target_host(self, target: str) -> str:
        host = (target or "").strip()
        if "@" in host:
            host = host.split("@", 1)[1]
        host = host.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        return host

    def _maybe_log_host_key_acceptance(self, target: str, port: str) -> None:
        host = self.target_host(target)
        if not host:
            return
        p = (port or "").strip() or "22"
        key = f"openssh:{host}:{p}"

        with self._hostkey_lock:
            if key in self._hostkey_logged:
                return
            self._hostkey_logged.add(key)

        if shutil.which("ssh-keyscan") is None or shutil.which("ssh-keygen") is None:
            self._log(
                f"(Info) Auto-accepting SSH host key for {host}:{p}. "
                "(ssh-keyscan/ssh-keygen not found; cannot display fingerprint.)\n"
            )
            return

        kh = self.known_hosts_path
        kh.parent.mkdir(parents=True, exist_ok=True)

        try:
            existing = subprocess.run(
                ["ssh-keygen", "-F", host, "-f", str(kh)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if existing.returncode == 0 and (existing.stdout or "").strip():
                fps = subprocess.run(
                    ["ssh-keygen", "-lf", str(kh), "-F", host],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                out = (fps.stdout or "").strip()
                if out:
                    self._log(f"(Info) SSH known_hosts entry for {host}:{p}:\n{out}\n")
                else:
                    self._log(f"(Info) SSH known_hosts entry already present for {host}:{p}.\n")
                return
        except Exception:
            pass

        try:
            scan = subprocess.run(
                ["ssh-keyscan", "-H", "-p", str(p), "-T", "5", host],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            scan_out = (scan.stdout or "").strip()
            if not scan_out:
                self._log(
                    f"(Info) Auto-accepting SSH host key for {host}:{p}. "
                    "(Unable to scan fingerprint with ssh-keyscan.)\n"
                )
                return

            with kh.open("a", encoding="utf-8") as f:
                f.write(scan_out + "\n")
            try:
                os.chmod(kh, 0o600)
            except Exception:
                pass

            fps = subprocess.run(
                ["ssh-keygen", "-lf", "-"],
                input=scan_out + "\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            fp_out = (fps.stdout or "").strip()
            if fp_out:
                self._log(f"(Info) Auto-accepted SSH host key fingerprint(s) for {host}:{p}:\n{fp_out}\n")
            else:
                self._log(f"(Info) Auto-accepted SSH host key for {host}:{p}.\n")
        except Exception as e:
            self._log(
                f"(Info) Auto-accepting SSH host key for {host}:{p}. "
                f"(Could not display fingerprint: {e})\n"
            )

    def _maybe_log_paramiko_host_key(self, host: str, port: int, client) -> None:
        key = f"paramiko:{host}:{port}"
        with self._hostkey_lock:
            if key in self._hostkey_logged:
                return
            self._hostkey_logged.add(key)

        try:
            transport = client.get_transport()
            if transport is None:
                return
            pkey = transport.get_remote_server_key()
            if pkey is None:
                return

            raw = pkey.asbytes()
            sha256 = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
            self._log(
                f"(Info) Auto-accepted SSH host key for {host}:{port} (Paramiko): "
                f"{pkey.get_name()} SHA256:{sha256}\n"
            )
        except Exception:
            pass

    def ssh_args(self, target: str, port: str, keyfile: str, *, tty: bool = True) -> list[str]:
        self._maybe_log_host_key_acceptance(target, port)
        args = ["ssh"]
        if tty:
            args.append("-tt")
        if (port or "").strip():
            args += ["-p", port.strip()]
        if (keyfile or "").strip():
            args += ["-i", keyfile.strip()]
        args += self.ssh_common_opts()
        args.append(target)
        return args

    def scp_args(self, target: str, port: str, keyfile: str) -> list[str]:
        self._maybe_log_host_key_acceptance(target, port)
        args = ["scp"]
        if (port or "").strip():
            args += ["-P", port.strip()]
        if (keyfile or "").strip():
            args += ["-i", keyfile.strip()]
        args += self.ssh_common_opts()
        return args

    def _parse_target(self, target: str) -> tuple[str, str]:
        if "@" in target:
            user, host = target.split("@", 1)
            return user, host
        return (self._default_user_getter() or "").strip(), target

    def connect_paramiko(self, target: str, port: str, keyfile: str, password: str):
        if not PARAMIKO_AVAILABLE:
            raise ValueError("Paramiko is not available.")

        user, host = self._parse_target(target)
        if not user:
            raise ValueError("User is required for password-based SSH.")

        p = int((port or "").strip() or "22")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kf = (keyfile or "").strip()
        if kf:
            client.connect(hostname=host, port=p, username=user, key_filename=kf, password=password or None)
        else:
            client.connect(hostname=host, port=p, username=user, password=password)

        self._maybe_log_paramiko_host_key(host, p, client)
        return client

    def exec_paramiko(self, client, command: str) -> tuple[int, str]:
        _stdin, stdout, stderr = client.exec_command(command)
        out = (stdout.read() or b"").decode("utf-8", errors="replace")
        err = (stderr.read() or b"").decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out + err

    def sftp_put(self, client, local_path: str, remote_path: str) -> None:
        sftp = client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def run_bash(
        self,
        target: str,
        port: str,
        keyfile: str,
        password: str,
        cmd: str,
        *,
        interactive: bool = False,
    ) -> tuple[int, str]:
        """Run a short remote bash command and capture output.

        - interactive=False uses `bash -lc` (default for automation)
        - interactive=True uses `bash -lic` (matches many users' interactive shell PATH)
        """

        bash_flag = "-lic" if interactive else "-lc"

        if password:
            client = self.connect_paramiko(target, port, keyfile, password)
            try:
                return self.exec_paramiko(client, f"bash {bash_flag} " + shlex.quote(cmd))
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        ssh_base = self.ssh_args(target, port, keyfile, tty=False)
        res = subprocess.run(
            ssh_base + ["bash", bash_flag, shlex.quote(cmd)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return res.returncode, res.stdout or ""

    def remote_run(self, target: str, port: str, keyfile: str, password: str, cmd: str) -> tuple[int, str]:
        """Backward-compatible alias for run_bash(interactive=False)."""

        return self.run_bash(target, port, keyfile, password, cmd, interactive=False)
