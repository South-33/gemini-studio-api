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

    def test_only_sigtrap_browser_start_is_retryable(self):
        self.assertTrue(
            WorkerPool._is_retryable_browser_start_error(
                RuntimeError("Chromium exited before CDP attach with code -5")
            )
        )
        self.assertTrue(
            WorkerPool._is_retryable_browser_start_error(
                RuntimeError("Chromium exited during startup with code -5")
            )
        )
        self.assertFalse(
            WorkerPool._is_retryable_browser_start_error(
                RuntimeError("Chromium did not publish a DevTools endpoint")
            )
        )

    async def test_auth_required_virtual_display_releases_playwright(self):
        pool = WorkerPool()
        pool.browser_uses_virtual_display = True
        pool.virtual_display_process = SimpleNamespace(pid=4321)
        pool.playwright = SimpleNamespace(stop=AsyncMock())
        with patch.object(pool, "_stop_browser_process") as stop_browser:
            await pool._close_browser_context("auth_required", "test")

        stop_browser.assert_called_once()
        self.assertIsNone(pool.playwright)
        self.assertEqual(pool._browser_state, "auth_required")

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

    def test_live_display_is_preferred_over_virtual_display(self):
        pool = WorkerPool()
        environment = {"DISPLAY": ":7"}
        with (
            patch.object(pool, "_browser_environment", return_value=environment),
            patch.object(pool, "_display_is_usable", return_value=True),
            patch.object(pool, "_start_virtual_display") as start_virtual,
        ):
            result = pool._browser_launch_environment()

        self.assertIs(result, environment)
        self.assertEqual(pool.browser_display, ":7")
        self.assertFalse(pool.browser_uses_virtual_display)
        start_virtual.assert_not_called()


    def test_stale_private_xvfb_artifacts_are_removed_when_owned(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lock_path = root / ".X99-lock"
            socket_path = root / "X99"
            lock_path.write_text("999999", encoding="utf-8")
            socket_path.write_text("stale", encoding="utf-8")

            with patch("worker_pool.os.kill", side_effect=ProcessLookupError):
                self.assertTrue(
                    WorkerPool._remove_stale_x11_artifacts(lock_path, socket_path)
                )
            self.assertFalse(lock_path.exists())
            self.assertFalse(socket_path.exists())

    def test_live_private_xvfb_artifacts_are_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lock_path = root / ".X99-lock"
            socket_path = root / "X99"
            lock_path.write_text("12345", encoding="utf-8")
            socket_path.write_text("live", encoding="utf-8")

            with patch("worker_pool.os.kill", return_value=None):
                self.assertFalse(
                    WorkerPool._remove_stale_x11_artifacts(lock_path, socket_path)
                )
            self.assertTrue(lock_path.exists())
            self.assertTrue(socket_path.exists())

    def test_missing_display_starts_private_xvfb(self):
        pool = WorkerPool()
        process = SimpleNamespace(pid=4321, poll=lambda: None)
        with (
            patch.object(pool, "_browser_environment", return_value={"DISPLAY": ":stale"}),
            patch.object(pool, "_display_is_usable", side_effect=[False, True]),
            patch("worker_pool.shutil.which", return_value="/usr/bin/Xvfb"),
            patch("worker_pool.Path.exists", return_value=False),
            patch("worker_pool.subprocess.Popen", return_value=process) as popen,
        ):
            result = pool._browser_launch_environment()

        self.assertEqual(result["DISPLAY"], ":99")
        self.assertEqual(pool.browser_display, ":99")
        self.assertTrue(pool.browser_uses_virtual_display)
        self.assertIs(pool.virtual_display_process, process)
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/Xvfb", ":99"])
        self.assertIn("-nolisten", command)
        self.assertIn("-ac", command)

    def test_rdp_resize_skips_private_virtual_display(self):
        pool = WorkerPool()
        pool.browser_process = SimpleNamespace(pid=123)
        pool.browser_uses_virtual_display = True
        with patch("worker_pool.subprocess.run") as run:
            pool._resize_browser_window_for_rdp({"DISPLAY": ":99"})
        run.assert_not_called()

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

    async def test_dead_warm_browser_is_relaunched_before_request(self):
        pool = WorkerPool()
        await pool.init()
        worker = FakeWorker([])
        worker.page = SimpleNamespace(is_closed=lambda: False)
        pool.workers = [worker]
        pool._browser_state = "warm"
        pool.browser_process = SimpleNamespace(poll=lambda: -5)
        pool.browser = SimpleNamespace(is_connected=lambda: True)
        pool.context = SimpleNamespace()
        pool._close_browser_context = AsyncMock()
        pool._warm_browser = AsyncMock(return_value=True)

        self.assertTrue(await pool._ensure_browser_ready("request r1"))
        pool._close_browser_context.assert_awaited_once_with(
            "error", "browser runtime disappeared before request r1"
        )
        pool._warm_browser.assert_awaited_once_with("request r1")
        await pool.close()

    async def test_tracked_configuration_skips_redundant_ui_verification(self):
        pool = WorkerPool()
        worker = FakeWorker([])
        worker._current_selected_model = "neon"
        worker._current_selected_thinking_level = "Medium"
        worker.ensure_configuration = AsyncMock(return_value=(True, False))
        pool.workers = [worker]

        ok, error = await pool._prepare_request_configuration("neon", "Medium", "r1")

        self.assertTrue(ok)
        self.assertIsNone(error)
        worker.ensure_configuration.assert_not_awaited()

    async def test_init_is_truly_lazy(self):
        pool = WorkerPool()
        self.assertTrue(await pool.init())
        self.assertIsNone(pool.playwright)
        self.assertIsNone(pool.context)
        self.assertEqual(pool._browser_state, "stopped")
        await pool.close()


    async def test_cancelled_request_cleans_browser_before_queued_handoff(self):
        pool = WorkerPool(idle_browser_seconds=0)
        await pool.init()
        first_entered = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        second_started = asyncio.Event()
        calls = 0

        async def wait_for_browser(_reason):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                await asyncio.Future()
            second_started.set()
            return False

        async def close_browser(_state, _reason):
            cleanup_started.set()
            await release_cleanup.wait()

        pool._ensure_browser_ready = wait_for_browser
        pool._close_browser_context = close_browser

        first = asyncio.create_task(
            pool.send_message(
                "first", model="neon", thinking_level="Medium", request_id="first"
            )
        )
        await first_entered.wait()
        second = asyncio.create_task(
            pool.send_message(
                "second", model="neon", thinking_level="Medium", request_id="second"
            )
        )
        await asyncio.sleep(0)

        first.cancel()
        await cleanup_started.wait()
        await asyncio.sleep(0.01)
        self.assertFalse(second_started.is_set())

        release_cleanup.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertTrue(second_started.is_set())
        self.assertFalse(pool._active_request)
        await pool.close()

    async def test_retry_closes_failed_tab_before_creating_replacement(self):
        pool = WorkerPool()
        old_worker = FakeWorker([])
        order = []

        async def close_old():
            order.append("close_old")

        async def new_page():
            order.append("new_page")
            return object()

        old_worker.close = close_old
        pool.workers = [old_worker]
        pool.context = SimpleNamespace(new_page=new_page)
        pool._initialize_page = AsyncMock(return_value=FakeWorker([]))

        self.assertTrue(await pool._recreate_worker("retry", close_old_first=True))
        self.assertEqual(order[:2], ["close_old", "new_page"])


    async def test_retry_continues_when_failed_tab_close_raises(self):
        pool = WorkerPool()
        old_worker = FakeWorker([])

        async def close_old():
            raise RuntimeError("page already gone")

        old_worker.close = close_old
        pool.workers = [old_worker]
        pool.context = SimpleNamespace(new_page=AsyncMock(return_value=object()))
        replacement = FakeWorker([])
        pool._initialize_page = AsyncMock(return_value=replacement)

        self.assertTrue(await pool._recreate_worker("retry", close_old_first=True))
        self.assertIs(pool.worker, replacement)

    def test_active_request_reports_zero_idle_seconds(self):
        pool = WorkerPool()
        pool._last_activity = time.time() - 3600
        pool._active_request = True
        self.assertEqual(pool.get_diagnostics()["idle_for_seconds"], 0)

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

    async def test_successful_cleanup_is_deferred_until_next_request(self):
        pool = WorkerPool()
        await pool.init()
        worker = FakeWorker([
            {"success": True, "response": "one"},
            {"success": True, "response": "two"},
        ])
        pool.workers = [worker]
        pool._browser_state = "warm"
        pool._ensure_browser_ready = AsyncMock(return_value=True)
        pool._prepare_request_configuration = AsyncMock(return_value=(True, None))
        pool._ensure_ready_after_request = AsyncMock(return_value=True)

        first = await pool.send_message(
            "a", model="neon", thinking_level="Medium", request_id="r1"
        )
        self.assertTrue(first["success"])
        self.assertTrue(pool._needs_ready_reset)
        pool._ensure_ready_after_request.assert_not_awaited()

        second = await pool.send_message(
            "b", model="neon", thinking_level="Medium", request_id="r2"
        )
        self.assertTrue(second["success"])
        pool._ensure_ready_after_request.assert_awaited_once_with("before request r2")
        self.assertTrue(pool._needs_ready_reset)
        await pool.close()

    async def test_failed_deferred_reset_recovers_browser_before_request(self):
        pool = WorkerPool()
        await pool.init()
        worker = FakeWorker([{"success": True, "response": "ok"}])
        pool.workers = [worker]
        pool._browser_state = "warm"
        pool._needs_ready_reset = True
        pool._ensure_browser_ready = AsyncMock(side_effect=[True, True])
        pool._ensure_ready_after_request = AsyncMock(return_value=False)
        pool._prepare_request_configuration = AsyncMock(return_value=(True, None))

        result = await pool.send_message(
            "next", model="neon", thinking_level="Medium", request_id="r2"
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            pool._ensure_browser_ready.await_args_list[1].args[0],
            "recover after failed ready reset for request r2",
        )
        self.assertTrue(pool._needs_ready_reset)
        await pool.close()


if __name__ == "__main__":
    unittest.main()
