"""Current API model and thinking-level contract."""

from __future__ import annotations

import re


MODEL_FAMILIES = ("flash", "flash-lite", "pro")
THINKING_LEVELS = ("low", "medium", "high")


def normalize_model(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in MODEL_FAMILIES:
        raise ValueError(f"Unsupported model {value!r}; use flash, flash-lite, or pro")
    return normalized


def model_family_from_ui(value: str) -> str | None:
    """Extract the stable family from Gemini's visible, versioned picker copy."""
    tokens = set(re.findall(r"[a-z0-9]+", (value or "").strip().lower()))
    if "flash" in tokens and "lite" in tokens:
        return "flash-lite"
    if "pro" in tokens:
        return "pro"
    if "flash" in tokens:
        return "flash"
    return None


def normalize_thinking_level(value: str) -> str:
    """Normalize Gemini's current Low/Medium/High thinking levels."""
    normalized = (value or "").strip().lower()
    if normalized in THINKING_LEVELS:
        return normalized.capitalize()
    raise ValueError(f"Unsupported thinking level {value!r}; use Low, Medium, or High")


def thinking_level_from_picker_text(value: str) -> str | None:
    """Read the current thinking level from the closed Gemini model picker."""
    tokens = set(re.findall(r"[a-z]+", (value or "").strip().lower()))
    for level in THINKING_LEVELS:
        if level in tokens:
            return level.capitalize()
    # Gemini omits the word "Low" from the closed picker. For example, after
    # choosing Low the button reads just "Flash" instead of "Flash Low".
    if "flash" in tokens or "pro" in tokens:
        return "Low"
    return None
