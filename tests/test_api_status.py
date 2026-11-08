import unittest
import asyncio
import base64
import json
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from fastapi import Response


class FakePage:
    def __init__(self, closed=False, url="https://gemini.google.com/app"):
        self.closed = closed
        self.url = url

    def is_closed(self):
        return self.closed


class FakeLoop:
    def __init__(self, running=True):
        self.running = running

    def is_running(self):
        return self.running


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

    async def send_message(self, *args, **kwargs):
        return {
            "success": True,
            "response": "gemini-ok",
            "queue_wait_ms": 0,
            "attempts": 1,
            "effective_thinking_level": kwargs.get("thinking_level"),
            "request_logs": [],
        }


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

    def test_async_capacity_evicts_terminal_job_before_active_job(self):
        original_jobs = main.async_jobs.copy()
        original_limit = main.ASYNC_JOB_LIMIT
        try:
            main.async_jobs.clear()
            main.ASYNC_JOB_LIMIT = 2
            main.async_jobs["active"] = {"request_id": "active", "status": "started"}
            main.async_jobs["done"] = {"request_id": "done", "status": "ok"}

            self.assertTrue(main.make_async_job_capacity())
            self.assertIn("active", main.async_jobs)
            self.assertNotIn("done", main.async_jobs)
        finally:
            main.ASYNC_JOB_LIMIT = original_limit
            main.async_jobs.clear()
            main.async_jobs.update(original_jobs)

    def test_async_capacity_refuses_to_evict_active_jobs(self):
        original_jobs = main.async_jobs.copy()
        original_limit = main.ASYNC_JOB_LIMIT
        try:
            main.async_jobs.clear()
            main.ASYNC_JOB_LIMIT = 2
            main.async_jobs["one"] = {"request_id": "one", "status": "started"}
            main.async_jobs["two"] = {"request_id": "two", "status": "started"}

            self.assertFalse(main.make_async_job_capacity())
            self.assertEqual(set(main.async_jobs), {"one", "two"})
        finally:
            main.ASYNC_JOB_LIMIT = original_limit
            main.async_jobs.clear()
            main.async_jobs.update(original_jobs)

    def test_completion_payload_falls_back_when_actual_usage_is_malformed(self):
        payload = main.build_completion_payload(
            trace_id="req-usage",
            model="flash",
            content="ok",
            prompt_tokens_est=7,
            prompt_chars=12,
            backend="agy",
            actual_usage={
                "input_tokens": 100,
                "output_tokens": "bad",
                "total_tokens": 999,
            },
        )

        self.assertTrue(payload["usage"]["token_estimate"])
        self.assertEqual(payload["usage"]["prompt_tokens"], 7)

    def test_completion_payload_sanitizes_optional_actual_usage_counts(self):
        payload = main.build_completion_payload(
            trace_id="req-usage",
            model="flash",
            content="ok",
            prompt_tokens_est=7,
            prompt_chars=12,
            backend="agy",
            actual_usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": -1,
                "thinking_tokens": "bad",
                "cache_read_tokens": True,
            },
        )

        self.assertFalse(payload["usage"]["token_estimate"])
        self.assertEqual(payload["usage"]["total_tokens"], 120)
        self.assertEqual(payload["usage"]["thinking_tokens"], 0)
        self.assertEqual(payload["usage"]["cache_read_tokens"], 0)

    def test_completion_ids_are_unique_even_within_same_second(self):
        with patch.object(main.time, "time", return_value=1234567890):
            first = main.build_completion_payload(
                trace_id="req-one",
                model="flash",
                content="one",
                prompt_tokens_est=1,
            )
            second = main.build_completion_payload(
                trace_id="req-two",
                model="flash",
                content="two",
                prompt_tokens_est=1,
            )

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["created"], second["created"])


