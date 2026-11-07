import unittest
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import main
from fastapi import Response


class FakePage:
    def __init__(self, closed=False, url="https://gemini.google.com/app"):
        self.closed = closed
        self.url = url

    def is_closed(self):
        return self.closed


class FakeWorker:
    def __init__(self, initialized=True, closed=False, url="https://gemini.google.com/app"):
        self._initialized = initialized
        self.page = FakePage(closed=closed, url=url)


class FakePool:
    def __init__(self, initialized, workers, active_request=False, queued_requests=0, browser_state="warm"):
        self._initialized = initialized
        self.workers = workers
        self._active_request = active_request
        self._queued_requests = queued_requests
        self._browser_state = browser_state


class ApiStatusTests(unittest.TestCase):
    def setUp(self):
        self.original_pool = main.worker_pool

    def tearDown(self):
        main.worker_pool = self.original_pool

    def test_ready_worker_count_uses_cached_worker_state(self):
        main.worker_pool = FakePool(True, [
            FakeWorker(initialized=True),
            FakeWorker(initialized=False),
            FakeWorker(initialized=True, closed=True),
        ])

        self.assertEqual(main.ready_worker_count(), 2)

    def test_ready_worker_count_is_zero_when_browser_not_warm(self):
        main.worker_pool = FakePool(True, [FakeWorker(initialized=True)], browser_state="cold")

        self.assertEqual(main.ready_worker_count(), 0)

    def test_public_trace_excludes_caller_ip(self):
        public = main.public_request_trace({
            "request_id": "req-1",
            "ip": "127.0.0.1",
            "status": "failed",
            "prompt_chars": 1234,
        })

        self.assertEqual(public, {
            "request_id": "req-1",
            "status": "failed",
            "prompt_chars": 1234,
        })

    def test_public_api_timeout_finishes_before_tunnel_ceiling(self):
        self.assertLess(main.PUBLIC_API_TIMEOUT_SECONDS, 300)

    def test_async_jobs_can_outlive_tunnel_idle_ceiling(self):
        self.assertGreater(main.ASYNC_JOB_TIMEOUT_SECONDS, 300)

    def test_async_job_response_is_idempotent_for_pending_and_completed_jobs(self):
        pending = main.async_job_response({"request_id": "req-1", "status": "started"})
        self.assertEqual(pending.status_code, 202)

        payload = {
            "id": "chatcmpl-1",
            "request_id": "req-1",
            "choices": [{"message": {"content": "ok"}}],
        }
        complete = main.async_job_response({
            "request_id": "req-1",
            "status": "ok",
            "response": payload,
        })
        self.assertEqual(complete.status_code, 200)
        self.assertIn(b'"content":"ok"', complete.body)


