import asyncio
import base64
import math
import os
import sys
import random
import socket
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any, List, Dict, Optional, Tuple
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Route
from notifier import notify_error
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

# Low memory mode: block images, fonts, etc.
LOW_MEMORY_MODE = os.getenv("LOW_MEMORY_MODE", "true").lower() == "true"

# Slow VM mode: use JavaScript clicks instead of Playwright clicks
SLOW_VM_MODE = os.getenv("SLOW_VM_MODE", "true").lower() == "true"

# Debug screenshots on failure (disabled by default for performance)
DEBUG_SCREENSHOTS = os.getenv("DEBUG_SCREENSHOTS", "false").lower() == "true"
DEBUG_SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "debug_screenshots")

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
STALL_THINKING_NO_PROGRESS_SECONDS = 30
STALL_THINKING_NO_PROGRESS_SECONDS_WITH_ACTIVITY = 45
STALL_STATIC_THINKING_SECONDS = 180
STALL_STATIC_THINKING_SECONDS_WITH_ACTIVITY = 300
FINALIZE_STABLE_RESPONSE_SECONDS = 45
FINALIZE_STABLE_RESPONSE_LEN = 800
RECENT_NETWORK_ACTIVITY_SECONDS = 75
MAX_SEND_RETRIES = 3
IDLE_REFRESH_AFTER_SECONDS = 600
IDLE_REFRESH_WORKER_TIMEOUT_SECONDS = 45
POOL_RECOVERY_WORKER_TIMEOUT_SECONDS = 75
STALE_BUSY_WITHOUT_ACTIVE_SECONDS = 90
SCROLL_NUDGE_AFTER_NO_PROGRESS_SECONDS = 8
SCROLL_NUDGE_MIN_INTERVAL_SECONDS = 4
UNSENT_STUCK_SECONDS = 20
STALL_RECREATE_THRESHOLD = 1
NETWORK_OUTAGE_PROBE_TIMEOUT_SECONDS = 2.0


def _read_headed_page_zoom() -> float:
    raw = os.getenv("HEADED_PAGE_ZOOM", "1.0").strip()
    try:
        value = float(raw)
    except Exception:
        value = 1.0
    return max(0.5, min(1.0, value))


HEADED_PAGE_ZOOM = _read_headed_page_zoom()
class BaseAutomation:
    """Base class for browser-based AI automation."""
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._initialized = False
        self._owns_browser = False
        self._generation_in_progress = False
        self._pending_result: Optional[Dict] = None
        self._last_activity = time.time()

    @staticmethod
    async def _human_delay(min_ms: int = 50, max_ms: int = 150):
        """Add random delay to simulate human interaction. Reduced for speed."""
        delay = random.uniform(min_ms, max_ms) / 1000
        await asyncio.sleep(delay)

    async def init_with_page(self, page: Page, context: BrowserContext) -> bool:
        raise NotImplementedError

    async def send_message(self, prompt: str, **kwargs) -> Dict:
        raise NotImplementedError

    async def close(self):
        try:
            if self.page: await self.page.close()
            if self._owns_browser:
                if self.context: await self.context.close()
                if self.browser: await self.browser.close()
                if self.playwright: await self.playwright.stop()
        except:
            pass

