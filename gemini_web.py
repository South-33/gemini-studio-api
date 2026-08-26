import asyncio
import base64
import math
import os
import sys
import random
import re
import socket
import tempfile
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any, List, Dict, Optional, Tuple
from urllib.parse import urlparse
from playwright.async_api import BrowserContext, Page
from gemini_models import model_family
import contextvars

# ContextVar to hold current request's log lines buffer
current_request_log_buffer = contextvars.ContextVar("current_request_log_buffer", default=None)

# --- Timestamped Logging (use stderr - always unbuffered) ---
def log(msg: str, tag: str = "Core"):
    """Print with timestamp for debugging. Uses stderr for guaranteed immediate output."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    formatted_msg = f"[{ts}] [{tag}] {msg}"
    print(formatted_msg, file=sys.stderr, flush=True)
    
    # Also append to request log buffer if active in current context
    buf = current_request_log_buffer.get()
    if buf is not None:
        buf.append(formatted_msg)

# Production is a visible, text-first browser with long prompts attached as files.
LOW_MEMORY_MODE = True
PROMPT_FILE_UPLOAD_THRESHOLD = 1500
BROWSER_TIMEOUT_SECONDS = 480

# Reliability constants (intentionally hardcoded)
WAIT_LOG_INTERVAL_SECONDS = 10
STALL_EMPTY_SECONDS = 45
STALL_EMPTY_SECONDS_WITH_ACTIVITY = 90
# Extra grace for large prompts (>15k tokens) where backend prefill can take 5+ minutes
STALL_EMPTY_SECONDS_LARGE_PROMPT = 360
LARGE_PROMPT_TOKEN_THRESHOLD = 15000
STALL_NO_PROGRESS_SECONDS = 90
STALL_NO_PROGRESS_SECONDS_SMALL = 180
STALL_NO_PROGRESS_SECONDS_SMALL_WITH_ACTIVITY = 300
STALL_NO_PROGRESS_SECONDS_WITH_ACTIVITY = 180
STALL_SMALL_LEN_THRESHOLD = 200
STALL_THINKING_NO_PROGRESS_SECONDS = 300
STALL_THINKING_NO_PROGRESS_SECONDS_WITH_ACTIVITY = 420
STALL_STATIC_THINKING_SECONDS = 300
STALL_STATIC_THINKING_SECONDS_WITH_ACTIVITY = 420
FINALIZE_STABLE_RESPONSE_SECONDS = 45
FINALIZE_STABLE_RESPONSE_LEN = 800
RECENT_NETWORK_ACTIVITY_SECONDS = 75
MAX_SEND_RETRIES = 2
POOL_RECOVERY_WORKER_TIMEOUT_SECONDS = 75
STALE_BUSY_WITHOUT_ACTIVE_SECONDS = 90
SCROLL_NUDGE_AFTER_NO_PROGRESS_SECONDS = 8
SCROLL_NUDGE_MIN_INTERVAL_SECONDS = 4
UNSENT_STUCK_SECONDS = 20
STALL_RECREATE_THRESHOLD = 1
NETWORK_OUTAGE_PROBE_TIMEOUT_SECONDS = 2.0


class GeminiWebAutomation:
    # Gemini remembers the last side-bar mode (Chat or Spark).  Always start on
    # the Chat surface because the automation selectors and generation flow are
    # implemented for Chat, not Spark.
    CHAT_URL = "https://gemini.google.com/app"
    URL = CHAT_URL
    
    # Shared lock for clipboard operations (clipboard is shared across all tabs)
    # Note: Using class-level lock - all workers share this across tabs
    _clipboard_lock: asyncio.Lock = None  # Lazy init to ensure correct event loop
    NETWORK_URL_HOSTS = (
        "gemini.google.com",
        "bard.google.com",
    )
    NETWORK_URL_PATH_KEYWORDS = (
        "/app",
        "/_/bardchatui/data/",
        "streamgenerate",
        "generatecontent",
        "batchexecute",
    )
    NETWORK_IGNORED_HOSTS = (
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "googleadservices.com",
        "www.google.com",
        "www.google.com.kh",
    )
    NETWORK_IGNORED_PATH_KEYWORDS = (
        "jserror",
        "cspreport",
        "/pagead/",
        "/measurement/",
        "1p-conversion",
    )
    NETWORK_OUTAGE_ERROR_HINTS = (
        "err_name_not_resolved",
        "err_internet_disconnected",
        "err_network_changed",
        "err_address_unreachable",
        "getaddrinfo failed",
    )
    
    @classmethod
    def _get_clipboard_lock(cls) -> asyncio.Lock:
        """Get or create clipboard lock (lazy init for correct event loop)."""
        if cls._clipboard_lock is None:
            cls._clipboard_lock = asyncio.Lock()
        return cls._clipboard_lock
    
    # Stable selectors first; bounded fallbacks second with fuzzy/partial matching
    SELECTORS = {
        "input": [
            'div[role="textbox"][aria-label*="prompt" i]',
            'div[role="textbox"][aria-label*="Enter" i]',
            'div[role="textbox"][contenteditable="true"]',
            'div[role="textbox"]',
            'textarea[placeholder*="Ask" i]',
        ],
        "send_btn": [
            'button[aria-label="Send message"]',
            'button[aria-label*="Send" i]',
            'button[aria-label*="Submit" i]',
            'button[data-test-id*="send" i]',
        ],
        "model_btn": [
            'button[aria-label^="Open mode picker" i]',
            'button[data-test-id="bard-mode-menu-button"]',
            'button[aria-label*="model" i][aria-label*="picker" i]',
        ],
        "new_chat": [
            'a[aria-label="New chat"]',
            'button[aria-label="New chat"]',
            'a[href="/app"]',
            '[data-test-id="new-chat-button"]',
        ],
        "temp_chat": [
            'button[aria-label="Temporary chat"]',
            '[data-test-id="temp-chat-button"]',
        ],
        "sidebar_toggle": [
            'button[aria-label="Open sidebar"]',
            'button[aria-label="Close sidebar"]',
            'button[aria-label*="sidebar" i]',
            'button[aria-label="Main menu"]',
        ],
        # These test ids are present on the Gemini mode switcher and are more
        # reliable than the Ctrl+Shift+S shortcut, which merely toggles from
        # whichever mode happened to be selected last.
        "chat_tab": [
            'button[data-test-id="app-tab-chat"]',
        ],
        "copy_btn": [
            'button[aria-label="Copy"]',
        ],
        "menu_item": [
            'gem-menu-item[role="menuitem"]',
            '[role="menuitem"]',
            'gem-menu-item',
        ],
    }

    _last_errors: Dict[int, Dict[str, Any]] = {}
    
    def __init__(self, worker_id: int = 0):
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._initialized = False
        self._generation_in_progress = False
        self.worker_id = worker_id  # For logging
        self._request_id = None  # Set per-request for log tracing
        self._wait_log_interval_seconds = WAIT_LOG_INTERVAL_SECONDS
        self._stall_empty_seconds = STALL_EMPTY_SECONDS
        self._stall_no_progress_seconds = STALL_NO_PROGRESS_SECONDS
        self._network_logging_attached = False
        self._recent_network_events = deque(maxlen=40)
        self._network_failure_counts: Dict[str, int] = {}
        self._last_network_outage: Optional[Dict[str, Any]] = None
        self._current_prompt_tokens_est: int = 0  # Set per-request for stall scaling
        self._request_log_lines: List[str] = []   # Per-request log buffer for error reports
        self._current_selected_model: Optional[str] = None
        self._current_selected_thinking_level: Optional[str] = None

    @staticmethod
    async def _human_delay(min_ms: int = 50, max_ms: int = 150):
        await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)

    def _reset_model_tracking(self):
        self._current_selected_model = None
        self._current_selected_thinking_level = None


    @classmethod
    def _is_relevant_network_url(cls, url: str) -> bool:
        text = (url or "").strip().lower()
        if not text:
            return False

        parsed = urlparse(text)
        host = (parsed.netloc or "").lower()
        path_query = f"{parsed.path or ''}?{parsed.query or ''}".lower()

        if any(ignore in host for ignore in cls.NETWORK_IGNORED_HOSTS):
            return False

        if any(ignore in path_query for ignore in cls.NETWORK_IGNORED_PATH_KEYWORDS):
            return False

        if not any(host == allowed or host.endswith(f".{allowed}") for allowed in cls.NETWORK_URL_HOSTS):
            return False

        return any(k in path_query for k in cls.NETWORK_URL_PATH_KEYWORDS)

    @classmethod
    def _is_network_outage_error_text(cls, error_text: str) -> bool:
        text = (error_text or "").strip().lower()
        if not text:
            return False
        return any(hint in text for hint in cls.NETWORK_OUTAGE_ERROR_HINTS)

    @classmethod
    def _format_host_for_log(cls, url: str) -> str:
        try:
            return (urlparse(url or "").netloc or "unknown-host").lower()
        except Exception:
            return "unknown-host"

    def _mark_network_outage(self, source: str, url: str, error_text: str):
        host = self._format_host_for_log(url)
        key = f"{source}:{host}:{(error_text or '').strip().lower()}"
        count = self._network_failure_counts.get(key, 0) + 1
        self._network_failure_counts[key] = count
        self._last_network_outage = {
            "source": source,
            "url": url,
            "host": host,
            "error": (error_text or "").strip(),
            "count": count,
            "request_id": self._request_id,
            "timestamp": time.time(),
        }

        if count <= 2 or count in (5, 10):
            log(
                f"[{self._request_id}] Network outage detected via {source}: host={host} error={error_text or 'unknown'} count={count}",
                f"Worker {self.worker_id}",
            )

    async def _probe_host_resolution(self, host: str) -> str:
        host = (host or "").strip().lower()
        if not host or host == "unknown-host":
            return "unknown"

        try:
            await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM),
                timeout=NETWORK_OUTAGE_PROBE_TIMEOUT_SECONDS,
            )
            return "resolved"
        except asyncio.TimeoutError:
            return "timeout"
        except Exception as e:
            return f"failed:{type(e).__name__}"

    async def _build_network_outage_error(self) -> Tuple[str, Dict[str, Any]]:
        issue = self._last_network_outage or {}
        host = (issue.get("host") or "gemini.google.com").strip().lower()
        probe = await self._probe_host_resolution(host)
        error_text = (issue.get("error") or "network resolution failed").strip()
        source = issue.get("source") or "network"
        count = int(issue.get("count") or 0)
        message = f"Network outage: {host} unreachable ({error_text})"
        diagnostics = {
            "network_outage": {
                "host": host,
                "source": source,
                "error": error_text,
                "count": count,
                "dns_probe": probe,
                "url": issue.get("url") or "",
            }
        }
        return message, diagnostics

    def _get_active_network_outage(self) -> Optional[Dict[str, Any]]:
        issue = self._last_network_outage
        if not issue:
            return None
        if issue.get("request_id") not in (None, self._request_id):
            return None
        if (time.time() - float(issue.get("timestamp") or 0)) > 90:
            return None
        return issue

    def _record_network_event(self, kind: str, url: str, status: Optional[int] = None, error: str = ""):
        self._recent_network_events.append(
            {
                "t": time.time(),
                "ts": datetime.now().isoformat(timespec="seconds"),
                "request_id": self._request_id,
                "kind": kind,
                "status": int(status) if status is not None else None,
                "error": (error or "")[:180],
                "url": (url or "")[:240],
            }
        )

    @staticmethod
    def _is_generation_network_url(url: str) -> bool:
        text = (url or "").lower()
        if not text:
            return False
        generation_markers = (
            "streamgenerate",
            "generatecontent",
            "batchexecute",
            "/_/bardchatui/data/",
        )
        return any(marker in text for marker in generation_markers)

    def _get_recent_generation_activity_age(self) -> Optional[float]:
        now = time.time()
        for evt in reversed(self._recent_network_events):
            if evt.get("request_id") != self._request_id:
                continue
            if evt.get("kind") != "response":
                continue
            status = int(evt.get("status") or 0)
            if status < 200 or status >= 400:
                continue
            url = str(evt.get("url") or "")
            if not self._is_generation_network_url(url):
                continue
            event_time = float(evt.get("t") or 0)
            if event_time <= 0:
                continue
            return max(0.0, now - event_time)
        return None

    def _get_recent_any_relevant_activity_age(self) -> Optional[float]:
        """Like _get_recent_generation_activity_age but accepts ANY relevant 200 response
        from gemini.google.com — not just generation-specific URLs. Used as a broader
        liveness signal during the pre-first-token phase where the streaming connection
        headers have already fired but body chunks haven't appeared in the DOM yet.
        Playwright only fires on_response once per request (on headers), so the strict
        generation URL check goes stale after 75s even on healthy long-running requests.
        """
        now = time.time()
        for evt in reversed(self._recent_network_events):
            if evt.get("request_id") != self._request_id:
                continue
            if evt.get("kind") != "response":
                continue
            status = int(evt.get("status") or 0)
            if status < 200 or status >= 400:
                continue
            event_time = float(evt.get("t") or 0)
            if event_time <= 0:
                continue
            return max(0.0, now - event_time)
        return None

    def get_request_log(self) -> List[str]:
        """Return buffered log lines for the current/last request (for error reports)."""
        return list(self._request_log_lines)

    async def _save_diagnostic_artifacts(self, reason: str):
        """Save a screenshot and visible DOM summary before the page is replaced."""
        if not self.page or not self._request_id:
            return

        try:
            import pathlib
            from datetime import datetime
            diag_dir = pathlib.Path(__file__).parent / "logs" / "errors"
            diag_dir.mkdir(parents=True, exist_ok=True)

            snapshot = await self._capture_state_snapshot()
            has_useful_final_state = any([
                bool(snapshot.get("stop_visible")),
                bool(snapshot.get("send_visible")),
                int(snapshot.get("input_text_len") or 0) > 0,
                int(snapshot.get("user_query_count") or 0) > 0,
                int(snapshot.get("response_count") or 0) > 0,
                bool(snapshot.get("error_page_500")),
                bool(snapshot.get("network_outage")),
            ])
            if str(reason) == "failed" and not has_useful_final_state:
                log("Skipped empty final diagnostic capture after recovery/refresh", f"Worker {self.worker_id}")
                return

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            req_id = self._request_id
            w_id = self.worker_id

            safe_reason = "".join(c for c in str(reason) if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
            safe_reason = safe_reason[:30]

            # Screenshot (captured while page is still live)
            png_path = diag_dir / f"{ts}_{req_id}_worker_{w_id}_{safe_reason}.png"
            try:
                # Try full page screenshot first so we see all context
                await self.page.screenshot(path=str(png_path), full_page=True, timeout=8000)
                log(f"Saved diagnostic screenshot: {png_path.name}", f"Worker {w_id}")
            except Exception:
                try:
                    # Fallback to viewport screenshot
                    await self.page.screenshot(path=str(png_path), full_page=False, timeout=4000)
                    log(f"Saved diagnostic screenshot (viewport fallback): {png_path.name}", f"Worker {w_id}")
                except Exception as fallback_err:
                    log(f"Failed to save diagnostic screenshot: {fallback_err}", f"Worker {w_id}")

            # DOM text extract — pull the visible text the user would see:
            # page URL, title, any error/toast messages, and the tail of the
            # last model response.  Much more useful than a black HTML blob.
            txt_path = diag_dir / f"{ts}_{req_id}_worker_{w_id}_{safe_reason}.diag.txt"
            try:
                dom_info = await self.page.evaluate("""
                    () => {
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                            return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        };

                        const url = window.location.href;
                        const title = document.title;

                        // Any visible error / toast messages
                        const errorSels = [
                            '[class*="error"]', '[class*="toast"]', '[class*="snack"]',
                            '[role="alert"]', '[class*="banner"]', '.error-message',
                        ];
                        const errors = [];
                        for (const sel of errorSels) {
                            document.querySelectorAll(sel).forEach(el => {
                                const t = (el.innerText || '').trim();
                                if (t && t.length > 2 && t.length < 500) errors.push(t);
                            });
                        }

                        // Last model response tail (up to 800 chars)
                        const responseSels = [
                            'model-response', '[data-content-type="response"]',
                            'assistant-message-content', '.response-content',
                        ];
                        let responseTail = '';
                        for (const sel of responseSels) {
                            const nodes = document.querySelectorAll(sel);
                            if (nodes.length > 0) {
                                responseTail = (nodes[nodes.length - 1].innerText || '').slice(-800);
                                break;
                            }
                        }

                        // User prompt that was sent (first user-query bubble)
                        let userPromptPreview = '';
                        const uq = document.querySelectorAll('user-query');
                        if (uq.length > 0) {
                            userPromptPreview = (uq[uq.length - 1].innerText || '').slice(0, 500);
                        }

                        // Stop/Send button states
                        const visibleButtons = Array.from(document.querySelectorAll('button')).filter(isVisible);
                        const stopVisible = !!visibleButtons.find((b) => {
                            const label = (b.getAttribute('aria-label') || '').toLowerCase();
                            const text = (b.innerText || '').toLowerCase();
                            return label.includes('stop') || text.includes('stop');
                        });
                        const sendVisible = !!visibleButtons.find((b) => {
                            const label = (b.getAttribute('aria-label') || '').toLowerCase();
                            const text = (b.innerText || '').toLowerCase();
                            return label.includes('send') || text.includes('send');
                        });

                        return { url, title, errors: [...new Set(errors)].slice(0, 10),
                                 responseTail, userPromptPreview, stopVisible, sendVisible };
                    }
                """)
                lines = [
                    f"URL        : {dom_info.get('url', '')}",
                    f"Title      : {dom_info.get('title', '')}",
                    f"Mode       : chat_active={snapshot.get('chat_mode_active')} spark_active={snapshot.get('spark_mode_active')}",
                    f"Stop btn   : {dom_info.get('stopVisible')}  Send btn: {dom_info.get('sendVisible')}",
                    f"Phase      : {snapshot.get('phase')}  Visibility: {snapshot.get('visibility')}",
                    f"Counts     : users={snapshot.get('user_query_count')} responses={snapshot.get('response_count')} copy={snapshot.get('copy_count')} response_copy={snapshot.get('response_copy_count')} input_copy={snapshot.get('input_copy_count')}",
                    f"Input      : visible={snapshot.get('input_visible')} len={snapshot.get('input_text_len')} head={snapshot.get('input_text_head')}",
                    f"Thinking   : visible={snapshot.get('thinking_visible')} active={snapshot.get('thinking_active')} len={snapshot.get('thinking_len')} label={snapshot.get('thinking_label')}",
                    f"Response   : body_len={snapshot.get('response_body_len')} tail={snapshot.get('last_response_tail')}",
                    "",
                    "=== ERROR / TOAST MESSAGES ===",
                ]
                for err in (dom_info.get("errors") or []):
                    lines.append(f"  {err}")
                if not dom_info.get("errors"):
                    lines.append("  (none found)")
                lines += [
                    "",
                    "=== USER PROMPT PREVIEW (last bubble, first 500 chars) ===",
                    dom_info.get("userPromptPreview") or "  (not found)",
                    "",
                    "=== MODEL RESPONSE TAIL (last 800 chars) ===",
                    dom_info.get("responseTail") or "  (empty)",
                ]
                if snapshot.get("network_outage"):
                    lines += [
                        "",
                        "=== NETWORK OUTAGE ===",
                        str(snapshot.get("network_outage")),
                    ]
                net_events = snapshot.get("network_events") or []
                if net_events:
                    lines += [
                        "",
                        "=== NETWORK EVENTS TAIL ===",
                    ]
                    for evt in net_events[-8:]:
                        lines.append(str(evt))
                txt_path.write_text("\n".join(lines), encoding="utf-8")
                log(f"Saved diagnostic text extract: {txt_path.name}", f"Worker {w_id}")
            except Exception as e:
                log(f"Failed to save diagnostic text extract: {e}", f"Worker {w_id}")

        except Exception as e:
            log(f"Error saving diagnostic artifacts: {e}", f"Worker {self.worker_id}")

    # Track whether diagnostics were already saved pre-refresh for this request
    # so the finally-block doesn't double-save a post-refresh greeting page.

    def _log(self, msg: str):
        """Log to stderr (via module log()) AND append to the per-request buffer."""
        tag = f"Worker {self.worker_id}"
        log(msg, tag)

    def _attach_network_logging(self):
        if not self.page or self._network_logging_attached:
            return

        def on_response(response):
            try:
                if not self._generation_in_progress:
                    return
                url = response.url or ""
                if not self._is_relevant_network_url(url):
                    return
                status = int(response.status)
                self._record_network_event("response", url, status=status)
                if status >= 400:
                    log(
                        f"[{self._request_id}] Net response status={status} url={url[:140]}",
                        f"Worker {self.worker_id}",
                    )
            except:
                pass

        def on_request_failed(request):
            try:
                if not self._generation_in_progress:
                    return
                url = request.url or ""
                if not self._is_relevant_network_url(url):
                    return
                failure = request.failure
                error_text = ""
                if failure and isinstance(failure, dict):
                    error_text = failure.get("errorText") or ""
                elif isinstance(failure, str):
                    error_text = failure
                self._record_network_event("request_failed", url, error=error_text)

                if self._is_network_outage_error_text(error_text):
                    self._mark_network_outage("request_failed", url, error_text)
                    return

                key = f"request_failed:{self._format_host_for_log(url)}:{(error_text or 'unknown').strip().lower()}"
                count = self._network_failure_counts.get(key, 0) + 1
                self._network_failure_counts[key] = count
                if count <= 2 or count in (5, 10):
                    log(
                        f"[{self._request_id}] Net request failed: {error_text or 'unknown'} url={url[:140]} count={count}",
                        f"Worker {self.worker_id}",
                    )
            except:
                pass

        self.page.on("response", on_response)
        self.page.on("requestfailed", on_request_failed)
        self._network_logging_attached = True

    def _selector_candidates(self, key: str) -> List[str]:
        raw = self.SELECTORS.get(key, [])
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, str) and item.strip()]
        return []

    async def _resolve_selector(self, key: str, require_visible: bool = False, timeout_ms: int = 900) -> str:
        """Resolve a selector key to the first existing/visible candidate."""
        candidates = self._selector_candidates(key)
        if not candidates:
            return ""

        for selector in candidates:
            locator = self.page.locator(selector).first
            try:
                if require_visible:
                    await locator.wait_for(state="visible", timeout=timeout_ms)
                else:
                    if await locator.count() == 0:
                        continue
                return selector
            except:
                continue

        return ""

    async def _resolve_locator(self, key: str, require_visible: bool = False, timeout_ms: int = 900):
        selector = await self._resolve_selector(key, require_visible=require_visible, timeout_ms=timeout_ms)
        if not selector:
            return None
        return self.page.locator(selector).first

    def _matches_model(self, item_text: str, model_name: str) -> bool:
        """Match stable model families while ignoring Gemini version changes."""
        requested_family = model_family(model_name)
        visible_family = model_family(item_text)
        return bool(requested_family and requested_family == visible_family)

    async def _get_current_ui_model_and_thinking(self) -> Tuple[Optional[str], Optional[str]]:
        """Read the model button text to detect currently active model and thinking level in the UI."""
        try:
            model_selector = await self._resolve_selector("model_btn", require_visible=True, timeout_ms=1500)
            if not model_selector:
                return None, None
            
            btn = self.page.locator(model_selector).first
            text = (await btn.inner_text()).strip().lower()
            label = (await btn.get_attribute("aria-label") or "").strip().lower()
            text = f"{text} {label}".strip()
            if not text:
                return None, None

            # Detect thinking level
            ui_thinking = "Extended" if "extended" in text else "Standard"

            # Detect model
            ui_model = model_family(text)

            return ui_model, ui_thinking
        except Exception as e:
            print(f"[Worker {self.worker_id}] Failed to read current model/thinking level from button: {e}")
            return None, None

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate for logging only."""
        if not text:
            return 0

        chars = len(text)
        words = len(text.split())
        by_chars = max(1, math.ceil(chars / 4))
        by_words = max(1, math.ceil(words * 1.3))
        return max(by_chars, by_words)

    async def _capture_state_snapshot(self) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {
            "request_id": self._request_id,
            "url": "",
            "page_title": "",
            "stop_visible": False,
            "send_visible": False,
            "input_visible": False,
            "input_placeholder": "",
            "input_text_len": 0,
            "input_text_head": "",
            "new_chat_visible": False,
            "sidebar_expanded": False,
            "copy_count": 0,
            "send_btn_disabled": False,
            "send_btn_aria_disabled": None,
            "send_btn_class": "",
            "active_element_tag": "",
            "active_element_aria_label": "",
            "overlay_visible": False,
            "response_count": 0,
            "last_response_len": 0,
            "last_response_signature": "",
            "last_response_tail": "",
            "response_body_len": 0,
            "response_visible": False,
            "response_copy_count": 0,
            "input_copy_count": 0,
            "user_query_count": 0,
            "empty_chat_visible": False,
            "temp_chat_landing_visible": False,
            "temp_chat_button_visible": False,
            "temp_chat_active": False,
            "temp_chat_button_classes": [],
            "transition_state": False,
            "thinking_visible": False,
            "thinking_active": False,
            "thinking_label": "",
            "thinking_len": 0,
            "phase": "idle_or_unknown",
            "error_page_500": False,
            "active_button": "",
            "visibility": "unknown",
            "network_events": [],
            "network_outage": None,
            "chat_tab_visible": False,
            "chat_mode_active": False,
            "spark_mode_active": False,
            "model_button_text": "",
            "model_button_aria_label": "",
            "model_picker_count": 0,
            "sign_in_visible": False,
            "ui_state_hint": "unknown",
        }

        if not self.page:
            return snapshot

        try:
            snapshot["url"] = self.page.url
        except:
            pass

        try:
            data = await self.page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    };

                    const pageTitle = (document.title || '').trim();
                    const bodyText = (document.body?.innerText || '').toLowerCase();
                    const isGoogle500 =
                        pageTitle.toLowerCase().includes('500') ||
                        bodyText.includes("500. that's an error") ||
                        bodyText.includes("there was an error. please try again later. that's all we know.");
                    const tempBtn = document.querySelector('[data-test-id="temp-chat-button"], button[aria-label="Temporary chat"]');
                    const chatTab = document.querySelector('button[data-test-id="app-tab-chat"]');
                    const sparkTab = document.querySelector('button[data-test-id="app-tab-agent"]');
                    const modelPickers = Array.from(document.querySelectorAll(
                        'button[data-test-id="bard-mode-menu-button"], button[aria-label^="Open mode picker" i]'
                    )).filter(isVisible);
                    const modelPicker = modelPickers[0] || null;
                    const signInVisible = Array.from(document.querySelectorAll('a, button')).some((el) => {
                        if (!isVisible(el)) return false;
                        const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                        const label = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                        return text === 'sign in' || label === 'sign in';
                    });
                    const isActiveModeTab = (el) => !!(el && (
                        el.classList.contains('app-tab--active') ||
                        el.getAttribute('aria-selected') === 'true' ||
                        el.getAttribute('aria-current') === 'page'
                    ));
                    const transitionSpinner = document.querySelector('.loading-content-spinner-container');

                    const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible);
                    const newChatLink = Array.from(document.querySelectorAll('a[aria-label="New chat"], button[aria-label="New chat"], [data-test-id="new-chat-button"] a, [data-test-id="new-chat-button"], a[href="/app"]')).find(isVisible) || null;
                    const sidebarEl = document.querySelector('bard-sidenav');
                    const closeSidebarBtn = document.querySelector('button[aria-label="Close sidebar"]');
                    const sidebarExpanded = !!(sidebarEl && !sidebarEl.classList.contains('collapsed')) || !!closeSidebarBtn || Array.from(document.querySelectorAll('a, button, div, span')).some((el) => {
                        if (!isVisible(el)) return false;
                        const text = (el.innerText || '').trim();
                        return text === 'Scheduled actions' || text === 'Gems' || text === 'My stuff' ||
                            text === 'Search chats' || text === 'Library' || text === 'New notebook' ||
                            text === 'All notebooks' || text === 'Recents';
                    });
                    const stopBtn = buttons.find((b) => {
                        const label = (b.getAttribute('aria-label') || '').toLowerCase();
                        const text = (b.innerText || '').toLowerCase();
                        return label.includes('stop') || text.includes('stop');
                    });
                    const sendBtn = buttons.find((b) => {
                        const label = (b.getAttribute('aria-label') || '').toLowerCase();
                        const text = (b.innerText || '').toLowerCase();
                        return label.includes('send') || text.includes('send');
                    });
                    const inputBox = Array.from(document.querySelectorAll(
                        'div[role="textbox"][aria-label="Enter a prompt for Gemini"], ' +
                        'div[role="textbox"][aria-label="Enter a prompt here"], ' +
                        'div[role="textbox"][contenteditable="true"], ' +
                        '.ql-editor[role="textbox"], .ql-editor'
                    )).find(isVisible) || null;
                    const inputPlaceholder = inputBox ? ((inputBox.getAttribute('data-placeholder') || '').trim()) : '';
                    const inputText = inputBox ? ((inputBox.innerText || inputBox.textContent || '').trim()) : '';
                    const tempChatLandingVisible =
                        inputPlaceholder.toLowerCase().includes('temporary') ||
                        bodyText.includes("temporary chats don't appear in recent chats") ||
                        bodyText.includes('temporary chats are saved for 72 hours');

                    let responses = document.querySelectorAll('[data-content-type="response"]');
                    if (!responses.length) responses = document.querySelectorAll('model-response, assistant-message-content');
                    const userQueries = document.querySelectorAll('user-query');
                    const emptyChat = document.querySelector('modular-zero-state, zero-state, bard-zero-state');
                    const overlay = document.querySelector('.cdk-overlay-backdrop');
                    const activeElement = document.activeElement;
                    const last = responses.length ? responses[responses.length - 1] : null;
                    const lastText = last ? (last.innerText || '') : '';
                    const responseCopyButtons = last
                        ? Array.from(last.querySelectorAll('button[aria-label="Copy"]')).filter(isVisible)
                        : [];
                    const inputCopyButtons = Array.from(document.querySelectorAll('button[aria-label="Copy prompt"]')).filter(isVisible);
                    const legacyThinkingBtn = last
                        ? Array.from(last.querySelectorAll('button.thoughts-header-button')).find(isVisible) || null
                        : Array.from(document.querySelectorAll('button.thoughts-header-button')).find(isVisible) || null;
                    const legacyThinkingLabel = (legacyThinkingBtn?.innerText || legacyThinkingBtn?.textContent || '').trim().slice(0, 120);
                    const legacyThinkingDoneLabels = new Set(['Show thinking', 'Hide thinking']);
                    const legacyThinkingActive = !!legacyThinkingBtn && !legacyThinkingDoneLabels.has(legacyThinkingLabel);

                    let legacyThoughtContainer = null;
                    if (legacyThinkingBtn) {
                        legacyThoughtContainer =
                            (last && (last.querySelector('.thought-container') || last.querySelector('[class*="thought-container"]'))) ||
                            legacyThinkingBtn.closest('[class*="thought-container"]');
                    }
                    const legacyThinkingText = legacyThoughtContainer ? (legacyThoughtContainer.innerText || legacyThoughtContainer.textContent || '') : '';

                    // Check for new 2026 thinking overlay
                    const thinkingOverlay = (last && last.querySelector('thinking-overlay')) || document.querySelector('thinking-overlay');
                    const newThinkingMounted = !!(thinkingOverlay && isVisible(thinkingOverlay));
                    const newThinkingActive = newThinkingMounted && !!(
                        thinkingOverlay.querySelector('thinking-dots-animation, .thinking-dots-animation, .thinking-container')
                    );
                    const newThinkingLabel = thinkingOverlay ? (thinkingOverlay.innerText || '').trim().slice(0, 120) : '';
                    // Gemini keeps an empty thinking-overlay mounted after a response.
                    // Treat only its animation or non-empty summary as liveness.
                    const newThinkingVisible = newThinkingActive || newThinkingLabel.length > 0;

                    // Resolve final values
                    const thinkingVisible = legacyThinkingActive || newThinkingVisible;
                    const thinkingActive = legacyThinkingActive || newThinkingActive;
                    const thinkingLabel = legacyThinkingActive ? legacyThinkingLabel : newThinkingLabel;
                    const thinkingText = legacyThinkingActive ? legacyThinkingText : newThinkingLabel;
                    const thinkingLen = thinkingText.trim().length;

                    // Extract actual response body text (ignoring thinking status overlay)
                    const msgContentEl = last ? last.querySelector('message-content') : null;
                    const responseBodyText = msgContentEl ? (msgContentEl.innerText || msgContentEl.textContent || '') : '';
                    
                    let responseBodyLen = 0;
                    if (msgContentEl) {
                        responseBodyLen = responseBodyText.trim().length;
                    } else {
                        responseBodyLen = Math.max(0, lastText.trim().length - thinkingLen);
                    }
                    const responseVisible = responseBodyLen > 0;

                    let uiStateHint = 'unknown';
                    if (isGoogle500) uiStateHint = 'google_500';
                    else if (inputBox) uiStateHint = 'composer_ready';
                    else if (signInVisible) uiStateHint = 'sign_in_required';
                    else if (transitionSpinner && isVisible(transitionSpinner)) uiStateHint = 'loading';
                    else if (modelPicker) uiStateHint = 'partial_ui_no_composer';

                    let phase = 'idle_or_unknown';
                    if (responseCopyButtons.length > 0) {
                        phase = 'response_copyable_postprocessing';
                    } else if (stopBtn) {
                        phase = thinkingVisible && responseBodyLen < 120 ? 'thinking_only' : 'response_streaming';
                    } else if (responseVisible) {
                        phase = 'response_complete_postprocessing';
                    }

                    return {
                        page_title: pageTitle.slice(0, 160),
                        stop_visible: !!stopBtn,
                        send_visible: !!sendBtn,
                        input_visible: !!inputBox,
                        input_placeholder: inputPlaceholder.slice(0, 160),
                        input_text_len: inputText.length,
                        input_text_head: inputText.slice(0, 160),
                        new_chat_visible: !!newChatLink,
                        sidebar_expanded: sidebarExpanded,
                        active_button: (stopBtn?.getAttribute('aria-label') || stopBtn?.innerText || sendBtn?.getAttribute('aria-label') || sendBtn?.innerText || '').trim().slice(0, 80),
                        send_btn_disabled: !!(sendBtn && (sendBtn.disabled || sendBtn.getAttribute('disabled') !== null)),
                        send_btn_aria_disabled: sendBtn?.getAttribute('aria-disabled') || null,
                        send_btn_class: (sendBtn?.className || '').toString().slice(0, 240),
                        active_element_tag: activeElement?.tagName || '',
                        active_element_aria_label: activeElement?.getAttribute?.('aria-label') || '',
                        overlay_visible: !!(overlay && isVisible(overlay)),
                        copy_count: document.querySelectorAll('button[aria-label="Copy"]').length,
                        response_copy_count: responseCopyButtons.length,
                        input_copy_count: inputCopyButtons.length,
                        user_query_count: userQueries.length,
                        empty_chat_visible: !!emptyChat,
                        temp_chat_landing_visible: tempChatLandingVisible,
                        temp_chat_button_visible: !!(tempBtn && isVisible(tempBtn)),
                        temp_chat_active: !!(tempBtn && (
                            tempBtn.classList.contains('temp-chat-on') ||
                            tempBtn.querySelector('mat-icon[data-mat-icon-name="close"], mat-icon[fonticon="close"]')
                        )),
                        temp_chat_button_classes: tempBtn ? Array.from(tempBtn.classList).slice(0, 20) : [],
                        transition_state: !!(transitionSpinner && isVisible(transitionSpinner)),
                        response_count: responses.length,
                        last_response_len: lastText.length,
                        last_response_signature: `${lastText.slice(0, 80)}|${lastText.slice(-80)}`.slice(0, 200),
                        last_response_tail: lastText.slice(-120),
                        response_body_len: responseBodyLen,
                        response_visible: responseVisible,
                        thinking_visible: thinkingVisible,
                        thinking_active: thinkingActive,
                        thinking_label: thinkingLabel,
                        thinking_len: thinkingLen,
                        phase,
                        error_page_500: isGoogle500,
                        visibility: document.visibilityState || 'unknown',
                        chat_tab_visible: !!(chatTab && isVisible(chatTab)),
                        chat_mode_active: isActiveModeTab(chatTab),
                        spark_mode_active: isActiveModeTab(sparkTab),
                        model_button_text: (modelPicker?.innerText || modelPicker?.textContent || '').trim().slice(0, 160),
                        model_button_aria_label: (modelPicker?.getAttribute('aria-label') || '').trim().slice(0, 160),
                        model_picker_count: modelPickers.length,
                        sign_in_visible: signInVisible,
                        ui_state_hint: uiStateHint,
                    };
                }
                """
            )
            if isinstance(data, dict):
                snapshot.update(data)
        except Exception as e:
            log(f"Snapshot DOM eval failed: {e}", f"Worker {self.worker_id}")

        try:
            snapshot["network_events"] = list(self._recent_network_events)[-8:]
        except:
            pass

        try:
            issue = self._get_active_network_outage()
            if issue:
                snapshot["network_outage"] = {
                    "host": issue.get("host"),
                    "source": issue.get("source"),
                    "error": issue.get("error"),
                    "count": issue.get("count"),
                }
        except:
            pass

        return snapshot

    async def _click_stop_if_visible(self) -> bool:
        """Best-effort stop click using in-page DOM lookup."""
        try:
            clicked = await self.page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    };

                    const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible);
                    const stopBtn = buttons.find((b) => {
                        const label = (b.getAttribute('aria-label') || '').toLowerCase();
                        const text = (b.innerText || '').toLowerCase();
                        return label.includes('stop') || text.includes('stop');
                    });

                    if (!stopBtn) return false;

                    try {
                        stopBtn.click();
                        return true;
                    } catch (_) {}

                    try {
                        stopBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        return true;
                    } catch (_) {}

                    return false;
                }
                """
            )
            return bool(clicked)
        except:
            return False

    async def _extract_latest_via_copy(self, copy_selector: str, pre_send_count: int) -> Optional[str]:
        """Try extracting response markdown via latest copy button."""
        try:
            buttons = self.page.locator(copy_selector)
            current_count = await buttons.count()
            if current_count <= pre_send_count:
                return None

            copy_btn = buttons.nth(current_count - 1)
            if not await copy_btn.is_visible():
                return None

            async with GeminiWebAutomation._get_clipboard_lock():
                await copy_btn.click()
                await self._human_delay(100, 200)
                markdown = await self.page.evaluate("navigator.clipboard.readText()")

            if markdown and markdown.strip():
                return markdown.strip()
            return None
        except:
            return None

    async def _attempt_finalize_stalled_response(
        self,
        copy_selector: str,
        pre_send_count: int,
        click_stop: bool = True,
    ) -> Optional[str]:
        """Try to finalize an in-flight stalled generation before giving up."""
        try:
            if click_stop:
                stop_clicked = await self._click_stop_if_visible()
                if stop_clicked:
                    log("Attempted stop click on stalled generation", f"Worker {self.worker_id}")
                await self._human_delay(1200, 1800)
            else:
                await self._human_delay(300, 600)

            markdown = await self._extract_latest_via_copy(copy_selector, pre_send_count)
            if markdown:
                return markdown
            return None
        except:
            return None

    async def _recover_from_google_500_page(self, reason: str) -> bool:
        """Refresh away from transient Google 500 pages before giving up on the worker."""
        try:
            log(f"Detected Google 500 page ({reason}), refreshing", f"Worker {self.worker_id}")
            await self._force_reload(timeout_ms=30000)
            await self._human_delay(1000, 1500)
            return True
        except Exception as e:
            log(f"Google 500 recovery failed: {e}", f"Worker {self.worker_id}")
            return False

    async def _force_reload(self, timeout_ms: int = 30000) -> None:
        """Perform a true hard reload bypassing browser cache (CDP ignoreCache / Control+Shift+R)."""
        try:
            log("Attempting CDP hard refresh (ignoreCache=True)...", f"Worker {self.worker_id}")
            cdp = await self.page.context.new_cdp_session(self.page)
            await cdp.send("Page.reload", {"ignoreCache": True})
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return
        except Exception as cdp_err:
            log(f"CDP hard refresh failed: {cdp_err}, trying Control+Shift+R keyboard shortcut...", f"Worker {self.worker_id}")

        try:
            await self.page.bring_to_front()
            await self.page.keyboard.press("Control+Shift+R")
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return
        except Exception as kbd_err:
            log(f"Keyboard hard refresh failed: {kbd_err}, falling back to standard page.reload...", f"Worker {self.worker_id}")

        await self.page.reload(wait_until="domcontentloaded", timeout=timeout_ms)

    async def _clear_retained_prompt_draft(self, prompt_len: int) -> bool:
        """Clear a sent prompt that Gemini leaves in the composer.

        Only a proven request-start signal permits this cleanup, so an unsent
        request can never be silently erased.
        """
        if prompt_len <= 0:
            return True

        try:
            before = await self._capture_state_snapshot()
            input_len = int(before.get("input_text_len") or 0)
            request_accepted = bool(
                before.get("stop_visible")
                or int(before.get("user_query_count") or 0) > 0
                or int(before.get("response_count") or 0) > 0
            )
            retained = input_len >= max(1, prompt_len // 2)
            if not request_accepted or not retained:
                return True

            input_selector = await self._resolve_selector("input", require_visible=True, timeout_ms=1500)
            if not input_selector:
                log(
                    f"Retained composer draft could not be cleared: input selector missing (input_len={input_len})",
                    f"Worker {self.worker_id}",
                )
                return False

            input_area = self.page.locator(input_selector).first
            await input_area.fill("", timeout=3000)
            await self._human_delay(100, 200)
            after = await self._capture_state_snapshot()
            after_len = int(after.get("input_text_len") or 0)
            cleared = after_len < max(1, prompt_len // 2)
            log(
                f"Retained composer draft clear result: ok={cleared} before={input_len} after={after_len} "
                f"stop={after.get('stop_visible')} users={after.get('user_query_count')}",
                f"Worker {self.worker_id}",
            )
            return cleared
        except Exception as e:
            log(f"Retained composer draft clear failed: {e}", f"Worker {self.worker_id}")
            return False

    async def _nudge_scroll_to_bottom(self):
        """Best-effort scroll nudge to keep streaming region active/visible."""
        try:
            await self.page.evaluate(
                """
                () => {
                    const selectors = [
                        'main',
                        '[role="main"]',
                        'mat-sidenav-content',
                        '.conversation-container',
                        '.chat-history',
                        'body',
                        'html',
                    ];

                    for (const selector of selectors) {
                        const nodes = document.querySelectorAll(selector);
                        nodes.forEach((el) => {
                            try { el.scrollTop = el.scrollHeight; } catch (_) {}
                        });
                    }

                    try { window.scrollTo(0, document.body.scrollHeight); } catch (_) {}

                    const responses = document.querySelectorAll('[data-content-type="response"], model-response, assistant-message-content');
                    if (responses.length > 0) {
                        const last = responses[responses.length - 1];
                        try { last.scrollIntoView({ block: 'end', inline: 'nearest' }); } catch (_) {}
                    }
                }
                """
            )
        except:
            pass

    @staticmethod
    def _classify_new_chat_state(snapshot: Optional[Dict[str, Any]]) -> str:
        snap = snapshot or {}
        response_count = int(snap.get("response_count") or 0)
        user_query_count = int(snap.get("user_query_count") or 0)
        empty_chat_visible = bool(snap.get("empty_chat_visible"))
        temp_chat_landing_visible = bool(snap.get("temp_chat_landing_visible"))
        stop_visible = bool(snap.get("stop_visible"))
        input_visible = bool(snap.get("input_visible"))
        error_page_500 = bool(snap.get("error_page_500"))

        if (
            response_count == 0
            and user_query_count == 0
            and not stop_visible
            and not error_page_500
            and (input_visible or empty_chat_visible or temp_chat_landing_visible)
        ):
            return "confirmed_cleared"
        if response_count > 0 or user_query_count > 0 or stop_visible:
            return "definitely_not_cleared"
        if (empty_chat_visible or temp_chat_landing_visible) and not stop_visible and not error_page_500:
            return "confirmed_cleared"
        return "transitional_or_uncertain"

    @staticmethod
    def _is_fresh_temp_chat_ready(snapshot: Optional[Dict[str, Any]]) -> bool:
        snap = snapshot or {}
        placeholder = str(snap.get("input_placeholder") or "").lower()
        return bool(
            int(snap.get("user_query_count") or 0) == 0
            and int(snap.get("response_count") or 0) == 0
            and snap.get("input_visible")
            and (snap.get("temp_chat_active") or "temporary" in placeholder)
            and not snap.get("error_page_500")
        )

    @staticmethod
    def _is_fresh_regular_chat_ready(snapshot: Optional[Dict[str, Any]]) -> bool:
        snap = snapshot or {}
        placeholder = str(snap.get("input_placeholder") or "").lower()
        return bool(
            int(snap.get("user_query_count") or 0) == 0
            and int(snap.get("response_count") or 0) == 0
            and snap.get("input_visible")
            and snap.get("new_chat_visible")
            and not snap.get("error_page_500")
            and not snap.get("temp_chat_active")
            and "temporary" not in placeholder
        )

    @staticmethod
    def _is_chat_mode(snapshot: Optional[Dict[str, Any]]) -> bool:
        """Return true only when Gemini's Chat tab is the active mode."""
        snap = snapshot or {}
        return bool(snap.get("chat_mode_active") and not snap.get("spark_mode_active"))

    @staticmethod
    def _is_implicit_chat_mode(snapshot: Optional[Dict[str, Any]]) -> bool:
        """Accept a ready /app composer when Google omits the mode switcher.

        Some accounts/layouts do not render Chat/Spark tabs. A visible prompt
        composer on /app is still an authoritative Chat signal; /spark is not.
        """
        snap = snapshot or {}
        try:
            path = urlparse(str(snap.get("url") or "")).path.rstrip("/")
        except Exception:
            path = ""
        return bool(
            path == "/app"
            and snap.get("input_visible")
            and not snap.get("chat_tab_visible")
            and not snap.get("spark_mode_active")
            and not snap.get("error_page_500")
        )

    async def _ensure_chat_mode(self, timeout_seconds: float = 8.0) -> bool:
        """Select Gemini Chat explicitly before using Chat-only selectors.

        Gemini persists the Chat/Spark choice and Ctrl+Shift+S is a toggle, so
        sending the shortcut is not idempotent.  The mode switcher exposes
        stable test ids; click the Chat button only when it is not already
        active, then verify the active class and Chat input before continuing.
        """
        snapshot = await self._capture_state_snapshot()
        if self._is_chat_mode(snapshot):
            return True
        if self._is_implicit_chat_mode(snapshot):
            log("Gemini Chat inferred from ready /app composer (mode switcher absent)", f"Worker {self.worker_id}")
            return True

        # The mode switcher lives in the side navigation in the current UI.
        # Opening it is harmless when it is already visible.
        await self._ensure_sidebar_open()
        chat_tab = await self._resolve_locator("chat_tab", require_visible=True, timeout_ms=2000)

        async def toggle_chat_with_shortcut() -> bool:
            # Ctrl+Shift+S is a toggle, not an idempotent selector. Only use it
            # as a last-resort fallback when the snapshot already proves Spark
            # is active, and always verify the resulting mode below.
            if not snapshot.get("spark_mode_active"):
                return False
            try:
                await self.page.bring_to_front()
                await self.page.keyboard.press("Control+Shift+S")
                await self._human_delay(250, 400)
                return True
            except Exception as shortcut_error:
                log(f"Chat mode shortcut failed: {shortcut_error}", f"Worker {self.worker_id}")
                return False

        if chat_tab is None:
            if not await toggle_chat_with_shortcut():
                log("Chat mode tab was not found", f"Worker {self.worker_id}")
                self._track_error("Chat mode tab not found", "chat_tab", "ensure_chat_mode", snapshot)
                return False

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                current = await self._capture_state_snapshot()
                if self._is_chat_mode(current) and current.get("input_visible"):
                    log("Gemini Chat mode confirmed via shortcut", f"Worker {self.worker_id}")
                    return True
                await asyncio.sleep(0.2)

            final_snapshot = await self._capture_state_snapshot()
            self._track_error("Chat mode shortcut confirmation timed out", "chat_tab", "ensure_chat_mode", final_snapshot)
            return False

        clicked = False
        try:
            await chat_tab.click(timeout=2500)
            clicked = True
        except Exception as click_error:
            # Keep a DOM click fallback for transient overlay/focus issues.
            try:
                await self.page.evaluate(
                    """
                    () => {
                        const button = document.querySelector('button[data-test-id="app-tab-chat"]');
                        if (!button) throw new Error('Chat mode tab not found');
                        button.click();
                    }
                    """
                )
                clicked = True
            except Exception as dom_error:
                log(
                    f"Chat mode click failed: {click_error}; DOM fallback failed: {dom_error}",
                    f"Worker {self.worker_id}",
                )

        if not clicked and not await toggle_chat_with_shortcut():
            self._track_error("Chat mode click failed", "chat_tab", "ensure_chat_mode", snapshot)
            return False

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            current = await self._capture_state_snapshot()
            if self._is_chat_mode(current) and current.get("input_visible"):
                log("Gemini Chat mode confirmed", f"Worker {self.worker_id}")
                return True
            await asyncio.sleep(0.2)

        final_snapshot = await self._capture_state_snapshot()
        log(
            f"Chat mode confirmation timed out: chat_active={final_snapshot.get('chat_mode_active')} "
            f"spark_active={final_snapshot.get('spark_mode_active')} input={final_snapshot.get('input_visible')} "
            f"url={final_snapshot.get('url')}",
            f"Worker {self.worker_id}",
        )
        self._track_error("Chat mode confirmation timed out", "chat_tab", "ensure_chat_mode", final_snapshot)
        return False

    async def _ensure_sidebar_open(self) -> bool:
        snapshot = await self._capture_state_snapshot()
        if snapshot.get("sidebar_expanded"):
            return True

        sidebar_btn = await self._resolve_locator("sidebar_toggle")
        if sidebar_btn is None:
            return False

        try:
            if await sidebar_btn.is_visible():
                await sidebar_btn.click(timeout=1500)
                await self._human_delay(300, 500)
        except Exception:
            return False

        deadline = time.time() + 3.0
        while time.time() < deadline:
            snap = await self._capture_state_snapshot()
            if snap.get("sidebar_expanded"):
                return True
            await asyncio.sleep(0.2)
        return False

    async def _wait_for_fresh_regular_chat(self, timeout_seconds: float) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            snap = await self._capture_state_snapshot()
            if self._is_fresh_regular_chat_ready(snap):
                return True
            await asyncio.sleep(0.2)
        return False

    async def _trigger_new_chat_shortcut(self) -> bool:
        """Use Gemini's Chat-only new-conversation shortcut after mode guard."""
        try:
            await self.page.bring_to_front()
            await self.page.keyboard.press("Control+Shift+O")
            await self._human_delay(250, 400)
            return True
        except Exception as e:
            log(f"New Chat shortcut failed: {e}", f"Worker {self.worker_id}")
            return False

    async def _ensure_fresh_chat(self) -> bool:
        """Ensure the worker is on a fresh Chat conversation before sending."""
        if not await self._ensure_chat_mode():
            return False

        baseline = await self._capture_state_snapshot()
        if self._classify_new_chat_state(baseline) == "confirmed_cleared":
            return True

        async def wait_until_clear(timeout_seconds: float = 4.0) -> bool:
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                snap = await self._capture_state_snapshot()
                if self._classify_new_chat_state(snap) == "confirmed_cleared":
                    return True
                await asyncio.sleep(0.2)
            return False

        # Prefer the labeled control. It is idempotent and was verified in the
        # live UI; the keyboard shortcut remains a fallback for compact layouts.
        new_chat = await self._resolve_locator("new_chat", require_visible=True, timeout_ms=1500)
        if new_chat is not None:
            try:
                await new_chat.click(timeout=2000)
                if await wait_until_clear():
                    return True
            except Exception as exc:
                log(f"New chat control failed: {exc}", f"Worker {self.worker_id}")

        if await self._trigger_new_chat_shortcut():
            if await self._ensure_chat_mode() and await wait_until_clear():
                return True

        final_snapshot = await self._capture_state_snapshot()
        final_state = self._classify_new_chat_state(final_snapshot)
        log(f"New chat reset not confirmed ({final_state})", f"Worker {self.worker_id}")
        self._track_error("New chat reset not confirmed", "new_chat", "ensure_fresh_chat", final_snapshot)
        return False

    async def _get_temp_chat_button(self):
        temp_btn = await self._resolve_locator("temp_chat")
        if temp_btn is None:
            return None

        try:
            if not await temp_btn.is_visible():
                sidebar_btn = await self._resolve_locator("sidebar_toggle")
                if sidebar_btn is not None and await sidebar_btn.is_visible():
                    await sidebar_btn.click()
                    await self._human_delay(300, 500)
        except:
            pass

        try:
            if await temp_btn.is_visible():
                return temp_btn
        except:
            pass
        return None

    async def _click_temp_chat_toggle(self) -> bool:
        btn = await self._get_temp_chat_button()
        if btn is None:
            return False

        try:
            await btn.click(timeout=1500)
            await self._human_delay(250, 400)
            return True
        except Exception:
            try:
                selector = await self._resolve_selector("temp_chat")
                if not selector:
                    return False
                safe_selector = selector.replace("'", "\\'")
                await self.page.evaluate(
                    f"""
                    () => {{
                        const el = document.querySelector('{safe_selector}');
                        if (el) el.click();
                    }}
                    """
                )
                await self._human_delay(250, 400)
                return True
            except:
                return False

    async def _ensure_fresh_temp_chat(self) -> bool:
        """Ensure each request starts from a fresh regular page, then enters temp chat."""
        if not await self._ensure_chat_mode():
            self._track_error("Chat mode is not active", "chat_tab", "ensure_fresh_temp_chat")
            return False

        if not await self._ensure_sidebar_open():
            self._track_error("Sidebar did not open", "sidebar_toggle", "ensure_fresh_temp_chat")
            return False

        baseline = await self._capture_state_snapshot()
        log(
            f"Temp baseline: input={baseline.get('input_visible')} placeholder={baseline.get('input_placeholder')!r} "
            f"resp={baseline.get('response_count')} user={baseline.get('user_query_count')} "
            f"new_chat={baseline.get('new_chat_visible')} temp_btn={baseline.get('temp_chat_button_visible')} "
            f"landing={baseline.get('temp_chat_landing_visible')} temp_active={baseline.get('temp_chat_active')} 500={baseline.get('error_page_500')}",
            f"Worker {self.worker_id}",
        )

        if self._is_fresh_temp_chat_ready(baseline):
            log("Temp reset path: already on fresh temporary chat", f"Worker {self.worker_id}")
            return True

        if not self._is_fresh_regular_chat_ready(baseline):
            if not await self._ensure_fresh_chat():
                final_snapshot = await self._capture_state_snapshot()
                self._track_error("Fresh regular chat reset not confirmed", "new_chat", "ensure_fresh_temp_chat", final_snapshot)
                return False
            if not await self._wait_for_fresh_regular_chat(6.0):
                final_snapshot = await self._capture_state_snapshot()
                self._track_error("Fresh regular chat wait timed out", "new_chat", "ensure_fresh_temp_chat", final_snapshot)
                return False
            log("Temp reset path: stale chat -> fresh regular via New Chat", f"Worker {self.worker_id}")
        else:
            log("Temp reset path: fresh regular -> entering temporary chat", f"Worker {self.worker_id}")

        temp_btn = await self._get_temp_chat_button()
        if temp_btn is None:
            log("Temp chat button not found on current Gemini UI layout - checking clean chat fallback", f"Worker {self.worker_id}")
            snap = await self._capture_state_snapshot()
            if snap.get("input_visible") and snap.get("response_count", 0) == 0:
                log("Temp reset path: clean regular chat ready as fallback", f"Worker {self.worker_id}")
                return True
            if await self._ensure_fresh_chat():
                return True
            final_snapshot = await self._capture_state_snapshot()
            self._track_error("Clean chat fallback failed", "new_chat", "ensure_fresh_temp_chat", final_snapshot)
            return False

        if not await self._click_temp_chat_toggle():
            log("⚠️ Temp Chat toggle click failed - proceeding with fresh chat session", f"Worker {self.worker_id}")
            snap = await self._capture_state_snapshot()
            if snap.get("input_visible"):
                return True
            return False

        deadline = time.time() + 8.0
        while time.time() < deadline:
            snap = await self._capture_state_snapshot()
            if self._is_fresh_temp_chat_ready(snap):
                log("Temp reset path: fresh temporary chat ready", f"Worker {self.worker_id}")
                return True
            await asyncio.sleep(0.2)

        final_snapshot = await self._capture_state_snapshot()
        log(
            f"⚠️ Temp Chat reset not confirmed ({self._classify_new_chat_state(final_snapshot)})",
            f"Worker {self.worker_id}",
        )
        log(
            f"Temp final: input={final_snapshot.get('input_visible')} placeholder={final_snapshot.get('input_placeholder')!r} "
            f"resp={final_snapshot.get('response_count')} user={final_snapshot.get('user_query_count')} "
            f"new_chat={final_snapshot.get('new_chat_visible')} temp_btn={final_snapshot.get('temp_chat_button_visible')} "
            f"landing={final_snapshot.get('temp_chat_landing_visible')} temp_active={final_snapshot.get('temp_chat_active')} 500={final_snapshot.get('error_page_500')}",
            f"Worker {self.worker_id}",
        )
        self._track_error("Temp chat reset not confirmed", "temp_chat", "ensure_fresh_temp_chat", final_snapshot)

        return False

    async def prepare_idle(self) -> bool:
        """Pre-position an idle worker in a clean Temporary Chat."""
        if not self._initialized or self._generation_in_progress:
            return False
        previous_request_id = self._request_id
        self._request_id = "idle-prewarm"
        try:
            return await self._ensure_fresh_temp_chat()
        finally:
            self._request_id = previous_request_id

    def _track_error(self, error: str, selector_key: str, action: str, diagnostics: Optional[Dict[str, Any]] = None):
        payload = {
            "error": error,
            "selector_key": selector_key,
            "action": action,
            "timestamp": datetime.now().isoformat(),
            "worker_id": self.worker_id,
            "request_id": self._request_id,
            "diagnostics": diagnostics or {},
        }
        GeminiWebAutomation._last_errors[self.worker_id] = payload
        log(f"Error tracked: {error} (selector={selector_key}, action={action})", f"Worker {self.worker_id}")

    @classmethod
    def get_all_errors(cls) -> Dict[int, Dict[str, Any]]:
        return dict(cls._last_errors)

    @classmethod
    def clear_errors(cls):
        cls._last_errors.clear()

    async def init_with_page(self, page: Page, context: BrowserContext) -> bool:
        self.page = page
        self.context = context
        self._reset_model_tracking()
        try:
            for attempt in range(2):
                snapshot = await self._capture_state_snapshot()
                if snapshot.get("error_page_500"):
                    if attempt == 0:
                        recovered = await self._recover_from_google_500_page("init_precheck")
                        if recovered:
                            continue
                    raise Exception(f"Google 500 error page during init: {snapshot.get('page_title') or self.page.url}")

                # Do this before resolving the prompt selector: Spark has a
                # different composer and can look superficially healthy while
                # all Chat generation/reset selectors are unavailable.
                if not await self._ensure_chat_mode():
                    raise Exception("Gemini Chat mode could not be selected")

                # Wait for input to be ready (login check)
                input_selector = await self._resolve_selector("input", require_visible=True, timeout_ms=12000)
                if not input_selector:
                    if attempt == 0 and snapshot.get("error_page_500"):
                        recovered = await self._recover_from_google_500_page("init_missing_input")
                        if recovered:
                            continue
                    raise Exception("Input selector not found")

                try:
                    await self.page.wait_for_selector(input_selector, timeout=30000)
                    await self._human_delay(500, 1000)
                    print("[GeminiWeb] ✅ Logged in and ready")
                    
                    self._attach_network_logging()
                    await self._ensure_sidebar_open()
                    
                    self._initialized = True
                    return True
                except Exception as e:
                    snapshot = await self._capture_state_snapshot()
                    if attempt == 0 and snapshot.get("error_page_500"):
                        recovered = await self._recover_from_google_500_page("init_wait_timeout")
                        if recovered:
                            continue
                    raise e
        except Exception as e:
            try:
                snapshot = await self._capture_state_snapshot()
            except Exception:
                snapshot = {}
            log(
                f"Init failed: {e}; state={snapshot.get('ui_state_hint', 'unknown')} "
                f"url={snapshot.get('url', '')} title={snapshot.get('page_title', '')!r} "
                f"input={snapshot.get('input_visible')} chat={snapshot.get('chat_mode_active')} "
                f"spark={snapshot.get('spark_mode_active')} sign_in={snapshot.get('sign_in_visible')} "
                f"model={snapshot.get('model_button_text', '')!r}",
                f"Worker {self.worker_id}",
            )
            self._track_error(str(e), "input", "init", snapshot)
            return False

    async def send_message(
        self,
        prompt: str,
        model: str = None,
        thinking_level: str = None,
        use_search: bool = False,
        images: List[str] = None,
        request_id: str = None,
    ) -> Dict:
        if not self._initialized: 
            return {"success": False, "error": "Not initialized"}

        # Prepend instruction to prevent accidental image generation on text-only prompts
        if not images:
            anti_image_inst = (
                "IMPORTANT: Do NOT generate or create any images. "
                "Respond ONLY with text/data. Do not attempt to draw, paint, or generate any visual output.\n\n"
            )
            if not prompt.strip().startswith("IMPORTANT: Do NOT generate or create any images"):
                prompt = anti_image_inst + prompt

        log_buffer = []
        self._request_log_lines = log_buffer
        token = current_request_log_buffer.set(log_buffer)
        self._last_request_success = False
        # The finally block always cleans up an attachment path, including
        # failures during Chat reset or model selection.
        temp_prompt_file = None

        try:
            self._thinking_requested = bool(thinking_level and thinking_level.lower() in {"extended", "high", "deep"})
            self._generation_in_progress = True
            self._request_id = (request_id or "").strip() or uuid.uuid4().hex[:8]
            self._recent_network_events.clear()
            self._network_failure_counts.clear()
            self._last_network_outage = None
            prompt_chars = len(prompt or "")
            prompt_tokens_est = self._estimate_tokens(prompt or "")
            self._current_prompt_tokens_est = prompt_tokens_est
            self._log(
                f"[{self._request_id}] Request: model={model}, prompt_chars={prompt_chars}, prompt_tokens_est={prompt_tokens_est}"
            )

            # A user/browser session can switch modes after worker init.  Make
            # the Chat requirement idempotent at the request boundary too.
            if not await self._ensure_chat_mode():
                err = "Gemini Chat mode is not active"
                self._track_error(err, "chat_tab", "send_message")
                return {"success": False, "error": err}

            copy_selector = await self._resolve_selector("copy_btn")
            if not copy_selector:
                err = "Copy selector not found"
                self._track_error(err, "copy_btn", "send_message")
                return {"success": False, "error": err}

            # A dirty page is replaced by WorkerPool; request code never refreshes it.
            preflight = await self._capture_state_snapshot()
            if preflight.get("error_page_500"):
                err = "Google 500 error page detected before send"
                self._track_error(err, "input", "preflight", preflight)
                return {"success": False, "error": err}

            if preflight.get("stop_visible", False):
                err = "Previous generation is still active before send"
                self._track_error(err, "send_btn", "preflight", preflight)
                return {"success": False, "error": err}

            # In headed mode, explicitly foreground the active worker page before send.
            try:
                await self.page.bring_to_front()
                await self._human_delay(60, 120)
            except:
                pass
            
            # 0. Dismiss any stuck overlays/modals (Angular Material CDK overlays block clicks)
            try:
                await self.page.keyboard.press("Escape")
                await self._human_delay(100, 200)
                # Also try clicking the backdrop if it exists
                backdrop = self.page.locator('.cdk-overlay-backdrop')
                if await backdrop.count() > 0:
                    log("Dismissing stuck overlay", f"Worker {self.worker_id}")
                    await backdrop.first.click(force=True)
                    await self._human_delay(200, 400)
            except:
                pass
            
            # 1. Fresh temp chat
            if not await self._ensure_fresh_temp_chat():
                return {"success": False, "error": "Fresh temp chat reset not confirmed"}

            await self._human_delay()

            # 2. Select Model and Thinking Level
            if model or thinking_level:
                # First check the current active UI configuration
                ui_model, ui_thinking = await self._get_current_ui_model_and_thinking()
                
                # Update tracked state from what is currently on the screen
                if ui_model:
                    self._current_selected_model = ui_model
                if ui_thinking:
                    self._current_selected_thinking_level = ui_thinking

                # Select model if it differs
                if model and self._current_selected_model != model:
                    selected = await self._select_model(model)
                    # Re-read UI state since selecting a model changes the layout / resets defaults
                    ui_model, ui_thinking = await self._get_current_ui_model_and_thinking()
                    if not selected or not ui_model or not self._matches_model(ui_model, model):
                        err = f"Requested model {model!r} but Gemini UI is {ui_model or 'unknown'!r}"
                        self._track_error(
                            err,
                            "model_btn",
                            "send_message",
                            {"requested_model": model, "ui_model": ui_model, "selection_ok": bool(selected)},
                        )
                        return {"success": False, "error": err}
                    self._current_selected_model = ui_model
                    if ui_thinking:
                        self._current_selected_thinking_level = ui_thinking

                # Select thinking level if it differs
                target_thinking = thinking_level or "Standard"
                if target_thinking and self._current_selected_thinking_level != target_thinking:
                    await self._set_thinking_level(target_thinking)
                    _, verified_thinking = await self._get_current_ui_model_and_thinking()
                    if verified_thinking != target_thinking:
                        err = f"Requested thinking {target_thinking!r} but Gemini UI is {verified_thinking or 'unknown'!r}"
                        self._track_error(err, "thinking_level", "send_message")
                        return {"success": False, "error": err}
                    self._current_selected_thinking_level = verified_thinking

            # 3. Enter Prompt
            input_selector = await self._resolve_selector("input", require_visible=True, timeout_ms=2000)
            if not input_selector:
                err = "Input selector not found"
                self._track_error(err, "input", "send_message")
                return {"success": False, "error": err}
            input_area = self.page.locator(input_selector)
            await input_area.click()
            await self._human_delay()
            
            # 3.5 Prepare and Enter Prompt (converts long prompts to .txt file in clipboard/attachment if >= THRESHOLD)
            temp_prompt_file = None
            filled_text, temp_prompt_file = await self._prepare_and_enter_prompt(
                input_area, prompt, images, use_search=use_search
            )
            
            # Capture state BEFORE sending. These baselines drive all later
            # "did generation start?" checks; taking them after a send attempt
            # can make a real in-flight request look unsent.
            pre_send_snapshot = await self._capture_state_snapshot()
            pre_send_count = await self.page.locator(copy_selector).count()
            pre_send_resp_count = int(pre_send_snapshot.get("response_count") or 0)
            pre_send_resp_len = int(pre_send_snapshot.get("last_response_len") or 0)
            pre_send_resp_sig = str(pre_send_snapshot.get("last_response_signature") or "")
            pre_send_user_query_count = int(pre_send_snapshot.get("user_query_count") or 0)
            prompt_len = len((filled_text or "").strip())

            # 4. Click Send - VERIFIED
            worker_id = self.worker_id  # Capture for closure

            def start_signal_from_snapshot(snap: Dict[str, Any], input_text_len: Optional[int] = None) -> str:
                stop_now = bool(snap.get("stop_visible"))
                send_now = bool(snap.get("send_visible"))
                resp_now = int(snap.get("response_count") or 0)
                copy_now = int(snap.get("copy_count") or 0)
                user_now = int(snap.get("user_query_count") or 0)

                if copy_now > pre_send_count:
                    return "copy_increased"
                if resp_now > pre_send_resp_count:
                    return "response_increased"
                if user_now > pre_send_user_query_count:
                    return "user_query_increased"
                if stop_now and not send_now:
                    return "stop_visible"
                if input_text_len is not None:
                    input_cleared = prompt_len == 0 or input_text_len < max(1, prompt_len // 2)
                    if input_cleared and not send_now:
                        return "input_cleared"
                return ""
            
            async def get_input_text():
                try:
                    return await input_area.inner_text()
                except:
                    return ""
            
            async def verify_send_worked(before_text):
                # Wait a moment then check if input is cleared
                await self._human_delay(200, 400)  # Reduced for speed
                try:
                    # Try multiple methods to get input text (contenteditable vs textarea)
                    after_text = ""
                    try:
                        after_text = await input_area.inner_text()
                    except:
                        try:
                            after_text = await input_area.input_value()
                        except:
                            pass
                    
                    # Handle empty prompt case (e.g., image-only messages)
                    before_len = len(before_text.strip()) if before_text else 0
                    after_len = len(after_text.strip())
                    
                    if before_len == 0:
                        return True  # Empty prompt - can't verify, assume success
                    elif after_len < before_len / 2:
                        return True  # Input cleared = send worked
                    else:
                        snap = await self._capture_state_snapshot()
                        signal = start_signal_from_snapshot(snap, after_len)
                        if signal:
                            log(
                                f"Send accepted despite composer retaining text (signal={signal}, input_len={after_len})",
                                f"Worker {worker_id}",
                            )
                            return True
                        log(f"⚠️ Send failed: input not cleared ({after_len} chars remain)", f"Worker {worker_id}")
                        return False
                except Exception as e:
                    log(f"⚠️ Send verification error: {e}", f"Worker {worker_id}")
                    return False  # Don't assume success on error

            async def attempt_send_submission(reason: str, before_text: str) -> bool:
                log(f"Attempting send submission ({reason})", f"Worker {self.worker_id}")
                try:
                    await self.page.bring_to_front()
                except:
                    pass

                # Ensure any pending attachment upload spinner has cleared and Send button is active
                await self._wait_for_attachment_upload_complete(15.0)

                try:
                    await input_area.click()
                    await self._human_delay(80, 160)
                except:
                    pass

                try:
                    await self.page.keyboard.press("Control+Enter")
                    await self._human_delay(250, 400)
                    if await verify_send_worked(before_text):
                        log(f"Send submission worked via Ctrl+Enter ({reason})", f"Worker {self.worker_id}")
                        return True
                except:
                    pass

                try:
                    for selector in self._selector_candidates("send_btn"):
                        send_btn = self.page.locator(selector).first
                        if await send_btn.is_visible():
                            await send_btn.click()
                            await self._human_delay(250, 400)
                            if await verify_send_worked(before_text):
                                log(f"Send submission worked via button ({reason})", f"Worker {self.worker_id}")
                                return True
                except:
                    pass

                return False

            async def attempt_same_page_resend(reason: str) -> bool:
                snap = await self._capture_state_snapshot()
                signal = start_signal_from_snapshot(snap, int(snap.get("input_text_len") or 0))
                if signal:
                    log(f"Skipping resend because generation already started (reason={reason}, signal={signal})", f"Worker {self.worker_id}")
                    return True
                before_text = await get_input_text()
                return await attempt_send_submission(reason, before_text)
            
            send_before_text = await get_input_text()
            send_success = await attempt_send_submission("initial_send", send_before_text)
            
            if not send_success:
                log("❌ Send button click failed", f"Worker {self.worker_id}")
                snapshot = await self._capture_state_snapshot()
                outage = self._get_active_network_outage()
                if outage:
                    outage_error, outage_diag = await self._build_network_outage_error()
                    snapshot.update(outage_diag)
                    self._track_error(outage_error, "send_btn", "send_message", snapshot)
                    return {"success": False, "error": outage_error}
                self._track_error("Send button click failed", "send_btn", "send_message", snapshot)
                return {"success": False, "error": "Send button click failed"}
            
            # 4.5 Verify generation started using a short observation loop.
            # This avoids false negatives for very fast responses and avoids
            # resubmitting when Gemini starts thinking but leaves text in the editor.
            start_observe_seconds = 6.0
            start_poll_seconds = 0.2
            generation_started = False

            for send_attempt in range(MAX_SEND_RETRIES):
                start_signal = ""
                last_snap = None
                observe_deadline = time.time() + start_observe_seconds

                while time.time() < observe_deadline:
                    snap = await self._capture_state_snapshot()
                    last_snap = snap
                    outage = self._get_active_network_outage()
                    if outage:
                        outage_error, outage_diag = await self._build_network_outage_error()
                        snap.update(outage_diag)
                        self._track_error(outage_error, "send_btn", "verify_generation_started", snap)
                        return {"success": False, "error": outage_error}
                    input_now = await get_input_text()
                    input_now_len = len((input_now or "").strip())
                    start_signal = start_signal_from_snapshot(snap, input_now_len)

                    if start_signal:
                        break

                    await asyncio.sleep(start_poll_seconds)

                if start_signal:
                    log(f"✅ Generation started (attempt {send_attempt + 1}, signal={start_signal})", f"Worker {self.worker_id}")
                    generation_started = True
                    break

                # No start signals detected: confirm unsent before retrying.
                if last_snap is None:
                    last_snap = await self._capture_state_snapshot()
                send_still_visible = bool(last_snap.get("send_visible"))
                input_after = await get_input_text()
                input_after_len = len((input_after or "").strip())
                input_still_present = prompt_len > 0 and input_after_len >= max(1, prompt_len // 2)
                confirmed_unsent = send_still_visible and input_still_present

                if confirmed_unsent and send_attempt < MAX_SEND_RETRIES - 1:
                    log(
                        f"⚠️ Confirmed unsent; retrying send (attempt {send_attempt + 2}/{MAX_SEND_RETRIES})",
                        f"Worker {self.worker_id}"
                    )
                    await attempt_same_page_resend(f"soft_retry_{send_attempt + 2}")
                    continue

                if not confirmed_unsent:
                    # Ambiguous state: continue to normal wait path instead of over-retrying.
                    log("⚠️ Ambiguous start state; proceeding to response wait", f"Worker {self.worker_id}")
                    generation_started = True
                    break

                # Confirmed unsent and out of retries.
                log("Soft send retries failed", f"Worker {self.worker_id}")
                break
            
            if not generation_started:
                snapshot = await self._capture_state_snapshot()
                err = "Generation did not start after verified send retries"
                self._track_error(err, "send_btn", "verify_generation_started", snapshot)
                return {"success": False, "error": err}
            
            # Gemini can accept the request (user bubble + Stop state) while
            # retaining the full prompt as an editable draft. Clear only after
            # start was proven so retries and diagnostics cannot mistake it for
            # an unsent request.
            if generation_started:
                await self._clear_retained_prompt_draft(prompt_len)

            # 5. Wait for Response (Copy button to appear)
            log("Waiting for response...", f"Worker {self.worker_id}")
            await self._human_delay(300, 600)  # Reduced initial wait
            
            # Polling for copy button (Wait until we have MORE buttons than before)
            start_time = time.time()
            max_wait = BROWSER_TIMEOUT_SECONDS
            copy_btn = None
            last_wait_log = start_time
            last_thinking_len = -1
            last_thinking_label = ""
            last_thinking_label_change_at = start_time
            last_response_body_len = -1
            last_thinking_progress_at = start_time
            last_response_progress_at = start_time
            last_phase = "idle_or_unknown"
            last_phase_change_at = start_time
            last_scroll_nudge_at = 0.0
            finalize_attempted = False
            seen_new_response = False
            while (time.time() - start_time) < max_wait:
                outage = self._get_active_network_outage()
                if outage:
                    snapshot = await self._capture_state_snapshot()
                    outage_error, outage_diag = await self._build_network_outage_error()
                    snapshot.update(outage_diag)
                    log(f"⚠️ {outage_error}", f"Worker {self.worker_id}")
                    self._track_error(outage_error, "copy_btn", "wait_for_response_network_outage", snapshot)
                    return {"success": False, "error": outage_error}

                try:
                    # Capture page state and check copy buttons with a strict timeout to prevent indefinite hangs
                    async def gather_page_state():
                        shot = await self._capture_state_snapshot()
                        c_btns = self.page.locator(copy_selector)
                        c_count = await c_btns.count()
                        is_done = False
                        btn = None

                        if c_count > pre_send_count:
                            last_btn = c_btns.nth(c_count - 1)
                            if await last_btn.is_visible():
                                is_done = True
                                btn = last_btn

                        # Completion fallback: stop button gone, send button returned, and response present
                        if not is_done and not shot.get("stop_btn_visible") and shot.get("send_btn_visible") and shot.get("response_count", 0) > 0:
                            if c_count > 0:
                                last_btn = c_btns.nth(c_count - 1)
                                if await last_btn.is_visible():
                                    is_done = True
                                    btn = last_btn

                        return shot, c_count, is_done, btn

                    page_snapshot, current_count, done_signaled, found_btn = await asyncio.wait_for(
                        gather_page_state(),
                        timeout=15.0
                    )
                except asyncio.TimeoutError:
                    log("Page evaluation timed out inside response wait", f"Worker {self.worker_id}")
                    self._track_error("Page evaluation hung in wait loop", "copy_btn", "wait_for_response_hung")
                    return {"success": False, "error": "Page evaluation hung in wait loop"}
                except Exception as eval_err:
                    log(f"⚠️ Page evaluation error in wait loop: {eval_err}", f"Worker {self.worker_id}")
                    await asyncio.sleep(0.5)
                    continue

                if page_snapshot.get("error_page_500"):
                    log("⚠️ Google 500 error page detected during response wait", f"Worker {self.worker_id}")
                    self._track_error("Google 500 error page", "input", "wait_for_response_500_page", page_snapshot)
                    return {"success": False, "error": "Google 500 error page"}

                if done_signaled:
                    copy_btn = found_btn
                    break

                now = time.time()
                if (now - last_wait_log) >= self._wait_log_interval_seconds:
                    snap = page_snapshot
                    elapsed = int(now - start_time)
                    resp_count_total = int(snap.get("response_count") or 0)
                    resp_len_total = int(snap.get("last_response_len") or 0)
                    resp_sig = str(snap.get("last_response_signature") or "")
                    response_body_len = int(snap.get("response_body_len") or 0)
                    thinking_len = int(snap.get("thinking_len") or 0)
                    thinking_label = str(snap.get("thinking_label") or "").strip()
                    response_copy_count = int(snap.get("response_copy_count") or 0)
                    input_copy_count = int(snap.get("input_copy_count") or 0)
                    phase = str(snap.get("phase") or "idle_or_unknown")
                    recent_activity_age = self._get_recent_generation_activity_age()
                    backend_activity_live = (
                        recent_activity_age is not None and recent_activity_age <= RECENT_NETWORK_ACTIVITY_SECONDS
                    )

                    response_changed = False
                    if resp_count_total > pre_send_resp_count:
                        response_changed = True
                    elif resp_sig and resp_sig != pre_send_resp_sig:
                        response_changed = True
                    elif resp_len_total > pre_send_resp_len:
                        response_changed = True

                    if response_changed:
                        seen_new_response = True

                    if seen_new_response:
                        resp_count = max(1, resp_count_total - pre_send_resp_count) if resp_count_total > pre_send_resp_count else 1
                        resp_len = resp_len_total if resp_sig != pre_send_resp_sig else max(0, resp_len_total - pre_send_resp_len)
                    else:
                        resp_count = 0
                        resp_len = 0

                    if phase != last_phase:
                        last_phase = phase
                        last_phase_change_at = now
                        finalize_attempted = False

                    if thinking_len > last_thinking_len:
                        last_thinking_len = thinking_len
                        last_thinking_progress_at = now

                    if thinking_label and thinking_label != last_thinking_label:
                        last_thinking_label = thinking_label
                        last_thinking_label_change_at = now
                        last_thinking_progress_at = now

                    if response_body_len > last_response_body_len:
                        last_response_body_len = response_body_len
                        last_response_progress_at = now
                        finalize_attempted = False

                    log(
                        f"[{self._request_id}] Wait state: elapsed={elapsed}s stop={snap.get('stop_visible')} "
                        f"send={snap.get('send_visible')} resp={resp_count} "
                        f"len={resp_len} body={response_body_len} think={thinking_len} phase={phase} "
                        f"copy={snap.get('copy_count')}/{response_copy_count} input_copy={input_copy_count} "
                        f"vis={snap.get('visibility')} seen_new={seen_new_response} "
                        f"net_age={int(recent_activity_age) if recent_activity_age is not None else '-'}",
                        f"Worker {self.worker_id}"
                    )

                    # Progress watchdog: recover poisoned/stalled generation before full timeout
                    stop_visible = bool(snap.get("stop_visible"))
                    send_visible = bool(snap.get("send_visible"))
                    if phase == "thinking_only":
                        progress_age_basis = max(last_phase_change_at, last_thinking_progress_at)
                    else:
                        progress_age_basis = max(last_phase_change_at, last_response_progress_at)
                    no_progress_age = int(now - progress_age_basis)
                    stall_reason = ""

                    # Guardrail: request likely never sent, avoid waiting full timeout.
                    if (
                        (not stop_visible)
                        and send_visible
                        and resp_count == 0
                        and elapsed >= UNSENT_STUCK_SECONDS
                    ):
                        input_now = await get_input_text()
                        input_now_len = len((input_now or "").strip())
                        prompt_still_present = prompt_len > 0 and input_now_len >= max(1, prompt_len // 2)

                        if prompt_still_present:
                            log(
                                f"[{self._request_id}] Unsent diagnostics: input_len={snap.get('input_text_len')} "
                                f"send_disabled={snap.get('send_btn_disabled')} aria_disabled={snap.get('send_btn_aria_disabled')} "
                                f"overlay={snap.get('overlay_visible')} active={snap.get('active_element_tag')}:{snap.get('active_element_aria_label')} "
                                f"send_class={snap.get('send_btn_class')}",
                                f"Worker {self.worker_id}"
                            )
                            resend_ok = await attempt_same_page_resend("unsent_stuck")
                            if resend_ok:
                                resend_deadline = time.time() + 5.0
                                while time.time() < resend_deadline:
                                    resend_snap = await self._capture_state_snapshot()
                                    resend_stop = bool(resend_snap.get("stop_visible"))
                                    resend_send = bool(resend_snap.get("send_visible"))
                                    resend_resp = int(resend_snap.get("response_count") or 0)
                                    resend_input_len = int(resend_snap.get("input_text_len") or 0)
                                    resend_input_cleared = prompt_len == 0 or resend_input_len < max(1, prompt_len // 2)
                                    if resend_stop or resend_resp > 0 or resend_input_cleared or not resend_send:
                                        log("✅ Same-page resend recovered unsent start", f"Worker {self.worker_id}")
                                        stall_reason = ""
                                        break
                                    await asyncio.sleep(0.2)
                                if not stall_reason:
                                    last_wait_log = now
                                    continue
                            stall_reason = (
                                f"Unsent stuck: send visible and no output for {UNSENT_STUCK_SECONDS}s"
                            )

                    # If generation appears active but no progress, nudge scroll to bottom.
                    if (
                        stop_visible
                        and phase != "thinking_only"
                        and no_progress_age >= SCROLL_NUDGE_AFTER_NO_PROGRESS_SECONDS
                        and (now - last_scroll_nudge_at) >= SCROLL_NUDGE_MIN_INTERVAL_SECONDS
                    ):
                        await self._nudge_scroll_to_bottom()
                        last_scroll_nudge_at = now
                        log(
                            f"Scroll nudge at no-progress age {no_progress_age}s",
                            f"Worker {self.worker_id}"
                        )

                    if (
                        (not finalize_attempted)
                        and stop_visible
                        and phase in ("response_streaming", "response_complete_postprocessing")
                        and seen_new_response
                        and response_body_len >= FINALIZE_STABLE_RESPONSE_LEN
                        and no_progress_age >= FINALIZE_STABLE_RESPONSE_SECONDS
                    ):
                        finalize_attempted = True
                        log(
                            f"Attempting finalize after stable response body={response_body_len} no_progress={no_progress_age}s",
                            f"Worker {self.worker_id}"
                        )
                        finalized_text = await self._attempt_finalize_stalled_response(
                            copy_selector,
                            pre_send_count,
                            click_stop=False,
                        )
                        if finalized_text:
                            log("✅ Finalized stable response during post-processing", f"Worker {self.worker_id}")
                            self._last_request_success = True
                            return {"success": True, "response": finalized_text}

                    empty_threshold = self._stall_empty_seconds
                    if backend_activity_live:
                        empty_threshold = max(empty_threshold, STALL_EMPTY_SECONDS_WITH_ACTIVITY)

                    # For the pre-first-token phase (no output yet), also check broader network
                    # liveness from any relevant gemini.google.com traffic. Playwright fires
                    # on_response once per request (on headers), so the strict generation-URL
                    # check goes stale after RECENT_NETWORK_ACTIVITY_SECONDS even on healthy
                    # long-running prefills. The broader check keeps backend_activity_live=True
                    # as long as any relevant polling/keepalive traffic is flowing.
                    if stop_visible and resp_count == 0:
                        broader_age = self._get_recent_any_relevant_activity_age()
                        broader_live = (
                            broader_age is not None and broader_age <= RECENT_NETWORK_ACTIVITY_SECONDS
                        )
                        if broader_live and not backend_activity_live:
                            # Broader traffic is alive even though generation URL is stale —
                            # use the extended threshold so we don't kill a healthy slow prefill.
                            empty_threshold = max(empty_threshold, STALL_EMPTY_SECONDS_WITH_ACTIVITY)
                            log(
                                f"[{self._request_id}] Broader liveness active (broad_net_age={int(broader_age)}s); "
                                f"extending empty threshold to {empty_threshold}s",
                                f"Worker {self.worker_id}",
                            )
                        # Large-prompt additional grace: server prefill of very large contexts
                        # (>LARGE_PROMPT_TOKEN_THRESHOLD tokens) can silently take 2+ minutes before
                        # any DOM output appears. Grant this grace period unconditionally when stop is
                        # visible and there's no output yet, since the server won't start responding or
                        # firing network events until prefill is done.
                        if self._current_prompt_tokens_est >= LARGE_PROMPT_TOKEN_THRESHOLD:
                            empty_threshold = max(empty_threshold, STALL_EMPTY_SECONDS_LARGE_PROMPT)
                            log(
                                f"[{self._request_id}] Large prompt prefill grace active (prompt_tokens_est={self._current_prompt_tokens_est}); "
                                f"extending empty threshold to {empty_threshold}s",
                                f"Worker {self.worker_id}",
                            )

                    # Check if this is the new 2026 thinking overlay (status text is static, doesn't stream character-by-character)
                    is_new_thinking = snap.get("thinking_visible", False) and not snap.get("thinking_label", "").lower().startswith("show thinking")

                    # Cooked check for thinking models:
                    # Extended thinking can take up to 120s to begin returning visible output.
                    cooked_threshold = 120
                    if self._current_prompt_tokens_est and self._current_prompt_tokens_est >= LARGE_PROMPT_TOKEN_THRESHOLD:
                        cooked_threshold = STALL_EMPTY_SECONDS_LARGE_PROMPT

                    if (not stall_reason) and getattr(self, "_thinking_requested", False) and elapsed >= cooked_threshold:
                        if not snap.get("thinking_active") and response_body_len == 0:
                            stall_reason = "Stalled generation: thinking model failed to start reasoning (request cooked)"

                    can_track_thinking_progress = thinking_len > 0 or last_thinking_len > 0

                    if (
                        (not stall_reason)
                        and stop_visible
                        and phase == "thinking_only"
                        and can_track_thinking_progress
                    ):
                        if is_new_thinking:
                            static_thinking_age = int(now - last_thinking_label_change_at)
                            static_thinking_threshold = STALL_STATIC_THINKING_SECONDS
                            if backend_activity_live:
                                static_thinking_threshold = max(
                                    static_thinking_threshold,
                                    STALL_STATIC_THINKING_SECONDS_WITH_ACTIVITY,
                                )
                            if response_body_len == 0 and static_thinking_age >= static_thinking_threshold:
                                stall_reason = (
                                    f"Stalled generation: static thinking label unchanged for "
                                    f"{static_thinking_threshold}s (label={thinking_label or '-'})"
                                )
                        else:
                            thinking_threshold = STALL_THINKING_NO_PROGRESS_SECONDS
                            if backend_activity_live:
                                thinking_threshold = max(
                                    thinking_threshold,
                                    STALL_THINKING_NO_PROGRESS_SECONDS_WITH_ACTIVITY,
                                )
                            if no_progress_age >= thinking_threshold:
                                stall_reason = (
                                    f"Stalled generation: thinking made no progress for {thinking_threshold}s "
                                    f"(thinking_len={thinking_len})"
                                )
                    elif (not stall_reason) and stop_visible and resp_count == 0 and elapsed >= empty_threshold:
                        stall_reason = f"Stalled generation: no output for {empty_threshold}s"
                    elif (not stall_reason) and stop_visible and resp_count > 0:
                        no_progress_threshold = self._stall_no_progress_seconds
                        if backend_activity_live:
                            no_progress_threshold = max(
                                no_progress_threshold,
                                STALL_NO_PROGRESS_SECONDS_WITH_ACTIVITY,
                            )
                        if response_body_len < STALL_SMALL_LEN_THRESHOLD:
                            no_progress_threshold = STALL_NO_PROGRESS_SECONDS_SMALL
                            if self._current_prompt_tokens_est and self._current_prompt_tokens_est >= LARGE_PROMPT_TOKEN_THRESHOLD:
                                no_progress_threshold = max(no_progress_threshold, STALL_EMPTY_SECONDS_LARGE_PROMPT)
                            if backend_activity_live:
                                no_progress_threshold = max(
                                    no_progress_threshold,
                                    STALL_NO_PROGRESS_SECONDS_SMALL_WITH_ACTIVITY,
                                )

                        if no_progress_age >= no_progress_threshold:
                            stall_reason = (
                                f"Stalled generation: no progress for {no_progress_threshold}s "
                                f"(body_len={response_body_len})"
                            )

                    if stall_reason:
                        log(f"⚠️ {stall_reason}", f"Worker {self.worker_id}")
                        net_events = snap.get("network_events") or []
                        if net_events:
                            tail = []
                            for evt in net_events[-4:]:
                                kind = evt.get("kind", "?")
                                status = evt.get("status")
                                err = evt.get("error")
                                code = status if status is not None else (err or "ok")
                                url = (evt.get("url") or "")
                                tail.append(f"{kind}:{code}:{url[-48:]}")
                            log(f"[{self._request_id}] Network tail: {' | '.join(tail)}", f"Worker {self.worker_id}")

                        recovered_text = await self._attempt_finalize_stalled_response(copy_selector, pre_send_count)
                        if recovered_text:
                            log("✅ Recovered stalled generation via finalize path", f"Worker {self.worker_id}")
                            self._last_request_success = True
                            return {"success": True, "response": recovered_text}

                        error_snapshot = dict(snap)
                        try:
                            refreshed_snapshot = await self._capture_state_snapshot()
                            for key, value in refreshed_snapshot.items():
                                if key in ("network_events", "network_outage"):
                                    error_snapshot[key] = value
                        except:
                            pass
                        self._track_error(stall_reason, "copy_btn", "wait_for_response_stalled", error_snapshot)
                        return {"success": False, "error": stall_reason}

                    last_wait_log = now
                        
                await asyncio.sleep(1)  # Reduced from 2s
            
            if not copy_btn:
                log(f"❌ Timeout after {max_wait}s waiting for response", f"Worker {self.worker_id}")
                snapshot = await self._capture_state_snapshot()
                self._track_error(
                    f"Timeout after {max_wait}s waiting for response",
                    "copy_btn",
                    "wait_for_response",
                    snapshot,
                )
                return {"success": False, "error": f"Timeout after {max_wait}s waiting for response"}

            # Auto-scroll to ensure copy button is visible
            await self.page.evaluate('''
                (selector) => {
                    const copyButtons = document.querySelectorAll(selector);
                    if (copyButtons.length > 0) {
                        const lastBtn = copyButtons[copyButtons.length - 1];
                        lastBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                }
            ''', copy_selector)
            await self._human_delay(150, 300)

            # 6. Extraction via Copy Button (with lock to prevent clipboard race condition)
            async with GeminiWebAutomation._get_clipboard_lock():
                await copy_btn.click()
                await self._human_delay(100, 200)
                markdown = await self.page.evaluate("navigator.clipboard.readText()")
            
            self._generation_in_progress = False
            
            if not markdown:
                log("⚠️ Clipboard empty after copy", f"Worker {self.worker_id}")
                snapshot = await self._capture_state_snapshot()
                self._track_error("Clipboard empty", "copy_btn", "extract_response", snapshot)
                return {"success": False, "error": "Clipboard extraction failed"}

            out_chars = len(markdown)
            out_tokens_est = self._estimate_tokens(markdown)
            log(
                f"✅ Response: chars={out_chars}, tokens_est={out_tokens_est}",
                f"Worker {self.worker_id}",
            )
            self._last_request_success = True
            return {"success": True, "response": markdown.strip()}

        except Exception as e:
            self._generation_in_progress = False
            log(f"❌ Error: {e}", f"Worker {self.worker_id}")
            try:
                snapshot = await self._capture_state_snapshot()
            except:
                snapshot = {}
            if self._is_network_outage_error_text(str(e)):
                current_url = ""
                try:
                    current_url = self.page.url or ""
                except Exception:
                    current_url = ""
                self._mark_network_outage("send_exception", current_url or self.URL, str(e))
            outage = self._get_active_network_outage()
            if outage:
                outage_error, outage_diag = await self._build_network_outage_error()
                snapshot.update(outage_diag)
                self._track_error(outage_error, "unknown", "send_message", snapshot)
                return {"success": False, "error": outage_error}
            self._track_error(str(e), "unknown", "send_message", snapshot)
            
            return {"success": False, "error": str(e)}
        finally:
            if temp_prompt_file and os.path.exists(temp_prompt_file):
                try:
                    os.remove(temp_prompt_file)
                except Exception:
                    pass
            if not self._last_request_success:
                try:
                    await self._save_diagnostic_artifacts("failed")
                except:
                    pass
            self._generation_in_progress = False
            self._request_id = None
            current_request_log_buffer.reset(token)

    async def _select_model(self, model_name: str):
        """Select model from dropdown."""
        try:
            # Click model picker
            model_selector = await self._resolve_selector("model_btn", require_visible=True, timeout_ms=1500)
            if not model_selector:
                self._track_error("Model picker not found", "model_btn", "select_model")
                return False

            btn = self.page.locator(model_selector).first
            current = await btn.inner_text()
            if self._matches_model(current, model_name):
                return True
            
            await btn.click()
            await self._human_delay(300, 450)

            # Select from menu
            menu_item_selector = await self._resolve_selector("menu_item")
            if not menu_item_selector:
                self._track_error("Model menu item selector missing", "menu_item", "select_model")
                # Close the picker
                await self.page.keyboard.press("Escape")
                return False

            items = self.page.locator(menu_item_selector)
            item_count = await items.count()
            
            # Try to match by text first
            for i in range(item_count):
                item = items.nth(i)
                text = await item.inner_text()
                if self._matches_model(text, model_name):
                    await item.click()
                    print(f"[Worker {self.worker_id}] ✅ Selected model: {model_name}")
                    await self._human_delay(300, 600)
                    return True

            # If not found at all, close menu
            await self.page.keyboard.press("Escape")
            await self._human_delay(100, 300)
            
            self._track_error(
                f"Requested model family {model_name!r} not found",
                "model_btn",
                "select_model",
                {"requested_model": model_name, "menu_item_count": item_count},
            )
            return False

        except Exception as e:
            print(f"[Worker {self.worker_id}] ⚠️ Model selection failed: {e}")
            self._track_error(str(e), "model_btn", "select_model")
            # Ensure menu is closed
            try:
                await self.page.keyboard.press("Escape")
                await self._human_delay(100, 200)
            except:
                pass
            return False

    async def _set_thinking_level(self, level: str):
        """Set Gemini Web thinking level when explicitly requested."""
        normalized = (level or "").strip().lower()
        if not normalized:
            return
        if normalized in {"high", "extended", "deep"}:
            target_text = "Extended"
        elif normalized in {"standard", "medium", "low", "minimal"}:
            target_text = "Standard"
        else:
            return

        try:
            model_selector = await self._resolve_selector("model_btn", require_visible=True, timeout_ms=1500)
            if not model_selector:
                self._track_error("Model picker not found", "model_btn", "set_thinking_level")
                return

            await self.page.locator(model_selector).first.click()
            await self._human_delay(300, 450)

            # Check if new UI style (Extended thinking toggle directly in main dropdown menu) is active
            extended_thinking_item = self.page.locator(
                'gem-menu-item:has-text("Extended thinking"), [role="menuitem"]:has-text("Extended thinking")'
            ).first

            is_new_ui = False
            try:
                await extended_thinking_item.wait_for(state="visible", timeout=1200)
                is_new_ui = True
            except Exception:
                pass

            if is_new_ui:
                # Read selected state of "Extended thinking" toggle
                class_attr = await extended_thinking_item.get_attribute("class") or ""
                content_item = extended_thinking_item.locator('gem-menu-item-content, [class*="content"]').first
                content_class_attr = ""
                if await content_item.count() > 0:
                    content_class_attr = await content_item.get_attribute("class") or ""
                
                has_checkmark = await extended_thinking_item.locator(
                    'gem-icon[aria-label="Selected"], mat-icon:has-text("check"), [class*="selected"]'
                ).count() > 0

                is_currently_extended = (
                    "selected" in class_attr.lower() or 
                    "selected" in content_class_attr.lower() or 
                    has_checkmark
                )

                log(f"Detected thinking level via toggle state: is_currently_extended={is_currently_extended}", f"Worker {self.worker_id}")

                if target_text == "Extended":
                    if not is_currently_extended:
                        log("Extended thinking is currently OFF, clicking to toggle ON", f"Worker {self.worker_id}")
                        await extended_thinking_item.click()
                        await self._human_delay(300, 500)
                    else:
                        log("Extended thinking is already ON, closing menu", f"Worker {self.worker_id}")
                        await self.page.keyboard.press("Escape")
                        await self._human_delay(150, 300)
                else:  # target_text == "Standard"
                    if is_currently_extended:
                        log("Extended thinking is currently ON, clicking to toggle OFF", f"Worker {self.worker_id}")
                        await extended_thinking_item.click()
                        await self._human_delay(300, 500)
                    else:
                        log("Extended thinking is already OFF (Standard), closing menu", f"Worker {self.worker_id}")
                        await self.page.keyboard.press("Escape")
                        await self._human_delay(150, 300)
                return

            # Legacy sub-menu thinking level selection fallback. Match labels,
            # never positions: menu order can change without warning.
            log("Extended thinking toggle not found in main menu, falling back to legacy sub-menu", f"Worker {self.worker_id}")
            trigger = self.page.locator('gem-menu-item[value="thinking_level"]').first
            await trigger.wait_for(state="visible", timeout=1500)
            await trigger.click(timeout=1500)
            await self._human_delay(300, 450)

            # Try to match by text first
            option = self.page.locator(f'gem-menu-item:has-text("{target_text}")').filter(has_not_text="Thinking level").first
            try:
                await option.wait_for(state="visible", timeout=1500)
                await option.click(timeout=1500)
                log(f"Selected thinking level (legacy): {target_text}", f"Worker {self.worker_id}")
                await self._human_delay(250, 400)
                return
            except Exception as text_error:
                log(
                    f"Thinking level label {target_text!r} was not found: {text_error}",
                    f"Worker {self.worker_id}",
                )

            item_count = await self.page.locator('gem-menu-item, [role="menuitem"]').count()
            raise Exception(f"No labeled submenu option found for thinking level {target_text} (count={item_count})")
        except Exception as e:
            log(f"Thinking level selection failed: {e}", f"Worker {self.worker_id}")
            self._track_error(str(e), "thinking_level", "set_thinking_level")
            # Ensure menu is closed
            try:
                await self.page.keyboard.press("Escape")
                await self._human_delay(100, 200)
                await self.page.keyboard.press("Escape")
                await self._human_delay(100, 200)
            except:
                pass

    async def _paste_image(self, image_path: str):
        """Paste an image via clipboard into Gemini Web."""
        try:
            print(f"[Worker {self.worker_id}] Pasting image: {image_path}")
            
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            base64_image = base64.b64encode(image_data).decode('utf-8')
            
            ext = image_path.split('.')[-1].lower()
            mime_map = {
                'png': 'image/png',
                'jpg': 'image/jpeg',
                'jpeg': 'image/jpeg',
                'gif': 'image/gif',
                'webp': 'image/webp'
            }
            mime_type = mime_map.get(ext, 'image/png')
            
            # Focus input first
            input_selector = await self._resolve_selector("input", require_visible=True, timeout_ms=2000)
            if not input_selector:
                raise Exception("Input selector not found for image paste")

            input_area = self.page.locator(input_selector)
            await input_area.click()
            await asyncio.sleep(0.1)
            
            # Write image to clipboard
            await self.page.evaluate(f'''
                async () => {{
                    const base64 = "{base64_image}";
                    const mimeType = "{mime_type}";
                    const byteCharacters = atob(base64);
                    const byteNumbers = new Array(byteCharacters.length);
                    for (let i = 0; i < byteCharacters.length; i++) {{
                        byteNumbers[i] = byteCharacters.charCodeAt(i);
                    }}
                    const byteArray = new Uint8Array(byteNumbers);
                    const blob = new Blob([byteArray], {{ type: mimeType }});
                    const item = new ClipboardItem({{ [mimeType]: blob }});
                    await navigator.clipboard.write([item]);
                }}
            ''')
            
            await self.page.keyboard.press("Control+v")
            await asyncio.sleep(1.0)
            print(f"[Worker {self.worker_id}] ✅ Image pasted")
        except Exception as e:
            print(f"[Worker {self.worker_id}] ⚠️ Image paste warning: {e}")

    async def _wait_for_attachment_upload_complete(self, max_timeout_seconds: float = 30.0) -> bool:
        """Wait until the Send button becomes enabled/clickable after file attachment."""
        log("Waiting for Send button to become enabled...", f"Worker {self.worker_id}")
        start = time.time()
        deadline = start + max_timeout_seconds

        await asyncio.sleep(0.3)

        while time.time() < deadline:
            send_selector = await self._resolve_selector("send_btn", require_visible=True, timeout_ms=500)
            if send_selector:
                send_btn = self.page.locator(send_selector).first
                if await send_btn.count() > 0 and await send_btn.is_visible():
                    try:
                        disabled_attr = await send_btn.get_attribute("disabled")
                        aria_disabled = await send_btn.get_attribute("aria-disabled")
                        is_disabled = await send_btn.is_disabled()
                        if disabled_attr is None and aria_disabled != "true" and not is_disabled:
                            elapsed = round(time.time() - start, 2)
                            log(f"✅ Send button enabled & ready ({elapsed}s)", f"Worker {self.worker_id}")
                            return True
                    except Exception:
                        pass

            await asyncio.sleep(0.2)

        elapsed = round(time.time() - start, 2)
        log(f"⚠️ Send button ready wait timeout ({elapsed}s), proceeding to send attempt", f"Worker {self.worker_id}")
        return False

    async def _upload_file_attachment(self, file_paths: List[str]) -> bool:
        """Upload local files through Gemini's Upload & tools control."""
        if not file_paths:
            return True
        try:
            log(f"Uploading {len(file_paths)} file attachment(s): {file_paths}", f"Worker {self.worker_id}")

            file_input = self.page.locator('input[type="file"][accept*=".txt"]').first
            if await file_input.count() == 0:
                upload_button = self.page.locator(
                    'button[aria-label="Upload & tools"], '
                    'button[aria-label*="Upload" i][aria-haspopup="menu"]'
                ).first
                if await upload_button.count() == 0 or not await upload_button.is_visible():
                    log("Upload & tools button is unavailable", f"Worker {self.worker_id}")
                    return False
                await upload_button.click()
                try:
                    await file_input.wait_for(state="attached", timeout=3000)
                except Exception:
                    log("Upload file input did not appear after opening the menu", f"Worker {self.worker_id}")
                    return False

            await file_input.set_input_files(file_paths)
            log("Attached files through Gemini's local file input", f"Worker {self.worker_id}")

            return await self._wait_for_attachment_upload_complete(30.0)
        except Exception as e:
            log(f"⚠️ Error uploading file attachments: {e}", f"Worker {self.worker_id}")
            return False

    @staticmethod
    def _needs_search_hint(prompt: str, use_search: bool = False) -> bool:
        """Keep search intent visible when a long prompt moves into an attachment."""
        return use_search or bool(
            re.search(r"\b(?:google|search|web)\b", prompt or "", flags=re.IGNORECASE)
        )

    async def _prepare_and_enter_prompt(
        self,
        input_area,
        prompt: str,
        images: Optional[List[str]] = None,
        use_search: bool = False,
    ) -> Tuple[str, Optional[str]]:
        """
        Enter prompt into Gemini Web UI.
        If len(prompt) >= PROMPT_FILE_UPLOAD_THRESHOLD, converts the prompt to a
        .txt attachment to prevent DOM contenteditable freezing.
        Returns tuple of (entered_text_or_empty, created_temp_file_path).
        """
        prompt_str = (prompt or "").strip()
        needs_search_hint = self._needs_search_hint(prompt_str, use_search)
        should_file_upload = len(prompt_str) >= PROMPT_FILE_UPLOAD_THRESHOLD

        if should_file_upload:
            temp_file_path = None
            try:
                log(
                    f"Prompt length ({len(prompt_str)} chars) >= threshold ({PROMPT_FILE_UPLOAD_THRESHOLD}). "
                    f"Converting prompt to text file in clipboard/attachment to prevent UI freeze...",
                    f"Worker {self.worker_id}"
                )
                temp_dir = os.path.join(tempfile.gettempdir(), "gemini_prompt_files")
                os.makedirs(temp_dir, exist_ok=True)
                temp_file_path = os.path.join(
                    temp_dir, f"prompt_{self.worker_id}_{int(time.time()*1000)}.txt"
                )
                with open(temp_file_path, "w", encoding="utf-8") as f:
                    f.write(prompt_str)

                file_paths = [temp_file_path]
                if images:
                    file_paths.extend(images)
                uploaded = await self._upload_file_attachment(file_paths)

                if uploaded:
                    log("✅ Prompt .txt file attached cleanly & ready to send.", f"Worker {self.worker_id}")
                    short_input_text = ""
                    if needs_search_hint:
                        short_input_text = "Use Google Search for this request and read the attached prompt."
                        try:
                            await input_area.fill(short_input_text)
                            log(f"Typed search command into input box: '{short_input_text}'", f"Worker {self.worker_id}")
                        except Exception as fe:
                            log(f"Failed to fill search command: {fe}", f"Worker {self.worker_id}")
                    else:
                        try:
                            await input_area.fill("")
                        except Exception:
                            pass
                    await self._human_delay(200, 400)
                    return short_input_text, temp_file_path
                else:
                    log("⚠️ File upload failed, falling back to direct text fill...", f"Worker {self.worker_id}")
                    if os.path.exists(temp_file_path):
                        try:
                            os.remove(temp_file_path)
                        except Exception:
                            pass
                    temp_file_path = None
            except Exception as e:
                log(f"⚠️ Prompt file attachment failed ({e}), falling back to direct text fill...", f"Worker {self.worker_id}")
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except Exception:
                        pass
                temp_file_path = None

        # Standard paste path (for short prompts or fallback)
        if images:
            for img_path in images:
                await self._paste_image(img_path)
                await self._human_delay(200, 500)

        entered_prompt = prompt_str
        if use_search:
            entered_prompt = f"Use Google Search for this request.\n\n{prompt_str}"
        await input_area.fill(entered_prompt)
        await self._human_delay(300, 600)
        return entered_prompt, None

    async def close(self):
        self._initialized = False
        if self.page and not self.page.is_closed():
            await self.page.close()
