import unittest

from gemini_models import (
    DEFAULT_MODEL,
    DEFAULT_THINKING_LEVEL,
    model_family_from_ui,
    normalize_model,
    normalize_thinking_level,
    thinking_level_from_picker_text,
)


class ModelContractTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULT_MODEL, "neon")
        self.assertEqual(DEFAULT_THINKING_LEVEL, "Medium")

    def test_models_are_exact(self):
        self.assertEqual(normalize_model(" Neon "), "neon")
        self.assertEqual(normalize_model("HYDROGEN"), "hydrogen")
        for value in ("auto", "flash", "pro", "gemini-neon"):
            with self.assertRaises(ValueError):
                normalize_model(value)

    def test_ui_family_ignores_version_and_description(self):
        self.assertEqual(model_family_from_ui("4 Hydrogen Everyday help"), "hydrogen")
        self.assertEqual(model_family_from_ui("4 Neon Deeper analysis"), "neon")
        self.assertEqual(model_family_from_ui("Open mode picker, Neon High"), "neon")
        self.assertIsNone(model_family_from_ui("Auto Adapts to your needs"))

    def test_thinking_levels(self):
        for value in ("low", "Medium", " HIGH "):
            self.assertIn(normalize_thinking_level(value), {"Low", "Medium", "High"})
        with self.assertRaises(ValueError):
            normalize_thinking_level("auto")

    def test_picker_requires_explicit_thinking_label(self):
        self.assertEqual(thinking_level_from_picker_text("Neon High"), "High")
        self.assertEqual(thinking_level_from_picker_text("Hydrogen Medium"), "Medium")
        self.assertIsNone(thinking_level_from_picker_text("Neon"))


if __name__ == "__main__":
    unittest.main()
