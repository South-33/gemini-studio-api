import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import response_log


class ResponseLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        patcher = patch.object(response_log, "RESPONSE_LOG_DIR", self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, text, **overrides):
        args = dict(request_id="../client-id", attempt=1,
                    result={"success": True, "response": text}, model="flash",
                    thinking_level="Extended", prompt_chars=117424)
        args.update(overrides)
        name = response_log.write_response_log(**args)
        return self.directory / name

    def test_exact_unicode_response_and_correlation_without_prompt(self):
        text = 'Request failed.\nសួស្តី\n{"result": null}'
        path = self.save(text)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.parent, self.directory)
        self.assertEqual(saved["response"], text)
        self.assertEqual(saved["request_id"], "../client-id")
        self.assertEqual(saved["prompt_chars"], 117424)
        self.assertNotIn("prompt", saved)
        self.assertFalse(saved["response_truncated"])

    def test_failures_without_response_are_recorded(self):
        saved = json.loads(self.save("", result={"success": False, "error": "timeout"}).read_text())
        self.assertFalse(saved["automation_success"])
        self.assertEqual(saved["error"], "timeout")
        self.assertEqual(saved["response"], "")

    def test_retention_keeps_newest_files(self):
        with patch.object(response_log, "MAX_FILES", 2):
            first = self.save("first")
            second = self.save("second")
            third = self.save("third")
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())

    def test_byte_limit_removes_oldest_record(self):
        first = self.save("a" * 1000)
        with patch.object(response_log, "MAX_TOTAL_BYTES", first.stat().st_size + 10):
            latest = self.save("b" * 1000)
        self.assertFalse(first.exists())
        self.assertTrue(latest.exists())

    def test_oversized_responses_are_marked_not_silently_truncated(self):
        with patch.object(response_log, "MAX_RESPONSE_CHARS", 5):
            saved = json.loads(self.save("123456789").read_text())
        self.assertEqual(saved["response"], "12345")
        self.assertEqual(saved["response_chars"], 9)
        self.assertTrue(saved["response_truncated"])
