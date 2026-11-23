"""Gemini Web model contract.

The web UI and Antigravity intentionally have separate model namespaces.
This module only describes the Gemini Web picker.
"""

from __future__ import annotations

import re


MODEL_FAMILIES = ("hydrogen", "neon")
THINKING_LEVELS = ("low", "medium", "high")
DEFAULT_MODEL = "neon"
DEFAULT_THINKING_LEVEL = "Medium"


def normalize_model(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in MODEL_FAMILIES:
        raise ValueError("Unsupported Gemini model; use 'hydrogen' or 'neon'")
    return normalized


def model_family_from_ui(value: str) -> str | None:
    """Extract the stable Gemini family from visible picker text."""
    tokens = set(re.findall(r"[a-z]+", (value or "").strip().lower()))
    for family in MODEL_FAMILIES:
        if family in tokens:
            return family
    return None


def normalize_thinking_level(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in THINKING_LEVELS:
        return normalized.capitalize()
    raise ValueError("Unsupported thinking level; use Low, Medium, or High")


def thinking_level_from_picker_text(value: str) -> str | None:
    """Read an explicit Low/Medium/High label from the closed picker."""
    tokens = set(re.findall(r"[a-z]+", (value or "").strip().lower()))
    for level in THINKING_LEVELS:
        if level in tokens:
            return level.capitalize()
    return None
