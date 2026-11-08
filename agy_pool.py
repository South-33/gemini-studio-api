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
    AgyCliUnavailableError,
    AgyModelSelectionError,
    MODEL_FAMILIES,
)


AGY_REQUEST_TOTAL_BUDGET_SECONDS = 480
# Final isolated-agent load tests show 4 concurrent processes improve throughput
# over 3 for both normal and search workloads; 5 becomes materially slower.
DEFAULT_AGY_MAX_CONCURRENCY = 4
MAX_AGY_MAX_CONCURRENCY = 8
# Keep three waiting waves behind the four default workers. This absorbs bursts
# without recreating the deep queues that mostly hold work until it times out.
DEFAULT_AGY_MAX_QUEUE = 12
MAX_AGY_MAX_QUEUE = 1000


class AgyQueueFullError(RuntimeError):
    """The bounded Antigravity queue cannot accept another request."""


class AgyPool:
    """Run a small number of independent Antigravity CLI requests in parallel.

    Each request starts a fresh CLI conversation from the same sterile runtime
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
        self._closed = False
        self._initialized = False
        self._models: Dict[tuple[str, str], str] = {}
        self._available_model_slugs: list[str] = []
        self._active_requests = 0
        self._queued_requests = 0
        self._last_error: str | None = None

    def init(self) -> bool:
        try:
            with self._state_lock:
                if self._closed:
                    self._last_error = "Antigravity CLI pool is closed"
                    return False
                if self.runner is None:
                    self.runner = AgyCliRunner()
                runner = self.runner

            runner.check_capabilities()
            models = runner.refresh_models()
            available_model_slugs = runner.available_model_slugs()
            with self._state_lock:
                if self._closed:
                    return False
                self._models = models
                self._available_model_slugs = available_model_slugs
                self._initialized = bool(available_model_slugs)
                self._last_error = None
        except Exception as exc:
            with self._state_lock:
                self._initialized = False
                self._models = {}
                self._available_model_slugs = []
                if not self._closed:
                    self._last_error = str(exc)
            return False
        with self._state_lock:
            return self._initialized

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            self._initialized = False
            runner = self.runner
        self._executor.shutdown(wait=False, cancel_futures=True)
        if runner is not None:
            close_runner = getattr(runner, "close", None)
            if callable(close_runner):
                close_runner()

    def is_model_available(self, family: str, thinking_level: str) -> bool:
        with self._state_lock:
            if self._closed or not self._initialized or not self.runner:
                return False
            runner = self.runner
        try:
            runner.resolve_model(family, thinking_level)
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
        queued_monotonic = time.monotonic()
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Antigravity CLI pool is closed")
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
    def _is_transient_refusal(response: str, prompt: str = "") -> bool:
        normalized = " ".join((response or "").strip().lower().split())
        normalized_prompt = " ".join((prompt or "").strip().lower().split())
        if normalized and normalized in normalized_prompt:
            return False
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
        queued_monotonic: float,
    ) -> Dict[str, Any]:
        with self._state_lock:
            self._queued_requests = max(0, self._queued_requests - 1)
            self._active_requests += 1
        wait_ms = max(0, int((time.monotonic() - queued_monotonic) * 1000))

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
            fallback_changes_model = bool(
                fallback_thinking_level
                and fallback_thinking_level.lower() != thinking_level.lower()
            )
            max_attempts = 2 if fallback_changes_model else 1
            last_response = ""

            for attempt in range(1, max_attempts + 1):
                remaining = int(deadline - time.monotonic())
                if remaining <= 0:
                    return {
                        "success": False,
                        "timed_out": True,
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
                except AgyCliUnavailableError as exc:
                    with self._state_lock:
                        self._initialized = False
                        self._last_error = str(exc)
                    return {
                        "success": False,
                        "backend_unavailable": True,
                        "error": f"Antigravity CLI backend became unavailable: {exc}",
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
                            current_slug = self.runner.resolve_model(model, effective_thinking)
                            if not exc.model_slug or current_slug == exc.model_slug:
                                refreshed_models = self.runner.refresh_models()
                                refreshed_slugs = self.runner.available_model_slugs()
                                with self._state_lock:
                                    self._models = refreshed_models
                                    self._available_model_slugs = refreshed_slugs
                        remaining = int(deadline - time.monotonic())
                        if remaining <= 0:
                            raise AgyCliTimeoutError(
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
                if not self._is_transient_refusal(last_response, prompt):
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

                if attempt < max_attempts and fallback_changes_model and fallback_thinking_level:
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
            initialized = self._initialized
            model_map = dict(self._models)
            model_ids = list(self._available_model_slugs)
            last_error = self._last_error
        models = [
            {"family": family, "thinking_level": thinking, "model": slug}
            for (family, thinking), slug in sorted(model_map.items())
        ]
        return {
            "available": initialized,
            "max_concurrency": self.max_concurrency,
            "max_queue": self.max_queue,
            "active_requests": active,
            "active_request": active > 0,
            "queued_requests": queued,
            "models": models,
            "model_ids": model_ids,
            "last_error": last_error,
        }
