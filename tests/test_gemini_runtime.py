import unittest

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


if __name__ == "__main__":
    unittest.main()
