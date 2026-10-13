import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock

from gemini_web import GeminiWebAutomation
from worker_pool import WorkerPool


class FakeLocator:
    def __init__(self):
        self.filled = []
        self.clicked = 0

    @property
    def first(self):
        return self

    async def fill(self, value, timeout=None):
        self.filled.append((value, timeout))

    async def click(self, timeout=None):
        self.clicked += 1


class FakeLocatorPage:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, _selector):
        return self._locator


class FakeNavigationPage:
    def __init__(self, url="https://gemini.google.com/app/old-chat"):
        self.url = url
        self.visited = []

    async def goto(self, url, **_kwargs):
        self.url = url
        self.visited.append(url)


class WorkerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_model_picker_prefers_semantic_label_then_test_id(self):
        selectors = GeminiWebAutomation.SELECTORS["model_btn"]
        self.assertEqual(selectors[0], 'button[aria-label^="Open mode picker" i]')
        self.assertEqual(selectors[1], 'button[data-test-id="bard-mode-menu-button"]')

    def test_copy_selector_can_be_used_before_a_copy_button_exists(self):
        worker = GeminiWebAutomation(worker_id=1)

        self.assertEqual(
            worker._selector_candidates("copy_btn"),
            ['button[aria-label="Copy"]'],
        )

    def test_submission_signal_accepts_late_ui_evidence_after_click_timeout(self):
        helper = GeminiWebAutomation._submission_start_signal
        self.assertEqual(
            helper(
                {"user_query_count": 1, "response_count": 0, "copy_count": 0},
                0,
                0,
                0,
            ),
            "user_query_increased",
        )
        self.assertEqual(
            helper(
                {"stop_visible": True, "send_visible": False},
                0,
                0,
                0,
            ),
            "stop_visible",
        )

    def test_send_path_never_resubmits_or_uses_ctrl_enter(self):
        source = inspect.getsource(GeminiWebAutomation.send_message)
        self.assertNotIn('keyboard.press("Control+Enter")', source)
        self.assertNotIn("attempt_same_page_resend", source)
        self.assertNotIn("retrying send", source.lower())
        self.assertIn("submit_once", source)
        self.assertIn("use_enter = False", source)

    def test_short_prompt_path_uses_clipboard_paste_not_fill(self):
        source = inspect.getsource(GeminiWebAutomation._prepare_and_enter_prompt)
        direct_path = source.split("# Direct editor path for short prompts or attachment fallback.", 1)[1]
        self.assertIn("_paste_text_into_input", direct_path)
        self.assertNotIn("input_area.fill(entered_prompt)", direct_path)

    async def test_ready_reset_refuses_to_navigate_active_generation(self):
        worker = GeminiWebAutomation(worker_id=1)
        worker._initialized = True
        worker._generation_in_progress = False
        worker._capture_state_snapshot = AsyncMock(return_value={"stop_visible": True})
        worker._ensure_fresh_chat = AsyncMock(return_value=True)

        ready = await worker.prepare_next_request()

        self.assertFalse(ready)
        worker._ensure_fresh_chat.assert_not_awaited()

    def test_dead_response_shell_is_detected_without_waiting_for_full_timeout(self):
        helper = GeminiWebAutomation._is_dead_response_shell
        self.assertTrue(helper({
            "user_query_count": 1,
            "response_count": 1,
            "phase": "idle_or_unknown",
            "stop_visible": False,
            "thinking_active": False,
            "response_body_len": 0,
            "response_copy_count": 0,
        }, 0))
        self.assertFalse(helper({
            "user_query_count": 1,
            "response_count": 1,
            "phase": "response_streaming",
            "stop_visible": True,
            "thinking_active": False,
            "response_body_len": 20,
            "response_copy_count": 0,
        }, 0))

    def test_model_matching_tolerates_version_and_label_changes(self):
        matcher = GeminiWebAutomation(worker_id=1)._matches_model
        self.assertTrue(matcher("Gemini 3.8 Flash", "flash"))
        self.assertTrue(matcher("Flash Lite", "flash-lite"))
        self.assertTrue(matcher("Gemini 4 Pro", "pro"))
        self.assertFalse(matcher("Gemini 3.7 Flash", "flash-lite"))

    def test_long_prompt_search_hint_requires_explicit_flag(self):
        helper = GeminiWebAutomation._needs_search_hint
        self.assertFalse(helper("Search the current reporting"))
        self.assertFalse(helper("Use the web for sources"))
        self.assertFalse(helper("Check Google", use_search=False))
        self.assertTrue(helper("Plain request", use_search=True))
        self.assertFalse(helper("Plain request"))
        self.assertFalse(helper("Improve this website layout"))

    async def test_attachment_upload_requires_visible_filename_not_just_ready_send(self):
        worker = GeminiWebAutomation(worker_id=1)
        send_btn = MagicMock()
        send_btn.first = send_btn
        send_btn.count = AsyncMock(return_value=1)
        send_btn.is_visible = AsyncMock(return_value=True)
        send_btn.get_attribute = AsyncMock(return_value=None)
        send_btn.is_disabled = AsyncMock(return_value=False)

        worker.page = MagicMock()
        worker.page.locator.return_value = send_btn
        worker._resolve_selector = AsyncMock(return_value="send")
        worker._visible_attachment_file_names = AsyncMock(side_effect=[[], ["prompt_1_123.txt"]])

        ready = await worker._wait_for_attachment_upload_complete(
            1.0,
            [r"C:\\Temp\\prompt_1_123.txt"],
        )

        self.assertTrue(ready)
        self.assertEqual(worker._visible_attachment_file_names.await_count, 2)

    async def test_dirty_chat_is_reset_by_navigating_to_app(self):
        worker = GeminiWebAutomation(worker_id=1)
        worker.page = FakeNavigationPage()
        worker._capture_state_snapshot = AsyncMock(side_effect=[
            {"input_visible": True, "user_query_count": 1, "response_count": 1},
            {
                "input_visible": True,
                "user_query_count": 0,
                "response_count": 0,
                "error_page_500": False,
                "sign_in_visible": False,
            },
        ])

        ready = await worker._ensure_fresh_chat()

        self.assertTrue(ready)
        self.assertEqual(worker.page.visited, ["https://gemini.google.com/app"])

    async def test_retained_prompt_is_cleared_only_after_acceptance(self):
        worker = GeminiWebAutomation(worker_id=1)
        locator = FakeLocator()
        worker.page = FakeLocatorPage(locator)
        worker._resolve_selector = AsyncMock(return_value="input")
        worker._human_delay = AsyncMock()
        worker._capture_state_snapshot = AsyncMock(side_effect=[
            {
                "stop_visible": True,
                "input_text_len": 1000,
                "user_query_count": 1,
                "response_count": 1,
            },
            {
                "stop_visible": True,
                "input_text_len": 0,
                "user_query_count": 1,
                "response_count": 1,
            },
        ])

        cleared = await worker._clear_retained_prompt_draft(1000)

        self.assertTrue(cleared)
        self.assertEqual(locator.filled, [("", 3000)])

    async def test_unsent_prompt_is_never_cleared(self):
        worker = GeminiWebAutomation(worker_id=1)
        locator = FakeLocator()
        worker.page = FakeLocatorPage(locator)
        worker._capture_state_snapshot = AsyncMock(return_value={
            "stop_visible": False,
            "input_text_len": 1000,
            "user_query_count": 0,
            "response_count": 0,
        })

        cleared = await worker._clear_retained_prompt_draft(1000)

        self.assertTrue(cleared)
        self.assertEqual(locator.filled, [])

    async def test_live_diagnostics_flag_idle_page_still_showing_stop(self):
        pool = WorkerPool()
        worker = GeminiWebAutomation(worker_id=1)
        worker._initialized = True
        worker._generation_in_progress = False
        worker._capture_state_snapshot = AsyncMock(return_value={
            "page_title": "Google Gemini",
            "url": "https://gemini.google.com/app",
            "phase": "thinking_only",
            "stop_visible": True,
            "send_visible": False,
            "input_text_len": 1000,
            "user_query_count": 1,
            "response_count": 1,
            "thinking_label": "Prioritizing Article Selection",
            "thinking_active": True,
            "error_page_500": False,
        })
        pool.workers = [worker]

        diagnostics = await pool.get_live_diagnostics()

        violations = diagnostics["workers"][0]["invariant_violations"]
        self.assertIn("stop_visible_while_worker_idle", violations)
        self.assertIn("retained_prompt_while_worker_idle", violations)


if __name__ == "__main__":
    unittest.main()
