import asyncio
import concurrent.futures
import json
import unittest

import main


class FakeRequest:
    def __init__(self, body, headers=None, disconnected=False):
        self._body = body
        self.headers = headers or {}
        self.disconnected = disconnected

    async def json(self):
        return self._body

    async def receive(self):
        if self.disconnected:
            return {"type": "http.disconnect"}
        await asyncio.Future()


class FakeGeminiPool:
    _initialized = True

    def __init__(self):
        self.calls = []

    async def send_message(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return {
            "success": True,
            "response": "reviewed",
            "attempts": 1,
            "queue_wait_ms": 0,
            "effective_thinking_level": kwargs["thinking_level"],
        }

    def get_diagnostics(self):
        return {"browser_state": "cold", "active_request": False, "queued_requests": 0}


class FakeAgyPool:
    def __init__(self):
        self.models = ["gpt-oss-120b-medium"]
        self.calls = []

    def is_ready(self):
        return True

    def available_models(self):
        return list(self.models)

    def has_model(self, model):
        return model in self.models

    def submit(self, prompt, *, model, use_search, timeout_seconds):
        self.calls.append((prompt, model, use_search, timeout_seconds))
        future = concurrent.futures.Future()
        future.set_result({
            "success": True,
            "response": "agy reviewed",
            "agy_model": model,
            "agy_usage": {},
            "attempts": 1,
            "queue_wait_ms": 0,
        })
        return future

    def diagnostics(self):
        return {"available": True, "models": list(self.models)}


class ApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_worker = main.worker_pool
        self.old_agy = main.agy_pool
        main.recent_requests.clear()
        main.async_jobs.clear()
        self.gemini = FakeGeminiPool()
        self.agy = FakeAgyPool()
        main.worker_pool = self.gemini
        main.agy_pool = self.agy

    async def asyncTearDown(self):
        main.worker_pool = self.old_worker
        main.agy_pool = self.old_agy
        main.recent_requests.clear()
        main.async_jobs.clear()

    async def test_default_is_gemini_neon_medium(self):
        response = await main.chat_completions(
            FakeRequest({"messages": [{"role": "user", "content": "review this"}]})
        )
        self.assertEqual(response["backend"], "gemini")
        self.assertEqual(response["model"], "neon")
        _, kwargs = self.gemini.calls[0]
        self.assertEqual(kwargs["model"], "neon")
        self.assertEqual(kwargs["thinking_level"], "Medium")

    async def test_sync_request_cancellation_waits_for_backend_cleanup(self):
        class CancellingGeminiPool(FakeGeminiPool):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.cleanup_finished = asyncio.Event()

            async def send_message(self, prompt, **kwargs):
                self.started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    await asyncio.sleep(0)
                    self.cleanup_finished.set()
                    raise

        self.gemini = CancellingGeminiPool()
        main.worker_pool = self.gemini
        task = asyncio.create_task(
            main.chat_completions(
                FakeRequest({"messages": [{"role": "user", "content": "review"}]})
            )
        )
        await self.gemini.started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(self.gemini.cleanup_finished.is_set())
        self.assertEqual(main.recent_requests[-1]["status"], "cancelled")

    async def test_sync_gemini_disconnect_cancels_backend_work(self):
        class DisconnectingGeminiPool(FakeGeminiPool):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.cancelled = False

            async def send_message(self, prompt, **kwargs):
                self.started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        class DisconnectingRequest(FakeRequest):
            async def receive(inner_self):
                await self.gemini.started.wait()
                return {"type": "http.disconnect"}

        self.gemini = DisconnectingGeminiPool()
        main.worker_pool = self.gemini
        response = await main.chat_completions(
            DisconnectingRequest(
                {"messages": [{"role": "user", "content": "review this"}]}
            )
        )
        self.assertTrue(self.gemini.cancelled)
        self.assertEqual(response.status_code, 499)
        self.assertEqual(json.loads(response.body), {"detail": "Client disconnected"})
        self.assertEqual(main.recent_requests[-1]["status"], "cancelled")

    async def test_async_gemini_job_ignores_client_disconnect(self):
        response = await main.chat_completions(
            FakeRequest(
                {
                    "async": True,
                    "messages": [{"role": "user", "content": "review this"}],
                },
                disconnected=True,
            )
        )
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 202)
        for _ in range(10):
            await asyncio.sleep(0)
            job = main.async_jobs[payload["request_id"]]
            if job.get("status") == "ok":
                break
        self.assertEqual(job["status"], "ok")

    async def test_gemini_accepts_hydrogen_high(self):
        await main.chat_completions(
            FakeRequest({
                "model": "hydrogen",
                "thinking_level": "high",
                "messages": [{"role": "user", "content": "hello"}],
            })
        )
        _, kwargs = self.gemini.calls[0]
        self.assertEqual(kwargs["model"], "hydrogen")
        self.assertEqual(kwargs["thinking_level"], "High")

    async def test_agy_uses_exact_model_namespace(self):
        response = await main.chat_completions(
            FakeRequest({
                "backend": "agy",
                "model": "gpt-oss-120b-medium",
                "messages": [{"role": "user", "content": "review"}],
            })
        )
        self.assertEqual(response["backend"], "agy")
        self.assertEqual(response["model"], "gpt-oss-120b-medium")
        self.assertEqual(self.agy.calls[0][1], "gpt-oss-120b-medium")

    async def test_agy_rejects_gemini_thinking_setting(self):
        with self.assertRaises(main.HTTPException) as ctx:
            await main.chat_completions(
                FakeRequest({
                    "backend": "agy",
                    "model": "gpt-oss-120b-medium",
                    "thinking_level": "high",
                    "messages": [{"role": "user", "content": "review"}],
                })
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_models_endpoint_is_small(self):
        payload = await main.list_models()
        self.assertEqual([item["id"] for item in payload["data"]], ["hydrogen", "neon"])
        self.assertEqual(payload["default"], {"model": "neon", "thinking_level": "Medium"})

    def test_starlette_route_surface_matches_supported_api(self):
        routes = {route.path for route in main.app.routes}
        self.assertEqual(
            routes,
            {
                "/v1/models",
                "/v1/backends",
                "/v1/chat/completions",
                "/v1/chat/completions/{request_id}",
                "/health",
                "/v1/diagnostics",
            },
        )

    async def test_health_http_propagates_unready_status(self):
        self.gemini.get_diagnostics = lambda: {
            "browser_state": "error",
            "active_request": False,
            "queued_requests": 0,
        }
        response = await main._health_http(None)
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'"ready":false', response.body)

    async def test_starlette_wrapper_json_encodes_endpoint_payload(self):
        response = await main._models_http(FakeRequest(None))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["default"]["model"], "neon")

    async def test_http_exception_handler_preserves_json_detail_and_headers(self):
        response = await main._http_exception_handler(
            FakeRequest(None),
            main.HTTPException(
                status_code=429,
                detail="busy",
                headers={"Retry-After": "5"},
            ),
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "5")
        self.assertEqual(json.loads(response.body), {"detail": "busy"})


if __name__ == "__main__":
    unittest.main()
