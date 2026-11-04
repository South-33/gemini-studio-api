"""Own the API plus Cloudflare data-plane and ngrok control-plane tunnels."""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
LAUNCHER_LOG = LOGS / "launcher.log"
MAX_PROCESS_LOG_BYTES = 10 * 1024 * 1024
PORT = 8000
CONTROL_DOMAIN = "believable-unplagiarised-josette.ngrok-free.dev"
CONTROL_ROOT = f"https://{CONTROL_DOMAIN}"
LOCAL_HEALTH = f"http://127.0.0.1:{PORT}/health"
CONTROL_HEALTH = f"{CONTROL_ROOT}/v1/endpoint"
ENDPOINT_STATE = ROOT / "runtime_endpoint.json"
CLOUDFLARE_BINARY = ROOT / "cloudflared.exe"
CLOUDFLARE_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
LAUNCHER_MUTEX_NAME = r"Global\GeminiStudioAPILauncher"
LOCAL_HEALTH_FAILURE_LIMIT = 3
PUBLIC_HEALTH_FAILURE_LIMIT = 3
BUSY_HEALTH_GRACE_SECONDS = 15 * 60
CLOUDFLARE_STARTUP_GRACE_SECONDS = 3 * 60


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        LOGS.mkdir(exist_ok=True)
        with LAUNCHER_LOG.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
    except OSError:
        pass


def rotate_log(path: Path) -> None:
    """Keep long-running process logs bounded without touching request records."""
    try:
        if not path.exists() or path.stat().st_size < MAX_PROCESS_LOG_BYTES:
            return
        backup = path.with_suffix(path.suffix + ".1")
        backup.unlink(missing_ok=True)
        path.replace(backup)
    except OSError:
        pass


def acquire_launcher_mutex(name: str = LAUNCHER_MUTEX_NAME) -> object | None:
    """Acquire the host-wide Windows launcher mutex, or return None if owned."""
    if os.name != "nt":
        return object()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle


