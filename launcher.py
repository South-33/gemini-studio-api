"""Own and monitor the API, Chromium, and ngrok as one Windows process tree."""

from __future__ import annotations

import ctypes
import os
import signal
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
LAUNCHER_LOG = LOGS / "launcher.log"
MAX_PROCESS_LOG_BYTES = 10 * 1024 * 1024
PORT = 8000
DOMAIN = "believable-unplagiarised-josette.ngrok-free.dev"
PUBLIC_ROOT = f"https://{DOMAIN}"
LOCAL_HEALTH = f"http://127.0.0.1:{PORT}/health"
PUBLIC_HEALTH = f"{PUBLIC_ROOT}/health"
LAUNCHER_MUTEX_NAME = r"Local\GeminiStudioAPILauncher"


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
    """Acquire the per-session Windows launcher mutex, or return None if owned."""
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
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    log(f"Started {name} (PID {process.pid})")
    return ManagedProcess(name, process, output)


def healthy(url: str, timeout: float = 4) -> bool:
    try:
        request = urllib.request.Request(url, headers={"ngrok-skip-browser-warning": "1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


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
    load_dotenv(ROOT / ".env")
    log("Launcher starting")
    launcher_mutex = acquire_launcher_mutex()
    if launcher_mutex is None:
        log("Another launcher already owns this desktop session; exiting")
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
    ngrok: Optional[ManagedProcess] = None
    try:
        api = start_process("API", [sys.executable, "-u", "main.py"], "server.log")
        if not wait_until_healthy(api, LOCAL_HEALTH, 120):
            log("API failed to become ready; see logs\\server.log")
            return 1

        ngrok = start_process(
            "ngrok",
            ["ngrok", "http", str(PORT), f"--url={PUBLIC_ROOT}"],
            "ngrok.log",
        )
        if not wait_until_healthy(ngrok, PUBLIC_HEALTH, 30):
            log("Public tunnel failed to become ready; see logs\\ngrok.log")
            return 1

        log(f"Ready: http://localhost:{PORT}")
        log(f"Tunnel: https://{DOMAIN}")
        log("Press Ctrl+C once, or close this window, to stop all services")

        while True:
            time.sleep(10)
            if api.process.poll() is not None or not healthy(LOCAL_HEALTH):
                log("API or Gemini worker unhealthy; restarting API...")
                api.stop()
                api = start_process("API", [sys.executable, "-u", "main.py"], "server.log")
                if not wait_until_healthy(api, LOCAL_HEALTH, 120):
                    log("API restart failed; retrying on the next monitor cycle")
            if ngrok.process.poll() is not None or not healthy(PUBLIC_HEALTH):
                log("Tunnel unhealthy; restarting ngrok...")
                ngrok.stop()
                ngrok = start_process(
                    "ngrok",
                    ["ngrok", "http", str(PORT), f"--url={PUBLIC_ROOT}"],
                    "ngrok.log",
                )
                if not wait_until_healthy(ngrok, PUBLIC_HEALTH, 30):
                    log("Tunnel restart failed; retrying on the next monitor cycle")
    except KeyboardInterrupt:
        log("Shutdown requested")
        return 0
    finally:
        if ngrok:
            ngrok.stop()
        if api:
            api.stop()
        close_launcher_mutex(launcher_mutex)


if __name__ == "__main__":
    raise SystemExit(run())
