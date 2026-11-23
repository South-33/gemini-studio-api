import concurrent.futures
import unittest

import main


class FakeRequest:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


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


if __name__ == "__main__":
    unittest.main()