def close_launcher_mutex(handle: object | None) -> None:
    if os.name != "nt" or not handle:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def enable_kill_on_close_job() -> object | None:
    """Put this process and descendants in a job killed when this owner exits."""
    if os.name != "nt":
        return None

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMITS),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    limits = EXTENDED_LIMITS()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        ctypes.c_void_p(job), 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel32.AssignProcessToJobObject(
        ctypes.c_void_p(job), kernel32.GetCurrentProcess()
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen
    output: IO[str]
    log_path: Path
    log_start_offset: int

    def stop(self) -> None:
        if self.process.poll() is not None:
            self.output.close()
            return
        log(f"Stopping {self.name}...")
        try:
            if os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
                self.process.wait(timeout=8)
            else:
                self.process.terminate()
                self.process.wait(timeout=8)
        except Exception:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                self.process.kill()
            try:
                self.process.wait(timeout=5)
            except Exception:
                pass
        self.output.close()


def start_process(name: str, command: list[str], log_name: str) -> ManagedProcess:
    LOGS.mkdir(exist_ok=True)
    log_path = LOGS / log_name
    rotate_log(log_path)
    output = log_path.open("a", encoding="utf-8")
    output.write(f"\n{'=' * 60}\n{name} launch {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    output.flush()
    log_start_offset = output.tell()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    log(f"Started {name} (PID {process.pid})")
    return ManagedProcess(name, process, output, log_path, log_start_offset)


def resolve_cloudflared() -> str | None:
    installed = shutil.which("cloudflared")
    if installed:
        return installed
    if CLOUDFLARE_BINARY.exists():
        return str(CLOUDFLARE_BINARY)
    return None


def quick_tunnel_url(process: ManagedProcess) -> str | None:
    """Read only this process launch's log segment and return its newest URL."""
    try:
        with process.log_path.open("r", encoding="utf-8", errors="ignore") as stream:
            stream.seek(process.log_start_offset)
            matches = CLOUDFLARE_URL_RE.findall(stream.read())
        return matches[-1].rstrip("/") if matches else None
    except OSError:
        return None


def quick_tunnel_connected(process: ManagedProcess) -> bool:
    """Return whether this launch has registered its Cloudflare connection."""
    try:
        with process.log_path.open("r", encoding="utf-8", errors="ignore") as stream:
            stream.seek(process.log_start_offset)
            return "Registered tunnel connection" in stream.read()
    except OSError:
        return False


def wait_for_quick_tunnel_url(process: ManagedProcess, seconds: int = 30) -> str | None:
    deadline = time.monotonic() + seconds
    latest_url = None
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            return None
        latest_url = quick_tunnel_url(process) or latest_url
        if latest_url and quick_tunnel_connected(process):
            return latest_url
        time.sleep(0.5)
    return None


def write_endpoint_state(data_plane_url: str, transport: str) -> None:
    """Atomically publish the current data-plane URL for the fixed ngrok handshake."""
    payload = {
        "version": 1,
        "data_plane_url": data_plane_url.rstrip("/"),
        "control_plane_url": CONTROL_ROOT,
        "transport": transport,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temp_path = ENDPOINT_STATE.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temp_path.replace(ENDPOINT_STATE)


def start_cloudflare_tunnel(cloudflared: str | None) -> tuple[ManagedProcess | None, str | None]:
    if not cloudflared:
        log("cloudflared is unavailable; using ngrok as the temporary data plane")
        return None, None

    process = start_process(
        "cloudflared",
        [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
        "cloudflared.log",
    )
    url = wait_for_quick_tunnel_url(process, 30)
    if not url:
        log("Cloudflare quick tunnel did not register; falling back to ngrok")
        process.stop()
        return None, None
    return process, url


def healthy(url: str, timeout: float = 4) -> bool:
    try:
        request = urllib.request.Request(url, headers={"ngrok-skip-browser-warning": "1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def health_payload(url: str, timeout: float = 4) -> dict | None:
    """Return a successful JSON health payload, or None when unavailable."""
    try:
        request = urllib.request.Request(url, headers={"ngrok-skip-browser-warning": "1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.load(response)
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def should_restart_tunnel(process_exited: bool, failures: int, active_request: bool) -> bool:
    """Never tear down a live tunnel merely because probes fail during a request."""
    return process_exited or (
        not active_request and failures >= PUBLIC_HEALTH_FAILURE_LIMIT
    )


def should_restart_data_tunnel(process_exited: bool, failures: int) -> bool:
    """Quick Tunnel rotation is safe because Gemini jobs stay in the local API."""
    return process_exited or failures >= PUBLIC_HEALTH_FAILURE_LIMIT


def should_restart_api(process_exited: bool, failures: int, busy_grace_active: bool) -> bool:
    """Do not interrupt a recently observed long-running browser request."""
    return process_exited or (
        not busy_grace_active and failures >= LOCAL_HEALTH_FAILURE_LIMIT
    )


def wait_until_healthy(process: ManagedProcess, url: str, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            return False
        if healthy(url):
            return True
        time.sleep(1)
    return False


def stop_existing_service_processes() -> None:
    if os.name != "nt":
        return
    subprocess.run(
        ["taskkill", "/F", "/T", "/IM", "ngrok.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        ["taskkill", "/F", "/T", "/IM", "cloudflared.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    netstat = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for line in netstat.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[3] == "LISTENING":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", parts[4]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def run() -> int:
    log("Launcher starting")
    launcher_mutex = acquire_launcher_mutex()
    if launcher_mutex is None:
        log("Another launcher already owns this host; exiting")
        return 0
    if shutil.which("ngrok") is None:
        log("ngrok was not found on PATH; install/configure ngrok before starting the server")
        close_launcher_mutex(launcher_mutex)
        return 1
    if not (ROOT / ".browser_session").exists():
        log("No .browser_session profile found; Chromium will require a first-time Gemini sign-in")

    job = enable_kill_on_close_job()
    if job:
        log("Process-tree cleanup enabled; closing this terminal stops everything")

    stop_existing_service_processes()
    api: Optional[ManagedProcess] = None
    cloudflared_path = resolve_cloudflared()
    cloudflare: Optional[ManagedProcess] = None
    cloudflare_url: Optional[str] = None
    ngrok: Optional[ManagedProcess] = None
    try:
        api = start_process("API", [sys.executable, "-u", "main.py"], "server.log")
        if not wait_until_healthy(api, LOCAL_HEALTH, 120):
            log("API failed to become ready; see logs\\server.log")
            return 1

        # Bring the fixed control plane up first. Until Cloudflare is ready,
        # discovery intentionally points callers back to ngrok as a fallback.
        write_endpoint_state(CONTROL_ROOT, "ngrok-fallback")
        ngrok = start_process(
            "ngrok",
            ["ngrok", "http", str(PORT), f"--url={CONTROL_ROOT}"],
            "ngrok.log",
        )
        if not wait_until_healthy(ngrok, CONTROL_HEALTH, 30):
            log("ngrok control plane failed to become ready; see logs\\ngrok.log")
            return 1

        # Cloudflare carries normal API traffic. Its Quick Tunnel hostname can
        # change on every launch, so publish the current URL through ngrok.
        cloudflare, cloudflare_url = start_cloudflare_tunnel(cloudflared_path)
        cloudflare_grace_until = 0.0
        if cloudflare_url:
            write_endpoint_state(cloudflare_url, "cloudflare-quick")
            cloudflare_grace_until = time.monotonic() + CLOUDFLARE_STARTUP_GRACE_SECONDS

        log(f"Ready: http://localhost:{PORT}")
        log(f"Control plane: {CONTROL_ROOT}")
        log(f"Data plane: {cloudflare_url or CONTROL_ROOT}")
        log("Launcher owns the API/cloudflared/ngrok process tree; stop the launcher to stop all services")

        local_health_failures = 0
        cloudflare_health_failures = 0
        control_health_failures = 0
        busy_grace_until = 0.0
        while True:
            time.sleep(10)
            api_exited = api.process.poll() is not None
            local_payload = None if api_exited else health_payload(LOCAL_HEALTH)
            local_ok = local_payload is not None
            active_request = bool(local_payload and local_payload.get("active_request"))
            queued_requests = int(local_payload.get("queued_requests", 0) or 0) if local_payload else 0
            if active_request or queued_requests > 0:
                busy_grace_until = time.monotonic() + BUSY_HEALTH_GRACE_SECONDS
            local_health_failures = 0 if local_ok else local_health_failures + 1
            busy_grace_active = time.monotonic() < busy_grace_until
            if should_restart_api(api_exited, local_health_failures, busy_grace_active):
                log("API or Gemini worker unhealthy; restarting API...")
                api.stop()
                api = start_process("API", [sys.executable, "-u", "main.py"], "server.log")
                if not wait_until_healthy(api, LOCAL_HEALTH, 120):
                    log("API restart failed; retrying on the next monitor cycle")
                local_health_failures = 0

            cloudflare_exited = bool(cloudflare and cloudflare.process.poll() is not None)
            cloudflare_in_startup_grace = time.monotonic() < cloudflare_grace_until
            if cloudflare and cloudflare_url and not cloudflare_in_startup_grace:
                data_ok = healthy(f"{cloudflare_url}/health")
                cloudflare_health_failures = 0 if data_ok else cloudflare_health_failures + 1
            elif cloudflare_exited:
                cloudflare_health_failures = PUBLIC_HEALTH_FAILURE_LIMIT
            else:
                cloudflare_health_failures = 0

            # Async Gemini work lives in the local API/browser, not in the
            # Cloudflare connection. Rotating a broken Quick Tunnel is safe
            # while Gemini is thinking; clients rediscover the new URL through
            # the stable ngrok control plane and keep the same request_id.
            if cloudflare and should_restart_data_tunnel(
                cloudflare_exited, cloudflare_health_failures
            ):
                log("Cloudflare data plane unhealthy; rotating Quick Tunnel...")
                cloudflare.stop()
                cloudflare, cloudflare_url = start_cloudflare_tunnel(cloudflared_path)
                if cloudflare_url:
                    write_endpoint_state(cloudflare_url, "cloudflare-quick")
                    cloudflare_grace_until = time.monotonic() + CLOUDFLARE_STARTUP_GRACE_SECONDS
                    log(f"Data plane rotated: {cloudflare_url}")
                else:
                    write_endpoint_state(CONTROL_ROOT, "ngrok-fallback")
                    cloudflare_grace_until = 0.0
                cloudflare_health_failures = 0
            elif not cloudflare and cloudflared_path:
                cloudflare, cloudflare_url = start_cloudflare_tunnel(cloudflared_path)
                if cloudflare_url:
                    write_endpoint_state(cloudflare_url, "cloudflare-quick")
                    cloudflare_grace_until = time.monotonic() + CLOUDFLARE_STARTUP_GRACE_SECONDS
                    log(f"Data plane restored: {cloudflare_url}")

            ngrok_exited = ngrok.process.poll() is not None
            if ngrok_exited:
                control_health_failures = PUBLIC_HEALTH_FAILURE_LIMIT
            elif active_request:
                control_health_failures = 0
            else:
                control_ok = healthy(CONTROL_HEALTH)
                control_health_failures = 0 if control_ok else control_health_failures + 1

            if should_restart_tunnel(ngrok_exited, control_health_failures, active_request):
                log("Control plane unhealthy; restarting ngrok...")
                ngrok.stop()
                ngrok = start_process(
                    "ngrok",
                    ["ngrok", "http", str(PORT), f"--url={CONTROL_ROOT}"],
                    "ngrok.log",
                )
                if not wait_until_healthy(ngrok, CONTROL_HEALTH, 30):
                    log("Control-plane restart failed; retrying on the next monitor cycle")
                control_health_failures = 0
    except KeyboardInterrupt:
        log("Shutdown requested")
        return 0
    finally:
        if ngrok:
            ngrok.stop()
        if cloudflare:
            cloudflare.stop()
        if api:
            api.stop()
        close_launcher_mutex(launcher_mutex)


if __name__ == "__main__":
    raise SystemExit(run())
