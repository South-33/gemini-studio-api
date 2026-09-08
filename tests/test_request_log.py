import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import request_log


class RequestLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        patcher = patch.object(request_log, "REQUEST_LOG_DIR", self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, response="ok", **overrides):
        args = dict(
            request_id="../client-id",
            attempt=1,
            prompt="full unicode prompt: สวัสดี កម្ពុជា",
            result={"success": True, "response": response},
            model="flash",
            thinking_level="Standard",
            use_search=True,
            queue_wait_ms=321,
            queued_at="2026-09-08T12:00:00+00:00",
            attempt_started_at="2026-09-08T12:00:01+00:00",
            attempt_finished_at="2026-09-08T12:00:03+00:00",
            attempt_duration_ms=2000,
            browser_log=["[19:00:01.000] [Worker 1] Single send click dispatched"],
            request_context={
                "project": "borderclash",
                "client": "borderclash-convex",
                "ip": "100.64.0.1",
                "raw_model": "flash-standard",
                "message_count": 1,
                "image_count": 0,
                "prompt_tokens_est": 42,
            },
            retryable=False,
            will_retry=False,
            ready_for_next_request=True,
        )
        args.update(overrides)
        name = request_log.write_request_log(**args)
        return self.directory / name

    def test_record_keeps_full_prompt_response_timing_and_browser_log(self):
        response = '```json\n{"result":"✓"}\n```'
        path = self.save(response)
        saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.parent, self.directory)
        self.assertEqual(saved["request_id"], "../client-id")
        self.assertEqual(saved["request"]["prompt"], "full unicode prompt: สวัสดี កម្ពុជា")
        self.assertEqual(saved["outcome"]["response"], response)
        self.assertEqual(saved["timing"]["queue_wait_ms"], 321)
        self.assertEqual(saved["timing"]["attempt_duration_ms"], 2000)
        self.assertEqual(saved["source"]["project"], "borderclash")
        self.assertIn("Single send click dispatched", saved["browser_log"][0])

    def test_failure_and_retry_decision_are_recorded(self):
        path = self.save(
            "markerless",
            result={"success": False, "error": "Gemini response missing response=good marker", "response": "markerless"},
            retryable=True,
            will_retry=True,
            ready_for_next_request=True,
        )
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(saved["outcome"]["automation_success"])
        self.assertTrue(saved["outcome"]["retryable_response_rejection"])
        self.assertTrue(saved["outcome"]["will_retry"])
        self.assertEqual(saved["outcome"]["response"], "markerless")

    def test_retention_keeps_newest_complete_records(self):
        with patch.object(request_log, "MAX_FILES", 2):
            first = self.save("first")
            second = self.save("second")
            third = self.save("third")
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())
        self.assertEqual(json.loads(third.read_text())["outcome"]["response"], "third")

    def test_byte_limit_rotates_whole_old_records(self):
        first = self.save("a" * 1000)
        with patch.object(request_log, "MAX_TOTAL_BYTES", first.stat().st_size + 10):
            latest = self.save("b" * 1000)
        self.assertFalse(first.exists())
        self.assertTrue(latest.exists())
        self.assertEqual(len(json.loads(latest.read_text())["outcome"]["response"]), 1000)


if __name__ == "__main__":
    unittest.main()
