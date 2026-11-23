"""One resilient Gemini Web worker with queued request execution."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import Page, async_playwright

from gemini_web import GeminiWebAutomation, log

REQUEST_TOTAL_BUDGET_SECONDS = 480
BROWSER_CONNECT_TIMEOUT_SECONDS = 15
AUTH_REQUIRED_IDLE_SECONDS = 900
BROWSER_SCOPE_MEMORY_HIGH = "1536M"
BROWSER_SCOPE_MEMORY_MAX = "2G"
BROWSER_SCOPE_SWAP_MAX = "256M"
BROWSER_SCOPE_TASKS_MAX = "512"
RDP_X11_WINDOW_WIDTH = 3600
RDP_X11_WINDOW_HEIGHT = 2100


class WorkerPool:
    """Serialize requests through one tab and recreate it after concrete failures."""

    def __init__(self, idle_browser_seconds: float = 60, idle_poll_seconds: float = 5):
        self.playwright = None
        self.browser = None
        self.browser_process: subprocess.Popen | None = None
        self.virtual_display_process: subprocess.Popen | None = None
        self.browser_display: str | None = None
        self.browser_uses_virtual_display = False
        self.browser_scope_unit: str | None = None
        self.browser_profile_dir: Path | None = None
        self.context = None
        self.workers: List[GeminiWebAutomation] = []
        self._request_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._initialized = False
        self._browser_state = "stopped"
        self._idle_browser_seconds = max(0.0, float(idle_browser_seconds))
        self._idle_poll_seconds = max(0.05, float(idle_poll_seconds))
        self._idle_task: Optional[asyncio.Task] = None
        self._active_request = False
        self._queued_requests = 0
        self._last_activity = time.time()
        self._browser_start_count = 0
        self._browser_stop_count = 0
        self._last_browser_start: Optional[Dict[str, Any]] = None
        self._last_browser_stop: Optional[Dict[str, Any]] = None
        self._startup_pages_closed = 0
        self._worker_recreation_count = 0
        self._last_worker_recreation: Optional[Dict[str, Any]] = None
        self._last_ready_reset: Optional[Dict[str, Any]] = None
        self._last_request_timing: Optional[Dict[str, Any]] = None
        self._needs_ready_reset = False

    @property
    def worker(self) -> Optional[GeminiWebAutomation]:
        return self.workers[0] if self.workers else None

    async def init(self, cleanup_stale_browser: bool = False) -> bool:
        """Initialize the pool without starting Chrome.

        Gemini Studio API is normally idle. Starting Playwright and Chrome only
        when a request arrives keeps the resident service small.
        """
        if cleanup_stale_browser:
            await asyncio.to_thread(self._cleanup_stale_browser_processes)
        self._initialized = True
        self._last_activity = time.time()
        self._idle_task = asyncio.create_task(self._idle_watchdog())
        return True

    @staticmethod
    def _browser_args() -> List[str]:
        return [
            "--disable-blink-features=AutomationControlled",
            "--ozone-platform=x11",
            "--disable-gpu",
            "--window-size=1440,900",
            "--disable-background-mode",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,BatterySaverModeAvailability",
        ]

    @staticmethod
    def _browser_environment() -> dict[str, str]:
        """Refresh desktop-session variables when Chromium is launched."""
        environment = dict(os.environ)
        if os.name != "posix":
            return environment
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return environment
        if result.returncode != 0:
            return environment
        wanted = {
            "DISPLAY",
            "XAUTHORITY",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
        }
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in wanted and value:
                environment[key] = value
        return environment

    @staticmethod
    def _display_is_usable(environment: dict[str, str]) -> bool:
        """Return whether Chromium can currently connect to the configured X11 display."""
        display = environment.get("DISPLAY")
        if not display:
            return False
        xdpyinfo = shutil.which("xdpyinfo")
        if not xdpyinfo:
            if not display.startswith(":"):
                return True
            number = display[1:].split(".", 1)[0]
            return bool(number) and (Path("/tmp/.X11-unix") / f"X{number}").exists()
        try:
            result = subprocess.run(
                [xdpyinfo, "-display", display],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _start_virtual_display(self, environment: dict[str, str]) -> dict[str, str]:
        """Start a private Xvfb only when no live desktop X11 display exists."""
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise RuntimeError("No usable X11 display and Xvfb is not installed")

        for number in range(99, 89, -1):
            socket_path = Path("/tmp/.X11-unix") / f"X{number}"
            lock_path = Path(f"/tmp/.X{number}-lock")
            if socket_path.exists() or lock_path.exists():
                continue
            display = f":{number}"
            candidate = dict(environment)
            candidate["DISPLAY"] = display
            candidate.pop("XAUTHORITY", None)
            process = subprocess.Popen(
                [
                    xvfb,
                    display,
                    "-screen",
                    "0",
                    "1440x900x24",
                    "-nolisten",
                    "tcp",
                    "-ac",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if self._display_is_usable(candidate):
                    self.virtual_display_process = process
                    self.browser_display = display
                    self.browser_uses_virtual_display = True
                    log(
                        f"No usable desktop X11 display; using temporary Xvfb {display}",
                        "Worker",
                    )
                    return candidate
                time.sleep(0.05)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

        raise RuntimeError("Could not start a private Xvfb display")

    def _browser_launch_environment(self) -> dict[str, str]:
        environment = self._browser_environment()
        self.browser_display = environment.get("DISPLAY")
        self.browser_uses_virtual_display = False
        if self._display_is_usable(environment):
            return environment
        return self._start_virtual_display(environment)

    @staticmethod
    def _state_dir() -> Path:
        return Path(__file__).resolve().parent / ".browser_session"

    @classmethod
    def _profile_dir(cls) -> Path:
        return cls._state_dir() / "profile"

    @staticmethod
    def _remove_profile_runtime_artifacts(profile_dir: Path) -> None:
        for name in (
            "DevToolsActivePort",
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
        ):
            path = profile_dir / name
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @classmethod
    def _prepare_profile_dir(cls) -> Path:
        state_dir = cls._state_dir()
        state_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            state_dir.chmod(0o700)
        except OSError:
            pass
        profile = cls._profile_dir()
        profile.mkdir(mode=0o700, exist_ok=True)
        try:
            profile.chmod(0o700)
        except OSError:
            pass
        cls._remove_profile_runtime_artifacts(profile)
        # Chromium 158/159 on this Ubuntu host crashes with SIGTRAP on the
        # second profile launch if Default/Preferences from the prior run is
        # retained. The credential/session databases relaunch cleanly. Let
        # Chromium regenerate only this preference file on each cold start.
        try:
            (profile / "Default" / "Preferences").unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(f"Could not reset Chromium Preferences: {exc}") from exc
        return profile

    @staticmethod
    def _scope_for_pid(pid: int) -> str | None:
        try:
            content = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
        except OSError:
            return None
        for line in content.splitlines():
            cgroup_path = line.rsplit(":", 1)[-1].strip()
            unit = Path(cgroup_path).name
            if unit.endswith(".scope"):
                return unit
        return None

    @staticmethod
    def _cgroup_metrics_for_pid(pid: int) -> Dict[str, int]:
        """Read cheap cgroup-v2 resource counters for diagnostics only."""
        try:
            lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
            cgroup_path = next(
                line.split("::", 1)[1]
                for line in lines
                if "::" in line
            )
            root = Path("/sys/fs/cgroup") / cgroup_path.lstrip("/")
        except (OSError, StopIteration, IndexError):
            return {}

        metrics: Dict[str, int] = {}
        for key, filename in (
            ("memory_current_bytes", "memory.current"),
            ("memory_peak_bytes", "memory.peak"),
            ("tasks_current", "pids.current"),
        ):
            try:
                metrics[key] = int((root / filename).read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass
        try:
            cpu_stat = (root / "cpu.stat").read_text(encoding="utf-8").splitlines()
            for line in cpu_stat:
                name, _, value = line.partition(" ")
                if name == "usage_usec" and value.isdigit():
                    metrics["cpu_usage_usec"] = int(value)
                    break
        except OSError:
            pass
        return metrics

    @classmethod
    def _is_profile_browser_command(cls, command: bytes) -> bool:
        normalized = command.replace(b"\0", b" ")
        profile_arg = f"--user-data-dir={cls._profile_dir()}".encode()
        return profile_arg in normalized and b" --type=" not in normalized

    @classmethod
    def _profile_browser_pids(cls) -> list[int]:
        pids: list[int] = []
        proc = Path("/proc")
        if not proc.exists():
            return pids
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if not cls._is_profile_browser_command(command):
                continue
            pids.append(int(entry.name))
        return pids

    @staticmethod
    def _stop_user_scope(unit: str) -> None:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "stop", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6,
                check=False,
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "kill",
                    "--kill-whom=all",
                    "--signal=SIGKILL",
                    unit,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    @classmethod
    def _cleanup_stale_browser_processes(cls) -> None:
        cleaned = 0
        initial = cls._profile_browser_pids()
        for pid in initial:
            scope = cls._scope_for_pid(pid)
            if scope:
                cls._stop_user_scope(scope)
                cleaned += 1
                continue
            try:
                os.killpg(pid, signal.SIGKILL)
                cleaned += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass

        deadline = time.monotonic() + 6
        remaining = cls._profile_browser_pids()
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining = cls._profile_browser_pids()

        for pid in remaining:
            try:
                os.killpg(pid, signal.SIGKILL)
                cleaned += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass

        deadline = time.monotonic() + 3
        remaining = cls._profile_browser_pids()
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining = cls._profile_browser_pids()
        if remaining:
            raise RuntimeError(
                f"Could not retire stale Chromium process(es): {remaining}"
            )

        state_dir = cls._state_dir()
        cls._remove_profile_runtime_artifacts(cls._profile_dir())
        # Remove runtime directories left by the retired clone-based design.
        for runtime_dir in state_dir.glob("runtime-*"):
            if runtime_dir.is_dir():
                shutil.rmtree(runtime_dir, ignore_errors=True)
        if cleaned:
            log(f"Retired {cleaned} stale Chromium process(es)", "Worker")

    async def _grant_clipboard_permissions(self) -> None:
        if not self.context:
            raise RuntimeError("Chromium browser context is unavailable")
        await self.context.grant_permissions(
            ["clipboard-read", "clipboard-write"],
            origin="https://gemini.google.com",
        )

    def _configure_browser_scope(self) -> None:
        process = self.browser_process
        if process is None:
            raise RuntimeError("Chromium process is unavailable")
        scope = self._scope_for_pid(process.pid)
        if not scope:
            # Chromium normally moves itself into an app scope on this host.
            # If it remains in the API service cgroup, the service-level
            # MemoryMax/TasksMax limits already provide containment.
            self.browser_scope_unit = None
            return
        if not scope.startswith("app-org.chromium.Chromium-"):
            # Never mutate an enclosing DevSpace/terminal/service scope if
            # Chromium has not moved into its own desktop application scope.
            self.browser_scope_unit = None
            return
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "set-property",
                "--runtime",
                scope,
                f"MemoryHigh={BROWSER_SCOPE_MEMORY_HIGH}",
                f"MemoryMax={BROWSER_SCOPE_MEMORY_MAX}",
                f"MemorySwapMax={BROWSER_SCOPE_SWAP_MAX}",
                f"TasksMax={BROWSER_SCOPE_TASKS_MAX}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown systemd error").strip()
            raise RuntimeError(f"Could not apply Chromium scope limits: {detail}")
        self.browser_scope_unit = scope

    def _resize_browser_window_for_rdp(
        self, environment: dict[str, str] | None = None
    ) -> None:
        """Compensate for GNOME RDP's Xwayland downscaling on South Server.

        Chromium reports a normal 1440x900 logical window in CDP, but the RDP
        compositor displays the X11 surface at roughly 40% of that size. Resize
        only the outer X11 window; Chromium keeps the same logical page size,
        avoiding the memory cost of rendering a giant viewport.
        """
        process = self.browser_process
        xdotool = shutil.which("xdotool")
        if process is None or not xdotool or self.browser_uses_virtual_display:
            return
        environment = environment or self._browser_environment()
        try:
            search = subprocess.run(
                [xdotool, "search", "--pid", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                env=environment,
            )
            if search.returncode != 0:
                return
            best_window: str | None = None
            best_area = 0
            for window in dict.fromkeys(search.stdout.split()):
                geometry = subprocess.run(
                    [xdotool, "getwindowgeometry", "--shell", window],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                    env=environment,
                )
                if geometry.returncode != 0:
                    continue
                values: dict[str, int] = {}
                for line in geometry.stdout.splitlines():
                    key, separator, value = line.partition("=")
                    if separator and value.isdigit():
                        values[key] = int(value)
                area = values.get("WIDTH", 0) * values.get("HEIGHT", 0)
                if area > best_area:
                    best_window = window
                    best_area = area
            if not best_window:
                return
            subprocess.run(
                [xdotool, "windowmove", best_window, "10", "10"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                env=environment,
            )
            subprocess.run(
                [
                    xdotool,
                    "windowsize",
                    best_window,
                    str(RDP_X11_WINDOW_WIDTH),
                    str(RDP_X11_WINDOW_HEIGHT),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return

    async def _launch_browser_context(self) -> None:
        """Launch Chromium directly, then attach Playwright over local CDP.

        """
        if self.playwright is None:
            raise RuntimeError("Playwright is not initialized")

        await asyncio.to_thread(self._cleanup_stale_browser_processes)
        profile_dir = self._prepare_profile_dir()
        self.browser_profile_dir = profile_dir
        active_port = profile_dir / "DevToolsActivePort"
        active_port.unlink(missing_ok=True)

        browser_environment = self._browser_launch_environment()
        command = [
            self.playwright.chromium.executable_path,
            "--no-sandbox",
            f"--user-data-dir={profile_dir}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            *self._browser_args(),
            "about:blank",
        ]
        self.browser_process = subprocess.Popen(
            command,
            cwd=profile_dir,
            env=browser_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        deadline = time.monotonic() + BROWSER_CONNECT_TIMEOUT_SECONDS
        port: str | None = None
        while time.monotonic() < deadline:
            if self.browser_process.poll() is not None:
                raise RuntimeError(
                    f"Chromium exited during startup with code "
                    f"{self.browser_process.returncode}"
                )
            try:
                lines = active_port.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            if lines and lines[0].isdigit():
                port = lines[0]
                break
            await asyncio.sleep(0.1)

        if port is None:
            raise RuntimeError("Chromium did not publish a DevTools endpoint")

        connect_deadline = time.monotonic() + 8
        last_connect_error: Exception | None = None
        while time.monotonic() < connect_deadline:
            if self.browser_process.poll() is not None:
                raise RuntimeError(
                    f"Chromium exited before CDP attach with code "
                    f"{self.browser_process.returncode}"
                )
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}",
                    timeout=2_000,
                )
                break
            except Exception as exc:
                last_connect_error = exc
                await asyncio.sleep(0.2)
        if self.browser is None:
            raise RuntimeError(f"Chromium CDP attach failed: {last_connect_error}")
        scope_deadline = time.monotonic() + 3
        while time.monotonic() < scope_deadline:
            scope = self._scope_for_pid(self.browser_process.pid)
            if scope and scope.startswith("app-org.chromium.Chromium-"):
                break
            await asyncio.sleep(0.1)
        await asyncio.to_thread(self._configure_browser_scope)
        await asyncio.to_thread(self._resize_browser_window_for_rdp, browser_environment)
        if not self.browser.contexts:
            raise RuntimeError("Chromium exposed no persistent browser context")
        self.context = self.browser.contexts[0]
        await self._grant_clipboard_permissions()

    @staticmethod
    def _is_retryable_browser_start_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "chromium exited during startup with code -5" in text
            or "chromium exited before cdp attach with code -5" in text
        )

    def _stop_browser_process(self) -> None:
        process = self.browser_process
        self.browser_process = None
        virtual_display = self.virtual_display_process
        self.virtual_display_process = None
        self.browser_display = None
        self.browser_uses_virtual_display = False
        scope = self.browser_scope_unit
        self.browser_scope_unit = None
        profile_dir = self.browser_profile_dir
        self.browser_profile_dir = None
        if scope:
            self._stop_user_scope(scope)
        if process is not None:
            process_group = process.pid
            try:
                os.killpg(process_group, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.killpg(process_group, 0)
                except (ProcessLookupError, OSError):
                    break
                time.sleep(0.1)
            try:
                os.killpg(process_group, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if virtual_display is not None:
            try:
                os.killpg(virtual_display.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                virtual_display.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(virtual_display.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            except OSError:
                pass
        if profile_dir:
            self._remove_profile_runtime_artifacts(profile_dir)

    async def _warm_browser(self, reason: str) -> bool:
        """Launch Chromium on demand with the dedicated persistent profile."""
        if self.worker and self.context and self._browser_state == "warm":
            return True
        if not self._initialized:
            return False

        started = time.time()
        startup_attempts = 1
        self._browser_state = "starting"
        try:
            reuse_auth_browser = bool(
                self.context
                and self.browser_process
                and self.browser_process.poll() is None
                and self._last_browser_start
                and self._last_browser_start.get("auth_required")
            )
            if reuse_auth_browser:
                pages = [page for page in self.context.pages if not page.is_closed()]
                page = pages[-1] if pages else await self.context.new_page()
                log(f"Rechecking Gemini authentication ({reason})", "Worker")
            else:
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                log(
                    f"Launching Chromium ({reason}) with background throttling disabled",
                    "Worker",
                )
                try:
                    await self._launch_browser_context()
                except Exception as exc:
                    if not self._is_retryable_browser_start_error(exc):
                        raise
                    startup_attempts = 2
                    log(
                        "Chromium SIGTRAP during cold start; cleaning up and retrying once",
                        "Worker",
                    )
                    await self._close_browser_context(
                        "starting",
                        "recoverable Chromium startup SIGTRAP",
                    )
                    await self._launch_browser_context()
                page = await self._replace_restored_pages()
            worker = await self._initialize_page(page)
            if not worker:
                last_error = GeminiWebAutomation.get_all_errors().get(1) or {}
                detail = str(last_error.get("error") or "Gemini page did not become ready")
                if "authentication required" in detail.lower():
                    self._browser_state = "auth_required"
                    self._last_browser_start = {
                        "ok": False,
                        "auth_required": True,
                        "reason": reason,
                        "error": detail,
                        "duration_ms": int((time.time() - started) * 1000),
                        "startup_attempts": startup_attempts,
                        "at_unix": int(time.time()),
                    }
                    log(detail, "Worker")
                    if self.browser_uses_virtual_display:
                        log(
                            "Authentication needs a visible desktop; releasing private Xvfb",
                            "Worker",
                        )
                        await self._close_browser_context(
                            "auth_required",
                            "authentication requires visible desktop",
                        )
                    return False
                raise RuntimeError(detail)

            self.workers = [worker]
            self._needs_ready_reset = False
            self._browser_state = "warm"
            self._browser_start_count += 1
            self._last_browser_start = {
                "ok": True,
                "reason": reason,
                "duration_ms": int((time.time() - started) * 1000),
                "startup_attempts": startup_attempts,
                "at_unix": int(time.time()),
            }
            log("Gemini worker ready", "Worker")
            return True
        except Exception as exc:
            self._last_browser_start = {
                "ok": False,
                "reason": reason,
                "error": str(exc),
                "duration_ms": int((time.time() - started) * 1000),
                "startup_attempts": startup_attempts,
                "at_unix": int(time.time()),
            }
            log(f"Browser warm-up failed: {exc}", "Worker")
            await self._close_browser_context("error", reason)
            return False

    async def _ensure_browser_ready(self, reason: str) -> bool:
        if self.worker and self._initialized and self._browser_state == "warm":
            process = self.browser_process
            page = self.worker.page
            runtime_alive = bool(
                process
                and process.poll() is None
                and self.browser
                and self.browser.is_connected()
                and self.context
                and page
                and not page.is_closed()
            )
            if runtime_alive:
                return True

            log(
                f"Chromium disappeared while marked warm; recovering before {reason}",
                "Worker",
            )
            await self._close_browser_context(
                "error",
                f"browser runtime disappeared before {reason}",
            )
        return await self._warm_browser(reason)

    async def _close_browser_context(self, state: str, reason: str) -> None:
        release_playwright = state in {"cold", "error", "stopped"} or (
            state == "auth_required" and self.browser_uses_virtual_display
        )
        worker = self.worker
        self.workers.clear()
        if worker:
            try:
                await worker.close()
            except Exception as exc:
                log(f"Worker close warning: {exc}", "Worker")
        if self.browser:
            try:
                await self.browser.close()
            except Exception as exc:
                log(f"Browser close warning: {exc}", "Worker")
            finally:
                self.browser = None
        self.context = None
        self._needs_ready_reset = False
        if self.browser_process or self.virtual_display_process:
            await asyncio.to_thread(self._stop_browser_process)
        if release_playwright and self.playwright:
            try:
                await self.playwright.stop()
            except Exception as exc:
                log(f"Playwright stop warning: {exc}", "Worker")
            finally:
                self.playwright = None
        self._browser_state = state
        if state == "cold":
            self._browser_stop_count += 1
            self._last_browser_stop = {
                "reason": reason,
                "at_unix": int(time.time()),
            }
            log(f"Chromium stopped after idle period: {reason}", "Worker")

    async def _idle_watchdog(self) -> None:
        """Stop Chromium after a quiet period; requests wake it under the queue lock."""
        if self._idle_browser_seconds <= 0:
            return
        try:
            while self._initialized:
                await asyncio.sleep(self._idle_poll_seconds)
                idle_for = time.time() - self._last_activity
                idle_limit = (
                    AUTH_REQUIRED_IDLE_SECONDS
                    if self._browser_state == "auth_required"
                    else self._idle_browser_seconds
                )
                if (
                    self._browser_state not in {"warm", "auth_required"}
                    or self._active_request
                    or self._queued_requests
                    or idle_for < idle_limit
                    or self._request_lock.locked()
                ):
                    continue
                async with self._request_lock:
                    idle_for = time.time() - self._last_activity
                    idle_limit = (
                        AUTH_REQUIRED_IDLE_SECONDS
                        if self._browser_state == "auth_required"
                        else self._idle_browser_seconds
                    )
                    if (
                        self._browser_state in {"warm", "auth_required"}
                        and not self._active_request
                        and not self._queued_requests
                        and idle_for >= idle_limit
                    ):
                        await self._close_browser_context(
                            "cold",
                            f"{int(idle_for)}s without requests",
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"Idle browser watchdog error: {exc}", "Worker")

    async def _replace_restored_pages(self) -> Page:
        """Create the managed page before closing restored Chromium pages."""
        restored = [page for page in self.context.pages if not page.is_closed()]
        managed = await self.context.new_page()
        for page in restored:
            try:
                await asyncio.wait_for(page.close(), timeout=5)
                self._startup_pages_closed += 1
            except Exception as exc:
                log(f"Could not close restored page: {exc}", "Worker")
        return managed

    async def _initialize_page(self, page: Page) -> Optional[GeminiWebAutomation]:
        try:
            await page.goto(GeminiWebAutomation.CHAT_URL, timeout=60_000, wait_until="domcontentloaded")
        except Exception as exc:
            log(f"Initial navigation warning: {exc}", "Worker")

        worker = GeminiWebAutomation(worker_id=1)
        if await worker.init_with_page(page, self.context):
            try:
                if await asyncio.wait_for(worker.prepare_next_request(), timeout=20):
                    return worker
                log("New Gemini page did not reach a clean /app state", "Worker")
            except Exception as exc:
                log(f"New Gemini page readiness failed: {exc}", "Worker")
        last_error = GeminiWebAutomation.get_all_errors().get(1) or {}
        if "authentication required" in str(last_error.get("error") or "").lower():
            return None
        try:
            await page.close()
        except Exception:
            pass
        return None

    async def _recreate_worker(self, reason: str) -> bool:
        """Replace a failed tab once; never refresh a healthy idle tab."""
        async with self._recovery_lock:
            success = False
            try:
                if not self.context:
                    raise RuntimeError("browser context is unavailable")
                old_worker = self.worker
                new_page = await self.context.new_page()
                new_worker = await self._initialize_page(new_page)
                success = new_worker is not None
                if success:
                    self.workers = [new_worker]
                    self._needs_ready_reset = False
                    if old_worker:
                        try:
                            await old_worker.close()
                        except Exception as exc:
                            log(f"Old worker close warning: {exc}", "Worker")
                    self._worker_recreation_count += 1
            except Exception as exc:
                log(f"Worker recreation error: {exc}", "Worker")
            self._last_worker_recreation = {
                "ok": success,
                "reason": reason,
                "at_unix": int(time.time()),
            }
            log(f"Worker recreation {'succeeded' if success else 'failed'}: {reason}", "Worker")
            return success

    async def _prepare_request_configuration(
        self,
        model: str | None,
        thinking_level: str | None,
        request_id: str | None,
    ) -> tuple[bool, str | None]:
        """Apply Gemini UI configuration before request execution.

        Gemini's current web app can leave a newly selected model attached to
        the old tab's generation session. Treat a model-family change as a tab
        boundary: persist the selection, replace the managed tab, then verify
        the requested configuration on the new tab before sending anything.
        """
        worker = self.worker
        if not worker:
            return False, "Gemini worker disappeared"

        target_thinking = thinking_level or "Medium"
        if (
            model
            and worker._current_selected_model == model
            and worker._current_selected_thinking_level == target_thinking
        ):
            return True, None

        ok, model_family_changed = await worker.ensure_configuration(model, thinking_level)
        if not ok:
            return False, "Requested Gemini configuration could not be confirmed"

        if not model_family_changed:
            return True, None

        log(f"[{request_id}] Model family changed; recreating managed Gemini tab", "Worker")
        await asyncio.sleep(1.5)
        if not await self._recreate_worker(f"model family change for request {request_id}"):
            return False, "Gemini tab recreation after model-family change failed"

        worker = self.worker
        if not worker:
            return False, "Gemini worker disappeared after model-family change"

        ok, changed_again = await worker.ensure_configuration(model, thinking_level)
        if not ok or changed_again:
            return False, "Requested Gemini configuration did not persist across tab recreation"
        return True, None

    @staticmethod
    def _is_transient_gemini_refusal(response: str) -> bool:
        normalized = " ".join((response or "").strip().lower().split())
        return (
            normalized.startswith("i'm having a hard time fulfilling your request.")
            and "something else instead?" in normalized
        ) or normalized in {
            "sorry, something went wrong. please try your request again.",
            "i encountered an error doing what you asked. could you try again?",
            "i seem to be encountering an error. can i try something else for you?",
        }

    @staticmethod
    def _is_retryable_response_rejection(result: Dict) -> bool:
        """Retry only when Gemini definitely ended the submitted generation transiently."""
        response = str(result.get("response") or "")
        if WorkerPool._is_transient_gemini_refusal(response):
            return True
        error = " ".join(str(result.get("error") or "").strip().lower().split())
        if error == "stalled generation" or error.startswith("stalled generation:"):
            return True
        return error in {
            "gemini generation ended without a response",
            "gemini entered a non-copyable terminal response state",
            "previous generation is still active before send",
        }

    async def send_message(
        self,
        prompt: str,
        model: str | None = None,
        thinking_level: str | None = None,
        use_search: bool = False,
        images: List[str] | None = None,
        request_id: str | None = None,
    ) -> Dict:
        """Serialize one logical Gemini request with bounded recovery."""
        if not self._initialized:
            return {"success": False, "error": "Gemini worker pool is not ready"}

        request_started = time.monotonic()
        timing: Dict[str, Any] = {"request_id": request_id}
        execution_started: float | None = None
        browser_resources_before: Dict[str, int] | None = None
        queued_at = time.time()
        self._queued_requests += 1
        waiting = True
        try:
            async with self._request_lock:
                self._queued_requests -= 1
                waiting = False
                wait_ms = int((time.time() - queued_at) * 1000)
                timing["queue_wait_ms"] = wait_ms
                if wait_ms >= 250:
                    log(f"[{request_id}] Queue wait: {wait_ms}ms", "Worker")

                self._active_request = True
                phase_started = time.monotonic()
                browser_ready = await self._ensure_browser_ready(f"request {request_id}")
                timing["browser_ready_ms"] = int(
                    (time.monotonic() - phase_started) * 1000
                )
                if not browser_ready:
                    startup_error = str(
                        (self._last_browser_start or {}).get("error")
                        or "browser could not be started"
                    )
                    return {
                        "success": False,
                        "error": f"Gemini request failed: {startup_error}",
                        "backend_unavailable": "authentication required" in startup_error.lower(),
                        "auth_required": "authentication required" in startup_error.lower(),
                        "queue_wait_ms": wait_ms,
                        "attempts": 0,
                    }
                browser_pid = self.browser_process.pid if self.browser_process else None
                if browser_pid:
                    browser_resources_before = self._cgroup_metrics_for_pid(browser_pid)

                if self._needs_ready_reset:
                    phase_started = time.monotonic()
                    reset_ready = await self._ensure_ready_after_request(
                        f"before request {request_id}"
                    )
                    timing["deferred_reset_ms"] = int(
                        (time.monotonic() - phase_started) * 1000
                    )
                    if not reset_ready:
                        browser_ready = await self._ensure_browser_ready(
                            f"recover after failed ready reset for request {request_id}"
                        )
                        if not browser_ready:
                            return {
                                "success": False,
                                "error": (
                                    "Gemini request failed: previous chat could not be reset"
                                ),
                                "queue_wait_ms": wait_ms,
                                "attempts": 0,
                            }
                    self._needs_ready_reset = False
                else:
                    timing["deferred_reset_ms"] = 0

                phase_started = time.monotonic()
                configured, error = await self._prepare_request_configuration(
                    model, thinking_level, request_id
                )
                timing["configuration_ms"] = int(
                    (time.monotonic() - phase_started) * 1000
                )
                if not configured:
                    return {
                        "success": False,
                        "error": f"Gemini request failed: {error}",
                        "queue_wait_ms": wait_ms,
                        "attempts": 0,
                    }

                last_error = "unknown"
                native_retry_used = False
                max_attempts = 3
                request_deadline = time.monotonic() + REQUEST_TOTAL_BUDGET_SECONDS
                execution_started = time.monotonic()

                for attempt in range(1, max_attempts + 1):
                    remaining = request_deadline - time.monotonic()
                    if remaining <= 0:
                        last_error = (
                            f"Gemini request budget exhausted after "
                            f"{REQUEST_TOTAL_BUDGET_SECONDS}s"
                        )
                        break

                    worker = self.worker
                    if not worker:
                        last_error = "Gemini worker disappeared"
                        break

                    try:
                        result = await asyncio.wait_for(
                            worker.send_message(
                                prompt,
                                model,
                                thinking_level,
                                use_search,
                                images,
                                request_id=request_id,
                            ),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        result = {
                            "success": False,
                            "error": (
                                f"Gemini request budget exhausted after "
                                f"{REQUEST_TOTAL_BUDGET_SECONDS}s"
                            ),
                        }
                    except Exception as exc:
                        result = {"success": False, "error": str(exc)}

                    if (
                        not native_retry_used
                        and result.get("success")
                        and self._is_transient_gemini_refusal(
                            str(result.get("response") or "")
                        )
                    ):
                        native_retry_used = True
                        native_retry = getattr(worker, "retry_last_response_native", None)
                        if callable(native_retry):
                            try:
                                retried = await native_retry()
                            except Exception as exc:
                                log(
                                    f"[{request_id}] Native retry failed "
                                    f"({type(exc).__name__}); using fresh-tab recovery",
                                    "Worker",
                                )
                            else:
                                if retried is not None:
                                    result = retried

                    response = str(result.get("response") or "")
                    web_execution = result.get("web_execution_ms")
                    if isinstance(web_execution, dict):
                        timing["web_execution_ms"] = dict(web_execution)
                    retryable = self._is_retryable_response_rejection(result)
                    if result.get("success") and response.strip():
                        if not self._is_transient_gemini_refusal(response):
                            result["queue_wait_ms"] = wait_ms
                            result["attempts"] = attempt
                            result["effective_thinking_level"] = thinking_level
                            self._needs_ready_reset = True
                            result["ready_for_next_request"] = False
                            return result
                        last_error = "Gemini returned its generic transient refusal"
                    else:
                        last_error = str(result.get("error") or "Empty response")

                    if attempt < max_attempts and retryable:
                        reason = (
                            "transient_refusal"
                            if self._is_transient_gemini_refusal(response)
                            else "transient_generation_failure"
                        )
                        try:
                            await worker._save_diagnostic_artifacts(
                                reason, request_id=request_id
                            )
                        except Exception as exc:
                            log(
                                f"[{request_id}] Diagnostics failed "
                                f"({type(exc).__name__}); continuing recovery",
                                "Worker",
                            )

                        log(
                            f"[{request_id}] Transient Gemini failure; "
                            f"recreating tab before retry {attempt + 1}/{max_attempts}",
                            "Worker",
                        )
                        ready = await self._recreate_worker(
                            f"retry {attempt + 1}/{max_attempts} for request {request_id}"
                        )
                        if ready:
                            configured, error = await self._prepare_request_configuration(
                                model, thinking_level, request_id
                            )
                            if configured:
                                continue
                            last_error = f"Gemini request failed: {error}"

                    recovered = await self._ensure_ready_after_request(
                        f"request {request_id} failed"
                    )
                    self._needs_ready_reset = not recovered
                    break

                log(f"[{request_id}] Request failed: {last_error}", "Worker")
                return {
                    "success": False,
                    "error": f"Gemini request failed: {last_error}",
                    "queue_wait_ms": wait_ms,
                    "attempts": max(1, locals().get("attempt", 0)),
                    "effective_thinking_level": thinking_level,
                }
        finally:
            if execution_started is not None:
                timing["execution_ms"] = int(
                    (time.monotonic() - execution_started) * 1000
                )
            timing["total_ms"] = int((time.monotonic() - request_started) * 1000)
            browser_pid = self.browser_process.pid if self.browser_process else None
            if browser_pid:
                resources = self._cgroup_metrics_for_pid(browser_pid)
                if resources:
                    timing["browser_resources_after"] = resources
                    before_cpu = (
                        browser_resources_before or {}
                    ).get("cpu_usage_usec")
                    after_cpu = resources.get("cpu_usage_usec")
                    if isinstance(before_cpu, int) and isinstance(after_cpu, int):
                        timing["browser_cpu_ms"] = max(
                            0,
                            (after_cpu - before_cpu) // 1000,
                        )
            self._last_request_timing = timing
            if waiting:
                self._queued_requests = max(0, self._queued_requests - 1)
            else:
                self._active_request = False
            self._last_activity = time.time()


    async def _ensure_ready_after_request(self, reason: str) -> bool:
        """Leave the serialized worker in a clean state before releasing it."""
        started = time.time()
        ok = False
        error = None
        try:
            worker = self.worker
            if worker and self._initialized:
                ok = await asyncio.wait_for(worker.prepare_next_request(), timeout=20)
            else:
                error = "worker unavailable"
        except Exception as exc:
            error = str(exc)
            log(f"Ready-state reset failed: {exc}", "Worker")

        if not ok and self._initialized:
            ok = await self._recreate_worker(f"ready reset: {reason}")
            if ok:
                error = None

        if not ok:
            error = error or "worker could not be restored to ready state"
            # A poisoned tab should not poison the resident API process. Drop
            # the whole browser runtime and let the next request cold-start a
            # clean one under the same serialized request lock.
            await self._close_browser_context("error", reason)

        self._last_ready_reset = {
            "ok": ok,
            "reason": reason,
            "error": error,
            "duration_ms": int((time.time() - started) * 1000),
            "at_unix": int(time.time()),
        }
        log(f"Next-request state {'ready' if ok else 'unavailable'}: {reason}", "Worker")
        return ok

    def get_diagnostics(self) -> Dict[str, Any]:
        worker = self.worker
        idle_for = max(0, int(time.time() - self._last_activity))
        browser_pid = self.browser_process.pid if self.browser_process else None
        return {
            "initialized": self._initialized,
            "parallel_capacity": 1,
            "active_request": self._active_request,
            "queued_requests": self._queued_requests,
            "browser_state": self._browser_state,
            "idle_browser_seconds": int(self._idle_browser_seconds),
            "idle_for_seconds": idle_for,
            "browser_start_count": self._browser_start_count,
            "browser_stop_count": self._browser_stop_count,
            "last_browser_start": self._last_browser_start,
            "last_browser_stop": self._last_browser_stop,
            "browser_channel": "playwright-chromium-cdp",
            "browser_pid": browser_pid,
            "browser_display": self.browser_display,
            "browser_virtual_display": self.browser_uses_virtual_display,
            "browser_scope_unit": self.browser_scope_unit,
            "browser_resources": (
                self._cgroup_metrics_for_pid(browser_pid)
                if browser_pid
                else {}
            ),
            "profile_state_present": self._profile_dir().is_dir(),
            "runtime_profile_active": bool(self.browser_profile_dir),
            "startup_pages_closed": self._startup_pages_closed,
            "worker_recreation_count": self._worker_recreation_count,
            "last_worker_recreation": self._last_worker_recreation,
            "last_ready_reset": self._last_ready_reset,
            "last_request_timing": self._last_request_timing,
            "needs_ready_reset": self._needs_ready_reset,
            "last_activity_unix": int(self._last_activity),
            "workers": [{
                "worker_id": 1,
                "initialized": bool(worker and worker._initialized),
                "busy": self._active_request,
                "generation_in_progress": bool(worker and worker._generation_in_progress),
                "last_error": GeminiWebAutomation.get_all_errors().get(1),
            }],
        }

    async def get_live_diagnostics(self) -> Dict[str, Any]:
        result = self.get_diagnostics()
        worker = self.worker
        if not worker:
            return result
        info = result["workers"][0]
        try:
            snapshot = await asyncio.wait_for(worker._capture_state_snapshot(), timeout=5)
            keys = (
                "page_title", "url", "phase", "stop_visible", "send_visible",
                "input_visible", "input_text_len", "user_query_count", "response_count",
                "thinking_label", "thinking_active", "error_page_500",
                "model_button_text", "model_button_aria_label",
                "model_picker_count", "sign_in_visible", "ui_state_hint",
            )
            info["current_state"] = {key: snapshot.get(key) for key in keys}
            violations = []
            if snapshot.get("stop_visible") and not info["generation_in_progress"]:
                violations.append("stop_visible_while_worker_idle")
            if (
                int(snapshot.get("input_text_len") or 0) > 0
                and int(snapshot.get("user_query_count") or 0) > 0
                and not info["generation_in_progress"]
            ):
                violations.append("retained_prompt_while_worker_idle")
            info["invariant_violations"] = violations
            info["selected_model"] = worker._current_selected_model
            info["selected_thinking_level"] = worker._current_selected_thinking_level
        except Exception as exc:
            info["current_state_error"] = str(exc)
        return result

    async def close(self) -> None:
        self._initialized = False
        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            finally:
                self._idle_task = None
        await self._close_browser_context("stopped", "pool shutdown")
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as exc:
                log(f"Playwright close warning: {exc}", "Worker")
            finally:
                self.playwright = None