class AsyncCancellationTests(unittest.IsolatedAsyncioTestCase):
    class FakeFuture:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True
            return True

    def setUp(self):
        self.original_jobs = main.async_jobs.copy()
        main.async_jobs.clear()

    def tearDown(self):
        main.async_jobs.clear()
        main.async_jobs.update(self.original_jobs)

    async def test_cancel_async_job_marks_terminal_failure_and_cancels_future(self):
        future = self.FakeFuture()
        main.async_jobs["req-cancel"] = {
            "request_id": "req-cancel",
            "status": "started",
            "_future": future,
        }

        payload = await main.cancel_chat_completion("req-cancel")

        self.assertTrue(future.cancelled)
        self.assertTrue(payload["cancelled"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(main.async_jobs["req-cancel"]["status"], "failed")
        response = main.async_job_response(main.async_jobs["req-cancel"])
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"cancelled by client", response.body)


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
        self.assertFalse(payload["active_request"])
        self.assertEqual(payload["status"], "ok")


class EndpointDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_defaults_to_stable_ngrok_control_plane(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_state = Path(temp_dir) / "missing.json"
            with patch.object(main, "ENDPOINT_STATE", missing_state):
                payload = await main.endpoint_discovery()

        self.assertEqual(payload["data_plane_url"], main.CONTROL_PLANE_URL)
        self.assertEqual(payload["control_plane_url"], main.CONTROL_PLANE_URL)
        self.assertEqual(payload["transport"], "ngrok-fallback")

    async def test_endpoint_publishes_current_cloudflare_quick_tunnel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "runtime_endpoint.json"
            state_path.write_text(json.dumps({
                "data_plane_url": "https://example-words.trycloudflare.com",
                "control_plane_url": main.CONTROL_PLANE_URL,
                "transport": "cloudflare-quick",
                "updated_at": "2026-11-05T00:00:00Z",
            }), encoding="utf-8")
            with patch.object(main, "ENDPOINT_STATE", state_path):
                payload = await main.endpoint_discovery()

        self.assertEqual(payload["data_plane_url"], "https://example-words.trycloudflare.com")
        self.assertEqual(payload["transport"], "cloudflare-quick")

    async def test_endpoint_rejects_untrusted_runtime_host(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "runtime_endpoint.json"
            state_path.write_text(json.dumps({
                "data_plane_url": "https://example.invalid",
                "transport": "cloudflare-quick",
            }), encoding="utf-8")
            with patch.object(main, "ENDPOINT_STATE", state_path):
                payload = await main.endpoint_discovery()

        self.assertEqual(payload["data_plane_url"], main.CONTROL_PLANE_URL)
        self.assertEqual(payload["transport"], "ngrok-fallback")
        self.assertEqual(payload["ready_workers"], 1)

    async def test_health_stays_ready_while_request_is_active(self):
        main.worker_pool = FakePool(
            True,
            [FakeWorker(initialized=False)],
            active_request=True,
        )
        response = Response()

        payload = await main.health(response)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["active_request"])
        self.assertEqual(payload["status"], "busy")

    async def test_health_reports_queued_requests(self):
        main.worker_pool = FakePool(
            True,
            [FakeWorker(initialized=True)],
            queued_requests=2,
        )
        response = Response()

        payload = await main.health(response)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["queued_requests"], 2)

    async def test_live_diagnostics_run_on_browser_owner_loop(self):
        class DiagnosticPool(FakePool):
            async def get_live_diagnostics(self):
                return {
                    "browser_state": self._browser_state,
                    "thread_id": threading.get_ident(),
                    "workers": [],
                }

            def get_diagnostics(self):
                return {"browser_state": self._browser_state, "workers": []}

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        self.addCleanup(thread.join, 2)

        main.worker_pool = DiagnosticPool(True, [FakeWorker(initialized=True)])
        main.browser_loop = loop
        caller_thread_id = threading.get_ident()

        payload = await main.get_live_pool_diagnostics()

        self.assertNotEqual(payload["thread_id"], caller_thread_id)
        self.assertEqual(payload["thread_id"], thread.ident)

    async def test_health_is_ok_while_browser_is_intentionally_cold(self):
        main.worker_pool = FakePool(
            True,
            [],
            active_request=False,
            browser_state="cold",
        )
        response = Response()

        payload = await main.health(response)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["ready_workers"], 0)
        self.assertEqual(payload["browser_state"], "cold")
        self.assertEqual(payload["status"], "ok")


class EndpointDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_returns_current_cloudflare_data_plane(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "runtime_endpoint.json"
            state.write_text(json.dumps({
                "data_plane_url": "https://fresh-route.trycloudflare.com",
                "control_plane_url": main.CONTROL_PLANE_URL,
                "transport": "cloudflare-quick",
                "updated_at": "2026-11-05T02:00:00Z",
            }), encoding="utf-8")

            with patch.object(main, "ENDPOINT_STATE", state):
                payload = await main.endpoint_discovery()

        self.assertEqual(payload["data_plane_url"], "https://fresh-route.trycloudflare.com")
        self.assertEqual(payload["control_plane_url"], main.CONTROL_PLANE_URL)
        self.assertEqual(payload["transport"], "cloudflare-quick")

    async def test_discovery_falls_back_to_ngrok_for_missing_or_invalid_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "runtime_endpoint.json"
            state.write_text(json.dumps({"data_plane_url": "https://example.com"}), encoding="utf-8")

            with patch.object(main, "ENDPOINT_STATE", state):
                payload = await main.endpoint_discovery()

        self.assertEqual(payload["data_plane_url"], main.CONTROL_PLANE_URL)
        self.assertEqual(payload["transport"], "ngrok-fallback")


if __name__ == "__main__":
    unittest.main()
