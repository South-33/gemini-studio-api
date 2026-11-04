import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from worker_pool import WorkerPool


class FakeWorker:
    def __init__(self, results=None, prepare_ok=True):
        self._initialized = True
        self._generation_in_progress = False
        self._results = list(results or [])
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.prepare_calls = 0
        self.ready = True
        self.started_ready = []
        self.prepare_ok = prepare_ok
        self.configuration_results = []
        self.diagnostic_reasons = []
        self.thinking_levels = []

    async def ensure_configuration(self, *_args, **_kwargs):
        if self.configuration_results:
            return self.configuration_results.pop(0)
        return True, False

    async def send_message(self, *_args, **_kwargs):
        self.thinking_levels.append(_args[2] if len(_args) > 2 else None)
        self.started_ready.append(self.ready)
        self.ready = False
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        if self._results:
            return self._results.pop(0)
        return {"success": True, "response": "ok"}

    def get_request_log(self):
        return []

    async def _save_diagnostic_artifacts(self, reason, request_id=None):
        self.diagnostic_reasons.append((reason, request_id))

    async def prepare_next_request(self):
        self.prepare_calls += 1
        self.ready = self.prepare_ok
        return self.prepare_ok

    async def close(self):
        self._initialized = False


class WorkerPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch("worker_pool.write_request_log", return_value="request_test.json")
        self.request_logger = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_concurrent_requests_are_serialized(self):
        pool = WorkerPool()
        worker = FakeWorker()
        pool.workers = [worker]
        pool._initialized = True

        first, second = await asyncio.gather(
            pool.send_message("one", request_id="one"),
            pool.send_message("two", request_id="two"),
        )

        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(worker.max_active, 1)
        self.assertEqual(worker.started_ready, [True, True])
        self.assertEqual(worker.prepare_calls, 2)
        self.assertEqual(pool._queued_requests, 0)

    async def test_success_prepares_next_clean_chat_before_returning(self):
        pool = WorkerPool()
        worker = FakeWorker()
        pool.workers = [worker]
        pool._initialized = True

        result = await pool.send_message("one", request_id="one")

        self.assertTrue(result["success"])
        self.assertTrue(result["ready_for_next_request"])
        self.assertEqual(worker.prepare_calls, 1)
        self.assertTrue(pool._last_ready_reset["ok"])

    async def test_transport_failure_is_not_resent_inside_pool(self):
        pool = WorkerPool()
        worker = FakeWorker([
            {"success": False, "error": "stalled generation"},
            {"success": True, "response": "recovered"},
        ])
        pool.workers = [worker]
        pool._initialized = True
        pool._recreate_worker = AsyncMock(return_value=True)

        result = await pool.send_message("hello", request_id="retry")

        self.assertFalse(result["success"])
        self.assertEqual(worker.calls, 1)
        pool._recreate_worker.assert_not_awaited()

    async def test_active_request_budget_stops_a_hung_generation_before_tunnel_timeout(self):
        pool = WorkerPool()
        worker = FakeWorker()

        async def slow_send(*_args, **_kwargs):
            worker.calls += 1
            await asyncio.sleep(0.1)
            return {"success": True, "response": "late"}

        worker.send_message = slow_send
        pool.workers = [worker]
        pool._initialized = True
        pool._recreate_worker = AsyncMock(return_value=True)

        with patch("worker_pool.REQUEST_ACTIVE_BUDGET_SECONDS", 0.02):
            result = await pool.send_message("hello", request_id="active-budget")

        self.assertFalse(result["success"])
        self.assertIn("active budget exhausted", result["error"])
        self.assertEqual(worker.calls, 1)
        pool._recreate_worker.assert_not_awaited()

    async def test_empty_terminal_generation_is_retried_on_a_fresh_chat(self):
        pool = WorkerPool()
        worker = FakeWorker([
            {"success": False, "error": "Gemini generation ended without a response"},
            {"success": True, "response": "recovered"},
        ])
        pool.workers = [worker]
        pool._initialized = True
        pool._recreate_worker = AsyncMock(return_value=True)
        pool._prepare_request_configuration = AsyncMock(return_value=(True, None))

        result = await pool.send_message("hello", request_id="empty-terminal")

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "recovered")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(
            worker.diagnostic_reasons,
            [("transient_generation_failure", "empty-terminal")],
        )
        pool._recreate_worker.assert_awaited_once()
        self.assertEqual(pool._prepare_request_configuration.await_count, 2)
        self.assertTrue(self.request_logger.call_args_list[0].kwargs["will_retry"])

    async def test_unrecoverable_ready_reset_marks_pool_unavailable(self):
        pool = WorkerPool()
        worker = FakeWorker(prepare_ok=False)
        pool.workers = [worker]
        pool._initialized = True
        pool._recreate_worker = AsyncMock(return_value=False)

        result = await pool.send_message("hello", request_id="reset-failure")

        self.assertTrue(result["success"])
        self.assertFalse(result["ready_for_next_request"])
        self.assertFalse(pool._initialized)

    async def test_generic_gemini_refusal_is_retried_on_a_fresh_chat(self):
        pool = WorkerPool()
        worker = FakeWorker([
            {
                "success": True,
                "response": "I'm having a hard time fulfilling your request. Can I help with something else instead?",
            },
            {"success": True, "response": "expected"},
        ])
        pool.workers = [worker]
        pool._initialized = True
        pool._recreate_worker = AsyncMock(return_value=True)
        pool._prepare_request_configuration = AsyncMock(return_value=(True, None))

        result = await pool.send_message("hello", request_id="transient-refusal")

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "expected")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(worker.diagnostic_reasons, [("transient_refusal", "transient-refusal")])
        pool._recreate_worker.assert_awaited_once()
        self.assertEqual(pool._prepare_request_configuration.await_count, 2)
        self.assertEqual(self.request_logger.call_count, 2)
        self.assertIn("hard time fulfilling", self.request_logger.call_args_list[0].kwargs["result"]["response"])
        self.assertTrue(self.request_logger.call_args_list[0].kwargs["will_retry"])
        self.assertEqual(self.request_logger.call_args_list[1].kwargs["attempt"], 2)

    async def test_transient_medium_failure_promotes_retry_to_high(self):
        pool = WorkerPool()
        worker = FakeWorker([
            {
                "success": True,
                "response": "Sorry, something went wrong. Please try your request again.",
            },
            {"success": True, "response": "recovered"},
        ])
        pool.workers = [worker]
        pool._initialized = True
        pool._recreate_worker = AsyncMock(return_value=True)
        pool._prepare_request_configuration = AsyncMock(return_value=(True, None))

        result = await pool.send_message(
            "hello",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level="High",
            request_id="medium-to-high",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["effective_thinking_level"], "High")
        self.assertEqual(worker.thinking_levels, ["Medium", "High"])
        self.assertEqual(
            pool._prepare_request_configuration.call_args_list[1].args[:2],
            ("flash", "High"),
        )
        self.assertEqual(self.request_logger.call_args_list[0].kwargs["thinking_level"], "Medium")
        self.assertEqual(self.request_logger.call_args_list[1].kwargs["thinking_level"], "High")

    async def test_fallback_request_stops_after_primary_and_one_fallback_attempt(self):
        pool = WorkerPool()
        worker = FakeWorker([
            {
                "success": True,
                "response": "Sorry, something went wrong. Please try your request again.",
            },
            {
                "success": True,
                "response": "I encountered an error doing what you asked. Could you try again?",
            },
            {"success": True, "response": "unexpected-third-attempt"},
        ])
        pool.workers = [worker]
        pool._initialized = True
        pool._recreate_worker = AsyncMock(return_value=True)
        pool._prepare_request_configuration = AsyncMock(return_value=(True, None))

        result = await pool.send_message(
            "hello",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level="High",
            request_id="bounded-fallback",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["effective_thinking_level"], "High")
        self.assertEqual(worker.calls, 2)
        self.assertEqual(worker.thinking_levels, ["Medium", "High"])

    async def test_request_log_disk_failure_does_not_fail_completion(self):
        self.request_logger.side_effect = OSError("disk unavailable")
        pool = WorkerPool()
        pool.workers = [FakeWorker()]
        pool._initialized = True

        result = await pool.send_message("hello", request_id="disk-error")

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "ok")

    async def test_request_log_receives_full_context_and_timing(self):
        pool = WorkerPool()
        pool.workers = [FakeWorker()]
        pool._initialized = True
        context = {
            "project": "borderclash",
            "client": "borderclash-convex",
            "raw_model": "flash",
            "message_count": 1,
            "image_count": 0,
            "prompt_tokens_est": 12,
        }

        result = await pool.send_message(
            "full prompt",
            model="flash",
            thinking_level="Medium",
            use_search=True,
            request_id="trace-me",
            request_context=context,
        )

        self.assertTrue(result["success"])
        kwargs = self.request_logger.call_args.kwargs
        self.assertEqual(kwargs["prompt"], "full prompt")
        self.assertEqual(kwargs["request_context"], context)
        self.assertEqual(kwargs["model"], "flash")
        self.assertEqual(kwargs["thinking_level"], "Medium")
        self.assertTrue(kwargs["use_search"])
        self.assertTrue(kwargs["search_instruction_present"])
        self.assertGreaterEqual(kwargs["attempt_duration_ms"], 0)
        self.assertIn("attempt_started_at", kwargs)
        self.assertIn("attempt_finished_at", kwargs)

    async def test_model_family_change_recreates_tab_before_send(self):
        pool = WorkerPool()
        first = FakeWorker()
        second = FakeWorker()
        first.configuration_results = [(True, True)]
        second.configuration_results = [(True, False), (True, False)]
        pool.workers = [first]
        pool._initialized = True

        async def recreate(_reason):
            pool.workers = [second]
            return True

        pool._recreate_worker = AsyncMock(side_effect=recreate)

        result = await pool.send_message(
            "hello",
            model="flash",
            thinking_level="Medium",
            request_id="switch-model",
        )

        self.assertTrue(result["success"])
        self.assertEqual(first.calls, 0)
        self.assertEqual(second.calls, 1)
        pool._recreate_worker.assert_awaited_once()

    async def test_cold_browser_warms_before_next_request(self):
        pool = WorkerPool()
        pool._initialized = True
        pool._browser_state = "cold"
        worker = FakeWorker()

        async def warm(_reason):
            pool.workers = [worker]
            pool._browser_state = "warm"
            return True

        pool._warm_browser = AsyncMock(side_effect=warm)

        result = await pool.send_message("hello", request_id="wake")

        self.assertTrue(result["success"])
        self.assertEqual(worker.calls, 1)
        pool._warm_browser.assert_awaited_once()

    async def test_concurrent_cold_requests_share_one_browser_warmup(self):
        pool = WorkerPool()
        pool._initialized = True
        pool._browser_state = "cold"
        worker = FakeWorker()

        async def warm(_reason):
            await asyncio.sleep(0.01)
            pool.workers = [worker]
            pool._browser_state = "warm"
            return True

        pool._warm_browser = AsyncMock(side_effect=warm)

        first, second = await asyncio.gather(
            pool.send_message("one", request_id="cold-one"),
            pool.send_message("two", request_id="cold-two"),
        )

        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(worker.max_active, 1)
        self.assertEqual(pool._warm_browser.await_count, 1)

    async def test_idle_watchdog_closes_browser_without_disabling_pool(self):
        pool = WorkerPool(idle_browser_seconds=0.01, idle_poll_seconds=0.01)
        pool._initialized = True
        pool._browser_state = "warm"
        pool.workers = [FakeWorker()]
        context = AsyncMock()
        pool.context = context
        pool._last_activity = time.time() - 1

        task = asyncio.create_task(pool._idle_watchdog())
        await asyncio.sleep(0.12)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(pool._initialized)
        self.assertEqual(pool._browser_state, "cold")
        self.assertEqual(pool.workers, [])
        context.close.assert_awaited_once()

    async def test_idle_watchdog_does_not_close_with_a_queued_request(self):
        pool = WorkerPool(idle_browser_seconds=0.01, idle_poll_seconds=0.01)
        pool._initialized = True
        pool._browser_state = "warm"
        pool.workers = [FakeWorker()]
        context = AsyncMock()
        pool.context = context
        pool._queued_requests = 1
        pool._last_activity = time.time() - 1

        task = asyncio.create_task(pool._idle_watchdog())
        await asyncio.sleep(0.12)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(pool._browser_state, "warm")
        context.close.assert_not_awaited()

    def test_known_gemini_error_replies_are_transient(self):
        for response in (
            "I'm having a hard time fulfilling your request. Can I help with something else instead?",
            "I'm having a hard time fulfilling your request. Can I help you with something else instead?",
            "Sorry, something went wrong. Please try your request again.",
            "I encountered an error doing what you asked. Could you try again?",
            "I seem to be encountering an error. Can I try something else for you?",
        ):
            self.assertTrue(WorkerPool._is_transient_gemini_refusal(response))


if __name__ == "__main__":
    unittest.main()
