import os
import time
import threading
import unittest
from unittest.mock import patch

from agy_cli import (
    AgyCliError,
    AgyCliTimeoutError,
    AgyCliUnavailableError,
    AgyModelSelectionError,
    AgyResult,
)
from agy_pool import AgyPool, AgyQueueFullError


class FakeRunner:
    def __init__(self, responses=None):
        self.responses = list(responses or ["ok"])
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.refresh_count = 0
        self.models = None

    def refresh_models(self):
        self.refresh_count += 1
        self.models = {
            ("flash", "medium"): "gemini-3.8-flash-medium",
            ("flash", "high"): "gemini-3.8-flash-high",
        }
        return dict(self.models)

    def check_capabilities(self):
        return None

    def available_model_slugs(self):
        mapping = self.models or self.refresh_models()
        return [*mapping.values(), "claude-sonnet-4-6"]

    def resolve_model(self, family, thinking_level):
        normalized = family.lower()
        if normalized == "flash":
            key = ("flash", thinking_level.lower())
            mapping = self.models or self.refresh_models()
            if key in mapping:
                return mapping[key]
        for model in self.available_model_slugs():
            if model.lower() == normalized:
                return model
        raise AgyCliError("unsupported")

    def run_prompt(self, prompt, *, family, thinking_level, use_search, timeout_seconds):
        self.calls.append((prompt, family, thinking_level, use_search, timeout_seconds))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        self.active -= 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return AgyResult(
            response=response,
            conversation_id="conv",
            usage={"input_tokens": 10},
            duration_seconds=0.1,
            model_slug=f"gemini-3.8-{family}-{thinking_level.lower()}",
            prompt_transport="inline",
            prompt_chars=len(prompt),
            effective_thinking_level=(thinking_level if family == "flash" else None),
            tools_used=(),
        )


