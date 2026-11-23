"""One resilient Gemini Web worker with queued request execution."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
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


class WorkerPool:
    """Serialize requests through one tab and recreate it after concrete failures."""

    def __init__(self, idle_browser_seconds: float = 60, idle_poll_seconds: float = 5):
        self.playwright = None
        self.browser = None
        self.browser_process: subprocess.Popen | None = None
        self.browser_scope_unit: str | None = None
        self.browser_profile_dir: Path | None = None
        self.browser_cdp_session = None
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
            "--window-size=1280,720",
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
    def _state_dir() -> Path:
        return Path(__file__).resolve().parent / ".browser_session"

    @classmethod
    def _auth_state_path(cls) -> Path:
        return cls._state_dir() / "auth.json"

    @classmethod
    def _new_runtime_profile(cls) -> Path:
        state_dir = cls._state_dir()
        state_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            state_dir.chmod(0o700)
        except OSError:
            pass
        return Path(tempfile.mkdtemp(prefix="runtime-", dir=state_dir))

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

    @classmethod
    def _is_runtime_browser_command(cls, command: bytes) -> bool:
        normalized = command.replace(b"\0", b" ")
        profile_prefix = f"--user-data-dir={cls._state_dir() / 'runtime-'}".encode()
        return profile_prefix in normalized and b" --type=" not in normalized

    @classmethod
    def _runtime_browser_pids(cls) -> list[int]:
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
            if not cls._is_runtime_browser_command(command):
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
        initial = cls._runtime_browser_pids()
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
        remaining = cls._runtime_browser_pids()
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining = cls._runtime_browser_pids()

        for pid in remaining:
            try:
                os.killpg(pid, signal.SIGKILL)
                cleaned += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass

        if not cls._runtime_browser_pids():
            state_dir = cls._state_dir()
            for runtime_dir in state_dir.glob("runtime-*"):
                if runtime_dir.is_dir():
                    shutil.rmtree(runtime_dir, ignore_errors=True)
        if cleaned:
            log(f"Retired {cleaned} stale Chromium runtime(s)", "Worker")

    @staticmethod
    def _is_google_cookie(cookie: dict[str, Any]) -> bool:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        return domain == "google.com" or domain.endswith(".google.com")

    @staticmethod
    def _cookie_param(cookie: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: cookie[key]
            for key in (
                "name",
                "value",
                "domain",
                "path",
                "secure",
                "httpOnly",
                "sameSite",
            )
            if key in cookie
        }
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            result["expires"] = expires
        return result

    async def _load_auth_state(self) -> None:
        auth_state = self._auth_state_path()
        if not auth_state.is_file() or not self.browser_cdp_session:
            return
        try:
            payload = json.loads(auth_state.read_text(encoding="utf-8"))
            cookies = payload.get("cookies") if isinstance(payload, dict) else None
            if not isinstance(cookies, list):
                raise ValueError("auth cookie file has no cookies array")
            await self.browser_cdp_session.send(
                "Storage.setCookies",
                {"cookies": cookies},
            )
            log(f"Loaded {len(cookies)} saved Gemini auth cookie(s)", "Worker")
        except Exception as exc:
            log(f"Saved Gemini authentication state could not be loaded: {exc}", "Worker")

    async def _save_auth_state(self) -> None:
        if not self.browser_cdp_session:
            return
        auth_state = self._auth_state_path()
        auth_state.parent.mkdir(mode=0o700, exist_ok=True)
        temporary = auth_state.with_suffix(".tmp")
        try:
            response = await self.browser_cdp_session.send(
                "Storage.getCookies",
            )
            cookies = [
                self._cookie_param(cookie)
                for cookie in response.get("cookies", [])
                if isinstance(cookie, dict) and self._is_google_cookie(cookie)
            ]
            temporary.write_text(
                json.dumps({"cookies": cookies}, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, auth_state)
            log(f"Saved {len(cookies)} Gemini auth cookie(s)", "Worker")
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    async def _grant_clipboard_permissions(self) -> None:
        if not self.browser_cdp_session:
            raise RuntimeError("Chromium browser CDP session is unavailable")
        for permission in (
            {"name": "clipboard-read"},
            {"name": "clipboard-write", "allowWithoutSanitization": True},
        ):
            await self.browser_cdp_session.send(
                "Browser.setPermission",
                {
                    "permission": permission,
                    "setting": "granted",
                    "origin": "https://gemini.google.com",
                },
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

    async def _launch_browser_context(self) -> None:
        """Launch Chromium directly, then attach Playwright over local CDP.

        """
        if self.playwright is None:
            raise RuntimeError("Playwright is not initialized")

        await asyncio.to_thread(self._cleanup_stale_browser_processes)
        profile_dir = self._new_runtime_profile()
        self.browser_profile_dir = profile_dir
        active_port = profile_dir / "DevToolsActivePort"
        active_port.unlink(missing_ok=True)

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
            env=self._browser_environment(),
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
        if not self.browser.contexts:
            raise RuntimeError("Chromium exposed no persistent browser context")
        self.context = self.browser.contexts[0]
        self.browser_cdp_session = await self.browser.new_browser_cdp_session()
        await self._load_auth_state()
        await self._grant_clipboard_permissions()

    def _stop_browser_process(self) -> None:
        process = self.browser_process
        self.browser_process = None
        scope = self.browser_scope_unit
        self.browser_scope_unit = None
        profile_dir = self.browser_profile_dir
        self.browser_profile_dir = None
        if scope:
            self._stop_user_scope(scope)
        if process is None:
            if profile_dir:
                shutil.rmtree(profile_dir, ignore_errors=True)
            return
        process_group = process.pid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                break
            except OSError:
                break
            time.sleep(0.1)
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)

    async def _warm_browser(self, reason: str) -> bool:
        """Launch Chromium on demand with a disposable profile and saved auth."""
        if self.worker and self.context and self._browser_state == "warm":
            return True
        if not self._initialized:
            return False

        started = time.time()
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
                        "at_unix": int(time.time()),
                    }
                    log(detail, "Worker")
                    return False
                raise RuntimeError(detail)

            self.workers = [worker]
            await self._save_auth_state()
            self._browser_state = "warm"
            self._browser_start_count += 1
            self._last_browser_start = {
                "ok": True,
                "reason": reason,
                "duration_ms": int((time.time() - started) * 1000),
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
                "at_unix": int(time.time()),
            }
            log(f"Browser warm-up failed: {exc}", "Worker")
            await self._close_browser_context("error", reason)
            return False

    async def _ensure_browser_ready(self, reason: str) -> bool:
        if self.worker and self._initialized and self._browser_state != "cold":
            self._browser_state = "warm"
            return True
        return await self._warm_browser(reason)

    async def _close_browser_context(self, state: str, reason: str) -> None:
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
        self.browser_cdp_session = None
        self.context = None
        if self.browser_process:
            await asyncio.to_thread(self._stop_browser_process)
        if state in {"cold", "error", "stopped"} and self.playwright:
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

        queued_at = time.time()
        self._queued_requests += 1
        waiting = True
        try:
            async with self._request_lock:
                self._queued_requests -= 1
                waiting = False
                wait_ms = int((time.time() - queued_at) * 1000)
                if wait_ms >= 250:
                    log(f"[{request_id}] Queue wait: {wait_ms}ms", "Worker")

                self._active_request = True
                if not await self._ensure_browser_ready(f"request {request_id}"):
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

                configured, error = await self._prepare_request_configuration(
                    model, thinking_level, request_id
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
                    retryable = self._is_retryable_response_rejection(result)
                    if result.get("success") and response.strip():
                        if not self._is_transient_gemini_refusal(response):
                            result["queue_wait_ms"] = wait_ms
                            result["attempts"] = attempt
                            result["effective_thinking_level"] = thinking_level
                            result["ready_for_next_request"] = (
                                await self._ensure_ready_after_request(
                                    f"request {request_id} completed"
                                )
                            )
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

                    await self._ensure_ready_after_request(
                        f"request {request_id} failed"
                    )
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
            "browser_pid": self.browser_process.pid if self.browser_process else None,
            "browser_scope_unit": self.browser_scope_unit,
            "auth_state_present": self._auth_state_path().is_file(),
            "runtime_profile_active": bool(self.browser_profile_dir),
            "startup_pages_closed": self._startup_pages_closed,
            "worker_recreation_count": self._worker_recreation_count,
            "last_worker_recreation": self._last_worker_recreation,
            "last_ready_reset": self._last_ready_reset,
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
