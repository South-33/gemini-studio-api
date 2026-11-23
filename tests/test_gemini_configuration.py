import unittest
from unittest.mock import AsyncMock

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


if __name__ == "__main__":
    unittest.main()