class AgyPoolTests(unittest.TestCase):
    def test_requests_run_in_parallel_up_to_configured_limit(self):
        runner = FakeRunner(["one", "two", "three"])
        pool = AgyPool(runner, max_concurrency=2)
        self.assertTrue(pool.init())
        first = pool.submit("one", model="flash", thinking_level="Medium")
        second = pool.submit("two", model="flash", thinking_level="Medium")
        third = pool.submit("three", model="flash", thinking_level="Medium")

        self.assertTrue(first.result(timeout=1)["success"])
        self.assertTrue(second.result(timeout=1)["success"])
        self.assertTrue(third.result(timeout=1)["success"])
        self.assertEqual(runner.max_active, 2)
        pool.close()

    def test_default_concurrency_is_bounded(self):
        with patch.dict(os.environ, {}, clear=True):
            pool = AgyPool(FakeRunner())
            self.assertEqual(pool.max_concurrency, 3)
            self.assertEqual(pool.max_queue, 9)
            pool.close()

    def test_full_queue_rejects_without_spawning_more_work(self):
        runner = FakeRunner(["one"])
        pool = AgyPool(runner, max_concurrency=1, max_queue=0)
        pool.init()
        with pool._state_lock:
            pool._active_requests = 1

        with self.assertRaisesRegex(AgyQueueFullError, "queue is full"):
            pool.submit("overflow", model="flash", thinking_level="Medium")

        self.assertEqual(runner.calls, [])
        pool.close()

    def test_cancelling_queued_future_releases_queue_capacity(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingRunner(FakeRunner):
            def run_prompt(self, prompt, *, family, thinking_level, use_search, timeout_seconds):
                self.calls.append((prompt, family, thinking_level, use_search, timeout_seconds))
                started.set()
                release.wait(timeout=1)
                return AgyResult(
                    response="ok",
                    conversation_id="conv",
                    usage={"input_tokens": 10},
                    duration_seconds=0.1,
                    model_slug="gemini-3.8-flash-medium",
                    prompt_transport="stdin",
                    prompt_chars=len(prompt),
                    effective_thinking_level="Medium",
                    tools_used=(),
                )

        pool = AgyPool(BlockingRunner(), max_concurrency=1, max_queue=1)
        pool.init()
        first = pool.submit("first", model="flash", thinking_level="Medium")
        self.assertTrue(started.wait(timeout=1))
        second = pool.submit("second", model="flash", thinking_level="Medium")
        self.assertEqual(pool.diagnostics()["queued_requests"], 1)

        self.assertTrue(second.cancel())
        self.assertEqual(pool.diagnostics()["queued_requests"], 0)

        third = pool.submit("third", model="flash", thinking_level="Medium")
        self.assertTrue(third.cancel())
        self.assertEqual(pool.diagnostics()["queued_requests"], 0)
        release.set()
        self.assertTrue(first.result(timeout=1)["success"])
        pool.close()

    def test_close_marks_pool_unavailable_and_failed_submit_does_not_leak_queue(self):
        pool = AgyPool(FakeRunner(), max_concurrency=1, max_queue=1)
        pool.init()
        pool.close()

        self.assertFalse(pool.diagnostics()["available"])
        self.assertFalse(pool.init())
        with self.assertRaises(RuntimeError):
            pool.submit("late", model="flash", thinking_level="Medium")
        self.assertEqual(pool.diagnostics()["queued_requests"], 0)

    def test_close_during_init_cannot_resurrect_pool(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingInitRunner(FakeRunner):
            def check_capabilities(self):
                started.set()
                release.wait(timeout=1)

        runner = BlockingInitRunner()
        pool = AgyPool(runner, max_concurrency=1, max_queue=1)
        result = []
        thread = threading.Thread(target=lambda: result.append(pool.init()))
        thread.start()
        self.assertTrue(started.wait(timeout=1))

        pool.close()
        release.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertFalse(pool.diagnostics()["available"])

    def test_queue_wait_consumes_request_budget_before_model_call(self):
        runner = FakeRunner(["unexpected"])
        pool = AgyPool(runner, max_concurrency=1)
        pool.init()

        result = pool._run_queued(
            "stale",
            "flash",
            "Medium",
            None,
            False,
            1,
            time.monotonic() - 5,
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["timed_out"])
        self.assertIn("exceeded", result["error"])
        self.assertEqual(runner.calls, [])
        pool.close()

    def test_medium_transient_refusal_can_promote_to_high_once(self):
        runner = FakeRunner([
            "Sorry, something went wrong. Please try your request again.",
            "recovered",
        ])
        pool = AgyPool(runner)
        pool.init()
        result = pool.submit(
            "hello",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level="High",
        ).result(timeout=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["effective_thinking_level"], "High")
        self.assertEqual([call[2] for call in runner.calls], ["Medium", "High"])
        pool.close()

    def test_same_thinking_fallback_does_not_duplicate_request(self):
        runner = FakeRunner(["Sorry, something went wrong. Please try your request again."])
        pool = AgyPool(runner)
        pool.init()
        result = pool.submit(
            "hello",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level="Medium",
        ).result(timeout=1)

        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(runner.calls), 1)
        pool.close()

    def test_requested_transient_phrase_is_returned_without_retry(self):
        phrase = "Sorry, something went wrong. Please try your request again."
        runner = FakeRunner([phrase])
        pool = AgyPool(runner)
        pool.init()
        result = pool.submit(
            f"Reply with exactly: {phrase}",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level="High",
        ).result(timeout=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], phrase)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(runner.calls), 1)
        pool.close()

    def test_cli_error_is_not_blindly_retried(self):
        runner = FakeRunner([AgyCliError("quota exhausted")])
        pool = AgyPool(runner)
        pool.init()
        result = pool.submit(
            "hello",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level="High",
        ).result(timeout=1)

        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(runner.calls), 1)
        pool.close()

    def test_runtime_unavailable_error_disables_pool_for_future_routing(self):
        runner = FakeRunner([AgyCliUnavailableError("binary vanished")])
        pool = AgyPool(runner)
        pool.init()

        result = pool.submit(
            "hello",
            model="flash",
            thinking_level="Medium",
        ).result(timeout=1)

        self.assertFalse(result["success"])
        self.assertIn("became unavailable", result["error"])
        self.assertFalse(pool.diagnostics()["available"])
        self.assertFalse(pool.is_model_available("flash", "Medium"))
        pool.close()

    def test_cli_timeout_is_not_retried_and_is_marked(self):
        runner = FakeRunner([AgyCliTimeoutError("print timeout")])
        pool = AgyPool(runner)
        pool.init()

        result = pool.submit(
            "hello",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level="High",
        ).result(timeout=1)

        self.assertFalse(result["success"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(runner.calls), 1)
        pool.close()

    def test_stale_stable_model_refreshes_once_then_retries(self):
        runner = FakeRunner([
            AgyModelSelectionError(
                "invalid model selection: stale slug",
                model_slug="gemini-3.8-flash-medium",
            ),
            "recovered",
        ])
        pool = AgyPool(runner)
        pool.init()
        refreshes_after_init = runner.refresh_count

        result = pool.submit(
            "hello",
            model="flash",
            thinking_level="Medium",
        ).result(timeout=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.refresh_count, refreshes_after_init + 1)
        pool.close()

    def test_auth_failure_after_stale_model_refresh_disables_pool(self):
        runner = FakeRunner([
            AgyModelSelectionError(
                "invalid model selection: stale slug",
                model_slug="gemini-3.8-flash-medium",
            ),
            AgyCliUnavailableError("authentication required"),
        ])
        pool = AgyPool(runner)
        pool.init()

        result = pool.submit(
            "hello",
            model="flash",
            thinking_level="Medium",
        ).result(timeout=1)

        self.assertFalse(result["success"])
        self.assertTrue(result["backend_unavailable"])
        self.assertFalse(pool.diagnostics()["available"])
        pool.close()

    def test_concurrent_stale_model_failures_share_one_refresh(self):
        barrier = threading.Barrier(3)

        class RotatingRunner(FakeRunner):
            def refresh_models(self):
                self.refresh_count += 1
                version = "3.7" if self.refresh_count == 1 else "3.8"
                self.models = {
                    ("flash", "medium"): f"gemini-{version}-flash-medium",
                    ("flash", "high"): f"gemini-{version}-flash-high",
                }
                return dict(self.models)

            def run_prompt(self, prompt, *, family, thinking_level, use_search, timeout_seconds):
                slug = self.resolve_model(family, thinking_level)
                self.calls.append((prompt, family, thinking_level, use_search, timeout_seconds, slug))
                if slug == "gemini-3.7-flash-medium":
                    barrier.wait(timeout=1)
                    raise AgyModelSelectionError(
                        "invalid model selection: stale slug",
                        model_slug=slug,
                    )
                return AgyResult(
                    response="recovered",
                    conversation_id="conv",
                    usage={"input_tokens": 10},
                    duration_seconds=0.1,
                    model_slug=slug,
                    prompt_transport="stdin",
                    prompt_chars=len(prompt),
                    effective_thinking_level=thinking_level,
                    tools_used=(),
                )

        runner = RotatingRunner()
        pool = AgyPool(runner, max_concurrency=3, max_queue=0)
        self.assertTrue(pool.init())
        refreshes_after_init = runner.refresh_count
        futures = [
            pool.submit(f"request-{index}", model="flash", thinking_level="Medium")
            for index in range(3)
        ]
        results = [future.result(timeout=2) for future in futures]

        self.assertTrue(all(result["success"] for result in results))
        self.assertEqual(runner.refresh_count, refreshes_after_init + 1)
        self.assertEqual(
            [call[5] for call in runner.calls].count("gemini-3.8-flash-medium"),
            3,
        )
        pool.close()

    def test_exact_model_selection_error_is_not_retried(self):
        runner = FakeRunner([AgyModelSelectionError("invalid model selection: removed exact id")])
        pool = AgyPool(runner)
        pool.init()
        refreshes_after_init = runner.refresh_count

        result = pool.submit(
            "hello",
            model="claude-sonnet-4-6",
            thinking_level="High",
        ).result(timeout=1)

        self.assertFalse(result["success"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.refresh_count, refreshes_after_init)
        pool.close()

    def test_exact_cli_model_can_be_selected_per_request(self):
        runner = FakeRunner(["claude-ok"])
        pool = AgyPool(runner)
        pool.init()
        result = pool.submit(
            "hello",
            model="claude-sonnet-4-6",
            thinking_level="High",
        ).result(timeout=1)

        self.assertTrue(result["success"])
        self.assertEqual(runner.calls[0][1], "claude-sonnet-4-6")
        self.assertEqual(runner.calls[0][2], "High")
        pool.close()


if __name__ == "__main__":
    unittest.main()
