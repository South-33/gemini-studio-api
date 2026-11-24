import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    async def test_selector_resolution_propagates_request_cancellation(self):
        worker = GeminiWebAutomation()
        locator = SimpleNamespace(count=AsyncMock(side_effect=asyncio.CancelledError))
        worker.page = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(first=locator)))
        with self.assertRaises(asyncio.CancelledError):
            await worker._resolve_selector("input")

    async def test_stalled_copy_propagates_request_cancellation(self):
        worker = GeminiWebAutomation()
        worker.page = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(count=AsyncMock(side_effect=asyncio.CancelledError))))
        with self.assertRaises(asyncio.CancelledError):
            await worker._extract_latest_via_copy("button", 0)

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


class PromptAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_upload_requires_all_files_before_entering_prompt(self):
        worker = GeminiWebAutomation()
        worker._upload_file_attachment = AsyncMock(return_value=False)
        worker._paste_text_into_input = AsyncMock()
        images = ["first.png", "second.webp"]
        with self.assertRaisesRegex(RuntimeError, "attachment upload could not be confirmed"):
            await worker._prepare_and_enter_prompt(None, "compare", images)
        worker._upload_file_attachment.assert_awaited_once_with(images)
        worker._paste_text_into_input.assert_not_awaited()

    async def test_upload_confirmation_requires_exact_attachment_count(self):
        worker = GeminiWebAutomation()
        close_controls = SimpleNamespace(count=AsyncMock(side_effect=[1, 3, 2]))
        send = SimpleNamespace(is_visible=AsyncMock(return_value=True), is_disabled=AsyncMock(return_value=False), get_attribute=AsyncMock(return_value=None))
        worker.page = SimpleNamespace(locator=Mock(side_effect=lambda selector: close_controls if "mat-basic-chip" in selector else SimpleNamespace(first=send)))
        worker._resolve_selector = AsyncMock(return_value="send")
        self.assertTrue(await worker._wait_for_attachment_upload_complete(2, ["a.jpg", "b.gif"]))
        self.assertEqual(close_controls.count.await_count, 3)
        send.is_disabled.assert_awaited_once()

    async def test_upload_refuses_existing_attachments(self):
        worker = GeminiWebAutomation()
        locator = Mock(return_value=SimpleNamespace(count=AsyncMock(return_value=1)))
        worker.page = SimpleNamespace(locator=locator)
        self.assertFalse(await worker._upload_file_attachment(["next.png"]))
        locator.assert_called_once_with('mat-basic-chip button[aria-label^="close " i]')

    async def test_long_prompt_and_images_use_one_upload(self):
        worker = GeminiWebAutomation()
        worker._upload_file_attachment = AsyncMock(return_value=True)
        worker._paste_text_into_input = AsyncMock()
        prompt = "x" * 2000
        with tempfile.TemporaryDirectory() as directory:
            with patch("gemini_web.tempfile.gettempdir", return_value=directory):
                entered, path = await worker._prepare_and_enter_prompt(None, prompt, ["first.png", "second.jpg"], use_search=True)
            worker._upload_file_attachment.assert_awaited_once_with([path, "first.png", "second.jpg"])
            self.assertEqual(Path(path).read_text(), prompt)
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            self.assertEqual(entered, "Use Google Search for this request and read the attached prompt.")
            worker._paste_text_into_input.assert_awaited_once_with(None, entered)

    async def test_cancelled_upload_removes_unclaimed_prompt_file(self):
        worker = GeminiWebAutomation()
        worker._upload_file_attachment = AsyncMock(side_effect=asyncio.CancelledError)
        with tempfile.TemporaryDirectory() as directory:
            with patch("gemini_web.tempfile.gettempdir", return_value=directory):
                with self.assertRaises(asyncio.CancelledError):
                    await worker._prepare_and_enter_prompt(None, "x" * 2000)
            self.assertEqual(list(Path(directory).rglob("*.txt")), [])


if __name__ == "__main__":
    unittest.main()
