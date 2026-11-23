import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from worker_pool import (
    RDP_X11_WINDOW_HEIGHT,
    RDP_X11_WINDOW_WIDTH,
    WorkerPool,
)


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
    def test_profile_browser_detection_handles_chromium_rewritten_argv(self):
        prefix = f"--user-data-dir={WorkerPool._profile_dir()}"
        rewritten = f"/chrome --no-sandbox {prefix} --remote-debugging-port=0".encode()
        nul_argv = b"/chrome\0--no-sandbox\0" + prefix.encode() + b"\0"
        child = rewritten + b" --type=renderer"

        self.assertTrue(WorkerPool._is_profile_browser_command(rewritten))
        self.assertTrue(WorkerPool._is_profile_browser_command(nul_argv))
        self.assertFalse(WorkerPool._is_profile_browser_command(child))

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
        self.assertIn("--window-size=1440,900", args)
        self.assertNotIn("--start-maximized", args)
        self.assertNotIn("--disable-crashpad-for-testing", args)
        self.assertIn("--disable-background-mode", args)
        self.assertIn("--disable-background-timer-throttling", args)

    def test_persistent_profile_preserves_auth_data_but_resets_preferences(self):
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            with patch.object(WorkerPool, "_state_dir", return_value=state_dir):
                profile = WorkerPool._profile_dir()
                (profile / "Default").mkdir(parents=True)
                (profile / "Default" / "Cookies").write_text("auth", encoding="utf-8")
                (profile / "Default" / "Preferences").write_text("poison", encoding="utf-8")
                (profile / "SingletonLock").symlink_to("stale")
                (profile / "DevToolsActivePort").write_text("1234", encoding="utf-8")

                first = WorkerPool._prepare_profile_dir()
                self.assertEqual(first, profile)
                self.assertEqual(
                    (profile / "Default" / "Cookies").read_text(encoding="utf-8"),
                    "auth",
                )
                self.assertFalse((profile / "Default" / "Preferences").exists())
                self.assertFalse((profile / "SingletonLock").exists())
                self.assertFalse((profile / "DevToolsActivePort").exists())

                (profile / "Default" / "Preferences").write_text("poison-again", encoding="utf-8")
                second = WorkerPool._prepare_profile_dir()
                self.assertEqual(second, first)
                self.assertFalse((profile / "Default" / "Preferences").exists())
                self.assertEqual(
                    (profile / "Default" / "Cookies").read_text(encoding="utf-8"),
                    "auth",
                )

    def test_rdp_resize_targets_largest_browser_window(self):
        pool = WorkerPool()
        pool.browser_process = SimpleNamespace(pid=123)

        def fake_run(command, **kwargs):
            if command[1:4] == ["search", "--pid", "123"]:
                return SimpleNamespace(returncode=0, stdout="10\n20\n")
            if command[1:3] == ["getwindowgeometry", "--shell"]:
                window = command[3]
                size = "WIDTH=10\nHEIGHT=10\n" if window == "10" else "WIDTH=1396\nHEIGHT=856\n"
                return SimpleNamespace(returncode=0, stdout=size)
            return SimpleNamespace(returncode=0, stdout="")

        with (
            patch("worker_pool.shutil.which", return_value="/usr/bin/xdotool"),
            patch.object(pool, "_browser_environment", return_value={"DISPLAY": ":0"}),
            patch("worker_pool.subprocess.run", side_effect=fake_run) as run,
        ):
            pool._resize_browser_window_for_rdp()

        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            ["/usr/bin/xdotool", "windowsize", "20", str(RDP_X11_WINDOW_WIDTH), str(RDP_X11_WINDOW_HEIGHT)],
            calls,
        )

    async def test_clipboard_permission_uses_browser_context(self):
        class FakeContext:
            def __init__(self):
                self.calls = []

            async def grant_permissions(self, permissions, *, origin):
                self.calls.append((permissions, origin))

        pool = WorkerPool()
        context = FakeContext()
        pool.context = context
        await pool._grant_clipboard_permissions()
        self.assertEqual(
            context.calls,
            [(["clipboard-read", "clipboard-write"], "https://gemini.google.com")],
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
