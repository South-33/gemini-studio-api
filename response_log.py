"""Private, bounded records of what Gemini returned, including retry attempts."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


RESPONSE_LOG_DIR = Path(__file__).parent / "logs" / "responses"
MAX_FILES = 200
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_CHARS = 1_000_000


def write_response_log(*, request_id, attempt, result, model, thinking_level, prompt_chars):
    """Return the saved filename. The caller handles disk errors without failing requests."""
    now = datetime.now(timezone.utc)
    response = str(result.get("response") or "")
    record = {
        "recorded_at": now.isoformat(),
        "request_id": request_id,
        "attempt": attempt,
        "model": model,
        "thinking_level": thinking_level,
        "prompt_chars": prompt_chars,
        # Transport/automation success is not a guarantee of useful model output.
        "automation_success": bool(result.get("success")),
        "error": str(result.get("error") or "")[:4000] or None,
        "response_chars": len(response),
        "response_truncated": len(response) > MAX_RESPONSE_CHARS,
        "response": response[:MAX_RESPONSE_CHARS],
    }
    RESPONSE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Request IDs come from clients. Keep them in JSON, never in filesystem paths.
    filename = f"response_{now:%Y%m%dT%H%M%S%fZ}_{uuid4().hex}.json"
    path = RESPONSE_LOG_DIR / filename
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    total_bytes = 0
    kept = 0
    for entry in sorted(RESPONSE_LOG_DIR.glob("response_*.json"), reverse=True):
        size = entry.stat().st_size
        if kept >= MAX_FILES or total_bytes + size > MAX_TOTAL_BYTES:
            entry.unlink()
        else:
            kept += 1
            total_bytes += size
    return filename
