import unittest

from gemini_models import (
    model_family_from_ui,
    normalize_model,
    normalize_thinking_level,
    thinking_level_from_picker_text,
)


class ModelTests(unittest.TestCase):
    def test_api_models_are_exact_current_families(self):
        for model in ("flash", "flash-lite", "pro"):
            self.assertEqual(normalize_model(model), model)
        for unsupported in ("thinking", "fast", "gemini-flash", "gemini-3.5-flash-lite"):
            with self.assertRaises(ValueError):
                normalize_model(unsupported)

    def test_ui_family_matching_ignores_visible_numeric_versions(self):
        self.assertEqual(model_family_from_ui("3.5 Flash-Lite Fastest answers"), "flash-lite")
        self.assertEqual(model_family_from_ui("3.8 Flash All-around help"), "flash")
        self.assertEqual(model_family_from_ui("3.1 Pro Advanced reasoning"), "pro")

    def test_unknown_thinking_level_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_thinking_level("auto")

    def test_picker_text_reports_current_thinking_level(self):
        self.assertEqual(thinking_level_from_picker_text("Flash\nMedium"), "Medium")
        self.assertEqual(thinking_level_from_picker_text("Open mode picker, currently Flash High"), "High")
        self.assertEqual(thinking_level_from_picker_text("Flash"), "Low")
        self.assertEqual(thinking_level_from_picker_text("Flash-Lite"), "Low")
        self.assertEqual(thinking_level_from_picker_text("Pro"), "Low")

if __name__ == "__main__":
    unittest.main()
