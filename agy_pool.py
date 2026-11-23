"""Bounded Antigravity execution pool."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from agy_cli import (
    AgyCliError,
    AgyCliRunner,
    AgyCliTimeoutError,
    AgyCliUnavailableError,
    AgyModelSelectionError,
)


REQUEST_BUDGET_SECONDS = 480
DEFAULT_MAX_CONCURRENCY = 3
DEFAULT_MAX_QUEUE = 6
MAX_CONCURRENCY = 8
MAX_QUEUE = 100


class AgyQueueFullError(RuntimeError):
    """The bounded Antigravity queue cannot accept more work."""


class AgyPool:
    def __init__(
        self,
        runner: AgyCliRunner | None = None,
        max_concurrency: int | None = None,
        max_queue: int | None = None,
    ):
        configured_concurrency = max_concurrency
        if configured_concurrency is None:
            try:
                configured_concurrency = int(
                    os.environ.get("AGY_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)
                )
            except ValueError:
                configured_concurrency = DEFAULT_MAX_CONCURRENCY

        configured_queue = max_queue
        if configured_queue is None:
            try:
                configured_queue = int(
                    os.environ.get("AGY_MAX_QUEUE", DEFAULT_MAX_QUEUE)
                )
            except ValueError:
                configured_queue = DEFAULT_MAX_QUEUE

        self.max_concurrency = max(
            1, min(MAX_CONCURRENCY, int(configured_concurrency))
        )
        self.max_queue = max(0, min(MAX_QUEUE, int(configured_queue)))
        self.runner = runner
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="agy-api",
        )
        self._state_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._closed = False
        self._initialized = False
        self._models: list[str] = []
        self._active = 0
        self._queued = 0
        self._last_error: str | None = None

    def init(self) -> bool:
        with self._init_lock:
            with self._state_lock:
                if self._closed:
                    return False
                if self._initialized:
                    return True
            try:
                if self.runner is None:
                    self.runner = AgyCliRunner()
                self.runner.check_capabilities()
                models = self.runner.refresh_models()
            except Exception as exc:
                with self._state_lock:
                    self._initialized = False
                    self._models = []
                    self._last_error = str(exc)
                return False

            with self._state_lock:
                if self._closed:
                    return False
                self._models = list(models)
                self._initialized = True
                self._last_error = None
            return True

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            self._initialized = False
            runner = self.runner
        self._executor.shutdown(wait=False, cancel_futures=True)
        if runner is not None:
            runner.close()

    def is_ready(self) -> bool:
        with self._state_lock:
            return self._initialized and not self._closed

    def available_models(self) -> list[str]:
        with self._state_lock:
            return list(self._models)

    def has_model(self, model: str) -> bool:
        normalized = (model or "").strip().lower()
        return any(candidate.lower() == normalized for candidate in self.available_models())

    def submit(
        self,
        prompt: str,
        *,
        model: str,
        use_search: bool = False,
        timeout_seconds: int = REQUEST_BUDGET_SECONDS,
    ) -> Future[dict[str, Any]]:
        queued_at = time.monotonic()
        with self._state_lock:
            if self._closed or not self._initialized:
                raise RuntimeError(
                    f"Antigravity backend is unavailable: {self._last_error or 'not initialized'}"
                )
            outstanding = self._active + self._queued
            if outstanding >= self.max_concurrency + self.max_queue:
                raise AgyQueueFullError(
                    f"Antigravity queue is full "
                    f"({self.max_concurrency} active, {self.max_queue} waiting)"
                )
            self._queued += 1

        try:
            future = self._executor.submit(
                self._run_queued,
                prompt,
                model,
                use_search,
                max(1, int(timeout_seconds)),
                queued_at,
            )
        except Exception:
            with self._state_lock:
                self._queued = max(0, self._queued - 1)
            raise
        future.add_done_callback(self._release_cancelled_slot)
        return future

    def _release_cancelled_slot(self, future: Future[dict[str, Any]]) -> None:
        if future.cancelled():
            with self._state_lock:
                self._queued = max(0, self._queued - 1)

    def _run_queued(
        self,
        prompt: str,
        model: str,
        use_search: bool,
        timeout_seconds: int,
        queued_at: float,
    ) -> dict[str, Any]:
        with self._state_lock:
            self._queued = max(0, self._queued - 1)
            self._active += 1
        wait_ms = max(0, int((time.monotonic() - queued_at) * 1000))

        try:
            runner = self.runner
            if not runner:
                return {
                    "success": False,
                    "backend_unavailable": True,
                    "error": "Antigravity backend is unavailable",
                    "queue_wait_ms": wait_ms,
                }

            deadline = queued_at + timeout_seconds
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                return {
                    "success": False,
                    "timed_out": True,
                    "error": (
                        f"Antigravity request expired in queue after "
                        f"{timeout_seconds}s"
                    ),
                    "queue_wait_ms": wait_ms,
                }
            try:
                result = runner.run_prompt(
                    prompt,
                    model=model,
                    use_search=use_search,
                    timeout_seconds=remaining,
                )
            except AgyModelSelectionError:
                # The CLI can rotate model IDs while the service is alive.
                # Refresh once before failing an exact-ID request.
                try:
                    with self._refresh_lock:
                        models = runner.refresh_models()
                        with self._state_lock:
                            self._models = list(models)
                    if not any(
                        candidate.lower() == model.strip().lower()
                        for candidate in models
                    ):
                        return {
                            "success": False,
                            "error": (
                                f"Antigravity no longer exposes model {model!r}"
                            ),
                            "queue_wait_ms": wait_ms,
                        }
                    remaining = int(deadline - time.monotonic())
                    if remaining <= 0:
                        return {
                            "success": False,
                            "timed_out": True,
                            "error": (
                                f"Antigravity request expired during model refresh "
                                f"after {timeout_seconds}s"
                            ),
                            "queue_wait_ms": wait_ms,
                        }
                    result = runner.run_prompt(
                        prompt,
                        model=model,
                        use_search=use_search,
                        timeout_seconds=remaining,
                    )
                except AgyModelSelectionError as exc:
                    return {
                        "success": False,
                        "error": str(exc),
                        "queue_wait_ms": wait_ms,
                    }
            except AgyCliTimeoutError as exc:
                return {
                    "success": False,
                    "timed_out": True,
                    "error": str(exc),
                    "queue_wait_ms": wait_ms,
                }
            except AgyCliUnavailableError as exc:
                with self._state_lock:
                    self._initialized = False
                    self._last_error = str(exc)
                return {
                    "success": False,
                    "backend_unavailable": True,
                    "error": str(exc),
                    "queue_wait_ms": wait_ms,
                }
            except AgyCliError as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "queue_wait_ms": wait_ms,
                }

            with self._state_lock:
                self._last_error = None
            return {
                "success": True,
                "response": result.response,
                "queue_wait_ms": wait_ms,
                "attempts": 1,
                "effective_thinking_level": result.effective_thinking_level,
                "agy_model": result.model_id,
                "agy_usage": result.usage,
                "agy_duration_seconds": result.duration_seconds,
                "agy_tools_used": list(result.tools_used),
            }
        finally:
            with self._state_lock:
                self._active = max(0, self._active - 1)

    def diagnostics(self) -> dict[str, Any]:
        with self._state_lock:
            state = (
                "ready"
                if self._initialized and not self._closed
                else "unavailable"
                if self._last_error
                else "cold"
            )
            return {
                "state": state,
                "available": self._initialized and not self._closed,
                "max_concurrency": self.max_concurrency,
                "max_queue": self.max_queue,
                "active_requests": self._active,
                "queued_requests": self._queued,
                "models": list(self._models),
                "last_error": self._last_error,
            }
