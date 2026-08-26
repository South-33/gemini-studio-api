import unittest

import main
from fastapi import Response


class FakePage:
    def __init__(self, closed=False):
        self.closed = closed

    def is_closed(self):
        return self.closed


class FakeWorker:
    def __init__(self, initialized=True, closed=False):
        self._initialized = initialized
        self.page = FakePage(closed=closed)


class FakePool:
    def __init__(self, initialized, workers):
        self._initialized = initialized
        self.workers = workers


class ApiStatusTests(unittest.TestCase):
    def setUp(self):
        self.original_pool = main.worker_pool

    def tearDown(self):
        main.worker_pool = self.original_pool

    def test_ready_worker_count_requires_a_live_initialized_page(self):
        main.worker_pool = FakePool(True, [
            FakeWorker(initialized=True),
            FakeWorker(initialized=False),
            FakeWorker(initialized=True, closed=True),
        ])

        self.assertEqual(main.ready_worker_count(), 1)

    def test_public_trace_excludes_prompt_content(self):
        public = main.public_request_trace({
            "request_id": "req-1",
            "ip": "127.0.0.1",
            "status": "failed",
            "prompt_chars": 1234,
            "prompt_preview": "private preview",
            "prompt_full": "private full prompt",
        })

        self.assertEqual(public, {
            "request_id": "req-1",
            "status": "failed",
            "prompt_chars": 1234,
        })


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_pool = main.worker_pool

    def tearDown(self):
        main.worker_pool = self.original_pool

    async def test_health_is_degraded_without_a_ready_worker(self):
        main.worker_pool = FakePool(True, [FakeWorker(initialized=False)])
        response = Response()

        payload = await main.health(response)

        self.assertEqual(response.status_code, 503)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["status"], "degraded")

    async def test_health_is_ok_with_a_ready_worker(self):
        main.worker_pool = FakePool(True, [FakeWorker(initialized=True)])
        response = Response()

        payload = await main.health(response)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["ready_workers"], 1)


if __name__ == "__main__":
    unittest.main()
