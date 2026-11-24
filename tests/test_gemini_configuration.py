import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gemini_web import GeminiWebAutomation


class _Button:
    async def inner_text(self):
        return "Neon"

    async def get_attribute(self, name):
        return "Open mode picker" if name == "aria-label" else None


class _Locator:
    @property
    def first(self):
        return _Button()


class _Page:
    def locator(self, _selector):
        return _Locator()


class GeminiConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_chat_prefers_native_reset_without_navigation(self):
        worker = GeminiWebAutomation()
        ready = {"input_visible": True, "model_picker_count": 1,
                 "model_button_text": "Neon"}
        control = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
        page = SimpleNamespace(url=worker.CHAT_URL, locator=Mock(return_value=SimpleNamespace(first=control)), goto=AsyncMock())
        worker.page = page
        worker._capture_state_snapshot = AsyncMock(return_value=ready)
        self.assertTrue(await worker._ensure_fresh_chat({"response_count": 1, "input_visible": True}))
        control.click.assert_awaited_once()
        page.goto.assert_not_awaited()
        self.assertEqual(worker._current_selected_model, "neon")

    async def test_native_reset_falls_back_to_navigation_if_not_confirmed(self):
        worker = GeminiWebAutomation()
        control = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
        worker.page = SimpleNamespace(url=worker.CHAT_URL, locator=Mock(return_value=SimpleNamespace(first=control)), goto=AsyncMock())
        worker._capture_state_snapshot = AsyncMock(return_value={"input_visible": True})
        with patch("gemini_web.NEW_CHAT_CONTROL_CONFIRM_SECONDS", 0):
            self.assertTrue(await worker._ensure_fresh_chat({"response_count": 1, "input_visible": True}))
        control.click.assert_awaited_once()
        worker.page.goto.assert_awaited_once()

    def test_fresh_chat_requires_empty_stable_composer(self):
        ready = {"input_visible": True}
        self.assertTrue(GeminiWebAutomation._is_fresh_chat_ready(ready))
        for key in ("input_text_len", "attachment_count", "stop_visible", "transition_state"):
            with self.subTest(key=key):
                self.assertFalse(GeminiWebAutomation._is_fresh_chat_ready({**ready, key: 1}))

    def test_signed_out_composer_is_not_ready(self):
        self.assertFalse(
            GeminiWebAutomation._is_fresh_chat_ready(
                {
                    "user_query_count": 0,
                    "response_count": 0,
                    "input_visible": True,
                    "sign_in_visible": True,
                    "error_page_500": False,
                }
            )
        )

    def test_fresh_snapshot_reconciles_tracked_model_without_extra_ui_query(self):
        worker = GeminiWebAutomation(worker_id=1)
        worker._current_selected_model = "neon"
        worker._current_selected_thinking_level = "High"

        worker._reconcile_tracked_configuration(
            {
                "model_picker_count": 1,
                "model_button_text": "Hydrogen",
                "model_button_aria_label": "Open mode picker",
            }
        )

        self.assertEqual(worker._current_selected_model, "hydrogen")
        self.assertIsNone(worker._current_selected_thinking_level)

    async def test_closed_picker_can_omit_reasoning_label(self):
        worker = GeminiWebAutomation(worker_id=1)
        worker.page = _Page()
        worker._current_selected_model = "neon"
        worker._current_selected_thinking_level = "Medium"
        worker._resolve_selector = AsyncMock(return_value="button")

        model, thinking = await worker._get_current_ui_model_and_thinking()

        self.assertEqual(model, "neon")
        self.assertEqual(thinking, "Medium")

    async def test_tracking_does_not_cross_model_change(self):
        class HydrogenButton(_Button):
            async def inner_text(self):
                return "Hydrogen"

        class HydrogenLocator(_Locator):
            @property
            def first(self):
                return HydrogenButton()

        class HydrogenPage(_Page):
            def locator(self, _selector):
                return HydrogenLocator()

        worker = GeminiWebAutomation(worker_id=1)
        worker.page = HydrogenPage()
        worker._current_selected_model = "neon"
        worker._current_selected_thinking_level = "High"
        worker._resolve_selector = AsyncMock(return_value="button")

        model, thinking = await worker._get_current_ui_model_and_thinking()

        self.assertEqual(model, "hydrogen")
        self.assertIsNone(thinking)

    async def test_thinking_selection_is_tracked_for_newly_confirmed_family(self):
        worker = GeminiWebAutomation()
        worker._current_selected_model = "neon"
        worker._current_selected_thinking_level = "Medium"
        visible_family = "Neon"

        async def inner_text():
            return visible_family

        async def select_model(model):
            nonlocal visible_family
            visible_family = model.capitalize()
            return True

        async def select_thinking(level):
            worker._current_selected_thinking_level = level
            return True

        async def settle(**kwargs):
            return await worker._get_current_ui_model_and_thinking()

        button = SimpleNamespace(inner_text=inner_text, get_attribute=AsyncMock(return_value="Open mode picker"))
        worker.page = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(first=button)))
        worker._resolve_selector = AsyncMock(return_value="button")
        worker._select_model = AsyncMock(side_effect=select_model)
        worker._set_thinking_level = AsyncMock(side_effect=select_thinking)
        worker._wait_for_ui_configuration = AsyncMock(side_effect=settle)

        # The picker omits the level, so the successful click must belong to
        # the confirmed Hydrogen family rather than the previous Neon family.
        self.assertEqual(await worker.ensure_configuration("hydrogen", "Low"), (True, True))
        self.assertEqual(worker._current_selected_model, "hydrogen")
        self.assertEqual(worker._current_selected_thinking_level, "Low")


if __name__ == "__main__":
    unittest.main()