class AIStudioAutomation(BaseAutomation):
    URL = "https://aistudio.google.com/"
    PLAYGROUND_URL = "https://aistudio.google.com/prompts/new_chat"
    
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    
    # Full rendering args for local non-headless (looks great)
    BROWSER_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--enable-gpu",
        "--enable-accelerated-2d-canvas",
        "--enable-features=VaapiVideoDecoder",
        "--force-color-profile=srgb",
    ]
    
    # Extra args for server/headless mode (minimal rendering)
    HEADLESS_ARGS = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
    ]
    
    LOW_MEMORY_ARGS = [
        "--js-flags=--max-old-space-size=512",
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
    ]

    # Hard refresh every N requests to clear browser cache/memory
    REFRESH_EVERY_N_REQUESTS = 10
    
    def __init__(self):
        super().__init__()
        self._request_count = 0

    async def init_with_page(self, page: Page, context: BrowserContext) -> bool:
        """Initialize with externally provided page (multi-tab mode)."""
        self.page = page
        self.context = context
        self._owns_browser = False
        
        try:
            # Check if we are seeing the playground. 
            # If not, and we are in non-headless mode, wait longer for user to login manually.
            is_headless = os.getenv("HEADLESS", "false").lower() == "true"
            timeout = 15000 if is_headless else 300000 # 5 minutes for manual login
            
            if not is_headless:
                print("[AIStudio] ℹ️ Non-headless mode: If you are not logged in, please log in now in the browser window.")

            await self.page.wait_for_selector('textarea[aria-label="Enter a prompt"]', timeout=timeout)
            await self._human_delay(500, 1000) # Wait after selector appears
            print("[AIStudio] ✅ Tab initialized and logged in")
            self._initialized = True
            return True
        except Exception as e:
            print(f"[AIStudio] ❌ Tab initialization failed (timeout/not logged in): {e}")
            return False
    async def js_click(self, selector: str, description: str = "element") -> bool:
        """Click an element using JavaScript - bypasses Playwright stability checks."""
        try:
            result = await self.page.evaluate(f'''
                () => {{
                    const el = document.querySelector('{selector}');
                    if (el) {{
                        el.click();
                        return true;
                    }}
                    return false;
                }}
            ''')
            if result:
                print(f"[AIStudio] ✅ Clicked {description}")
                await self._human_delay() # Add delay after click
            return result
        except Exception as e:
            print(f"[AIStudio] ⚠️ JS click failed for {description}: {e}")
            return False

    async def _wait_and_extract_pending(self) -> Dict:
        """
        Handle retry after HTTP timeout - wait for any in-progress generation and extract result.
        Called when client retries after a timeout.
        """
        try:
            # Check if Run button shows "Stop" (still generating)
            button = self.page.locator('button[aria-label="Run"]')
            btn_text = await button.inner_text(timeout=2000)
            
            if "Stop" in btn_text or "progress_activity" in btn_text:
                print("[AIStudio] Generation still in progress, waiting...")
                await self._wait_for_generation()
            else:
                print("[AIStudio] Generation already complete, extracting...")
            
            # Extract the result
            markdown = await self._extract_markdown()
            self._generation_in_progress = False
            
            if not markdown:
                return {"success": False, "error": "Failed to extract pending response"}
            
            result = {"success": True, "response": markdown}
            self._pending_result = result
            return result
            
        except Exception as e:
            self._generation_in_progress = False
            print(f"[AIStudio] Pending extraction error: {e}")
            return {"success": False, "error": str(e)}

    async def send_message(self, prompt: str, model: str = None, thinking_level: str = None, use_search: bool = False, images: List[str] = None) -> Dict:
        """
        Send a message to AI Studio.
        """
        if not self._initialized:
            return {"success": False, "error": "Automation not initialized"}
        
        # Check if generation is already in progress (client retried after timeout)
        if self._generation_in_progress:
            print("[AIStudio] ⏳ Generation already in progress, waiting for it...")
            return await self._wait_and_extract_pending()

        try:
            self._generation_in_progress = True
            self._request_count += 1
            
            # 0. Periodic hard refresh to clear browser cache/memory
            if self._request_count >= self.REFRESH_EVERY_N_REQUESTS:
                print(f"[AIStudio] 🔄 Hard refresh (clearing cache after {self._request_count} requests)...")
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
                await self._human_delay(1000, 1500)
                self._request_count = 0
            
            # 1. Dismiss any popups/tooltips
            await self.page.keyboard.press("Escape")
            await self._human_delay(400, 800)
            
            # 2. Navigate to new chat via URL (more reliable than clicking)
            print("[AIStudio] Creating new chat session...")
            await self.page.goto(self.PLAYGROUND_URL, wait_until="domcontentloaded", timeout=30000)
            await self._human_delay(1500, 2500)  # Let page stabilize
            
            # 2. Wait for textarea to be ready
            try:
                await self.page.wait_for_selector('textarea[aria-label="Enter a prompt"]', timeout=15000)
                await self._human_delay(200, 500)
            except:
                print("[AIStudio] ⚠️ Textarea not found, continuing anyway...")
            
            # 3. Skip model/thinking config on slow VMs (use AI Studio defaults)
            if not SLOW_VM_MODE:
                if model:
                    await self._select_model(model)
                if thinking_level:
                    await self._set_thinking_level(thinking_level)
            else:
                print("[AIStudio] ℹ️ SLOW_VM_MODE: Skipping model/thinking config (using defaults)")
            
            # 4. Toggle Web Search (if needed)
            if use_search:
                await self._toggle_search(True)
            
            # 5. Paste Images (if provided)
            if images:
                for img_path in images:
                    await self._paste_image(img_path)
                    await self._human_delay(200, 500)
            
            # 6. Type Prompt using JavaScript
            print(f"[AIStudio] Typing prompt ({len(prompt)} chars)...")
            await self.page.evaluate('''(text) => {
                const textarea = document.querySelector('textarea[aria-label="Enter a prompt"]');
                if (textarea) {
                    textarea.value = text;
                    textarea.dispatchEvent(new Event('input', {bubbles: true}));
                    textarea.focus();
                }
            }''', prompt)
            await self._human_delay(800, 1200)  # Let UI update
            
            # 7. Click Run button using JavaScript
            print("[AIStudio] Generating response...")
            clicked = await self.js_click('button[aria-label="Run"]', "Run button")
            if not clicked:
                # Fallback: try keyboard shortcut
                await self.page.keyboard.press("Control+Enter")
                await self._human_delay(200, 500)
            await self._human_delay(800, 1200)
            
            # 8. Wait for Generation
            await self._wait_for_generation()
            
            # 9. Extract Markdown
            markdown = await self._extract_markdown()
            
            self._generation_in_progress = False
            
            if not markdown:
                return {"success": False, "error": "Failed to extract markdown response"}
            
            return {"success": True, "response": markdown}

        except Exception as e:
            self._generation_in_progress = False
            print(f"[AIStudio] Interaction error: {e}")
            
            # Force refresh to reset page state for next request
            try:
                print("[AIStudio] 🔄 Error recovery: refreshing page...")
                await self.page.reload(wait_until="domcontentloaded", timeout=15000)
                self._request_count = 0  # Reset counter since we just refreshed
            except:
                pass
            
            return {"success": False, "error": str(e)}


    async def _select_model(self, model_id: str):
        """Open model selector and pick model - SKIP if already selected."""
        try:
            # First check if the desired model is ALREADY selected
            model_card = self.page.locator('.model-selector-card')
            try:
                current_model_text = await model_card.inner_text(timeout=2000)
                if model_id.lower() in current_model_text.lower():
                    print(f"[AIStudio] Model {model_id} already selected, skipping")
                    return
            except:
                pass
            
            print(f"[AIStudio] Selecting model: {model_id}")
            
            selector = '.model-selector-card'
            try:
                await self.page.wait_for_selector(selector, timeout=2000)
            except:
                selector = 'mat-drawer[position="end"] button:has-text("Gemini")'
            
            await self.page.click(selector)
            await self._human_delay(200, 400)
            
            search_input = self.page.locator('input[placeholder*="Search"], input[aria-label*="Search"]')
            await search_input.fill(model_id)
            await self._human_delay(200, 400)
            
            model_btn = self.page.locator(f'button:has-text("{model_id}"), button[id*="{model_id}"]').first
            await model_btn.click(timeout=3000)
            await self._human_delay(100, 300)
            print(f"[AIStudio] ✅ Model {model_id} selected")
        except Exception as e:
            print(f"[AIStudio] ⚠️ Model selection warning: {e}")

    async def _set_thinking_level(self, level: str):
        """Set thinking level from dropdown."""
        try:
            print(f"[AIStudio] Setting thinking level: {level}")
            await self.page.click('mat-select[aria-label="Thinking Level"]', timeout=3000)
            await self._human_delay(200, 400)
            await self.page.click(f'mat-option:has-text("{level}")', timeout=3000)
            await self._human_delay(100, 300)
        except Exception as e:
            print(f"[AIStudio] Thinking level warning: {e}")

    async def _paste_image(self, image_path: str):
        """Paste an image via clipboard into AI Studio."""
        try:
            print(f"[AIStudio] Pasting image: {image_path}")
            
            # Read image file as base64
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            base64_image = base64.b64encode(image_data).decode('utf-8')
            
            # Determine mime type
            ext = image_path.split('.')[-1].lower()
            mime_map = {
                'png': 'image/png',
                'jpg': 'image/jpeg',
                'jpeg': 'image/jpeg',
                'gif': 'image/gif',
                'webp': 'image/webp'
            }
            mime_type = mime_map.get(ext, 'image/png')
            
            # Focus textarea first
            textarea = self.page.locator('textarea[aria-label="Enter a prompt"]')
            await textarea.click()
            await self._human_delay(50, 150)
            
            # Write image to clipboard using JavaScript
            await self.page.evaluate(f'''
                async () => {{
                    const base64 = "{base64_image}";
                    const mimeType = "{mime_type}";
                    
                    // Convert base64 to blob
                    const byteCharacters = atob(base64);
                    const byteNumbers = new Array(byteCharacters.length);
                    for (let i = 0; i < byteCharacters.length; i++) {{
                        byteNumbers[i] = byteCharacters.charCodeAt(i);
                    }}
                    const byteArray = new Uint8Array(byteNumbers);
                    const blob = new Blob([byteArray], {{ type: mimeType }});
                    
                    // Create ClipboardItem and write to clipboard
                    const item = new ClipboardItem({{ [mimeType]: blob }});
                    await navigator.clipboard.write([item]);
                }}
            ''')
            
            # Use Playwright's keyboard to paste (Ctrl+V)
            await self.page.keyboard.press("Control+v")
            await self._human_delay(800, 1200) # Give time for upload
            
            # Wait for image to appear in the prompt area
            # Check if image preview appeared (look for img or media indicators)
            try:
                # AI Studio shows uploaded images in the prompt area
                img_count = await self.page.locator('.prompt-box-container img, .prompt-box-container .media-preview').count()
                if img_count > 0:
                    print(f"[AIStudio] ✅ Image uploaded ({img_count} media items visible)")
                else:
                    print(f"[AIStudio] ⚠️ Image pasted but preview not detected - may still work")
            except:
                print(f"[AIStudio] ✅ Image paste completed")
        except Exception as e:
            print(f"[AIStudio] Image paste warning: {e}")

    async def _toggle_search(self, enabled: bool):
        """Toggle Google Search grounding."""
        try:
            btn = self.page.locator('button[aria-label="Grounding with Google Search"]')
            is_checked = await btn.get_attribute("aria-checked") == "true"
            if is_checked != enabled:
                await btn.click()
                await self._human_delay(400, 600)
        except Exception as e:
            print(f"[AIStudio] Search toggle warning: {e}")

    async def _wait_for_generation(self):
        """
        Wait until generation ends.
        
        IMPORTANT: The button's aria-label="Run" NEVER changes!
        We must check the button's INNER TEXT for "Stop" vs "Run".
        """
        print("[AIStudio] Waiting for generation...")
        try:
            # Get the run/stop button (aria-label is always "Run")
            button = self.page.locator('button[aria-label="Run"]')
            
            # Wait for button text to contain "Stop" (generation started)
            start_time = asyncio.get_event_loop().time()
            max_wait = 10  # 10 seconds to detect start
            
            generation_started = False
            while (asyncio.get_event_loop().time() - start_time) < max_wait:
                try:
                    btn_text = await button.inner_text(timeout=1000)
                    if "Stop" in btn_text or "progress_activity" in btn_text:
                        print("[AIStudio] Generation started (button shows Stop)")
                        generation_started = True
                        break
                except:
                    pass
                await asyncio.sleep(0.5)  # Reduced CPU pressure (was 0.1)
            
            if not generation_started:
                print("[AIStudio] Generation may have finished instantly or failed to start")
                return
            
            # Wait for button text to return to "Run" (generation complete)
            # Also scroll on every poll to ensure content renders
            start_time = asyncio.get_event_loop().time()
            max_wait = 300  # 5 minutes max
            
            while (asyncio.get_event_loop().time() - start_time) < max_wait:
                try:
                    # Scroll to bottom on every poll to ensure content renders
                    await self.page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    
                    btn_text = await button.inner_text(timeout=1000)
                    
                    # Check if "Stop" is gone and we're back to "Run"
                    if "Stop" not in btn_text and "progress_activity" not in btn_text:
                        print("[AIStudio] Generation complete (button shows Run)")
                        break
                except:
                    pass
                
                await asyncio.sleep(2)  # Poll every 2 seconds (reduced CPU pressure)
            
            # Content stability check - wait until text stops changing
            print("[AIStudio] Waiting for content to stabilize...")
            last_length = 0
            stable_count = 0
            for _ in range(50):  # Max 5 seconds
                try:
                    length = await self.page.evaluate('''() => {
                        const containers = document.querySelectorAll('.chat-turn-container.model');
                        if (containers.length === 0) return 0;
                        const last = containers[containers.length - 1];
                        last.scrollIntoView({ behavior: 'instant', block: 'end' });
                        return last.innerText.length;
                    }''')
                    
                    if length > 0 and length == last_length:
                        stable_count += 1
                        if stable_count >= 3:  # Stable for 300ms
                            print(f"[AIStudio] Content stable at {length} chars")
                            break
                    else:
                        stable_count = 0
                        last_length = length
                except:
                    pass
                
                await asyncio.sleep(0.3)  # Reduced CPU pressure (was 0.1)
            
        except Exception as e:
            print(f"[AIStudio] Wait warning: {e}")


    async def _extract_markdown(self) -> Optional[str]:
        """Extract response as markdown via the 'Copy as markdown' button."""
        try:
            print("[AIStudio] Extracting response...")
            
            # On slow VMs, skip the clipboard method entirely (too many timeouts)
            if SLOW_VM_MODE:
                await self._human_delay(800, 1200) # Brief wait for DOM to settle
                return await self._extract_from_dom()
            
            # Scope to MODEL response containers only
            menus = self.page.locator('.chat-turn-container.model button[aria-label="Open options"]')
            menu_count = await menus.count()
            print(f"[AIStudio] Found {menu_count} model menu buttons")
            
            if menu_count > 0:
                # Hover on the LAST model turn container to reveal the button
                try:
                    model_containers = self.page.locator('.chat-turn-container.model')
                    container_count = await model_containers.count()
                    if container_count > 0:
                        last_container = model_containers.nth(container_count - 1)
                        await last_container.hover(timeout=2000)
                        await self._human_delay(100, 300) # Minimal time for button to appear
                    
                    # Now click the button
                    last_menu = menus.nth(menu_count - 1)
                    await last_menu.click(timeout=3000)
                    print("[AIStudio] Clicked menu button")
                except Exception as click_err:
                    print(f"[AIStudio] Click failed, trying JS: {click_err}")
                    # Force via JavaScript (hover + click)
                    try:
                        await self.page.evaluate('''
                            (() => {
                                const containers = document.querySelectorAll('.chat-turn-container.model');
                                if (containers.length > 0) {
                                    const last = containers[containers.length - 1];
                                    last.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                                    setTimeout(() => {
                                        const btn = last.querySelector('button[aria-label="Open options"]');
                                        if (btn) btn.click();
                                    }, 200);
                                }
                            })()
                        ''')
                        await self._human_delay(200, 400)
                        print("[AIStudio] Clicked via JS")
                    except Exception as js_err:
                        print(f"[AIStudio] JS also failed: {js_err}")
                        return await self._extract_from_dom()
                
                await self._human_delay(100, 300)  # Wait for menu to appear
                
                # Find and click "Copy as markdown"
                copy_btn = self.page.locator('button:has-text("Copy as markdown")')
                try:
                    if await copy_btn.count() > 0:
                        await copy_btn.first.click(timeout=3000)
                        print("[AIStudio] Clicked 'Copy as markdown'")
                        await self._human_delay(100, 200)  # Minimal time for clipboard
                        
                        # Read from clipboard
                        markdown = await self.page.evaluate("navigator.clipboard.readText()")
                        if markdown and len(markdown.strip()) > 0:
                            print(f"[AIStudio] ✅ Got {len(markdown)} chars via clipboard")
                            return markdown.strip()
                    else:
                        print("[AIStudio] 'Copy as markdown' not found, pressing Escape")
                        await self.page.keyboard.press("Escape")
                        await self._human_delay(100, 300)
                except Exception as copy_err:
                    print(f"[AIStudio] Copy failed: {copy_err}")
                    await self.page.keyboard.press("Escape")
                    await self._human_delay(100, 300)
            
            # Fallback to DOM
            return await self._extract_from_dom()
            
        except Exception as e:
            print(f"[AIStudio] Extraction error: {e}")
            return await self._extract_from_dom()
    
    async def _extract_from_dom(self) -> Optional[str]:
        """Fallback: Extract text from DOM using pure JavaScript (fast, no timeouts)."""
        try:
            print("[AIStudio] Using DOM fallback...")
            
            # Wait a moment for response to render
            await self._human_delay(400, 800)
            
            # Use JavaScript to extract text - specifically target markdown content
            text = await self.page.evaluate('''
                () => {
                    // Find model response containers
                    const modelContainers = document.querySelectorAll('.chat-turn-container.model');
                    if (modelContainers.length === 0) return null;
                    
                    // Get the last (most recent) model response
                    const lastContainer = modelContainers[modelContainers.length - 1];
                    
                    // Try to find markdown-body first (contains actual formatted response)
                    const markdownBody = lastContainer.querySelector('.markdown-body');
                    if (markdownBody) {
                        const text = markdownBody.innerText || markdownBody.textContent;
                        if (text && text.trim().length > 20) {
                            return text.trim();
                        }
                    }
                    
                    // Try ms-text-chunk (the actual text content element)
                    const textChunks = lastContainer.querySelectorAll('ms-text-chunk');
                    if (textChunks.length > 0) {
                        let fullText = '';
                        textChunks.forEach(chunk => {
                            const chunkText = chunk.innerText || chunk.textContent;
                            if (chunkText) fullText += chunkText + ' ';
                        });
                        if (fullText.trim().length > 20) {
                            return fullText.trim();
                        }
                    }
                    
                    // Last resort: get innerText but exclude buttons/menus
                    const clone = lastContainer.cloneNode(true);
                    // Remove menu buttons, icons, and toolbar elements
                    clone.querySelectorAll('button, mat-icon, .toolbar, [aria-label]').forEach(el => el.remove());
                    const cleanText = clone.innerText || clone.textContent;
                    if (cleanText && cleanText.trim().length > 20) {
                        return cleanText.trim();
                    }
                    
                    return null;
                }
            ''')
            
            if text and len(text) > 50:  # Ensure we got real content, not just UI garbage
                print(f"[AIStudio] ✅ Got {len(text)} chars via DOM")
                return text
            else:
                print(f"[AIStudio] ⚠️ Response too short or empty ({len(text) if text else 0} chars)")
                return None
                
        except Exception as e:
            print(f"[AIStudio] DOM fallback failed: {e}")
        return None


    async def close(self):
        await super().close()

