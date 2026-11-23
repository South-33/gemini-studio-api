import asyncio
import json
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from worker_pool import WorkerPool


TRANSIENT = "I'm having a hard time fulfilling your request. Can I help with something else instead?"


class FakeWorker:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self._initialized = True
        self._generation_in_progress = False
        self._current_selected_model = None
        self._current_selected_thinking_level = None

    async def send_message(self, prompt, model, thinking, use_search, images, request_id=None):
        self.calls.append((prompt, model, thinking, use_search, tuple(images or []), request_id))
        return self.responses.pop(0)

    async def retry_last_response_native(self):
        return None

    async def _save_diagnostic_artifacts(self, *args, **kwargs):
        return None

    async def close(self):
        return None


class WorkerPoolTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_browser_detection_handles_chromium_rewritten_argv(self):
        prefix = f"--user-data-dir={WorkerPool._state_dir() / 'runtime-test'}"
        rewritten = f"/chrome --no-sandbox {prefix} --remote-debugging-port=0".encode()
        nul_argv = b"/chrome\0--no-sandbox\0" + prefix.encode() + b"\0"
        child = rewritten + b" --type=renderer"

        self.assertTrue(WorkerPool._is_runtime_browser_command(rewritten))
        self.assertTrue(WorkerPool._is_runtime_browser_command(nul_argv))
        self.assertFalse(WorkerPool._is_runtime_browser_command(child))

    async def test_auth_required_browser_is_eventually_released(self):
        pool = WorkerPool(idle_browser_seconds=60, idle_poll_seconds=0.01)
        await pool.init()
        pool._browser_state = "auth_required"
        pool._last_activity = time.time() - 901
        pool._close_browser_context = AsyncMock()
        for _ in range(50):
            if pool._close_browser_context.await_count:
                break
            await asyncio.sleep(0.01)
        pool._close_browser_context.assert_awaited()
        await pool.close()

    def test_linux_browser_args_avoid_unstable_gpu_path(self):
        args = WorkerPool._browser_args()
        self.assertIn("--ozone-platform=x11", args)
        self.assertIn("--disable-gpu", args)
        self.assertIn("--window-size=1280,720", args)
        self.assertNotIn("--start-maximized", args)
        self.assertNotIn("--disable-crashpad-for-testing", args)
        self.assertIn("--disable-background-mode", args)
        self.assertIn("--disable-background-timer-throttling", args)

    def test_runtime_profiles_are_disposable_and_unique(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            with patch.object(WorkerPool, "_state_dir", return_value=state_dir):
                first = WorkerPool._new_runtime_profile()
                second = WorkerPool._new_runtime_profile()
            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith("runtime-"))
            self.assertTrue(second.name.startswith("runtime-"))
            self.assertEqual(first.parent, state_dir)
            self.assertEqual(second.parent, state_dir)

    async def test_auth_state_is_saved_and_loaded_separately_from_runtime_profile(self):
        class FakeSession:
            def __init__(self):
                self.set_cookies = None

            async def send(self, method, params=None):
                if method == "Storage.getCookies":
                    return {
                        "cookies": [
                            {
                                "name": "SID",
                                "value": "secret",
                                "domain": ".google.com",
                                "path": "/",
                                "secure": True,
                                "httpOnly": True,
                                "sameSite": "Lax",
                                "expires": 9999999999,
                                "size": 100,
                            },
                            {
                                "name": "other",
                                "value": "drop",
                                "domain": ".example.com",
                                "path": "/",
                            },
                        ]
                    }
                if method == "Storage.setCookies":
                    self.set_cookies = params["cookies"]
                    return {}
                raise AssertionError(method)

        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            with patch.object(WorkerPool, "_state_dir", return_value=state_dir):
                pool = WorkerPool()
                pool.browser_cdp_session = FakeSession()
                await pool._save_auth_state()
                auth_state = WorkerPool._auth_state_path()
                self.assertTrue(auth_state.is_file())
                self.assertEqual(
                    stat.S_IMODE(auth_state.stat().st_mode),
                    0o600,
                )
                payload = json.loads(auth_state.read_text(encoding="utf-8"))
                self.assertEqual([cookie["name"] for cookie in payload["cookies"]], ["SID"])
                self.assertNotIn("size", payload["cookies"][0])

                restored = FakeSession()
                pool.browser_cdp_session = restored
                await pool._load_auth_state()
                self.assertEqual(restored.set_cookies, payload["cookies"])

    async def test_clipboard_permission_uses_browser_cdp_session(self):
        class FakeSession:
            def __init__(self):
                self.calls = []

            async def send(self, method, params=None):
                self.calls.append((method, params))
                return {}

        pool = WorkerPool()
        session = FakeSession()
        pool.browser_cdp_session = session
        await pool._grant_clipboard_permissions()
        self.assertEqual(
            [params["permission"]["name"] for _, params in session.calls],
            ["clipboard-read", "clipboard-write"],
        )

    async def test_failed_ready_reset_keeps_pool_recoverable(self):
        pool = WorkerPool()
        await pool.init()
        worker = FakeWorker([])
        worker.prepare_next_request = AsyncMock(return_value=False)
        pool.workers = [worker]
        pool._browser_state = "warm"
        pool._recreate_worker = AsyncMock(return_value=False)
        pool._close_browser_context = AsyncMock()

        self.assertFalse(await pool._ensure_ready_after_request("test"))
        self.assertTrue(pool._initialized)
        pool._close_browser_context.assert_awaited_once_with("error", "test")
        await pool.close()

    async def test_init_is_truly_lazy(self):
        pool = WorkerPool()
        self.assertTrue(await pool.init())
        self.assertIsNone(pool.playwright)
        self.assertIsNone(pool.context)
        self.assertEqual(pool._browser_state, "stopped")
        await pool.close()

    async def test_retry_keeps_same_model_and_thinking(self):
        pool = WorkerPool()
        await pool.init()
        worker = FakeWorker([
            {"success": True, "response": TRANSIENT},
            {"success": True, "response": "done"},
        ])
        pool.workers = [worker]
        pool._browser_state = "warm"
        pool._ensure_browser_ready = AsyncMock(return_value=True)
        pool._prepare_request_configuration = AsyncMock(return_value=(True, None))
        pool._recreate_worker = AsyncMock(return_value=True)
        pool._ensure_ready_after_request = AsyncMock(return_value=True)

        result = await pool.send_message(
            "review",
            model="neon",
            thinking_level="Medium",
            request_id="r1",
        )
        self.assertTrue(result["success"])
        self.assertEqual([call[1:3] for call in worker.calls], [
            ("neon", "Medium"),
            ("neon", "Medium"),
        ])
        await pool.close()

    async def test_requests_are_serialized(self):
        pool = WorkerPool()
        await pool.init()
        active = 0
        max_active = 0

        class SlowWorker(FakeWorker):
            async def send_message(self, *args, **kwargs):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.02)
                active -= 1
                return {"success": True, "response": "ok"}

        worker = SlowWorker([])
        pool.workers = [worker]
        pool._browser_state = "warm"
        pool._ensure_browser_ready = AsyncMock(return_value=True)
        pool._prepare_request_configuration = AsyncMock(return_value=(True, None))
        pool._ensure_ready_after_request = AsyncMock(return_value=True)

        await asyncio.gather(
            pool.send_message("a", model="neon", thinking_level="Medium"),
            pool.send_message("b", model="neon", thinking_level="Medium"),
        )
        self.assertEqual(max_active, 1)
        await pool.close()


if __name__ == "__main__":
    unittest.main()
