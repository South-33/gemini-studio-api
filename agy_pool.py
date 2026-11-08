"""Bounded parallel Antigravity CLI execution for the API backend."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict

from agy_cli import (
    AgyCliError,
    AgyCliRunner,
    AgyCliTimeoutError,
    AgyModelSelectionError,
    MODEL_FAMILIES,
)


AGY_REQUEST_TOTAL_BUDGET_SECONDS = 480
# Local load testing found little throughput gain at 5 concurrent processes,
# while latency rose materially and one request regained high agent overhead.
# Keep 3 as the efficient default; operators can raise it deliberately.
DEFAULT_AGY_MAX_CONCURRENCY = 3
MAX_AGY_MAX_CONCURRENCY = 8
# At the measured 45-56s/request, 24 waiting jobs is about eight waves behind
# three workers and stays near the 480s logical budget. A much deeper default
# queue mostly holds work that will expire before execution.
DEFAULT_AGY_MAX_QUEUE = 24
MAX_AGY_MAX_QUEUE = 1000


class AgyQueueFullError(RuntimeError):
    """The bounded Antigravity queue cannot accept another request."""


class AgyPool:
    """Run a small number of independent Antigravity CLI requests in parallel.

    Each request starts a fresh CLI conversation with its own temporary
    workspace. Requests beyond the configured process limit wait in the
    executor queue instead of creating unbounded CLI processes.
    """

    def __init__(
        self,
        runner: AgyCliRunner | None = None,
        max_concurrency: int | None = None,
        max_queue: int | None = None,
    ):
        self.runner = runner
        configured = max_concurrency
        if configured is None:
            try:
                configured = int(os.environ.get("AGY_MAX_CONCURRENCY", DEFAULT_AGY_MAX_CONCURRENCY))
            except ValueError:
                configured = DEFAULT_AGY_MAX_CONCURRENCY
        self.max_concurrency = max(1, min(MAX_AGY_MAX_CONCURRENCY, int(configured)))
        queue_limit = max_queue
        if queue_limit is None:
            try:
                queue_limit = int(os.environ.get("AGY_MAX_QUEUE", DEFAULT_AGY_MAX_QUEUE))
            except ValueError:
                queue_limit = DEFAULT_AGY_MAX_QUEUE
        self.max_queue = max(0, min(MAX_AGY_MAX_QUEUE, int(queue_limit)))
        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="agy-api")
        self._state_lock = threading.Lock()
        self._model_refresh_lock = threading.Lock()
        self._initialized = False
        self._models: Dict[tuple[str, str], str] = {}
        self._available_model_slugs: list[str] = []
        self._active_requests = 0
        self._queued_requests = 0
        self._last_error: str | None = None

    def init(self) -> bool:
        try:
            if self.runner is None:
                self.runner = AgyCliRunner()
            self.runner.check_capabilities()
            self.runner.check_environment_isolation()
            self._models = self.runner.refresh_models()
            self._available_model_slugs = self.runner.available_model_slugs()
            self._initialized = bool(self._available_model_slugs)
            self._last_error = None
        except Exception as exc:
            self._initialized = False
            self._models = {}
            self._available_model_slugs = []
            self._last_error = str(exc)
        return self._initialized

    def close(self) -> None:
        with self._state_lock:
            self._initialized = False
        self._executor.shutdown(wait=False, cancel_futures=True)

    def is_model_available(self, family: str, thinking_level: str) -> bool:
        if not self.runner:
            return False
        try:
            self.runner.resolve_model(family, thinking_level)
            return True
        except AgyCliError:
            return False

    def submit(
        self,
        prompt: str,
        *,
        model: str,
        thinking_level: str,
        fallback_thinking_level: str | None = None,
        use_search: bool = False,
        timeout_seconds: int = AGY_REQUEST_TOTAL_BUDGET_SECONDS,
    ) -> Future:
        queued_at = time.time()
        queued_monotonic = time.monotonic()
        with self._state_lock:
            outstanding = self._active_requests + self._queued_requests
            if outstanding >= self.max_concurrency + self.max_queue:
                raise AgyQueueFullError(
                    f"Antigravity CLI queue is full ({self.max_concurrency} active, {self.max_queue} waiting)"
                )
            self._queued_requests += 1
        try:
            future = self._executor.submit(
                self._run_queued,
                prompt,
                model,
                thinking_level,
                fallback_thinking_level,
                use_search,
                max(1, int(timeout_seconds)),
                queued_at,
                queued_monotonic,
            )
        except Exception:
            with self._state_lock:
                self._queued_requests = max(0, self._queued_requests - 1)
            raise
        future.add_done_callback(self._release_cancelled_queue_slot)
        return future

    def _release_cancelled_queue_slot(self, future: Future) -> None:
        """Release capacity when a queued future is cancelled before it starts."""

        if not future.cancelled():
            return
        with self._state_lock:
            self._queued_requests = max(0, self._queued_requests - 1)

    @staticmethod
    def _is_transient_refusal(response: str) -> bool:
        normalized = " ".join((response or "").strip().lower().split())
        return (
            normalized.startswith("i'm having a hard time fulfilling your request.")
            and "something else instead?" in normalized
        ) or normalized in {
            "sorry, something went wrong. please try your request again.",
            "i encountered an error doing what you asked. could you try again?",
            "i seem to be encountering an error. can i try something else for you?",
        }

    def _run_queued(
        self,
        prompt: str,
        model: str,
        thinking_level: str,
        fallback_thinking_level: str | None,
        use_search: bool,
        timeout_seconds: int,
        queued_at: float,
        queued_monotonic: float,
    ) -> Dict[str, Any]:
        with self._state_lock:
            self._queued_requests = max(0, self._queued_requests - 1)
            self._active_requests += 1
        wait_ms = int((time.time() - queued_at) * 1000)

        try:
            if not self._initialized or self.runner is None:
                return {
                    "success": False,
                    "error": f"Antigravity CLI backend is unavailable: {self._last_error or 'not initialized'}",
                    "queue_wait_ms": wait_ms,
                    "attempts": 0,
                    "effective_thinking_level": thinking_level,
                }

            deadline = queued_monotonic + timeout_seconds
            effective_thinking = thinking_level
            max_attempts = 2 if fallback_thinking_level else 1
            last_response = ""

            for attempt in range(1, max_attempts + 1):
                remaining = int(deadline - time.monotonic())
                if remaining <= 0:
                    return {
                        "success": False,
                        "error": f"Antigravity CLI request exceeded {timeout_seconds}s budget",
                        "queue_wait_ms": wait_ms,
                        "attempts": attempt - 1,
                        "effective_thinking_level": effective_thinking,
                    }
                try:
                    result = self.runner.run_prompt(
                        prompt,
                        family=model,
                        thinking_level=effective_thinking,
                        use_search=use_search,
                        timeout_seconds=remaining,
                    )
                except AgyCliTimeoutError as exc:
                    self._last_error = str(exc)
                    return {
                        "success": False,
                        "timed_out": True,
                        "error": str(exc),
                        "queue_wait_ms": wait_ms,
                        "attempts": attempt,
                        "effective_thinking_level": effective_thinking,
                    }
                except AgyModelSelectionError as exc:
                    if model.strip().lower() not in MODEL_FAMILIES:
                        self._last_error = str(exc)
                        return {
                            "success": False,
                            "error": f"Antigravity CLI request failed: {exc}",
                            "queue_wait_ms": wait_ms,
                            "attempts": attempt,
                            "effective_thinking_level": effective_thinking,
                        }
                    try:
                        with self._model_refresh_lock:
                            self._models = self.runner.refresh_models()
                            self._available_model_slugs = self.runner.available_model_slugs()
                        remaining = int(deadline - time.monotonic())
                        if remaining <= 0:
                            raise AgyCliError(
                                f"Antigravity CLI request exceeded {timeout_seconds}s budget during model refresh"
                            )
                        result = self.runner.run_prompt(
                            prompt,
                            family=model,
                            thinking_level=effective_thinking,
                            use_search=use_search,
                            timeout_seconds=remaining,
                        )
                    except AgyCliTimeoutError as refresh_exc:
                        self._last_error = str(refresh_exc)
                        return {
                            "success": False,
                            "timed_out": True,
                            "error": str(refresh_exc),
                            "queue_wait_ms": wait_ms,
                            "attempts": attempt,
                            "effective_thinking_level": effective_thinking,
                        }
                    except AgyCliError as refresh_exc:
                        self._last_error = str(refresh_exc)
                        return {
                            "success": False,
                            "error": f"Antigravity CLI request failed: {refresh_exc}",
                            "queue_wait_ms": wait_ms,
                            "attempts": attempt,
                            "effective_thinking_level": effective_thinking,
                        }
                except AgyCliError as exc:
                    self._last_error = str(exc)
                    return {
                        "success": False,
                        "error": f"Antigravity CLI request failed: {exc}",
                        "queue_wait_ms": wait_ms,
                        "attempts": attempt,
                        "effective_thinking_level": effective_thinking,
                    }

                last_response = result.response
                if not self._is_transient_refusal(last_response):
                    self._last_error = None
                    return {
                        "success": True,
                        "response": last_response,
                        "queue_wait_ms": wait_ms,
                        "attempts": attempt,
                        "effective_thinking_level": result.effective_thinking_level,
                        "agy_model": result.model_slug,
                        "agy_usage": result.usage,
                        "agy_duration_seconds": result.duration_seconds,
                        "prompt_transport": result.prompt_transport,
                        "prompt_chars": result.prompt_chars,
                        "conversation_id": result.conversation_id,
                        "agy_tools_used": list(result.tools_used),
                        "request_logs": [],
                    }

                if attempt < max_attempts and fallback_thinking_level:
                    effective_thinking = fallback_thinking_level
                    continue

            return {
                "success": False,
                "error": "Antigravity CLI returned Gemini's transient failure response",
                "response": last_response,
                "queue_wait_ms": wait_ms,
                "attempts": max_attempts,
                "effective_thinking_level": effective_thinking,
            }
        finally:
            with self._state_lock:
                self._active_requests = max(0, self._active_requests - 1)

    def diagnostics(self) -> Dict[str, Any]:
        with self._state_lock:
            active = self._active_requests
            queued = self._queued_requests
        models = [
            {"family": family, "thinking_level": thinking, "model": slug}
            for (family, thinking), slug in sorted(self._models.items())
        ]
        return {
            "available": self._initialized,
            "max_concurrency": self.max_concurrency,
            "max_queue": self.max_queue,
            "active_requests": active,
            "active_request": active > 0,
            "queued_requests": queued,
            "models": models,
            "model_ids": list(self._available_model_slugs),
            "last_error": self._last_error,
        }