class GeminiWebAutomation(BaseAutomation):
    URL = "https://gemini.google.com/"
    
    # Hard refresh every N requests to clear browser cache/memory
    REFRESH_EVERY_N_REQUESTS = 10
    
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
            '[data-test-id*="mode" i]',
            'button[data-test-id="bard-mode-menu-button"]',
            'button[aria-label*="mode" i]',
            'button[aria-label*="model" i]',
            'button[aria-label*="picker" i]',
            '.input-area-switch',
        ],
        "thinking_level": [
            'gem-menu-item[value="thinking_level"]',
            'gem-menu-item[value*="thinking" i]',
            '[role="menuitem"][value*="thinking" i]',
        ],
        "new_chat": [
            '[aria-label*="New chat" i]',
            '[aria-label*="New" i]',
            '[data-test-id*="new-chat" i]',
            '[data-test-id="side-nav-sparkle-button"][aria-label*="New" i]',
            'a[aria-label="New chat"]',
            'button[aria-label="New chat"]',
            '[data-test-id="new-chat-button"] a',
            '[data-test-id="new-chat-button"]',
            'a[href="/app"]',
            'a[href="/"]',
        ],
        "temp_chat": [
            '[data-test-id*="temp" i]',
            '[aria-label*="temporary" i]',
            '[aria-label*="temp chat" i]',
            'button[aria-label*="temp" i]',
            '[data-test-id="temp-chat-button"]',
            'button[aria-label="Temporary chat"]',
            '[aria-label="Temporary chat"]',
        ],
        "temp_chat_active": [
            '[data-test-id*="temp" i].temp-chat-on',
            '[aria-label*="temporary" i].temp-chat-on',
            '[data-test-id="temp-chat-button"].temp-chat-on',
            '[aria-label="Temporary chat"].temp-chat-on',
        ],
        "sidebar_toggle": [
            '[data-test-id="side-nav-sparkle-button"][aria-label*="sidebar" i]',
            '[data-test-id="side-nav-sparkle-button"]',
            'button[aria-label*="sidebar" i]',
            'button[aria-label="Open sidebar"]',
            'button[aria-label="Close sidebar"]',
            'button.close-sidenav-button',
            'button[aria-label="Main menu"]',
            '[aria-label="Main menu"]',
        ],
        "copy_btn": [
            'button[aria-label="Copy"]',
            '[aria-label="Copy"]',
            'button[aria-label*="Copy" i]',
            '[aria-label*="Copy" i]',
        ],
        "menu_panel": [
            '.mat-mdc-menu-panel',
            '[role="menu"]',
        ],
        "menu_item": [
            'gem-menu-item[role="menuitem"]',
            'gem-menu-item',
            '.mat-mdc-menu-item',
            '[role="menuitem"]',
        ],
    }

    _last_errors: Dict[int, Dict[str, Any]] = {}
    
    def __init__(self, worker_id: int = 0):
        super().__init__()
        self.worker_id = worker_id  # For logging
        self._request_count = 0
        self._request_id = None  # Set per-request for log tracing
        self._wait_log_interval_seconds = WAIT_LOG_INTERVAL_SECONDS
        self._stall_empty_seconds = STALL_EMPTY_SECONDS
        self._stall_no_progress_seconds = STALL_NO_PROGRESS_SECONDS
        self._network_logging_attached = False
        self._recent_network_events = deque(maxlen=40)
        self._network_failure_counts: Dict[str, int] = {}
        self._last_network_outage: Optional[Dict[str, Any]] = None
        self._last_recovery: Optional[Dict[str, Any]] = None
        self._zoom_applied = False
        self._browser_zoom_reset = False
        self._current_prompt_tokens_est: int = 0  # Set per-request for stall scaling
        self._request_log_lines: List[str] = []   # Per-request log buffer for error reports
        self._current_selected_model: Optional[str] = None
        self._current_selected_thinking_level: Optional[str] = None

    def _reset_model_tracking(self):
        self._current_selected_model = None
        self._current_selected_thinking_level = None

    async def _reset_browser_zoom(self, force: bool = False):
        try:
            if os.getenv("HEADLESS", "false").lower() == "true":
                return
            if self._browser_zoom_reset and not force:
                return

            await self.page.bring_to_front()
            await self.page.keyboard.press("Control+0")
            await self._human_delay(50, 90)
            self._browser_zoom_reset = True
            log("Reset browser zoom to 100%", f"Worker {self.worker_id}")
        except Exception:
            pass

    async def _apply_headed_zoom(self, force: bool = False):
        """Keep headed sessions zoomed out to reduce long-output clipping/stalls."""
        try:
            if os.getenv("HEADLESS", "false").lower() == "true":
                return

            if HEADED_PAGE_ZOOM >= 0.999:
                return

            zoom_css = f"{int(HEADED_PAGE_ZOOM * 100)}%"
            if not force:
                current_zoom = await self.page.evaluate(
                    "() => (document.documentElement && document.documentElement.style && document.documentElement.style.zoom) || ''"
                )
                if str(current_zoom).strip() == zoom_css:
                    return

            await self.page.evaluate(
                """
                (zoomCss) => {
                    try { document.documentElement.style.zoom = zoomCss; } catch (_) {}
                    try { if (document.body) document.body.style.zoom = zoomCss; } catch (_) {}
                }
                """,
                zoom_css,
            )
            self._zoom_applied = True
            log(f"Applied page zoom: {zoom_css}", f"Worker {self.worker_id}")
        except Exception as e:
            if force:
                log(f"Zoom apply warning: {e}", f"Worker {self.worker_id}")

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
        """Save a screenshot + DOM text extract of the page at the moment of failure.

        Must be called BEFORE any page.reload() / hard refresh so the evidence is
        captured while the failing page is still live.  The old approach of saving
        page.content() as HTML is dropped — Gemini is a JS SPA so the saved HTML
        renders black locally and is useless.  A text extract + screenshot give us
        everything we actually need for diagnosis.
        """
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
            except Exception as e:
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
    _diag_saved_pre_refresh: bool = False

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

        return candidates[0]

    async def _resolve_locator(self, key: str, require_visible: bool = False, timeout_ms: int = 900):
        selector = await self._resolve_selector(key, require_visible=require_visible, timeout_ms=timeout_ms)
        if not selector:
            return None
        return self.page.locator(selector).first

    def _matches_model(self, item_text: str, model_name: str) -> bool:
        """Token-safe model matching to avoid accidental partial collisions."""
        if not item_text or not model_name:
            return False

        item_lower = item_text.lower()
        model_lower = model_name.strip().lower()

        # Check exact substrings first
        if model_lower in item_lower:
            return True

        # Group equivalents
        import re
        tokens = set(re.findall(r"[a-z0-9]+", item_lower))
        
        # Define clean aliases/mapping for validation
        if model_lower in {"gemini-3.1-flash-lite", "3.1-flash-lite", "flash-lite", "fast", "lite"}:
            return "3" in tokens and "1" in tokens and "flash" in tokens and "lite" in tokens
        
        if model_lower in {"gemini-3.5-flash", "3.5-flash", "flash", "thinking"}:
            return "3" in tokens and "5" in tokens and "flash" in tokens
            
        if model_lower in {"gemini-3.1-pro", "3.1-pro", "pro"}:
            return "3" in tokens and "1" in tokens and "pro" in tokens

        # Generic token-based fallback
        model_tokens = re.findall(r"[a-z0-9]+", model_lower)
        return all(t in tokens for t in model_tokens if t not in {"gemini"})

    def _model_target_selector(self, model_name: str) -> Optional[str]:
        mapping = {
            "gemini-3.1-flash-lite": 'gem-menu-item[data-test-id="bard-mode-option-cf41b0e0dd7d53e5"]',
            "3.1-flash-lite": 'gem-menu-item[data-test-id="bard-mode-option-cf41b0e0dd7d53e5"]',
            
            "gemini-3.5-flash": 'gem-menu-item[data-test-id="bard-mode-option-fbb127bbb056c959"]',
            "3.5-flash": 'gem-menu-item[data-test-id="bard-mode-option-fbb127bbb056c959"]',
            
            "gemini-3.1-pro": 'gem-menu-item[data-test-id="bard-mode-option-9d8ca3786ebdfbea"]',
            "3.1-pro": 'gem-menu-item[data-test-id="bard-mode-option-9d8ca3786ebdfbea"]',
        }
        return mapping.get((model_name or "").strip().lower())

    async def _get_current_ui_model_and_thinking(self) -> Tuple[Optional[str], Optional[str]]:
        """Read the model button text to detect currently active model and thinking level in the UI."""
        try:
            model_selector = await self._resolve_selector("model_btn", require_visible=True, timeout_ms=1500)
            if not model_selector:
                return None, None
            
            btn = self.page.locator(model_selector).first
            text = (await btn.inner_text()).strip().lower()
            if not text:
                return None, None

            # Detect thinking level
            ui_thinking = "Extended" if "extended" in text else "Standard"

            # Detect model
            if "lite" in text:
                ui_model = "gemini-3.1-flash-lite"
            elif "pro" in text:
                ui_model = "gemini-3.1-pro"
            elif "flash" in text:
                ui_model = "gemini-3.5-flash"
            else:
                ui_model = None

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
                    const newThinkingVisible = !!(thinkingOverlay && isVisible(thinkingOverlay));
                    const newThinkingActive = newThinkingVisible && !!(
                        thinkingOverlay.querySelector('thinking-dots-animation, .thinking-dots-animation, .thinking-container')
                    );
                    const newThinkingLabel = thinkingOverlay ? (thinkingOverlay.innerText || '').trim().slice(0, 120) : '';

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

    async def _hard_refresh_and_reinit(self, reason: str, save_diag: bool = False) -> bool:
        """Hard refresh page and re-run worker init path.

        If save_diag=True, save screenshot + DOM extract BEFORE reloading so we
        capture the failing state rather than the post-reload greeting screen.
        """
        try:
            if save_diag and not self._diag_saved_pre_refresh:
                await self._save_diagnostic_artifacts(reason)
                self._diag_saved_pre_refresh = True
            log(f"Hard refresh recovery ({reason})", f"Worker {self.worker_id}")
            await self._force_reload(timeout_ms=30000)
            await self._human_delay(1000, 1500)
            self._request_count = 0

            ok = await self.init_with_page(self.page, self.context)
            post_snapshot = await self._capture_state_snapshot()
            clean = bool(
                ok
                and not post_snapshot.get("stop_visible")
                and not post_snapshot.get("error_page_500")
                and post_snapshot.get("input_visible")
            )
            self._last_recovery = {
                "reason": reason,
                "ok": clean,
                "stop_visible": bool(post_snapshot.get("stop_visible")),
                "input_text_len": int(post_snapshot.get("input_text_len") or 0),
                "user_query_count": int(post_snapshot.get("user_query_count") or 0),
                "response_count": int(post_snapshot.get("response_count") or 0),
                "page_title": str(post_snapshot.get("page_title") or ""),
                "at_unix": int(time.time()),
            }
            log(
                f"Hard refresh recovery result: ok={clean} init={bool(ok)} "
                f"stop={post_snapshot.get('stop_visible')} input_len={post_snapshot.get('input_text_len')} "
                f"users={post_snapshot.get('user_query_count')} responses={post_snapshot.get('response_count')}",
                f"Worker {self.worker_id}",
            )
            if not clean:
                self._initialized = False
            return clean
        except Exception as e:
            if self._is_network_outage_error_text(str(e)):
                current_url = ""
                try:
                    current_url = self.page.url or ""
                except Exception:
                    current_url = ""
                self._mark_network_outage("page_reload", current_url or self.URL, str(e))
            self._last_recovery = {
                "reason": reason,
                "ok": False,
                "error": str(e),
                "at_unix": int(time.time()),
            }
            self._initialized = False
            log(f"Hard refresh recovery failed: {e}", f"Worker {self.worker_id}")
            return False

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
    def _is_temp_mode_page(snapshot: Optional[Dict[str, Any]]) -> bool:
        snap = snapshot or {}
        return bool(
            not snap.get("error_page_500")
            and (
                snap.get("temp_chat_active")
                or "temporary" in str(snap.get("input_placeholder") or "").lower()
            )
        )

    async def _wait_for_temp_page_mode(self, should_be_temp: bool, timeout_seconds: float) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            snap = await self._capture_state_snapshot()
            if self._is_fresh_temp_chat_ready(snap):
                return should_be_temp

            transition_state = bool(snap.get("transition_state"))
            current_temp = self._is_temp_mode_page(snap)
            if not transition_state:
                if should_be_temp and current_temp:
                    return True
                if (not should_be_temp) and (not current_temp) and bool(snap.get("input_visible")):
                    return True

            await asyncio.sleep(0.2)
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
        try:
            await self.page.bring_to_front()
        except:
            pass

        try:
            await self.page.keyboard.press("Control+Shift+KeyO")
            await self._human_delay(250, 400)
            return True
        except Exception:
            try:
                await self.page.keyboard.press("Control+Shift+O")
                await self._human_delay(250, 400)
                return True
            except:
                return False

    async def _ensure_fresh_chat(self) -> bool:
        """Ensure the worker is on a genuinely fresh Gemini chat before sending."""
        baseline = await self._capture_state_snapshot()
        baseline_state = self._classify_new_chat_state(baseline)
        if baseline_state == "confirmed_cleared":
            return True

        for attempt in range(2):
            if not await self._trigger_new_chat_shortcut():
                break

            deadline = time.time() + 4.0
            last_state = baseline_state
            while time.time() < deadline:
                snap = await self._capture_state_snapshot()
                state = self._classify_new_chat_state(snap)
                last_state = state
                if state == "confirmed_cleared":
                    if attempt > 0:
                        log(f"New Chat confirmed after shortcut attempt {attempt + 1}", f"Worker {self.worker_id}")
                    return True
                await asyncio.sleep(0.2)

            log(
                f"⚠️ New Chat shortcut attempt {attempt + 1}: state remained {last_state}",
                f"Worker {self.worker_id}",
            )

        new_chat_selectors = self._selector_candidates("new_chat")
        if not new_chat_selectors:
            return False

        click_attempts = 0
        max_click_attempts = 2
        last_state = baseline_state

        while click_attempts < max_click_attempts:
            clicked = False
            for selector in new_chat_selectors:
                try:
                    locator = self.page.locator(selector).first
                    if await locator.count() == 0:
                        continue
                    if not await locator.is_visible():
                        continue
                    try:
                        await locator.click(timeout=1500)
                    except Exception:
                        safe_selector = selector.replace("'", "\\'")
                        await self.page.evaluate(
                            f"""
                            () => {{
                                const el = document.querySelector('{safe_selector}');
                                if (el) el.click();
                            }}
                            """
                        )
                    clicked = True
                    break
                except:
                    continue

            if not clicked:
                break

            click_attempts += 1
            deadline = time.time() + 4.0
            while time.time() < deadline:
                snap = await self._capture_state_snapshot()
                state = self._classify_new_chat_state(snap)
                last_state = state
                if state == "confirmed_cleared":
                    if click_attempts > 1:
                        log(f"New Chat confirmed after {click_attempts} attempts", f"Worker {self.worker_id}")
                    return True
                await asyncio.sleep(0.2)

            log(
                f"⚠️ New Chat attempt {click_attempts}: state remained {last_state}",
                f"Worker {self.worker_id}",
            )

        final_snapshot = await self._capture_state_snapshot()
        final_state = self._classify_new_chat_state(final_snapshot)
        if final_state == "confirmed_cleared":
            return True

        log(f"⚠️ New Chat reset not confirmed ({final_state})", f"Worker {self.worker_id}")
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

        # The browser is in a broken post-refresh state (greeting screen but temp-chat
        # toggle not responding).  Do a fresh full navigate rather than just sitting here
        # — this is what causes the 21-instances-on-greeting-screen pileup.
        log("Temp chat broken post-refresh: navigating fresh to recover", f"Worker {self.worker_id}")
        try:
            await self.page.goto(self.URL, wait_until="domcontentloaded", timeout=30000)
            await self._human_delay(1500, 2500)
            reinit_ok = await self.init_with_page(self.page, self.context)
            if reinit_ok:
                # One more attempt after clean navigate
                temp_btn2 = await self._get_temp_chat_button()
                if temp_btn2 and await self._click_temp_chat_toggle():
                    deadline2 = time.time() + 8.0
                    while time.time() < deadline2:
                        snap2 = await self._capture_state_snapshot()
                        if self._is_fresh_temp_chat_ready(snap2):
                            log("Temp reset path: recovered via fresh navigate", f"Worker {self.worker_id}")
                            return True
                        await asyncio.sleep(0.2)
            log("⚠️ Temp chat still broken after fresh navigate — worker needs recreation", f"Worker {self.worker_id}")
        except Exception as nav_e:
            log(f"Temp chat fresh-navigate recovery failed: {nav_e}", f"Worker {self.worker_id}")
        return False

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

    async def _screenshot_on_failure(self, action_name: str):
        """
        Capture screenshot when an action fails (only if DEBUG_SCREENSHOTS=true).
        Uses JPEG quality 50 for minimal disk/CPU impact.
        """
        if not DEBUG_SCREENSHOTS:
            return
        
        try:
            # Create dir if needed
            os.makedirs(DEBUG_SCREENSHOT_DIR, exist_ok=True)
            
            # Clean filename
            safe_action = action_name.replace(" ", "_").lower()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            req_id = self._request_id[:8] if self._request_id else "unknown"
            filename = f"{timestamp}_{req_id}_{safe_action}.jpg"
            filepath = os.path.join(DEBUG_SCREENSHOT_DIR, filename)
            
            # Capture low-quality JPEG (fast, small)
            await self.page.screenshot(path=filepath, type="jpeg", quality=50)
            print(f"[GeminiWeb] 📸 Screenshot: {filename}")
            
            # Limit to 20 screenshots max (delete oldest)
            files = sorted(
                [f for f in os.listdir(DEBUG_SCREENSHOT_DIR) if f.endswith('.jpg')],
                key=lambda x: os.path.getmtime(os.path.join(DEBUG_SCREENSHOT_DIR, x))
            )
            while len(files) > 20:
                oldest = files.pop(0)
                os.remove(os.path.join(DEBUG_SCREENSHOT_DIR, oldest))
        except Exception as e:
            print(f"[GeminiWeb] ⚠️ Screenshot failed: {e}")

    async def _verified_click(
        self, 
        selector: str, 
        description: str,
        verify_before: callable = None,  # async () -> any (state before click)
        verify_after: callable = None,   # async (before_state) -> bool (True = success)
        timeout: int = 3000,
        max_retries: int = 2  # Retry up to 2 times on failure
    ) -> bool:
        """
        Click an element with retry logic, JS fallback, and verification.
        """
        for attempt in range(max_retries + 1):
            try:
                locator = self.page.locator(selector).first
                
                # Wait for visibility
                try:
                    await locator.wait_for(state="visible", timeout=timeout)
                except Exception:
                    if attempt == max_retries:
                        log(f"❌ {description}: not visible", f"Worker {self.worker_id}")
                        await self._screenshot_on_failure(f"{description}_not_visible")
                        return False
                    await self._human_delay(500, 1000)
                    continue
                
                # Scroll into view
                try:
                    await locator.scroll_into_view_if_needed(timeout=2000)
                    await self._human_delay(100, 200)
                except:
                    pass
                
                # Capture state before click
                before_state = None
                if verify_before:
                    try:
                        before_state = await verify_before()
                    except:
                        pass
                
                # Try Playwright click first
                click_succeeded = False
                try:
                    await locator.click(timeout=timeout)
                    await self._human_delay(300, 500)
                    click_succeeded = True
                except Exception:
                    # Fallback to JavaScript click
                    try:
                        safe_selector = selector.replace("'", "\\'")
                        await self.page.evaluate(f'''
                            () => {{
                                const el = document.querySelector('{safe_selector}');
                                if (el) {{ el.scrollIntoView(); el.click(); }}
                            }}
                        ''')
                        await self._human_delay(300, 500)
                        click_succeeded = True
                    except:
                        pass
                
                if not click_succeeded:
                    if attempt < max_retries:
                        await self._human_delay(500 * (attempt + 1), 1000 * (attempt + 1))
                        continue
                    else:
                        log(f"❌ {description}: click failed", f"Worker {self.worker_id}")
                        await self._screenshot_on_failure(f"{description}_click_failed")
                        return False
                
                # Verify state after click
                if verify_after:
                    try:
                        success = await verify_after(before_state)
                        if success:
                            return True
                        else:
                            if attempt < max_retries:
                                await self._human_delay(500 * (attempt + 1), 1000 * (attempt + 1))
                                continue
                            else:
                                log(f"⚠️ {description}: state change not confirmed", f"Worker {self.worker_id}")
                                await self._screenshot_on_failure(f"{description}_verify_failed")
                                return False
                    except Exception as e:
                        log(f"⚠️ {description}: verify error: {e}", f"Worker {self.worker_id}")
                        return False
                else:
                    # No verification provided, assume success
                    return True
                    
            except Exception as e:
                if attempt < max_retries:
                    await self._human_delay(500 * (attempt + 1), 1000 * (attempt + 1))
                else:
                    log(f"❌ {description}: failed: {e}", f"Worker {self.worker_id}")
                    await self._screenshot_on_failure(f"{description}_error")
                    return False
        
        return False

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
                    
                    await self._reset_browser_zoom(force=True)
                    await self._apply_headed_zoom(force=True)
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
            print(f"[GeminiWeb] ❌ Init failed: {e}")
            self._track_error(str(e), "input", "init")
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

        try:
            self._thinking_requested = bool(thinking_level and thinking_level.lower() in {"extended", "high", "deep"})
            self._generation_in_progress = True
            self._request_count += 1
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

            copy_selector = await self._resolve_selector("copy_btn")
            if not copy_selector:
                err = "Copy selector not found"
                self._track_error(err, "copy_btn", "send_message")
                return {"success": False, "error": err}

            # Preflight: if previous run left tab in Stop state, recover before sending
            preflight = await self._capture_state_snapshot()
            if preflight.get("error_page_500"):
                log("Preflight detected Google 500 page, refreshing", f"Worker {self.worker_id}")
                recovered = await self._recover_from_google_500_page("preflight_error_page_500")
                if not recovered:
                    self._track_error("Preflight 500-page recovery failed", "input", "preflight_recovery", preflight)
                    return {"success": False, "error": "Preflight 500-page recovery failed"}
                init_ok = await self.init_with_page(self.page, self.context)
                if not init_ok:
                    self._track_error("Preflight 500-page re-init failed", "input", "preflight_recovery", preflight)
                    return {"success": False, "error": "Preflight 500-page re-init failed"}
                copy_selector = await self._resolve_selector("copy_btn")
                if not copy_selector:
                    err = "Copy selector not found after 500-page recovery"
                    self._track_error(err, "copy_btn", "send_message")
                    return {"success": False, "error": err}

            if preflight.get("stop_visible", False):
                log("Preflight detected stale stop state, refreshing", f"Worker {self.worker_id}")
                recovered = await self._hard_refresh_and_reinit("preflight_stop_visible")
                if not recovered:
                    self._track_error("Preflight recovery failed", "send_btn", "preflight_recovery", preflight)
                    return {"success": False, "error": "Preflight recovery failed"}

                copy_selector = await self._resolve_selector("copy_btn")
                if not copy_selector:
                    err = "Copy selector not found after recovery"
                    self._track_error(err, "copy_btn", "send_message")
                    return {"success": False, "error": err}

            # In headed mode, explicitly foreground the active worker page before send.
            try:
                await self.page.bring_to_front()
                await self._reset_browser_zoom()
                await self._apply_headed_zoom()
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
            
            # 0.5 Periodic hard refresh to clear browser cache/memory
            if self._request_count >= self.REFRESH_EVERY_N_REQUESTS:
                log(f"Hard refresh (request #{self._request_count})", f"Worker {self.worker_id}")
                await self._force_reload(timeout_ms=30000)
                self._reset_model_tracking()
                await self._human_delay(1000, 1500)
                self._request_count = 0
            
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
                    await self._select_model(model)
                    self._current_selected_model = model
                    # Re-read UI state since selecting a model changes the layout / resets defaults
                    ui_model, ui_thinking = await self._get_current_ui_model_and_thinking()
                    if ui_thinking:
                        self._current_selected_thinking_level = ui_thinking

                # Select thinking level if it differs
                target_thinking = thinking_level or "Standard"
                if target_thinking and self._current_selected_thinking_level != target_thinking:
                    await self._set_thinking_level(target_thinking)
                    self._current_selected_thinking_level = target_thinking

            # 3. Enter Prompt
            input_selector = await self._resolve_selector("input", require_visible=True, timeout_ms=2000)
            if not input_selector:
                err = "Input selector not found"
                self._track_error(err, "input", "send_message")
                return {"success": False, "error": err}
            input_area = self.page.locator(input_selector)
            await input_area.click()
            await self._human_delay()
            
            # 3.5 Paste Images (if provided)
            if images:
                for img_path in images:
                    await self._paste_image(img_path)
                    await self._human_delay(200, 500)
            
            await input_area.fill(prompt)
            await self._human_delay(300, 600)

            # Capture state BEFORE sending. These baselines drive all later
            # "did generation start?" checks; taking them after a send attempt
            # can make a real in-flight request look unsent.
            pre_send_snapshot = await self._capture_state_snapshot()
            pre_send_count = await self.page.locator(copy_selector).count()
            pre_send_resp_count = int(pre_send_snapshot.get("response_count") or 0)
            pre_send_resp_len = int(pre_send_snapshot.get("last_response_len") or 0)
            pre_send_resp_sig = str(pre_send_snapshot.get("last_response_signature") or "")
            pre_send_user_query_count = int(pre_send_snapshot.get("user_query_count") or 0)
            prompt_len = len((prompt or "").strip())

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
                log(f"❌ Send button click failed", f"Worker {self.worker_id}")
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
                log(f"⚠️ Soft retries failed, attempting hard refresh recovery", f"Worker {self.worker_id}")
                break
            
            # Fallback: hard refresh and retry once more if generation never started
            if not generation_started:
                try:
                    recovered = await self._hard_refresh_and_reinit("send_not_started")
                    if not recovered:
                        log(f"❌ Re-init failed after hard refresh", f"Worker {self.worker_id}")
                        snapshot = await self._capture_state_snapshot()
                        outage = self._get_active_network_outage()
                        if outage:
                            outage_error, outage_diag = await self._build_network_outage_error()
                            snapshot.update(outage_diag)
                            self._track_error(outage_error, "init", "send_message", snapshot)
                            return {"success": False, "error": outage_error}
                        self._track_error("Re-init failed after hard refresh", "init", "send_message", snapshot)
                        return {"success": False, "error": "Worker re-init failed after hard refresh"}
                    
                    # Re-send the prompt
                    log(f"🔄 Retrying send after recovery", f"Worker {self.worker_id}")
                    input_selector = await self._resolve_selector("input", require_visible=True, timeout_ms=2000)
                    recovery_send_ok = False
                    if input_selector:
                        input_area = self.page.locator(input_selector)
                        await input_area.click()
                        await self._human_delay()
                        await input_area.fill(prompt)
                        await self._human_delay(300, 600)
                        recovery_send_ok = await attempt_send_submission(
                            "hard_refresh_recovery",
                            await get_input_text(),
                        )
                    if not recovery_send_ok:
                        snapshot = await self._capture_state_snapshot()
                        self._track_error("Recovery resend failed", "send_btn", "verify_generation_started", snapshot)
                        return {"success": False, "error": "Generation failed to start after hard refresh recovery"}
                    
                    # Wait and check if it worked
                    await self._human_delay(2000, 3000)
                    snap = await self._capture_state_snapshot()
                    if snap.get("stop_visible", False) or snap.get("response_count", 0) > pre_send_resp_count:
                        log(f"✅ Generation started after hard refresh recovery", f"Worker {self.worker_id}")
                        generation_started = True
                    else:
                        log(f"❌ Generation failed after hard refresh", f"Worker {self.worker_id}")
                        snapshot = await self._capture_state_snapshot()
                        self._track_error("Generation failed after hard refresh", "send_btn", "verify_generation_started", snapshot)
                        return {"success": False, "error": "Generation failed to start after hard refresh recovery"}
                        
                except Exception as e:
                    log(f"❌ Hard refresh recovery failed: {e}", f"Worker {self.worker_id}")
                    snapshot = await self._capture_state_snapshot()
                    outage = self._get_active_network_outage()
                    if outage:
                        outage_error, outage_diag = await self._build_network_outage_error()
                        snapshot.update(outage_diag)
                        self._track_error(outage_error, "send_btn", "verify_generation_started", snapshot)
                        return {"success": False, "error": outage_error}
                    self._track_error("Hard refresh recovery failed", "send_btn", "verify_generation_started", snapshot)
                    return {"success": False, "error": f"Generation failed after all recovery attempts: {e}"}
            
            # Gemini can accept the request (user bubble + Stop state) while
            # retaining the full prompt as an editable draft. Clear only after
            # start was proven so retries and diagnostics cannot mistake it for
            # an unsent request.
            if generation_started:
                await self._clear_retained_prompt_draft(prompt_len)

            # 5. Wait for Response (Copy button to appear)
            log(f"Waiting for response...", f"Worker {self.worker_id}")
            await self._human_delay(300, 600)  # Reduced initial wait
            
            # Polling for copy button (Wait until we have MORE buttons than before)
            start_time = time.time()
            max_wait = int(os.getenv("BROWSER_TIMEOUT", "480"))
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
                        if c_count > pre_send_count:
                            last_btn = c_btns.nth(c_count - 1)
                            if await last_btn.is_visible():
                                is_done = True
                        return shot, c_count, is_done

                    page_snapshot, current_count, done_signaled = await asyncio.wait_for(
                        gather_page_state(),
                        timeout=15.0
                    )
                except asyncio.TimeoutError:
                    log("⚠️ Page evaluation hung/timed out inside wait loop - triggering hard refresh recovery", f"Worker {self.worker_id}")
                    self._track_error("Page evaluation hung in wait loop", "copy_btn", "wait_for_response_hung")
                    await self._hard_refresh_and_reinit("wait_loop_hung", save_diag=False)
                    return {"success": False, "error": "Page evaluation hung in wait loop"}
                except Exception as eval_err:
                    log(f"⚠️ Page evaluation error in wait loop: {eval_err}", f"Worker {self.worker_id}")
                    await asyncio.sleep(0.5)
                    continue

                if page_snapshot.get("error_page_500"):
                    log("⚠️ Google 500 error page detected during response wait", f"Worker {self.worker_id}")
                    self._track_error("Google 500 error page", "input", "wait_for_response_500_page", page_snapshot)
                    await self._hard_refresh_and_reinit("google_500_page")
                    return {"success": False, "error": "Google 500 error page"}

                if done_signaled:
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
                    # Extended thinking models (Gemini 3.1 Flash-Lite / 3.5 Flash) take up to 120s to complete prefill/reasoning.
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
                        # save_diag=True: capture screenshot + DOM extract BEFORE reload
                        # so we see the stalled page, not the greeting screen after refresh.
                        await self._hard_refresh_and_reinit("stalled_generation", save_diag=True)
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
                await self._hard_refresh_and_reinit("copy_timeout", save_diag=True)
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
                log(f"⚠️ Clipboard empty after copy", f"Worker {self.worker_id}")
                snapshot = await self._capture_state_snapshot()
                self._track_error("Clipboard empty", "copy_btn", "extract_response", snapshot)
                await self._hard_refresh_and_reinit("clipboard_empty", save_diag=True)
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
            
            # Force refresh to reset page state for next request
            try:
                await self._force_reload(timeout_ms=15000)
                self._request_count = 0
            except:
                pass
            
            return {"success": False, "error": str(e)}
        finally:
            # Only save diagnostics here if we didn't already save pre-refresh above.
            # If we saved pre-refresh, the page is now the greeting screen and saving
            # again would just overwrite with useless data.
            if not self._last_request_success and not self._diag_saved_pre_refresh:
                try:
                    await self._save_diagnostic_artifacts("failed")
                except:
                    pass
            self._diag_saved_pre_refresh = False
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
                try:
                    await notify_error(
                        error=f"Model picker button not found: selector 'model_btn' failed for requested model '{model_name}'.",
                        selector_key="model_btn",
                        action="select_model_missing_picker",
                        worker_id=self.worker_id,
                        diagnostics={"requested_model": model_name}
                    )
                except Exception as ne:
                    log(f"Failed to send model picker missing notification: {ne}", f"Worker {self.worker_id}")
                return

            btn = self.page.locator(model_selector)
            current = await btn.inner_text()
            if self._matches_model(current, model_name):
                return
            
            await btn.click()
            await self._human_delay(300, 450)

            target_selector = self._model_target_selector(model_name)
            if target_selector:
                try:
                    target = self.page.locator(target_selector).first
                    await target.wait_for(state="visible", timeout=1500)
                    await target.click(timeout=1500)
                    print(f"[Worker {self.worker_id}] ✅ Selected model: {model_name}")
                    await self._human_delay(300, 500)
                    return
                except Exception:
                    pass
            
            # Select from menu
            menu_item_selector = await self._resolve_selector("menu_item")
            if not menu_item_selector:
                self._track_error("Model menu item selector missing", "menu_item", "select_model")
                try:
                    await notify_error(
                        error=f"Model menu item selector missing: selector 'menu_item' failed for requested model '{model_name}'.",
                        selector_key="menu_item",
                        action="select_model_missing_menu_item",
                        worker_id=self.worker_id,
                        diagnostics={"requested_model": model_name}
                    )
                except Exception as ne:
                    log(f"Failed to send model menu item missing notification: {ne}", f"Worker {self.worker_id}")
                # Close the picker
                await self.page.keyboard.press("Escape")
                return

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
                    return

            # Wording change fallback (Index-based selection: lite = 0, flash = 1, pro = 2)
            index_map = {
                "gemini-3.1-flash-lite": 0,
                "gemini-3.5-flash": 1,
                "gemini-3.1-pro": 2,
            }
            target_index = index_map.get(model_name.strip().lower())
            
            if target_index is not None and item_count > target_index:
                item = items.nth(target_index)
                text_fallback = await item.inner_text()
                await item.click()
                print(f"[Worker {self.worker_id}] ⚠️ Model text matching failed for {model_name}. Fell back to index {target_index} ({text_fallback.strip()})")
                
                # Send a non-fatal Discord warning notification about the wording change
                try:
                    await notify_error(
                        error=f"Model name/description changed: text matching failed for '{model_name}'. Fell back to index {target_index} ('{text_fallback.strip()}').",
                        selector_key="model_btn",
                        action="matches_model_fallback",
                        worker_id=self.worker_id,
                        diagnostics={
                            "requested_model": model_name,
                            "fallback_index": target_index,
                            "fallback_text": text_fallback,
                            "total_items": item_count
                        }
                    )
                except Exception as ne:
                    log(f"Failed to send model name change notification: {ne}", f"Worker {self.worker_id}")
                
                await self._human_delay(300, 600)
                return

            # If not found at all, close menu
            await self.page.keyboard.press("Escape")
            await self._human_delay(100, 300)
            
            # Since both text-based matching and index-based fallback failed, this is a big structural change!
            # Notify the user on Discord immediately, but do NOT crash/throw. Just print/log and proceed.
            try:
                await notify_error(
                    error=f"CRITICAL MODEL CHANGE: Could not find or select model '{model_name}'. Wording matching failed and index fallback failed (total menu items: {item_count}). Proceeding with current/default model.",
                    selector_key="model_btn",
                    action="select_model_failed_completely",
                    worker_id=self.worker_id,
                    diagnostics={
                        "requested_model": model_name,
                        "menu_item_count": item_count,
                    }
                )
            except Exception as ne:
                log(f"Failed to send critical model selection failure notification: {ne}", f"Worker {self.worker_id}")

        except Exception as e:
            print(f"[Worker {self.worker_id}] ⚠️ Model selection failed: {e}")
            self._track_error(str(e), "model_btn", "select_model")
            try:
                await notify_error(
                    error=f"Model selection exception: {e}",
                    selector_key="model_btn",
                    action="select_model_exception",
                    worker_id=self.worker_id,
                    diagnostics={"requested_model": model_name, "error": str(e)}
                )
            except Exception as ne:
                log(f"Failed to send model selection exception notification: {ne}", f"Worker {self.worker_id}")
            # Ensure menu is closed
            try:
                await self.page.keyboard.press("Escape")
                await self._human_delay(100, 200)
            except:
                pass

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
                try:
                    await notify_error(
                        error=f"Model picker button not found for thinking level: selector 'model_btn' failed for level '{level}'.",
                        selector_key="model_btn",
                        action="set_thinking_level_missing_picker",
                        worker_id=self.worker_id,
                        diagnostics={"level": level, "target_text": target_text}
                    )
                except Exception as ne:
                    log(f"Failed to send thinking level picker missing notification: {ne}", f"Worker {self.worker_id}")
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

            # Legacy sub-menu thinking level selection fallback
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
            except Exception as te:
                log(f"Thinking level text match failed for '{target_text}', attempting index fallback: {te}", f"Worker {self.worker_id}")

            # Index-based fallback (Standard = 0 (top), Extended = 1 (bottom))
            submenu_pane = self.page.locator('div[role="menu"], .mat-mdc-menu-panel, [class*="menu-panel"]').last
            submenu_items = submenu_pane.locator('gem-menu-item, [role="menuitem"]')
            item_count = await submenu_items.count()
            
            target_index = 0 if target_text == "Standard" else 1
            if item_count > target_index:
                option = submenu_items.nth(target_index)
                fallback_text = await option.inner_text()
                await option.click(timeout=1500)
                log(f"⚠️ Selected thinking level via fallback index {target_index} ({fallback_text.strip()})", f"Worker {self.worker_id}")
                
                # Send a non-fatal Discord warning notification about the wording change
                try:
                    await notify_error(
                        error=f"Thinking level name/description changed: text matching failed for '{target_text}'. Fell back to index {target_index} ('{fallback_text.strip()}').",
                        selector_key="thinking_level",
                        action="set_thinking_level_fallback",
                        worker_id=self.worker_id,
                        diagnostics={
                            "requested_level": level,
                            "target_text": target_text,
                            "fallback_index": target_index,
                            "fallback_text": fallback_text,
                            "total_items": item_count
                        }
                    )
                except Exception as ne:
                    log(f"Failed to send thinking level change notification: {ne}", f"Worker {self.worker_id}")
                
                await self._human_delay(250, 400)
                return
            
            raise Exception(f"No submenu option found for thinking level {target_text} (count={item_count})")
        except Exception as e:
            log(f"Thinking level selection failed: {e}", f"Worker {self.worker_id}")
            self._track_error(str(e), "thinking_level", "set_thinking_level")
            # Send immediate Discord alert for thinking level failure
            try:
                await notify_error(
                    error=f"CRITICAL THINKING LEVEL CHANGE: Could not select thinking level '{target_text}'. Wording matching failed and index fallback failed (total menu items: {item_count if 'item_count' in locals() else 'unknown'}). Proceeding with default/current thinking level.",
                    selector_key="thinking_level",
                    action="set_thinking_level_failed_completely",
                    worker_id=self.worker_id,
                    diagnostics={"requested_level": level, "target_text": target_text, "error": str(e)}
                )
            except Exception as ne:
                log(f"Failed to send thinking level failure notification: {ne}", f"Worker {self.worker_id}")
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

    async def close(self):
        await super().close()

