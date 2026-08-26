"""One resilient Gemini Web worker with queued request execution."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from playwright.async_api import Page, Route, async_playwright

from gemini_web import GeminiWebAutomation, LOW_MEMORY_MODE, log
from notifier import notify_error


class WorkerPool:
    """Serialize requests through one tab and recreate it after concrete failures."""

    def __init__(self):
        self.browser_channel = os.getenv("BROWSER_CHANNEL", "").strip()
        self.playwright = None
        self.context = None
        self.workers: List[GeminiWebAutomation] = []
        self._request_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._initialized = False
        self._active_request = False
        self._queued_requests = 0
        self._last_activity = time.time()
        self._startup_pages_closed = 0
        self._worker_recreation_count = 0
        self._last_worker_recreation: Optional[Dict[str, Any]] = None
        self._prewarm_task: Optional[asyncio.Task] = None
        self._last_idle_prewarm: Optional[Dict[str, Any]] = None

    @property
    def worker(self) -> Optional[GeminiWebAutomation]:
        return self.workers[0] if self.workers else None

    async def init(self) -> bool:
        """Launch the persistent browser profile and initialize one managed tab."""
        try:
            self.playwright = await async_playwright().start()
            headless = os.getenv("HEADLESS", "false").lower() == "true"
            args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,BatterySaverModeAvailability",
            ]
            if headless:
                args.extend(["--disable-gpu", "--disable-software-rasterizer"])
            if LOW_MEMORY_MODE:
                args.extend([
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-translate",
                    "--no-first-run",
                    "--disable-default-apps",
                ])

            log(
                "Chromium background throttling and native occlusion are disabled",
                "Worker",
            )

            launch_options: Dict[str, Any] = {}
            if self.browser_channel:
                launch_options["channel"] = self.browser_channel

            profile_dir = os.path.join(os.path.dirname(__file__), ".browser_session")
            self.context = await self.playwright.chromium.launch_persistent_context(
                profile_dir,
                headless=headless,
                args=args,
                permissions=["clipboard-read", "clipboard-write"],
                viewport={"width": 1600, "height": 900},
                **launch_options,
            )
            if LOW_MEMORY_MODE:
                await self.context.route("**/*", self._block_resources)

            page = await self._replace_restored_pages()
            worker = await self._initialize_page(page)
            if not worker:
                await self.close()
                return False

            self.workers = [worker]
            self._initialized = True
            log("Gemini worker ready", "Worker")
            return True
        except Exception as exc:
            log(f"Startup failed: {exc}", "Worker")
            await self.close()
            return False

    async def _replace_restored_pages(self) -> Page:
        """Create the managed page before closing pages restored by Chromium."""
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
            return worker
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

    @staticmethod
    def _is_network_outage(error: str) -> bool:
        return (error or "").strip().lower().startswith("network outage:")

    async def send_message(
        self,
        prompt: str,
        model: str | None = None,
        thinking_level: str | None = None,
        use_search: bool = False,
        images: List[str] | None = None,
        request_id: str | None = None,
    ) -> Dict:
        if not self.worker or not self._initialized:
            return {"success": False, "error": "Gemini worker is not ready"}

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
                attempt_logs: List[str] = []
                last_error = "unknown"
                for attempt in (1, 2):
                    worker = self.worker
                    if not worker:
                        last_error = "Gemini worker disappeared"
                        break
                    try:
                        result = await worker.send_message(
                            prompt,
                            model,
                            thinking_level,
                            use_search,
                            images,
                            request_id=request_id,
                        )
                    except Exception as exc:
                        result = {"success": False, "error": str(exc)}

                    request_log = worker.get_request_log()
                    if request_log:
                        attempt_logs.append(f"--- Attempt {attempt} ---")
                        attempt_logs.extend(request_log)

                    if result.get("success") and str(result.get("response") or "").strip():
                        result["queue_wait_ms"] = wait_ms
                        result["attempts"] = attempt
                        self._schedule_idle_prewarm()
                        return result

                    last_error = result.get("error") or "Empty response"
                    log(f"[{request_id}] Attempt {attempt} failed: {last_error}", "Worker")
                    if attempt == 2 or self._is_network_outage(last_error):
                        break
                    if not await self._recreate_worker(last_error):
                        break

                await self._notify_final_failure(last_error)
                return {
                    "success": False,
                    "error": f"Gemini request failed after retry: {last_error}",
                    "attempt_logs": attempt_logs,
                    "queue_wait_ms": wait_ms,
                    "attempts": attempt,
                }
        finally:
            if waiting:
                self._queued_requests -= 1
            else:
                self._active_request = False
            self._last_activity = time.time()

    def _schedule_idle_prewarm(self) -> None:
        """Prepare the next Temporary Chat only when no request is queued."""
        if self._queued_requests > 0 or not self._initialized:
            return
        if self._prewarm_task and not self._prewarm_task.done():
            return
        self._prewarm_task = asyncio.create_task(self._prewarm_when_idle())

    async def _prewarm_when_idle(self) -> None:
        await asyncio.sleep(0)
        started = time.time()
        ok = False
        error = None
        attempted = False
        try:
            async with self._request_lock:
                if self._queued_requests > 0 or not self._initialized or not self.worker:
                    return
                attempted = True
                ok = await asyncio.wait_for(self.worker.prepare_idle(), timeout=20)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc)
            log(f"Idle Temporary Chat preparation failed: {exc}", "Worker")
        finally:
            if attempted:
                self._last_idle_prewarm = {
                    "ok": ok,
                    "error": error,
                    "duration_ms": int((time.time() - started) * 1000),
                    "at_unix": int(time.time()),
                }

    async def _notify_final_failure(self, error: str) -> None:
        try:
            tracked = GeminiWebAutomation.get_all_errors().get(1) or {}
            await notify_error(
                error=f"Gemini request failed after retry: {error}",
                selector_key=tracked.get("selector_key") or "worker",
                action=tracked.get("action") or "final_failure",
                worker_id=1,
                diagnostics=tracked.get("diagnostics") or {"error": error},
            )
        except Exception as exc:
            log(f"Failure notification failed: {exc}", "Worker")

    def get_diagnostics(self) -> Dict[str, Any]:
        worker = self.worker
        return {
            "initialized": self._initialized,
            "parallel_capacity": 1,
            "active_request": self._active_request,
            "queued_requests": self._queued_requests,
            "browser_channel": self.browser_channel or "chromium",
            "startup_pages_closed": self._startup_pages_closed,
            "worker_recreation_count": self._worker_recreation_count,
            "last_worker_recreation": self._last_worker_recreation,
            "last_idle_prewarm": self._last_idle_prewarm,
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
                "thinking_label", "thinking_active", "error_page_500", "chat_mode_active",
                "spark_mode_active", "model_button_text", "model_button_aria_label",
                "model_picker_count", "sign_in_visible", "ui_state_hint",
            )
            info["current_state"] = {key: snapshot.get(key) for key in keys}
            violations = []
            if snapshot.get("spark_mode_active"):
                violations.append("spark_mode_active")
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

    async def _block_resources(self, route: Route) -> None:
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    async def close(self) -> None:
        self._initialized = False
        if self._prewarm_task and not self._prewarm_task.done():
            self._prewarm_task.cancel()
            try:
                await self._prewarm_task
            except asyncio.CancelledError:
                pass
        self._prewarm_task = None
        worker = self.worker
        self.workers.clear()
        if worker:
            try:
                await worker.close()
            except Exception as exc:
                log(f"Worker close warning: {exc}", "Worker")
        if self.context:
            try:
                await self.context.close()
            except Exception as exc:
                log(f"Browser context close warning: {exc}", "Worker")
            finally:
                self.context = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as exc:
                log(f"Playwright close warning: {exc}", "Worker")
            finally:
                self.playwright = None
