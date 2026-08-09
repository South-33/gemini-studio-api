import unittest
from unittest.mock import AsyncMock

from core import GeminiWebAutomation, WorkerPool


class FakePage:
    def __init__(self, url="https://gemini.google.com/app"):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    async def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


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
    def test_model_picker_prefers_exact_gemini_button(self):
        selectors = GeminiWebAutomation.SELECTORS["model_btn"]
        self.assertEqual(selectors[0], 'button[data-test-id="bard-mode-menu-button"]')
        self.assertEqual(selectors[1], 'button[aria-label^="Open mode picker" i]')

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

    async def test_startup_closes_all_restored_pages(self):
        pages = [FakePage(), FakePage("about:blank")]
        pool = WorkerPool(worker_count=1)
        pool.shared_context = FakeContext(pages)

        closed, failed = await pool._close_restored_context_pages()

        self.assertEqual((closed, failed), (2, 0))
        self.assertTrue(all(page.closed for page in pages[:2]))
        self.assertFalse(pool._startup_guard_page.closed)
        self.assertEqual(pool._startup_pages_closed, 2)

    async def test_hard_refresh_rejects_page_that_is_still_generating(self):
        worker = GeminiWebAutomation(worker_id=1)
        worker.page = object()
        worker.context = object()
        worker._initialized = True
        worker._force_reload = AsyncMock()
        worker._human_delay = AsyncMock()
        worker.init_with_page = AsyncMock(return_value=True)
        worker._capture_state_snapshot = AsyncMock(return_value={
            "stop_visible": True,
            "error_page_500": False,
            "input_visible": True,
            "input_text_len": 100,
            "user_query_count": 1,
            "response_count": 1,
            "page_title": "Google Gemini",
        })

        recovered = await worker._hard_refresh_and_reinit("test")

        self.assertFalse(recovered)
        self.assertFalse(worker._initialized)
        self.assertEqual(worker._last_recovery["stop_visible"], True)

    async def test_hard_refresh_accepts_clean_ready_page(self):
        worker = GeminiWebAutomation(worker_id=1)
        worker.page = object()
        worker.context = object()
        worker._force_reload = AsyncMock()
        worker._human_delay = AsyncMock()
        worker.init_with_page = AsyncMock(return_value=True)
        worker._capture_state_snapshot = AsyncMock(return_value={
            "stop_visible": False,
            "error_page_500": False,
            "input_visible": True,
            "input_text_len": 0,
            "user_query_count": 0,
            "response_count": 0,
            "page_title": "Google Gemini",
        })

        recovered = await worker._hard_refresh_and_reinit("test")

        self.assertTrue(recovered)
        self.assertTrue(worker._last_recovery["ok"])

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

    def test_reset_and_fill_timeouts_poison_worker(self):
        self.assertTrue(WorkerPool._is_stall_class_error("Fresh temp chat reset not confirmed"))
        self.assertTrue(WorkerPool._is_stall_class_error("Locator.fill: Timeout 30000ms exceeded"))

    async def test_live_diagnostics_flag_idle_page_still_showing_stop(self):
        pool = WorkerPool(worker_count=1)
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
        pool._worker_busy = [False]
        pool._worker_busy_since = [None]

        diagnostics = await pool.get_live_diagnostics()

        violations = diagnostics["workers"][0]["invariant_violations"]
        self.assertIn("stop_visible_while_worker_idle", violations)
        self.assertIn("retained_prompt_while_worker_idle", violations)


if __name__ == "__main__":
    unittest.main()
