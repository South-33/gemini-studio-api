"""One resilient Gemini Web worker with queued request execution."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from playwright.async_api import Page, async_playwright

from gemini_web import GeminiWebAutomation, log
from request_log import write_request_log


class WorkerPool:
    """Serialize requests through one tab and recreate it after concrete failures."""

    def __init__(self, idle_browser_seconds: float = 300, idle_poll_seconds: float = 10):
        self.playwright = None
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

    async def init(self) -> bool:
        """Start Playwright, warm one browser, and begin idle browser lifecycle."""
        try:
            self.playwright = await async_playwright().start()
            self._initialized = True
            if not await self._warm_browser("startup"):
                raise RuntimeError("Gemini browser failed to initialize")
            self._last_activity = time.time()
            self._idle_task = asyncio.create_task(self._idle_watchdog())
            return True
        except Exception as exc:
            log(f"Startup failed: {exc}", "Worker")
            await self.close()
            return False

    @staticmethod
    def _browser_args() -> List[str]:
        return [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,BatterySaverModeAvailability",
        ]

    async def _warm_browser(self, reason: str) -> bool:
        """Launch Chromium on demand with the persistent authenticated profile."""
        if self.worker and self.context and self._browser_state == "warm":
            return True
        if not self._initialized or not self.playwright:
            return False

        started = time.time()
        self._browser_state = "starting"
        try:
            log(
                f"Launching Chromium ({reason}) with native viewport and background throttling disabled",
                "Worker",
            )
            profile_dir = os.path.join(os.path.dirname(__file__), ".browser_session")
            self.context = await self.playwright.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                args=self._browser_args(),
                permissions=["clipboard-read", "clipboard-write"],
                no_viewport=True,
            )
            page = await self._replace_restored_pages()
            worker = await self._initialize_page(page)
            if not worker:
                raise RuntimeError("Gemini page did not become ready")

            self.workers = [worker]
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
        if self.context:
            try:
                await self.context.close()
            except Exception as exc:
                log(f"Browser context close warning: {exc}", "Worker")
            finally:
                self.context = None
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
                if (
                    self._browser_state != "warm"
                    or self._active_request
                    or self._queued_requests
                    or idle_for < self._idle_browser_seconds
                    or self._request_lock.locked()
                ):
                    continue
                async with self._request_lock:
                    idle_for = time.time() - self._last_activity
                    if (
                        self._browser_state == "warm"
                        and not self._active_request
                        and not self._queued_requests
                        and idle_for >= self._idle_browser_seconds
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
            try:
                if await asyncio.wait_for(worker.prepare_next_request(), timeout=20):
                    return worker
                log("New Gemini page did not reach a clean /app state", "Worker")
            except Exception as exc:
                log(f"New Gemini page readiness failed: {exc}", "Worker")
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
        """Retry only for Gemini's concrete transient refusal."""
        response = str(result.get("response") or "")
        return WorkerPool._is_transient_gemini_refusal(response)

    async def send_message(
        self,
        prompt: str,
        model: str | None = None,
        thinking_level: str | None = None,
        use_search: bool = False,
        images: List[str] | None = None,
        request_id: str | None = None,
        request_context: Dict[str, Any] | None = None,
    ) -> Dict:
        if not self._initialized:
            return {"success": False, "error": "Gemini worker pool is not ready"}

        queued_at = time.time()
        queued_at_iso = datetime.now(timezone.utc).isoformat()
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
                    return {
                        "success": False,
                        "error": "Gemini request failed: browser could not be started",
                        "queue_wait_ms": wait_ms,
                        "attempts": 0,
                    }
                configured, configuration_error = await self._prepare_request_configuration(
                    model,
                    thinking_level,
                    request_id,
                )
                if not configured:
                    return {
                        "success": False,
                        "error": f"Gemini request failed: {configuration_error}",
                        "queue_wait_ms": wait_ms,
                        "attempts": 0,
                    }

                attempt_logs: List[str] = []
                request_log_files: List[str] = []
                last_error = "unknown"
                attempt = 0
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    worker = self.worker
                    if not worker:
                        last_error = "Gemini worker disappeared"
                        break
                    attempt_started = time.time()
                    attempt_started_at = datetime.now(timezone.utc).isoformat()
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
                    attempt_finished_at = datetime.now(timezone.utc).isoformat()
                    attempt_duration_ms = int((time.time() - attempt_started) * 1000)

                    request_log = worker.get_request_log()
                    if request_log:
                        attempt_logs.append(f"--- Attempt {attempt} ---")
                        attempt_logs.extend(request_log)

                    retryable = self._is_retryable_response_rejection(result)
                    search_instruction_present = GeminiWebAutomation._needs_search_hint(prompt, use_search)
                    if result.get("success") and str(result.get("response") or "").strip():
                        response = str(result.get("response") or "")
                        if not self._is_transient_gemini_refusal(response):
                            result["queue_wait_ms"] = wait_ms
                            result["attempts"] = attempt
                            result["ready_for_next_request"] = await self._ensure_ready_after_request(
                                f"request {request_id} completed"
                            )
                            try:
                                request_file = await asyncio.to_thread(
                                    write_request_log,
                                    request_id=request_id,
                                    attempt=attempt,
                                    prompt=prompt,
                                    result=result,
                                    model=model,
                                    thinking_level=thinking_level,
                                    use_search=use_search,
                                    search_instruction_present=search_instruction_present,
                                    queue_wait_ms=wait_ms,
                                    queued_at=queued_at_iso,
                                    attempt_started_at=attempt_started_at,
                                    attempt_finished_at=attempt_finished_at,
                                    attempt_duration_ms=attempt_duration_ms,
                                    browser_log=request_log,
                                    request_context=request_context,
                                    retryable=False,
                                    will_retry=False,
                                    ready_for_next_request=result["ready_for_next_request"],
                                    ready_state=self._last_ready_reset,
                                )
                                request_log_files.append(request_file)
                                log(f"[{request_id}] Attempt {attempt} saved: logs/requests/{request_file}", "Worker")
                            except Exception as exc:
                                log(f"[{request_id}] Request logging failed ({type(exc).__name__}); request handling continues", "Worker")
                            result["request_logs"] = request_log_files
                            return result
                        last_error = "Gemini returned its generic transient refusal"
                    else:
                        last_error = result.get("error") or "Empty response"

                    ready_after_failure = None
                    if attempt < max_attempts and retryable:
                        try:
                            await worker._save_diagnostic_artifacts("transient_refusal")
                        except Exception as exc:
                            log(
                                f"[{request_id}] Transient-refusal diagnostics failed ({type(exc).__name__}); continuing retry",
                                "Worker",
                            )
                        log(
                            f"[{request_id}] Response rejected; recreating tab before retry {attempt + 1}/{max_attempts}",
                            "Worker",
                        )
                        ready_for_retry = await self._recreate_worker(
                            f"transient refusal retry {attempt + 1}/{max_attempts} for request {request_id}"
                        )
                        if ready_for_retry:
                            configured, configuration_error = await self._prepare_request_configuration(
                                model,
                                thinking_level,
                                request_id,
                            )
                            if not configured:
                                ready_for_retry = False
                                last_error = f"Gemini request failed: {configuration_error}"
                        try:
                            request_file = await asyncio.to_thread(
                                write_request_log,
                                request_id=request_id,
                                attempt=attempt,
                                prompt=prompt,
                                result=result,
                                model=model,
                                thinking_level=thinking_level,
                                use_search=use_search,
                                search_instruction_present=search_instruction_present,
                                queue_wait_ms=wait_ms,
                                queued_at=queued_at_iso,
                                attempt_started_at=attempt_started_at,
                                attempt_finished_at=attempt_finished_at,
                                attempt_duration_ms=attempt_duration_ms,
                                browser_log=request_log,
                                request_context=request_context,
                                retryable=True,
                                will_retry=bool(ready_for_retry),
                                ready_for_next_request=ready_for_retry,
                                ready_state=self._last_ready_reset,
                            )
                            request_log_files.append(request_file)
                            log(f"[{request_id}] Attempt {attempt} saved: logs/requests/{request_file}", "Worker")
                        except Exception as exc:
                            log(f"[{request_id}] Request logging failed ({type(exc).__name__}); request handling continues", "Worker")
                        if ready_for_retry:
                            continue
                        ready_after_failure = ready_for_retry

                    if ready_after_failure is None:
                        ready_after_failure = await self._ensure_ready_after_request(f"request {request_id} failed")
                    try:
                        request_file = await asyncio.to_thread(
                            write_request_log,
                            request_id=request_id,
                            attempt=attempt,
                            prompt=prompt,
                            result=result,
                            model=model,
                            thinking_level=thinking_level,
                            use_search=use_search,
                            search_instruction_present=search_instruction_present,
                            queue_wait_ms=wait_ms,
                            queued_at=queued_at_iso,
                            attempt_started_at=attempt_started_at,
                            attempt_finished_at=attempt_finished_at,
                            attempt_duration_ms=attempt_duration_ms,
                            browser_log=request_log,
                            request_context=request_context,
                            retryable=retryable,
                            will_retry=False,
                            ready_for_next_request=ready_after_failure,
                            ready_state=self._last_ready_reset,
                        )
                        request_log_files.append(request_file)
                        log(f"[{request_id}] Attempt {attempt} saved: logs/requests/{request_file}", "Worker")
                    except Exception as exc:
                        log(f"[{request_id}] Request logging failed ({type(exc).__name__}); request handling continues", "Worker")
                    break

                log(f"[{request_id}] Request failed: {last_error}", "Worker")
                return {
                    "success": False,
                    "error": f"Gemini request failed: {last_error}",
                    "attempt_logs": attempt_logs,
                    "request_logs": request_log_files,
                    "queue_wait_ms": wait_ms,
                    "attempts": max(1, attempt),
                }
        finally:
            if waiting:
                self._queued_requests -= 1
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
            self._initialized = False
            error = error or "worker could not be restored to ready state"

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
            "browser_channel": "chromium",
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
