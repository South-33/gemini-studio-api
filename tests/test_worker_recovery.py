import unittest
from unittest.mock import AsyncMock

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


class WorkerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_model_picker_prefers_semantic_label_then_test_id(self):
        selectors = GeminiWebAutomation.SELECTORS["model_btn"]
        self.assertEqual(selectors[0], 'button[aria-label^="Open mode picker" i]')
        self.assertEqual(selectors[1], 'button[data-test-id="bard-mode-menu-button"]')

    def test_model_matching_tolerates_version_and_label_changes(self):
        matcher = GeminiWebAutomation(worker_id=1)._matches_model
        self.assertTrue(matcher("Gemini 3.7 Flash", "gemini-3.6-flash"))
        self.assertTrue(matcher("Flash Lite", "flash-lite"))
        self.assertTrue(matcher("Gemini 4 Pro", "pro"))
        self.assertFalse(matcher("Gemini 3.7 Flash", "flash-lite"))

    def test_long_prompt_search_hint_uses_stable_intent_words(self):
        helper = GeminiWebAutomation._needs_search_hint
        self.assertTrue(helper("Search the current reporting"))
        self.assertTrue(helper("Use the web for sources"))
        self.assertTrue(helper("Check Google", use_search=False))
        self.assertTrue(helper("Plain request", use_search=True))
        self.assertFalse(helper("Plain request"))
        self.assertFalse(helper("Improve this website layout"))

    def test_chat_mode_requires_chat_active_and_spark_inactive(self):
        self.assertTrue(GeminiWebAutomation._is_chat_mode({
            "chat_mode_active": True,
            "spark_mode_active": False,
        }))
        self.assertFalse(GeminiWebAutomation._is_chat_mode({
            "chat_mode_active": False,
            "spark_mode_active": True,
        }))
        self.assertFalse(GeminiWebAutomation._is_chat_mode({
            "chat_mode_active": True,
            "spark_mode_active": True,
        }))

    async def test_chat_mode_clicks_chat_tab_when_spark_is_active(self):
        worker = GeminiWebAutomation(worker_id=1)
        chat_tab = FakeLocator()
        worker._ensure_sidebar_open = AsyncMock(return_value=True)
        worker._resolve_locator = AsyncMock(return_value=chat_tab)
        worker._capture_state_snapshot = AsyncMock(side_effect=[
            {"chat_mode_active": False, "spark_mode_active": True},
            {"chat_mode_active": True, "spark_mode_active": False, "input_visible": True},
        ])

        selected = await worker._ensure_chat_mode(timeout_seconds=0.5)

        self.assertTrue(selected)
        self.assertEqual(chat_tab.clicked, 1)

    async def test_new_chat_control_is_preferred_over_keyboard_shortcut(self):
        worker = GeminiWebAutomation(worker_id=1)
        new_chat = FakeLocator()
        worker._ensure_chat_mode = AsyncMock(return_value=True)
        worker._resolve_locator = AsyncMock(return_value=new_chat)
        worker._trigger_new_chat_shortcut = AsyncMock(return_value=True)
        worker._capture_state_snapshot = AsyncMock(side_effect=[
            {"input_visible": True, "user_query_count": 1, "response_count": 1},
            {"input_visible": True, "user_query_count": 0, "response_count": 0},
        ])

        ready = await worker._ensure_fresh_chat()

        self.assertTrue(ready)
        self.assertEqual(new_chat.clicked, 1)
        worker._trigger_new_chat_shortcut.assert_not_awaited()

    def test_ready_app_without_mode_switcher_is_chat(self):
        self.assertTrue(GeminiWebAutomation._is_implicit_chat_mode({
            "url": "https://gemini.google.com/app",
            "input_visible": True,
            "chat_tab_visible": False,
            "spark_mode_active": False,
            "error_page_500": False,
        }))
        self.assertFalse(GeminiWebAutomation._is_implicit_chat_mode({
            "url": "https://gemini.google.com/spark",
            "input_visible": True,
            "chat_tab_visible": False,
            "spark_mode_active": False,
            "error_page_500": False,
        }))

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
