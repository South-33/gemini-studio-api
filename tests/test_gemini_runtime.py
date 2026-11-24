import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from gemini_web import GeminiWebAutomation


class GeminiRuntimeTests(unittest.TestCase):
    def test_empty_generation_polling_backs_off_after_normal_response_window(self):
        empty = {
            "phase": "response_streaming",
            "stop_visible": True,
            "response_body_len": 0,
        }
        self.assertEqual(GeminiWebAutomation._response_poll_seconds(empty, 0), 0.5)
        self.assertEqual(GeminiWebAutomation._response_poll_seconds(empty, 10), 1.0)
        self.assertEqual(GeminiWebAutomation._response_poll_seconds(empty, 30), 2.0)

    def test_visible_response_keeps_fast_polling(self):
        response = {
            "phase": "response_streaming",
            "stop_visible": True,
            "response_body_len": 1,
        }
        self.assertEqual(GeminiWebAutomation._response_poll_seconds(response, 120), 0.2)


class ClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_copy_waits_for_fresh_markdown_and_preserves_raw_text(self):
        worker = GeminiWebAutomation()
        events = []
        reads = iter(["", "  fresh markdown\n"])

        async def evaluate(script):
            if "writeText" in script:
                events.append("clear")
                return None
            events.append("read")
            return next(reads)

        async def click():
            events.append("copy")

        worker.page = SimpleNamespace(evaluate=AsyncMock(side_effect=evaluate))
        button = SimpleNamespace(click=AsyncMock(side_effect=click))
        self.assertEqual(await worker._copy_response_markdown(button), "  fresh markdown\n")
        self.assertEqual(events, ["clear", "copy", "read", "read"])

    async def test_copy_with_no_new_content_fails(self):
        worker = GeminiWebAutomation()
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=""))
        button = SimpleNamespace(click=AsyncMock())
        self.assertEqual(await worker._copy_response_markdown(button, timeout_seconds=0.01), "")
        button.click.assert_awaited_once()

    async def test_stalled_copy_cannot_return_old_clipboard_when_clear_fails(self):
        worker = GeminiWebAutomation()
        button = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
        buttons = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=button))

        async def evaluate(script):
            if "writeText" in script:
                raise RuntimeError("clipboard permission denied")
            return "previous request text"

        worker.page = SimpleNamespace(locator=Mock(return_value=buttons), evaluate=AsyncMock(side_effect=evaluate))
        self.assertIsNone(await worker._extract_latest_via_copy("button", 0))
        button.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