class WorkerPool:
    """Multi-worker pool for Gemini Web with round-robin dispatch."""
    
    # Supported models (all route to Gemini Web)
    SUPPORTED_MODELS = ["thinking", "pro", "fast", "flash"]
    
    def __init__(self, worker_count: int = 1, provider: str = "auto"):
        self.worker_count = max(1, worker_count)  # At least 1 worker
        self.provider = provider.lower()
        self.browser_channel = os.getenv("BROWSER_CHANNEL", "").strip()
        self.headed_split_windows = os.getenv("HEADED_SPLIT_WINDOWS", "true").lower() == "true"
        self.headed_window_layout = os.getenv("HEADED_WINDOW_LAYOUT", "overlap").strip().lower()
        self.headed_screen_width = max(800, int(os.getenv("HEADED_SCREEN_WIDTH", "1920")))
        self.headed_screen_height = max(600, int(os.getenv("HEADED_SCREEN_HEIGHT", "1080")))
        self.headed_screen_left = int(os.getenv("HEADED_SCREEN_LEFT", "0"))
        self.headed_screen_top = int(os.getenv("HEADED_SCREEN_TOP", "0"))
        self.headed_window_width = max(900, int(os.getenv("HEADED_WINDOW_WIDTH", "1440")))
        self.headed_window_height = max(700, int(os.getenv("HEADED_WINDOW_HEIGHT", "900")))
        self.headed_window_offset_x = int(os.getenv("HEADED_WINDOW_OFFSET_X", "70"))
        self.headed_window_offset_y = int(os.getenv("HEADED_WINDOW_OFFSET_Y", "45"))
        
        # Array of Gemini Web workers (1 worker = 1 tab)
        self.workers: List[GeminiWebAutomation] = []
        self._worker_busy: List[bool] = []  # Track which workers are busy
        self._worker_busy_since: List[Optional[float]] = []
        self._worker_lock = asyncio.Lock()  # Lock for worker assignment
        self._next_worker_index = 0
        self._worker_stall_failures: Dict[int, int] = {}
        
        self.shared_context = None
        self.playwright = None
        self._initialized = False
        
        self._browser_semaphore = asyncio.Semaphore(worker_count)  # Allow N concurrent requests
        self._last_activity = time.time()
        self._active_requests = 0
        self._has_processed_request = False
        self._idle_refresh_lock = asyncio.Lock()
        self._pool_recovery_lock = asyncio.Lock()
        self._last_idle_refresh_at = 0.0
        self._startup_pages_closed = 0
        self._startup_page_close_failures = 0
        self._worker_recreation_count = 0
        self._last_worker_recreation: Optional[Dict[str, Any]] = None
        self._startup_guard_page: Optional[Page] = None
    
    async def _get_available_worker(self, exclude: set = None) -> Tuple[int, GeminiWebAutomation]:
        """Get available worker via fair round-robin. Waits if all are busy."""
        if exclude is None:
            exclude = set()
            
        start_time = time.time()
        max_wait = 60
        
        while time.time() - start_time < max_wait:
            await self._clear_stale_busy_flags_if_safe()
            async with self._worker_lock:
                count = len(self._worker_busy)
                for offset in range(count):
                    i = (self._next_worker_index + offset) % count
                    if i in exclude:
                        continue
                    if (not self._worker_busy[i]) and self.workers[i]._initialized:
                        self._worker_busy[i] = True
                        self._worker_busy_since[i] = time.time()
                        self._next_worker_index = (i + 1) % count
                        return i, self.workers[i]
            await asyncio.sleep(0.5)
        
        log(f"⚠️ Worker assignment timeout", "WorkerPool")
        return None, None
    
    async def _release_worker(self, index: int):
        """Mark worker as available."""
        async with self._worker_lock:
            if 0 <= index < len(self._worker_busy):
                self._worker_busy[index] = False
                if index < len(self._worker_busy_since):
                    self._worker_busy_since[index] = None

    async def _set_all_workers_busy(self, busy: bool):
        async with self._worker_lock:
            now = time.time() if busy else None
            for i in range(len(self._worker_busy)):
                self._worker_busy[i] = busy
                if i < len(self._worker_busy_since):
                    self._worker_busy_since[i] = now

    async def _clear_stale_busy_flags_if_safe(self) -> bool:
        """Recover from cancelled maintenance/request paths that left workers marked busy."""
        if self._active_requests != 0:
            return False

        now = time.time()
        cleared = []
        async with self._worker_lock:
            for i, busy in enumerate(self._worker_busy):
                if not busy:
                    continue
                busy_since = self._worker_busy_since[i] if i < len(self._worker_busy_since) else None
                if busy_since and (now - busy_since) >= STALE_BUSY_WITHOUT_ACTIVE_SECONDS:
                    self._worker_busy[i] = False
                    if i < len(self._worker_busy_since):
                        self._worker_busy_since[i] = None
                    cleared.append(i + 1)

        if cleared:
            log(f"Cleared stale busy worker flags with no active requests: {cleared}", "WorkerPool")
            return True
        return False

    @staticmethod
    def _is_dead_page_error(error: str) -> bool:
        """True when the Playwright page/context/browser has been destroyed.
        These errors are unrecoverable on the current worker — recreation is the only fix.
        """
        text = (error or "").strip().lower()
        if not text:
            return False
        keywords = (
            "target page, context or browser has been closed",
            "page has been closed",
            "browser has been closed",
            "context has been closed",
            "target closed",
        )
        return any(k in text for k in keywords)

    @staticmethod
    def _is_stall_class_error(error: str) -> bool:
        text = (error or "").strip().lower()
        if not text:
            return False

        keywords = (
            "stalled generation",
            "unsent stuck",
            "failed to start",
            "copy timeout",
            "waiting for response",
            "clipboard extraction failed",
            "fresh temp chat reset not confirmed",
            "temp chat reset not confirmed",
            "fresh regular chat reset not confirmed",
            "preflight recovery failed",
            "re-init failed",
            "send button click failed",
            "recovery resend failed",
            "locator.fill",
            "timeout 30000ms exceeded",
            "empty response",
            # Dead-page errors count as stalls too (for threshold tracking)
            "target page, context or browser has been closed",
            "page has been closed",
            "browser has been closed",
            "context has been closed",
            "target closed",
        )
        return any(k in text for k in keywords)

    @staticmethod
    def _is_network_outage_error(error: str) -> bool:
        text = (error or "").strip().lower()
        if not text:
            return False
        return text.startswith("network outage:")

    async def _quarantine_worker(self, index: int, reason: str) -> None:
        """Make a failed-to-recreate worker impossible to assign again."""
        if index < 0 or index >= len(self.workers):
            return

        worker = self.workers[index]
        worker._initialized = False
        try:
            await asyncio.wait_for(worker.close(), timeout=10.0)
        except Exception as e:
            log(f"Quarantine close failed for worker {index + 1}: {e}", "WorkerPool")
        log(f"Worker {index + 1} quarantined after recreation failure: {reason}", "WorkerPool")

    def _record_worker_failure(self, worker_index: int, error: str) -> bool:
        """Track consecutive stall-like failures and request worker recreation when threshold is hit."""
        if worker_index is None or worker_index < 0:
            return False

        if self._is_stall_class_error(error):
            current = self._worker_stall_failures.get(worker_index, 0) + 1
            self._worker_stall_failures[worker_index] = current
            log(
                f"Worker {worker_index + 1} stall-class failure count={current}/{STALL_RECREATE_THRESHOLD}",
                "WorkerPool",
            )
            return current >= STALL_RECREATE_THRESHOLD

        self._worker_stall_failures[worker_index] = 0
        return False

    def _record_worker_success(self, worker_index: int):
        if worker_index is None or worker_index < 0:
            return
        self._worker_stall_failures[worker_index] = 0

    async def _recreate_worker(self, index: int, reason: str) -> bool:
        """Replace a poisoned worker with a fresh page/window and re-init it."""
        if index < 0 or index >= len(self.workers):
            return False
        if not self.shared_context:
            return False

        split_windows_active = (
            os.getenv("HEADLESS", "false").lower() != "true"
            and self.headed_split_windows
            and self.worker_count >= 2
        )

        log(f"Recreating worker {index + 1} ({reason})", "WorkerPool")

        new_page: Optional[Page] = None
        try:
            if split_windows_active:
                new_page = await self._open_split_window_page(index)
            if new_page is None:
                new_page = await self.shared_context.new_page()
                if split_windows_active:
                    await self._position_page_window(new_page, index)

            await new_page.goto(GeminiWebAutomation.URL, timeout=60000, wait_until="domcontentloaded")

            new_worker = GeminiWebAutomation(worker_id=index + 1)
            ok = await new_worker.init_with_page(new_page, self.shared_context)
            if not ok:
                try:
                    await new_page.close()
                except:
                    pass
                log(f"Worker {index + 1} recreate init failed", "WorkerPool")
                return False

            old_worker = self.workers[index]
            self.workers[index] = new_worker
            self._worker_stall_failures[index] = 0

            try:
                if old_worker.page:
                    log("Closing poisoned worker page after recreation", "WorkerPool")
                await asyncio.wait_for(old_worker.close(), timeout=10.0)
            except Exception as e:
                log(f"Error closing poisoned worker page: {e}", "WorkerPool")

            self._worker_recreation_count += 1
            self._last_worker_recreation = {
                "worker_id": index + 1,
                "reason": reason,
                "ok": True,
                "at_unix": int(time.time()),
            }
            log(f"Worker {index + 1} recreated successfully", "WorkerPool")
            return True
        except Exception as e:
            self._last_worker_recreation = {
                "worker_id": index + 1,
                "reason": reason,
                "ok": False,
                "error": str(e),
                "at_unix": int(time.time()),
            }
            log(f"Worker {index + 1} recreate failed: {e}", "WorkerPool")
            if new_page:
                try:
                    await new_page.close()
                except:
                    pass
            return False

    def _are_workers_idle(self) -> bool:
        if self._active_requests != 0:
            return False
        return not any(self._worker_busy)

    async def _maybe_refresh_workers_after_idle(self):
        """Refresh all workers once after long idle, right before handling new work."""
        if not self._has_processed_request:
            return

        now = time.time()
        if (now - self._last_activity) < IDLE_REFRESH_AFTER_SECONDS:
            return
        if not self._are_workers_idle():
            return
        if self._idle_refresh_lock.locked():
            return

        async with self._idle_refresh_lock:
            # Re-check after lock acquisition to avoid races.
            now = time.time()
            if (now - self._last_activity) < IDLE_REFRESH_AFTER_SECONDS:
                return
            if not self._are_workers_idle():
                return

            idle_for = int(now - self._last_activity)
            log(f"Idle maintenance: refreshing all workers after {idle_for}s idle", "WorkerPool")

            await self._set_all_workers_busy(True)
            try:
                for i, worker in enumerate(self.workers):
                    if not worker or not worker._initialized:
                        continue
                    try:
                        ok = await asyncio.wait_for(
                            worker._hard_refresh_and_reinit("idle_maintenance"),
                            timeout=IDLE_REFRESH_WORKER_TIMEOUT_SECONDS,
                        )
                        if not ok:
                            log(f"Worker {i+1} idle refresh failed", "WorkerPool")
                    except asyncio.TimeoutError:
                        log(
                            f"Worker {i+1} idle refresh timed out after {IDLE_REFRESH_WORKER_TIMEOUT_SECONDS}s",
                            "WorkerPool",
                        )
                        break
                    except Exception as e:
                        log(f"Worker {i+1} idle refresh exception: {e}", "WorkerPool")
            finally:
                await self._set_all_workers_busy(False)

            self._last_idle_refresh_at = time.time()
            self._last_activity = self._last_idle_refresh_at

    async def _recover_after_assignment_timeout(self) -> bool:
        """Best-effort pool recovery when no worker can be assigned."""
        if self._pool_recovery_lock.locked():
            log("Pool recovery already running", "WorkerPool")
            return False

        async with self._pool_recovery_lock:
            await self._clear_stale_busy_flags_if_safe()
            if self._active_requests != 0:
                log(
                    f"Skipping assignment-timeout recovery; active_requests={self._active_requests}",
                    "WorkerPool",
                )
                return False

            log("Assignment timeout recovery: refreshing/recreating workers", "WorkerPool")
            await self._set_all_workers_busy(True)
            recovered = False
            try:
                for i, worker in enumerate(self.workers):
                    ok = False
                    try:
                        if worker and worker._initialized and worker.page and not worker.page.is_closed():
                            ok = await asyncio.wait_for(
                                worker._hard_refresh_and_reinit("assignment_timeout"),
                                timeout=POOL_RECOVERY_WORKER_TIMEOUT_SECONDS,
                            )
                    except asyncio.TimeoutError:
                        log(f"Worker {i+1} assignment-timeout refresh timed out", "WorkerPool")
                    except Exception as e:
                        log(f"Worker {i+1} assignment-timeout refresh failed: {e}", "WorkerPool")

                    if not ok:
                        try:
                            ok = await asyncio.wait_for(
                                self._recreate_worker(i, "assignment_timeout"),
                                timeout=POOL_RECOVERY_WORKER_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            log(f"Worker {i+1} assignment-timeout recreate timed out", "WorkerPool")
                        except Exception as e:
                            log(f"Worker {i+1} assignment-timeout recreate failed: {e}", "WorkerPool")

                    recovered = recovered or bool(ok)
            finally:
                await self._set_all_workers_busy(False)

            log(f"Assignment timeout recovery result: recovered={recovered}", "WorkerPool")
            return recovered

    def _get_reusable_context_pages(self) -> List[Page]:
        """Collect existing live pages that can be reused as worker windows."""
        if not self.shared_context:
            return []

        reusable: List[Page] = []
        for page in self.shared_context.pages:
            try:
                if page.is_closed():
                    continue
                url = (page.url or "").lower()
                if url.startswith("devtools://"):
                    continue
                reusable.append(page)
            except:
                continue
        return reusable

    async def _close_restored_context_pages(self) -> Tuple[int, int]:
        """Close every page restored by the persistent Chromium profile.

        Restored pages are not registered workers. Leaving them open made old
        failed requests remain visibly generating after the pool had recovered.
        Workers are always created from fresh pages immediately afterward.
        """
        pages = self._get_reusable_context_pages()
        if not pages:
            return 0, 0

        log(f"Closing {len(pages)} restored/unmanaged page(s) before worker startup", "WorkerPool")
        # A headed persistent context can exit when its last native window is
        # closed. Create the replacement first, then close restored pages. The
        # guard becomes worker 1 in single-window mode.
        try:
            self._startup_guard_page = await self.shared_context.new_page()
        except Exception as e:
            self._startup_page_close_failures += len(pages)
            log(f"Could not create startup guard page; restored pages left open: {e}", "WorkerPool")
            return 0, len(pages)

        closed = 0
        failed = 0
        for page in pages:
            try:
                await asyncio.wait_for(page.close(), timeout=8.0)
                closed += 1
            except Exception as e:
                failed += 1
                log(f"Failed to close restored page: {e}", "WorkerPool")

        self._startup_pages_closed += closed
        self._startup_page_close_failures += failed
        log(f"Restored page cleanup result: closed={closed} failed={failed}", "WorkerPool")
        return closed, failed

    def _window_bounds_for_index(self, index: int) -> Dict[str, int]:
        """Compute deterministic window bounds for headed split windows."""
        layout = self.headed_window_layout

        if layout == "tile":
            # Two columns by default; more workers spill to additional rows.
            cols = min(2, max(1, self.worker_count))
            rows = max(1, math.ceil(self.worker_count / cols))

            width = max(640, self.headed_screen_width // cols)
            height = max(480, self.headed_screen_height // rows)

            col = index % cols
            row = index // cols

            return {
                "left": self.headed_screen_left + (col * width),
                "top": self.headed_screen_top + (row * height),
                "width": width,
                "height": height,
            }

        # Default: overlap full-size windows so each worker keeps desktop layout.
        width = self.headed_window_width
        height = self.headed_window_height
        left = self.headed_screen_left + (index * self.headed_window_offset_x)
        top = self.headed_screen_top + (index * self.headed_window_offset_y)

        return {
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }

    async def _position_page_window(self, page: Page, index: int) -> bool:
        """Use CDP to position the page's native window."""
        try:
            session = await self.shared_context.new_cdp_session(page)
            window_info = await session.send("Browser.getWindowForTarget")
            window_id = window_info.get("windowId")
            if not window_id:
                return False

            bounds = self._window_bounds_for_index(index)
            await session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {
                        "windowState": "normal",
                        "left": bounds["left"],
                        "top": bounds["top"],
                        "width": bounds["width"],
                        "height": bounds["height"],
                    },
                },
            )
            return True
        except Exception as e:
            log(f"Window positioning failed for worker {index + 1}: {e}", "WorkerPool")
            return False

    async def _open_split_window_page(self, index: int) -> Optional[Page]:
        """Create a new Chromium window (not tab) and return the Page."""
        try:
            browser = self.shared_context.browser
            if not browser:
                return None

            existing = {id(p) for p in self.shared_context.pages}
            browser_session = await browser.new_browser_cdp_session()
            await browser_session.send(
                "Target.createTarget",
                {
                    "url": "about:blank",
                    "newWindow": True,
                    "background": False,
                },
            )

            deadline = time.time() + 10
            while time.time() < deadline:
                for page in self.shared_context.pages:
                    if id(page) not in existing:
                        await self._position_page_window(page, index)
                        return page
                await asyncio.sleep(0.1)

            return None
        except Exception as e:
            log(f"Split-window creation failed for worker {index + 1}: {e}", "WorkerPool")
            return None

    async def init(self, cookies: List[Dict]) -> bool:
        """Launch shared browser and N Gemini Web tabs."""
        try:
            self.playwright = await async_playwright().start()
            
            is_headless = os.getenv("HEADLESS", "false").lower() == "true"
            use_split_windows = (not is_headless) and self.headed_split_windows and self.worker_count >= 2
            if use_split_windows:
                log(
                    f"Headed split windows enabled ({self.worker_count} workers, layout={self.headed_window_layout})",
                    "WorkerPool",
                )
            
            browser_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ]

            log("Applied anti-throttle Chromium switches for backgrounding/occlusion", "WorkerPool")
            anti_throttle_args = [
                arg for arg in browser_args
                if ("background" in arg) or ("occlusion" in arg.lower()) or ("features=" in arg)
            ]
            log(f"Anti-throttle args: {' | '.join(anti_throttle_args)}", "WorkerPool")
            if is_headless:
                browser_args.extend([
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                ])
            if LOW_MEMORY_MODE:
                browser_args.extend([
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-translate",
                    "--no-first-run",
                    "--disable-default-apps",
                ])
            
            # Use .browser_session for persistence
            user_data_dir = os.path.join(os.path.dirname(__file__), ".browser_session")

            launch_kwargs: Dict[str, Any] = {}
            if self.browser_channel:
                launch_kwargs["channel"] = self.browser_channel
                log(f"Using browser channel: {self.browser_channel}", "WorkerPool")

            context_kwargs: Dict[str, Any] = {
                "permissions": ["clipboard-read", "clipboard-write"],
            }
            if use_split_windows:
                context_kwargs["no_viewport"] = True
            else:
                context_kwargs["viewport"] = {"width": 1920, "height": 1080}
            
            self.shared_context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=is_headless,
                args=browser_args,
                **launch_kwargs,
                **context_kwargs,
            )
            
            # Inject cookies if provided
            if cookies:
                sanitized_cookies = []
                for cookie in cookies:
                    try:
                        c = dict(cookie)
                        same_site = c.get("sameSite", "")
                        if same_site is None or str(same_site).lower() not in ["strict", "lax", "none"]:
                            c["sameSite"] = "Lax"
                        else:
                            c["sameSite"] = str(same_site).capitalize()
                        for field in ["id", "storeId", "session"]:
                            c.pop(field, None)
                        if "expirationDate" in c:
                            c["expires"] = c.pop("expirationDate")
                        sanitized_cookies.append(c)
                    except Exception as e:
                        print(f"[WorkerPool] Skipping malformed cookie: {e}")
                        continue
                
                try:
                    await self.shared_context.add_cookies(sanitized_cookies)
                    print(f"[WorkerPool] ✅ Added {len(sanitized_cookies)} cookies")
                except Exception as cookie_err:
                    print(f"[WorkerPool] ⚠️ Cookie injection failed: {cookie_err}")
            
            if LOW_MEMORY_MODE:
                await self.shared_context.route("**/*", self._block_resources)

            # Persistent Chromium may restore pages from an earlier process.
            # None of those pages are registered workers in this pool, so close
            # them deterministically instead of leaving stale generations visible.
            await self._close_restored_context_pages()

            # Create N Gemini Web workers (1 worker = 1 tab)
            print(f"[WorkerPool] Creating {self.worker_count} Gemini Web worker(s)...")
            
            workers_ok = 0
            for i in range(self.worker_count):
                print(f"[WorkerPool] Opening tab {i+1}/{self.worker_count}...")

                page: Optional[Page] = None
                if use_split_windows:
                    page = await self._open_split_window_page(i)
                    if page is not None and self._startup_guard_page is not None:
                        try:
                            await self._startup_guard_page.close()
                        except:
                            pass
                        self._startup_guard_page = None

                if page is None and i == 0 and self._startup_guard_page is not None:
                    page = self._startup_guard_page
                    self._startup_guard_page = None

                if page is None:
                    page = await self.shared_context.new_page()
                    if use_split_windows:
                        await self._position_page_window(page, i)

                try:
                    await page.goto(GeminiWebAutomation.URL, timeout=60000, wait_until="domcontentloaded")
                    print(f"[WorkerPool] ✅ Tab {i+1} loaded: {page.url}")
                except Exception as e:
                    print(f"[WorkerPool] ⚠️ Tab {i+1} navigation warning: {e}")
                
                worker = GeminiWebAutomation(worker_id=i+1)
                if await worker.init_with_page(page, self.shared_context):
                    workers_ok += 1
                self.workers.append(worker)
                self._worker_busy.append(False)
                self._worker_busy_since.append(None)
                
                # Stagger tab creation to avoid rate limiting
                if i < self.worker_count - 1:
                    await asyncio.sleep(2)
            
            print(f"[WorkerPool] ✅ {workers_ok}/{self.worker_count} workers ready")
            
            if self._startup_guard_page is not None:
                try:
                    await self._startup_guard_page.close()
                except:
                    pass
                self._startup_guard_page = None

            self._initialized = True
            return workers_ok > 0  # Success if at least one works
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[WorkerPool] Init error: {e}")
            return False

    async def _block_resources(self, route: Route):
        if route.request.resource_type in ["image", "media", "font"]:
            await route.abort()
        else:
            await route.continue_()

    async def _notify_final_failure(self, last_error: str, worker_index: Optional[int]):
        """Send one Discord alert only after all retries fail."""
        try:
            selector_key = "worker_pool"
            action = "final_failure"
            diagnostics: Dict[str, Any] = {
                "worker_count": self.worker_count,
                "active_requests": self._active_requests,
                "last_error": last_error,
            }

            worker_id = 0
            if worker_index is not None:
                worker_id = worker_index + 1
                payload = GeminiWebAutomation.get_all_errors().get(worker_id)
                if payload:
                    selector_key = payload.get("selector_key") or selector_key
                    action = payload.get("action") or action
                    diagnostics["tracked_error"] = payload.get("error")
                    if payload.get("diagnostics"):
                        diagnostics["worker_context"] = payload.get("diagnostics")

            await notify_error(
                error=f"All retries failed: {last_error}",
                selector_key=selector_key,
                action=action,
                worker_id=worker_id,
                diagnostics=diagnostics,
            )
        except Exception as e:
            log(f"Final failure notification failed: {e}", "WorkerPool")

    async def send_message(
        self,
        prompt: str,
        model: str = None,
        thinking_level: str = None,
        use_search: bool = False,
        images: List[str] = None,
        request_id: str = None,
    ) -> Dict:
        """
        Send message with round-robin worker dispatch.
        Supports N concurrent requests (1 per worker).
        """
        # No workers available
        if not self.workers:
            log("❌ No workers available", "WorkerPool")
            return {"success": False, "error": "No workers available"}

        # Idle maintenance: refresh workers once after long idle periods.
        await self._maybe_refresh_workers_after_idle()

        # Request accepted, update activity timestamp.
        self._last_activity = time.time()
        
        # Retry configuration
        # With 2 workers, allow a third total attempt so a refreshed/recreated worker
        # can be reused if the alternate worker is still occupied.
        max_attempts = 2 if len(self.workers) == 1 else 3
        extra_recovery_attempts_remaining = 1 if len(self.workers) > 1 else 0
        tried_workers = set()
        last_error = None
        last_failure_worker_index: Optional[int] = None
        attempts_used = 0
        all_attempt_logs: List[str] = []  # Accumulated per-attempt log lines for error reports
        
        while attempts_used < max_attempts:
            attempts_used += 1
            # Acquire semaphore (limits concurrent requests to N workers)
            sem_wait_start = time.time()
            async with self._browser_semaphore:
                sem_wait_ms = int((time.time() - sem_wait_start) * 1000)
                if sem_wait_ms > 250:
                    log(f"Worker assignment wait: {sem_wait_ms}ms", "WorkerPool")
                
                # Get available worker, excluding already-tried workers
                worker_index, worker = await self._get_available_worker(exclude=tried_workers)
                
                if worker is None:
                    log(f"⚠️ No available workers left to try", "WorkerPool")
                    if await self._recover_after_assignment_timeout():
                        tried_workers.clear()
                        if extra_recovery_attempts_remaining > 0:
                            max_attempts += 1
                            extra_recovery_attempts_remaining -= 1
                        continue
                    break

                self._active_requests += 1

                tried_workers.add(worker_index)
                should_recreate_worker = False
                recreate_reason = ""
                allow_same_worker_retry = False
                attempt_error = ""

                try:
                    result = await worker.send_message(
                        prompt,
                        model,
                        thinking_level,
                        use_search,
                        images,
                        request_id=request_id,
                    )

                    # Validate response
                    if result.get("success"):
                        response = result.get("response", "")
                        # Check for empty responses (extraction failed)
                        if not response.strip():
                            log(f"Worker {worker_index+1}: Empty response, retrying...", "WorkerPool")
                            last_error = "Empty response"
                            attempt_error = last_error
                            should_recreate_worker = self._record_worker_failure(worker_index, last_error)
                            recreate_reason = last_error
                            allow_same_worker_retry = True
                            # Don't call _release_worker here - finally block handles it
                            continue
                        self._record_worker_success(worker_index)
                        return result
                    else:
                        last_error = result.get('error', 'unknown')
                        attempt_error = last_error
                        last_failure_worker_index = worker_index
                        is_dead_page = self._is_dead_page_error(last_error)
                        # Dead-page: force immediate recreation regardless of stall threshold.
                        # The page is gone — retrying it is pointless and instant-fails.
                        if is_dead_page:
                            should_recreate_worker = True
                            allow_same_worker_retry = False  # Don't re-use until recreated
                            log(
                                f"Worker {worker_index+1} dead page detected — forcing recreation. Error: {last_error}",
                                "WorkerPool",
                            )
                        else:
                            should_recreate_worker = self._record_worker_failure(worker_index, last_error)
                            allow_same_worker_retry = not self._is_network_outage_error(last_error)
                            log(
                                f"Worker {worker_index+1} failed: {last_error}, retrying... (recreate={should_recreate_worker})",
                                "WorkerPool",
                            )
                        recreate_reason = last_error
                        if self._is_network_outage_error(last_error):
                            log("Network outage detected; skipping cross-worker retry", "WorkerPool")
                            break

                except Exception as e:
                    last_error = str(e)
                    attempt_error = last_error
                    last_failure_worker_index = worker_index
                    is_dead_page = self._is_dead_page_error(last_error)
                    if is_dead_page:
                        should_recreate_worker = True
                        allow_same_worker_retry = False
                        log(
                            f"Worker {worker_index+1} dead page exception — forcing recreation. Error: {e}",
                            "WorkerPool",
                        )
                    else:
                        should_recreate_worker = self._record_worker_failure(worker_index, last_error)
                        allow_same_worker_retry = not self._is_network_outage_error(last_error)
                        log(
                            f"Worker {worker_index+1} exception: {e}, retrying... (recreate={should_recreate_worker})",
                            "WorkerPool",
                        )
                    recreate_reason = last_error
                    if self._is_network_outage_error(last_error):
                        log("Network outage detected; skipping cross-worker retry", "WorkerPool")
                        break
                finally:
                    # Collect per-attempt log lines into the shared buffer for error reports.
                    # This always runs regardless of success, failure dict, or exception.
                    attempt_lines = worker.get_request_log()
                    if attempt_lines:
                        all_attempt_logs.append(
                            f"--- Attempt {attempts_used} (Worker {worker.worker_id}) ---"
                        )
                        all_attempt_logs.extend(attempt_lines)
                    recreated_ok = False
                    if should_recreate_worker:
                        recreated_ok = await self._recreate_worker(worker_index, recreate_reason)
                        if not recreated_ok:
                            await self._quarantine_worker(worker_index, recreate_reason)
                    self._active_requests = max(0, self._active_requests - 1)
                    await self._release_worker(worker_index)
                    self._has_processed_request = True
                    self._last_activity = time.time()

                    if attempt_error and (allow_same_worker_retry or recreated_ok):
                        if should_recreate_worker and not recreated_ok:
                            log(
                                f"Worker {worker_index+1} retry blocked because recreation failed; worker is quarantined",
                                "WorkerPool",
                            )
                        else:
                            tried_workers.discard(worker_index)
                        if recreated_ok:
                            if extra_recovery_attempts_remaining > 0:
                                max_attempts += 1
                                extra_recovery_attempts_remaining -= 1
                                log(
                                    f"Granted extra recovery attempt after recreating worker {worker_index+1}",
                                    "WorkerPool",
                                )
                            log(
                                f"Worker {worker_index+1} retry reopened after recreation",
                                "WorkerPool",
                            )
                        elif not should_recreate_worker:
                            log(
                                f"Worker {worker_index+1} retry reopened after recoverable failure",
                                "WorkerPool",
                            )
        
        # All retries exhausted
        log(f"❌ All {attempts_used} attempts failed. Last error: {last_error}", "WorkerPool")
        await self._notify_final_failure(last_error or "unknown", last_failure_worker_index)
        return {
            "success": False,
            "error": f"All workers failed. Last error: {last_error}",
            "attempt_logs": all_attempt_logs,
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        errors = GeminiWebAutomation.get_all_errors()
        workers = []
        for idx, worker in enumerate(self.workers):
            workers.append({
                "worker_id": idx + 1,
                "initialized": bool(worker and worker._initialized),
                "busy": bool(self._worker_busy[idx]) if idx < len(self._worker_busy) else False,
                "busy_since_unix": int(self._worker_busy_since[idx]) if idx < len(self._worker_busy_since) and self._worker_busy_since[idx] else None,
                "generation_in_progress": bool(worker and worker._generation_in_progress),
                "last_error": errors.get(idx + 1),
            })

        return {
            "initialized": self._initialized,
            "provider": self.provider,
            "worker_count": self.worker_count,
            "active_requests": self._active_requests,
            "browser_channel": self.browser_channel or "default",
            "next_worker_index": self._next_worker_index + 1,
            "headed_split_windows": self.headed_split_windows,
            "headed_window_layout": self.headed_window_layout,
            "headed_window_size": {
                "width": self.headed_window_width,
                "height": self.headed_window_height,
            },
            "stall_recreate_threshold": STALL_RECREATE_THRESHOLD,
            "worker_stall_failures": {str(k + 1): v for k, v in self._worker_stall_failures.items()},
            "startup_pages_closed": self._startup_pages_closed,
            "startup_page_close_failures": self._startup_page_close_failures,
            "worker_recreation_count": self._worker_recreation_count,
            "last_worker_recreation": self._last_worker_recreation,
            "context_page_count": len(self.shared_context.pages) if self.shared_context else 0,
            "workers": workers,
            "errors": errors,
            "last_activity_unix": int(self._last_activity),
        }

    async def get_live_diagnostics(self) -> Dict[str, Any]:
        """Return pool diagnostics plus bounded live DOM state per managed worker."""
        result = self.get_diagnostics()
        for idx, worker_info in enumerate(result["workers"]):
            worker = self.workers[idx]
            try:
                snapshot = await asyncio.wait_for(worker._capture_state_snapshot(), timeout=5.0)
                current_state = {
                    "page_title": snapshot.get("page_title"),
                    "url": snapshot.get("url"),
                    "phase": snapshot.get("phase"),
                    "stop_visible": bool(snapshot.get("stop_visible")),
                    "send_visible": bool(snapshot.get("send_visible")),
                    "input_text_len": int(snapshot.get("input_text_len") or 0),
                    "user_query_count": int(snapshot.get("user_query_count") or 0),
                    "response_count": int(snapshot.get("response_count") or 0),
                    "thinking_label": snapshot.get("thinking_label"),
                    "thinking_active": bool(snapshot.get("thinking_active")),
                    "error_page_500": bool(snapshot.get("error_page_500")),
                }
                invariant_violations = []
                if current_state["stop_visible"] and not worker_info["generation_in_progress"]:
                    invariant_violations.append("stop_visible_while_worker_idle")
                if (
                    current_state["input_text_len"] > 0
                    and current_state["user_query_count"] > 0
                    and not worker_info["generation_in_progress"]
                ):
                    invariant_violations.append("retained_prompt_while_worker_idle")
                worker_info["current_state"] = current_state
                worker_info["invariant_violations"] = invariant_violations
                worker_info["last_recovery"] = worker._last_recovery
            except Exception as e:
                worker_info["current_state_error"] = str(e)
        return result


    async def close(self):
        # Close all workers
        for w in self.workers:
            await w.close()
        
        if self.shared_context: await self.shared_context.close()
        if self.playwright: await self.playwright.stop()
