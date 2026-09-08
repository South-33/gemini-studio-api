import asyncio
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

    async def send_message(self, *_args, **_kwargs):
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

    async def prepare_next_request(self):
        self.prepare_calls += 1
        self.ready = self.prepare_ok
        return self.prepare_ok


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

    async def test_success_prepares_next_temp_chat_before_returning(self):
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
        pool._notify_final_failure = AsyncMock()

        result = await pool.send_message("hello", request_id="retry")

        self.assertFalse(result["success"])
        self.assertEqual(worker.calls, 1)
        pool._recreate_worker.assert_not_awaited()
        pool._notify_final_failure.assert_awaited_once()

    async def test_missing_response_marker_is_retried_once(self):
        pool = WorkerPool()
        worker = FakeWorker([
            {
                "success": False,
                "error": "Gemini response missing response=good marker",
                "response": "```json\n{\"probe\":\"ok\"}\n```",
            },
            {"success": True, "response": "expected"},
        ])
        pool.workers = [worker]
        pool._initialized = True

        result = await pool.send_message("hello", request_id="marker-retry")

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "expected")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(worker.calls, 2)
        self.assertEqual(worker.prepare_calls, 2)

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

    async def test_generic_gemini_refusal_is_retried_on_a_fresh_temp_chat(self):
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

        result = await pool.send_message("hello", request_id="transient-refusal")

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "expected")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(worker.prepare_calls, 2)
        self.assertEqual(self.request_logger.call_count, 2)
        self.assertIn("hard time fulfilling", self.request_logger.call_args_list[0].kwargs["result"]["response"])
        self.assertTrue(self.request_logger.call_args_list[0].kwargs["will_retry"])
        self.assertEqual(self.request_logger.call_args_list[1].kwargs["attempt"], 2)

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
            "raw_model": "flash-standard",
            "message_count": 1,
            "image_count": 0,
            "prompt_tokens_est": 12,
        }

        result = await pool.send_message(
            "full prompt",
            model="flash",
            thinking_level="Standard",
            use_search=True,
            request_id="trace-me",
            request_context=context,
        )

        self.assertTrue(result["success"])
        kwargs = self.request_logger.call_args.kwargs
        self.assertEqual(kwargs["prompt"], "full prompt")
        self.assertEqual(kwargs["request_context"], context)
        self.assertEqual(kwargs["model"], "flash")
        self.assertEqual(kwargs["thinking_level"], "Standard")
        self.assertTrue(kwargs["use_search"])
        self.assertTrue(kwargs["search_instruction_present"])
        self.assertGreaterEqual(kwargs["attempt_duration_ms"], 0)
        self.assertIn("attempt_started_at", kwargs)
        self.assertIn("attempt_finished_at", kwargs)


if __name__ == "__main__":
    unittest.main()
