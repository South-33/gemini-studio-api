import unittest

from gemini_models import model_family, parse_model_and_thinking


class ModelTests(unittest.TestCase):
    def test_family_matching_ignores_numeric_versions_and_copy(self):
        self.assertEqual(model_family("3.5 Flash-Lite Fastest answers"), "flash-lite")
        self.assertEqual(model_family("Gemini 4.2 Flash All-around help"), "flash")
        self.assertEqual(model_family("3.1 Pro Advanced reasoning"), "pro")

    def test_aliases_and_thinking_suffixes_are_normalized(self):
        self.assertEqual(parse_model_and_thinking("gemini-3.5-flash-lite"), ("flash-lite", "Standard"))
        self.assertEqual(parse_model_and_thinking("flash-extended"), ("flash", "Extended"))
        self.assertEqual(parse_model_and_thinking("thinking"), ("flash", "Extended"))

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_model_and_thinking("banana")


if __name__ == "__main__":
    unittest.main()
