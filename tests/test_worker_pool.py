import asyncio
import unittest
from unittest.mock import AsyncMock

from worker_pool import WorkerPool


class FakeWorker:
    def __init__(self, results=None):
        self._initialized = True
        self._generation_in_progress = False
        self._results = list(results or [])
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.prepare_calls = 0

    async def send_message(self, *_args, **_kwargs):
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

    async def prepare_idle(self):
        self.prepare_calls += 1
        return True


class WorkerPoolTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(pool._queued_requests, 0)

    async def test_success_prepares_next_temp_chat_while_idle(self):
        pool = WorkerPool()
        worker = FakeWorker()
        pool.workers = [worker]
        pool._initialized = True

        result = await pool.send_message("one", request_id="one")
        await asyncio.wait_for(pool._prewarm_task, timeout=1)

        self.assertTrue(result["success"])
        self.assertEqual(worker.prepare_calls, 1)
        self.assertTrue(pool._last_idle_prewarm["ok"])

    async def test_failed_request_recreates_once_then_retries(self):
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

        self.assertTrue(result["success"])
        self.assertEqual(worker.calls, 2)
        pool._recreate_worker.assert_awaited_once()
        pool._notify_final_failure.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