class FakeAgyApiPool:
    def __init__(self, *, initialized=True, available=True):
        self._initialized = initialized
        self.available = available
        self.submissions = []

    def is_model_available(self, family, thinking_level):
        return self.available and family in {"flash", "claude-sonnet-4-6"} and thinking_level in {"Medium", "High"}

    def diagnostics(self):
        return {
            "available": self._initialized,
            "active_request": False,
            "active_requests": 0,
            "queued_requests": 0,
            "max_concurrency": 4,
            "max_queue": 12,
            "models": [{"family": "flash", "thinking_level": "medium", "model": "gemini-3.8-flash-medium"}],
            "model_ids": ["gemini-3.8-flash-medium", "claude-sonnet-4-6"],
            "last_error": None,
        }

    def is_ready(self):
        return self._initialized

    def submit(self, prompt, **kwargs):
        self.submissions.append((prompt, kwargs))
        future = Future()
        future.set_result({
            "success": True,
            "response": "agy-ok",
            "queue_wait_ms": 0,
            "attempts": 1,
            "effective_thinking_level": kwargs["thinking_level"],
            "prompt_transport": "inline",
            "agy_model": "gemini-3.8-flash-medium",
            "agy_usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "thinking_tokens": 5,
                "cache_read_tokens": 0,
                "total_tokens": 120,
            },
            "request_logs": [],
        })
        return future


class FakeApiRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")

    async def json(self):
        return self._body


class AgyApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_agy_pool = main.agy_pool
        self.original_worker_pool = main.worker_pool
        self.original_browser_loop = main.browser_loop
        main.agy_pool = FakeAgyApiPool()
        main.worker_pool = None

    def tearDown(self):
        main.agy_pool = self.original_agy_pool
        main.worker_pool = self.original_worker_pool
        main.browser_loop = self.original_browser_loop

    async def test_agy_backend_uses_separate_dispatch_and_actual_usage(self):
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "thinking_level": "medium",
            "fallback_thinking_level": "high",
            "messages": [{"role": "user", "content": "hello"}],
        })

        payload = await main.openai_chat(request)

        self.assertEqual(payload["backend"], "agy")
        self.assertEqual(payload["choices"][0]["message"]["content"], "agy-ok")
        self.assertFalse(payload["usage"]["token_estimate"])
        self.assertEqual(payload["usage"]["prompt_tokens"], 100)
        self.assertEqual(payload["usage"]["completion_tokens"], 20)
        prompt, kwargs = main.agy_pool.submissions[0]
        self.assertEqual(prompt, "hello\n")
        self.assertEqual(kwargs["model"], "flash")
        self.assertEqual(kwargs["thinking_level"], "Medium")
        self.assertEqual(kwargs["fallback_thinking_level"], "High")

    async def test_omitted_backend_prefers_agy(self):
        request = FakeApiRequest({
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        payload = await main.openai_chat(request)

        self.assertEqual(payload["backend"], "agy")
        self.assertEqual(len(main.agy_pool.submissions), 1)

    async def test_non_string_message_content_is_rejected(self):
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "messages": [{"role": "user", "content": {"unexpected": "object"}}],
        })

        with self.assertRaises(main.HTTPException) as raised:
            await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("string or array", raised.exception.detail)

    async def test_non_string_text_part_is_rejected(self):
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": 123}],
            }],
        })

        with self.assertRaises(main.HTTPException) as raised:
            await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Text content must be a string", raised.exception.detail)

    async def test_image_data_url_requires_base64_marker(self):
        main.worker_pool = FakePool(True, [])
        main.browser_loop = FakeLoop()
        request = FakeApiRequest({
            "model": "flash",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": "data:image/png,AAAA"},
                }],
            }],
        })

        with self.assertRaises(main.HTTPException) as raised:
            await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("base64", raised.exception.detail.lower())

    async def test_unsupported_image_type_is_rejected_before_base64_decode(self):
        request = FakeApiRequest({
            "model": "flash",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": "data:image/svg+xml;base64,AAAA"},
                }],
            }],
        })

        with patch.object(main.base64, "b64decode") as decode:
            with self.assertRaises(main.HTTPException) as raised:
                await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("unsupported image mime type", raised.exception.detail.lower())
        decode.assert_not_called()

    async def test_image_write_failure_cleans_partial_temp_file(self):
        png_data = base64.b64encode(b"image-bytes").decode("ascii")
        request = FakeApiRequest({
            "model": "flash",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{png_data}"},
                }],
            }],
        })
        original_named_tempfile = tempfile.NamedTemporaryFile

        with tempfile.TemporaryDirectory() as temp_dir:
            class FailingTempFile:
                def __init__(self, *args, **kwargs):
                    kwargs["dir"] = temp_dir
                    self._file = original_named_tempfile(*args, **kwargs)
                    self.name = self._file.name

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self._file.close()

                def write(self, data):
                    self._file.write(b"partial")
                    self._file.flush()
                    raise OSError("disk full")

            with patch.object(main.tempfile, "NamedTemporaryFile", side_effect=FailingTempFile):
                with self.assertRaises(main.HTTPException) as raised:
                    await main.openai_chat(request)

            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_omitted_backend_async_job_records_actual_agy_backend(self):
        request = FakeApiRequest({
            "async": True,
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        response = await main.openai_chat(request)
        payload = json.loads(response.body)
        request_id = payload["request_id"]
        job = main.async_jobs[request_id]
        try:
            self.assertEqual(response.status_code, 202)
            self.assertEqual(payload["backend"], "agy")
            await job["_task"]
            self.assertEqual(job["status"], "ok")
            self.assertEqual(job["response"]["backend"], "agy")
        finally:
            main.async_jobs.pop(request_id, None)

    async def test_full_async_capacity_rejects_before_backend_submission(self):
        original_jobs = main.async_jobs.copy()
        original_limit = main.ASYNC_JOB_LIMIT
        try:
            main.async_jobs.clear()
            main.ASYNC_JOB_LIMIT = 1
            main.async_jobs["already-running"] = {
                "request_id": "already-running",
                "backend": "agy",
                "status": "started",
            }
            request = FakeApiRequest({
                "async": True,
                "backend": "agy",
                "model": "flash",
                "messages": [{"role": "user", "content": "hello"}],
            })

            with self.assertRaises(main.HTTPException) as raised:
                await main.openai_chat(request)

            self.assertEqual(raised.exception.status_code, 429)
            self.assertEqual(main.agy_pool.submissions, [])
        finally:
            main.ASYNC_JOB_LIMIT = original_limit
            main.async_jobs.clear()
            main.async_jobs.update(original_jobs)

    async def test_invalid_async_request_is_validated_before_capacity_rejection(self):
        original_jobs = main.async_jobs.copy()
        original_limit = main.ASYNC_JOB_LIMIT
        try:
            main.async_jobs.clear()
            main.ASYNC_JOB_LIMIT = 1
            main.async_jobs["already-running"] = {
                "request_id": "already-running",
                "backend": "agy",
                "status": "started",
            }
            request = FakeApiRequest({
                "async": True,
                "backend": "agy",
                "model": "flash",
                "messages": [{"role": "user", "content": {"invalid": True}}],
            })

            with self.assertRaises(main.HTTPException) as raised:
                await main.openai_chat(request)

            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(set(main.async_jobs), {"already-running"})
            self.assertEqual(main.agy_pool.submissions, [])
        finally:
            main.ASYNC_JOB_LIMIT = original_limit
            main.async_jobs.clear()
            main.async_jobs.update(original_jobs)

    async def test_agy_backend_rejects_model_not_exposed_by_cli(self):
        request = FakeApiRequest({
            "backend": "agy",
            "model": "pro",
            "thinking_level": "high",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with self.assertRaises(main.HTTPException) as raised:
            await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("does not currently expose", raised.exception.detail)

    async def test_agy_backend_accepts_exact_cli_model_id(self):
        request = FakeApiRequest({
            "backend": "agy",
            "model": "claude-sonnet-4-6",
            "thinking_level": "high",
            "messages": [{"role": "user", "content": "hello"}],
        })

        payload = await main.openai_chat(request)

        self.assertEqual(payload["backend"], "agy")
        _, kwargs = main.agy_pool.submissions[0]
        self.assertEqual(kwargs["model"], "claude-sonnet-4-6")
        self.assertEqual(kwargs["thinking_level"], "High")

    async def test_exact_cli_model_rejects_thinking_fallback(self):
        request = FakeApiRequest({
            "backend": "agy",
            "model": "claude-sonnet-4-6",
            "thinking_level": "high",
            "fallback_thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with self.assertRaises(main.HTTPException) as raised:
            await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("stable Gemini family alias", raised.exception.detail)

    async def test_backend_listing_reports_antigravity_models(self):
        payload = await main.list_backends()
        agy = next(item for item in payload["data"] if item["id"] == "agy")

        self.assertEqual(payload["default_policy"], "agy-first")
        self.assertTrue(agy["available"])
        self.assertEqual(agy["transport"], "antigravity-cli")
        self.assertEqual(agy["models"][0]["family"], "flash")
        self.assertIn("claude-sonnet-4-6", agy["model_ids"])
        self.assertEqual(agy["max_concurrency"], 3)
        self.assertEqual(agy["max_queue"], 9)

    async def test_uninitialized_gemini_backend_returns_503_even_if_object_exists(self):
        main.worker_pool = FakePool(False, [], browser_state="stopped")
        request = FakeApiRequest({
            "backend": "gemini",
            "model": "flash",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with self.assertRaises(main.HTTPException) as raised:
            await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("not initialized", raised.exception.detail)

    async def test_auto_routing_uses_gemini_when_agy_is_unavailable(self):
        main.agy_pool = FakeAgyApiPool(initialized=False)
        main.worker_pool = FakePool(True, [])
        main.browser_loop = FakeLoop()

        backend, model, reason = main.select_request_backend(
            requested_backend="auto",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level=None,
            has_images=False,
        )

        self.assertEqual((backend, model), ("gemini", "flash"))
        self.assertEqual(reason, "agy_unavailable")

    async def test_auto_routing_does_not_use_stopped_gemini_loop(self):
        main.agy_pool = FakeAgyApiPool(initialized=False)
        main.worker_pool = FakePool(True, [])
        main.browser_loop = FakeLoop(running=False)

        with self.assertRaises(main.HTTPException) as raised:
            main.select_request_backend(
                requested_backend="auto",
                model="flash",
                thinking_level="Medium",
                fallback_thinking_level=None,
                has_images=False,
            )

        self.assertEqual(raised.exception.status_code, 503)

    async def test_auto_routing_uses_gemini_for_images(self):
        main.worker_pool = FakePool(True, [])
        main.browser_loop = FakeLoop()

        backend, model, reason = main.select_request_backend(
            requested_backend="auto",
            model="flash",
            thinking_level="Medium",
            fallback_thinking_level=None,
            has_images=True,
        )

        self.assertEqual((backend, model), ("gemini", "flash"))
        self.assertEqual(reason, "images_require_gemini")

    async def test_gemini_dispatch_race_closes_coroutine_and_cleans_image(self):
        main.worker_pool = FakePool(True, [])
        main.browser_loop = FakeLoop()
        png_data = base64.b64encode(b"not-a-real-png-but-valid-base64").decode("ascii")
        request = FakeApiRequest({
            "backend": "gemini",
            "model": "flash",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{png_data}"},
                }],
            }],
        })
        scheduled = {}
        original_named_tempfile = tempfile.NamedTemporaryFile

        def fail_schedule(coro, loop):
            scheduled["coro"] = coro
            raise RuntimeError("browser loop stopped")

        with tempfile.TemporaryDirectory() as temp_dir:
            def local_named_tempfile(*args, **kwargs):
                kwargs["dir"] = temp_dir
                return original_named_tempfile(*args, **kwargs)

            with patch.object(main.tempfile, "NamedTemporaryFile", side_effect=local_named_tempfile), patch.object(
                main.asyncio,
                "run_coroutine_threadsafe",
                side_effect=fail_schedule,
            ):
                with self.assertRaises(main.HTTPException) as raised:
                    await main.openai_chat(request)

            self.assertEqual(raised.exception.status_code, 503)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])
            self.assertIsNone(scheduled["coro"].cr_frame)

    async def test_untagged_request_spills_to_gemini_when_agy_queue_is_full(self):
        main.worker_pool = FakePool(True, [])
        main.browser_loop = FakeLoop()
        request = FakeApiRequest({
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        def fake_run_coroutine_threadsafe(coro, loop):
            coro.close()
            future = Future()
            future.set_result({
                "success": True,
                "response": "gemini-ok",
                "queue_wait_ms": 0,
                "attempts": 1,
                "effective_thinking_level": "Medium",
                "request_logs": [],
            })
            return future

        with patch.object(
            main.agy_pool,
            "submit",
            side_effect=main.AgyQueueFullError("Antigravity CLI queue is full"),
        ), patch.object(
            main.asyncio,
            "run_coroutine_threadsafe",
            side_effect=fake_run_coroutine_threadsafe,
        ):
            payload = await main.openai_chat(request)

        self.assertEqual(payload["backend"], "gemini")
        self.assertEqual(payload["choices"][0]["message"]["content"], "gemini-ok")

    async def test_untagged_request_spills_when_agy_closes_before_submit(self):
        main.worker_pool = FakePool(True, [])
        main.browser_loop = FakeLoop()
        request = FakeApiRequest({
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        def fake_run_coroutine_threadsafe(coro, loop):
            coro.close()
            future = Future()
            future.set_result({
                "success": True,
                "response": "gemini-ok",
                "queue_wait_ms": 0,
                "attempts": 1,
                "effective_thinking_level": "Medium",
                "request_logs": [],
            })
            return future

        with patch.object(
            main.agy_pool,
            "submit",
            side_effect=RuntimeError("Antigravity CLI pool is closed"),
        ), patch.object(
            main.asyncio,
            "run_coroutine_threadsafe",
            side_effect=fake_run_coroutine_threadsafe,
        ):
            payload = await main.openai_chat(request)

        self.assertEqual(payload["backend"], "gemini")

    async def test_strict_agy_returns_503_when_pool_closes_before_submit(self):
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(
            main.agy_pool,
            "submit",
            side_effect=RuntimeError("Antigravity CLI pool is closed"),
        ):
            with self.assertRaises(main.HTTPException) as raised:
                await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_full_agy_queue_returns_429_with_retry_after(self):
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(
            main.agy_pool,
            "submit",
            side_effect=main.AgyQueueFullError("Antigravity CLI queue is full"),
        ):
            with self.assertRaises(main.HTTPException) as raised:
                await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers.get("Retry-After"), "5")

    async def test_agy_print_timeout_returns_504(self):
        future = Future()
        future.set_result({
            "success": False,
            "timed_out": True,
            "error": "Antigravity CLI print timeout after 1s; partial output was discarded",
            "queue_wait_ms": 0,
            "attempts": 1,
            "effective_thinking_level": "Medium",
            "request_logs": [],
        })
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(main.agy_pool, "submit", return_value=future):
            with self.assertRaises(main.HTTPException) as raised:
                await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 504)
        self.assertIn("partial output was discarded", raised.exception.detail)

    async def test_agy_runtime_unavailable_returns_503(self):
        future = Future()
        future.set_result({
            "success": False,
            "backend_unavailable": True,
            "error": "Antigravity CLI backend became unavailable: binary vanished",
            "queue_wait_ms": 0,
            "attempts": 1,
            "effective_thinking_level": "Medium",
            "request_logs": [],
        })
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(main.agy_pool, "submit", return_value=future):
            with self.assertRaises(main.HTTPException) as raised:
                await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_async_agy_print_timeout_is_reported_as_timeout(self):
        future = Future()
        future.set_result({
            "success": False,
            "timed_out": True,
            "error": "Antigravity CLI print timeout",
            "queue_wait_ms": 0,
            "attempts": 1,
            "effective_thinking_level": "Medium",
            "request_logs": [],
        })
        request = FakeApiRequest({
            "backend": "agy",
            "async": True,
            "model": "flash",
            "thinking_level": "medium",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(main.agy_pool, "submit", return_value=future):
            response = await main.openai_chat(request)
        payload = json.loads(response.body)
        request_id = payload["request_id"]
        job = main.async_jobs[request_id]
        try:
            await job["_task"]
            self.assertEqual(job["status"], "timeout")
            self.assertIn("print timeout", job["error"])
        finally:
            main.async_jobs.pop(request_id, None)

    async def test_async_malformed_backend_result_finishes_as_error(self):
        future = Future()
        future.set_result(None)
        request = FakeApiRequest({
            "backend": "agy",
            "async": True,
            "model": "flash",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(main.agy_pool, "submit", return_value=future):
            response = await main.openai_chat(request)
        payload = json.loads(response.body)
        request_id = payload["request_id"]
        job = main.async_jobs[request_id]
        try:
            await job["_task"]
            self.assertEqual(job["status"], "error")
            self.assertIn("invalid result", job["error"])
        finally:
            main.async_jobs.pop(request_id, None)

    async def test_sync_malformed_backend_result_returns_500(self):
        future = Future()
        future.set_result(None)
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(main.agy_pool, "submit", return_value=future):
            with self.assertRaises(main.HTTPException) as raised:
                await main.openai_chat(request)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("invalid result", raised.exception.detail)

    async def test_async_task_cancellation_cancels_queued_backend_future(self):
        future = Future()
        request = FakeApiRequest({
            "backend": "agy",
            "async": True,
            "model": "flash",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(main.agy_pool, "submit", return_value=future):
            response = await main.openai_chat(request)
        payload = json.loads(response.body)
        request_id = payload["request_id"]
        job = main.async_jobs[request_id]
        try:
            await asyncio.sleep(0)
            job["_task"].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job["_task"]
            self.assertTrue(future.cancelled())
            self.assertEqual(job["status"], "error")
        finally:
            main.async_jobs.pop(request_id, None)

    async def test_sync_task_cancellation_cancels_queued_backend_future(self):
        future = Future()
        request = FakeApiRequest({
            "backend": "agy",
            "model": "flash",
            "messages": [{"role": "user", "content": "hello"}],
        })

        with patch.object(main.agy_pool, "submit", return_value=future):
            task = asyncio.create_task(main.openai_chat(request))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(future.cancelled())


class FakeStartupAgyPool:
    def __init__(self):
        self.max_concurrency = 4
        self.max_queue = 12
        self.closed = False

    def init(self):
        return True

    def close(self):
        self.closed = True


class LifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_agy_does_not_wait_for_gemini_startup(self):
        fake_agy = FakeStartupAgyPool()
        gemini_started = asyncio.Event()
        gemini_release = asyncio.Event()

        async def slow_gemini_startup():
            gemini_started.set()
            await gemini_release.wait()
            return True

        with patch("main.AgyPool", return_value=fake_agy), patch(
            "main.initialize_gemini_backend",
            side_effect=slow_gemini_startup,
        ), patch("main.stop_browser_runtime"):
            async with main.lifespan(main.app):
                await asyncio.wait_for(gemini_started.wait(), timeout=1)
                self.assertFalse(gemini_release.is_set())

        self.assertTrue(fake_agy.closed)

    async def test_startup_cancellation_closes_agy_pool(self):
        fake_agy = FakeStartupAgyPool()
        init_started = asyncio.Event()
        release_init = asyncio.Event()

        async def blocked_to_thread(*args, **kwargs):
            init_started.set()
            await release_init.wait()
            return True

        manager = main.lifespan(main.app)
        with patch("main.AgyPool", return_value=fake_agy), patch(
            "main.asyncio.to_thread",
            side_effect=blocked_to_thread,
        ), patch("main.stop_browser_runtime"):
            task = asyncio.create_task(manager.__aenter__())
            await asyncio.wait_for(init_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(fake_agy.closed)


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_pool = main.worker_pool
        self.original_agy_pool = main.agy_pool
        main.agy_pool = None

    def tearDown(self):
        main.worker_pool = self.original_pool
        main.agy_pool = self.original_agy_pool

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

    async def test_health_stays_ready_when_only_antigravity_is_available(self):
        main.worker_pool = None
        main.agy_pool = FakeAgyApiPool()
        response = Response()

        payload = await main.health(response)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["backends"]["gemini"]["ready"])
        self.assertTrue(payload["backends"]["agy"]["ready"])


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
