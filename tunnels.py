"""Managed public-tunnel providers for deployed local applications.

The deployment service should retain one ``TunnelManager`` instance and call
``start_for`` once its application port has passed its local health check.  It
must call ``stop_for`` whenever that deployment is stopped, cancelled, or
replaced.
"""

from __future__ import annotations

import os
import queue
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
import json


# Pinggy has used several public domains over time. Accept the first syntactically
# valid HTTPS URL it prints rather than coupling the tunnel to a specific suffix.
PINGGY_HTTPS_URL_PATTERN = re.compile(r"https://[^\s'\"<>]+", re.IGNORECASE)


class TunnelError(RuntimeError):
    """Raised when a public tunnel cannot be started or checked."""


class TunnelProvider(Protocol):
    """Interface for providers that expose a local application publicly."""

    public_url: str | None

    def start(self) -> str: ...

    def stop(self) -> None: ...


@dataclass
class PinggyTunnel:
    """A Pinggy HTTPS tunnel backed by a managed OpenSSH subprocess."""

    local_port: int
    health_path: str = "/api/health"
    timeout_seconds: int = 45
    ssh_executable: str = "ssh"
    ssh_host: str = "free.pinggy.io"
    ssh_port: int = 443
    token: str = ""
    process: subprocess.Popen[str] | None = field(default=None, init=False)
    public_url: str | None = field(default=None, init=False)
    debugger_port: int | None = field(default=None, init=False)
    _lines: queue.Queue[str] = field(default_factory=queue.Queue, init=False)
    _output: list[str] = field(default_factory=list, init=False)

    def start(self) -> str:
        if not 1 <= self.local_port <= 65535:
            raise TunnelError(f"Invalid local port: {self.local_port}")

        self.stop()
        self.debugger_port = self._available_local_port()
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        
        # Build the destination host string: token@host if token is set, otherwise host
        dest_host = f"{self.token.strip()}@{self.ssh_host}" if self.token.strip() else self.ssh_host

        command = [
            self.ssh_executable,
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-T",
            "-p",
            str(self.ssh_port),
            "-R0:127.0.0.1:" + str(self.local_port),  # FIX: Explicit IPv4 loopback to avoid ::1 resolution
            "-L" + str(self.debugger_port) + ":localhost:4300",
            dest_host,
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise TunnelError("Could not start SSH for the Pinggy tunnel. Ensure OpenSSH is installed.") from exc

        assert self.process.stdout is not None and self.process.stderr is not None and self.process.stdin is not None
        
        # 1. Start the reader threads first so we can capture output and wait for the password prompt
        self._read_stream(self.process.stdout)
        self._read_stream(self.process.stderr)

        # 2. Wait for the password prompt to actually appear, then send Enter
        if not self.token.strip():
            prompt_deadline = time.monotonic() + 10
            while time.monotonic() < prompt_deadline:
                if self.process.poll() is not None:
                    break
                recent_output_lower = " ".join(self._output).lower()
                if "password" in recent_output_lower:
                    break
                time.sleep(0.1)

            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
            except OSError:
                # If writing to stdin fails because the pipe is closed or invalid on Windows,
                # we ignore it and let the tunnel attempt to establish.
                pass

        deadline = time.monotonic() + self.timeout_seconds
        public_url = None

        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise TunnelError(
                    f"Pinggy SSH exited before providing a public URL: {self._recent_output()}"
                )

            public_url = self._debugger_public_url()
            if public_url:
                break

            try:
                line = self._lines.get(
                    timeout=min(0.5, max(0.01, deadline - time.monotonic()))
                )
                public_url = self._https_url_from_text(line)
                if public_url:
                    break
            except queue.Empty:
                continue

        if public_url:
            self.public_url = public_url
            print("\n" + "=" * 70)
            print(f" Pinggy tunnel active: {self.public_url}")
            print("=" * 70 + "\n", flush=True)
            # FastAPI is still starting here, so don't block startup
            # waiting for /api/health. The tunnel is already established.
            threading.Thread(
                target=self._background_health_check,
                daemon=True,
            ).start()
            return self.public_url

        self.stop()
        raise TunnelError(
            f"Timed out waiting for Pinggy to provide an HTTPS URL: {self._recent_output()}"
        )

    def stop(self) -> None:
        process, self.process = self.process, None
        self.public_url = None
        self.debugger_port = None
        if not process or process.poll() is not None:
            return
        if os.name == "nt":
            # SSH can keep a child/session alive after terminate() on Windows.
            # taskkill with this exact PID and its child tree prevents orphan tunnels.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            process.wait(timeout=5)
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _read_stream(self, stream: object) -> None:
        def collect() -> None:
            for line in stream:  # type: ignore[union-attr]
                self._output.append(line.rstrip())
                self._output[:] = self._output[-100:]
                self._lines.put(line)

        threading.Thread(target=collect, daemon=True).start()

    def _health_check(self) -> None:
        if not self.public_url:
            raise TunnelError("Pinggy public URL has not been established.")

        health_url = self.public_url.rstrip("/") + "/" + self.health_path.lstrip("/")

        # FIX: Include headers to bypass Pinggy visitor screening page & bot detection
        req = Request(
            health_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "X-Pinggy-No-Screening": "true",
            },
        )

        try:
            with urlopen(req, timeout=5) as response:
                if not 200 <= response.status < 400:
                    raise TunnelError(
                        f"Pinggy public health check returned HTTP {response.status}."
                    )
        except (URLError, TimeoutError, OSError) as exc:
            raise TunnelError(
                f"Pinggy public health check failed for {health_url}."
            ) from exc

    def _background_health_check(self) -> None:
        """Retry the health check in the background after startup."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                self._health_check()
                print("[INFO] Pinggy health check passed.", flush=True)
                return
            except TunnelError:
                time.sleep(1)
        print(
            "[WARNING] Pinggy tunnel is active, but the health check timed out.",
            flush=True,
        )

    def _debugger_public_url(self) -> str | None:
        if not self.debugger_port:
            return None
        try:
            with urlopen(f"http://127.0.0.1:{self.debugger_port}/urls", timeout=1) as response:
                urls = json.load(response).get("urls", [])
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None
        return next((url for url in urls if isinstance(url, str) and self._valid_https_url(url)), None)

    @staticmethod
    def _https_url_from_text(text: str) -> str | None:
        for match in PINGGY_HTTPS_URL_PATTERN.finditer(text):
            url = match.group(0).rstrip(".,;:)]}")
            if PinggyTunnel._valid_https_url(url):
                return url
        return None

    @staticmethod
    def _valid_https_url(url: str) -> bool:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and bool(parsed.hostname) and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _available_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _recent_output(self) -> str:
        return " | ".join(self._output[-10:]) or "no SSH output captured"


class TunnelManager:
    """Owns one tunnel per deployment and prevents orphan replacement tunnels."""

    def __init__(self) -> None:
        self._tunnels: dict[str, TunnelProvider] = {}
        self._lock = threading.Lock()

    def start_for(self, deployment_id: str, tunnel: TunnelProvider) -> str:
        with self._lock:
            self.stop_for(deployment_id)
            self._tunnels[deployment_id] = tunnel
        try:
            return tunnel.start()
        except Exception:
            with self._lock:
                if self._tunnels.get(deployment_id) is tunnel:
                    self._tunnels.pop(deployment_id, None)
            tunnel.stop()
            raise

    def stop_for(self, deployment_id: str) -> None:
        tunnel = self._tunnels.pop(deployment_id, None)
        if tunnel:
            tunnel.stop()

    def stop_all(self) -> None:
        with self._lock:
            tunnels, self._tunnels = list(self._tunnels.values()), {}
        for tunnel in tunnels:
            tunnel.stop()

    def public_url_for(self, deployment_id: str) -> str | None:
        with self._lock:
            tunnel = self._tunnels.get(deployment_id)
            return tunnel.public_url if tunnel else None