"""Version-independent model names for the Gemini Web UI.

Google changes numeric model versions more often than the visible model families.
The automation therefore targets the stable families: Flash, Flash Lite, and Pro.
"""

from __future__ import annotations

import re


MODEL_FAMILIES = ("flash", "flash-lite", "pro")
THINKING_LEVELS = ("extended", "standard", "high", "medium", "low", "minimal")


def model_family(value: str) -> str | None:
    """Return a stable Gemini UI family from an alias or versioned model name."""
    normalized = (value or "").strip().lower()
    tokens = set(re.findall(r"[a-z0-9]+", normalized))

    if normalized in {"fast", "lite", "flash lite", "flash-lite", "gemini-flash-lite"}:
        return "flash-lite"
    if normalized in {"thinking", "flash", "gemini-flash"}:
        return "flash"
    if normalized in {"pro", "gemini-pro"}:
        return "pro"
    if "flash" in tokens and "lite" in tokens:
        return "flash-lite"
    if "pro" in tokens:
        return "pro"
    if "flash" in tokens:
        return "flash"
    return None


def normalize_thinking_level(value: str) -> str:
    """Collapse Gemini's UI wording to the two supported reasoning states."""
    normalized = (value or "").strip().lower()
    if normalized in {"extended", "high", "deep"}:
        return "Extended"
    if normalized in {"standard", "medium", "low", "minimal"}:
        return "Standard"
    raise ValueError(f"Unsupported thinking level {value!r}; use Standard or Extended")


def parse_model_and_thinking(model_name: str) -> tuple[str, str]:
    """Resolve an API model string to a UI family and Standard/Extended mode."""
    raw = (model_name or "").strip()
    normalized = raw.lower()
    thinking_level: str | None = None

    for suffix in THINKING_LEVELS:
        for separator in ("-", ":"):
            marker = f"{separator}{suffix}"
            if normalized.endswith(marker):
                thinking_level = suffix.capitalize()
                normalized = normalized[: -len(marker)].strip()
                break
        if thinking_level:
            break

    family = model_family(normalized)
    if not family:
        supported = ", ".join(MODEL_FAMILIES)
        raise ValueError(f"Unsupported model {raw!r}; use one of: {supported}")

    if normalized == "thinking" and not thinking_level:
        thinking_level = "Extended"
    if thinking_level:
        thinking_level = normalize_thinking_level(thinking_level)

    return family, thinking_level or "Standard"
