"""Private, structured request-attempt records for diagnosis and replay."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from uuid import uuid4


REQUEST_LOG_DIR = Path(__file__).parent / "logs" / "requests"
MAX_FILES = 500
MAX_TOTAL_BYTES = 512 * 1024 * 1024


def _rotate_logs() -> None:
    total_bytes = 0
    kept = 0
    for entry in sorted(REQUEST_LOG_DIR.glob("request_*.json"), reverse=True):
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        if kept >= MAX_FILES or total_bytes + size > MAX_TOTAL_BYTES:
            try:
                entry.unlink()
            except OSError:
                pass
        else:
            kept += 1
            total_bytes += size


def write_request_log(
    *,
    request_id: str | None,
    attempt: int,
    prompt: str,
    result: Dict[str, Any],
    model: str | None,
    thinking_level: str | None,
    use_search: bool,
    queue_wait_ms: int,
    queued_at: str,
    attempt_started_at: str,
    attempt_finished_at: str,
    attempt_duration_ms: int,
    browser_log: Iterable[str] | None = None,
    request_context: Dict[str, Any] | None = None,
    retryable: bool = False,
    will_retry: bool = False,
    ready_for_next_request: bool | None = None,
    ready_state: Dict[str, Any] | None = None,
) -> str:
    """Persist one complete browser attempt and return its private filename.

    Request IDs are caller-controlled, so they are stored only inside JSON and
    never interpolated into filenames. Retention rotates whole records instead
    of truncating prompt/response bodies, which keeps every retained record
    replayable.
    """
    now = datetime.now(timezone.utc)
    context = dict(request_context or {})
    response = str(result.get("response") or "")
    raw_response = str(result.get("raw_response") or response)
    error = str(result.get("error") or "") or None

    record = {
        "schema_version": 1,
        "recorded_at": now.isoformat(),
        "request_id": request_id,
        "attempt": attempt,
        "source": {
            "project": context.get("project"),
            "client": context.get("client"),
            "ip": context.get("ip"),
        },
        "request": {
            "raw_model": context.get("raw_model"),
            "resolved_model": model,
            "thinking_level": thinking_level,
            "use_search": bool(use_search),
            "message_count": context.get("message_count"),
            "image_count": context.get("image_count", 0),
            "prompt_chars": len(prompt),
            "prompt_tokens_est": context.get("prompt_tokens_est"),
            "prompt": prompt,
        },
        "timing": {
            "queued_at": queued_at,
            "attempt_started_at": attempt_started_at,
            "attempt_finished_at": attempt_finished_at,
            "queue_wait_ms": int(queue_wait_ms),
            "attempt_duration_ms": int(attempt_duration_ms),
            "queue_plus_attempt_ms": int(queue_wait_ms) + int(attempt_duration_ms),
        },
        "outcome": {
            "automation_success": bool(result.get("success")),
            "error": error,
            "response_chars": len(response),
            "response": response,
            "raw_response_chars": len(raw_response),
            "raw_response": raw_response,
            "response_marker_ok": result.get("response_marker_ok"),
            "retryable_response_rejection": bool(retryable),
            "will_retry": bool(will_retry),
            "ready_for_next_request": ready_for_next_request,
            "ready_state": dict(ready_state or {}),
        },
        "browser_log": list(browser_log or []),
    }

    REQUEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"request_{now:%Y%m%dT%H%M%S%fZ}_{uuid4().hex}.json"
    path = REQUEST_LOG_DIR / filename
    temp_path = path.with_suffix(".json.tmp")
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    temp_path.write_text(payload, encoding="utf-8")
    os.replace(temp_path, path)
    _rotate_logs()
    return filename
