import asyncio
import base64
import os
import sys
import random
import time
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Route

# --- Timestamped Logging (use stderr - always unbuffered) ---
def log(msg: str, tag: str = "Core"):
    """Print with timestamp for debugging. Uses stderr for guaranteed immediate output."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [{tag}] {msg}", file=sys.stderr, flush=True)

# Low memory mode: block images, fonts, etc.
LOW_MEMORY_MODE = os.getenv("LOW_MEMORY_MODE", "true").lower() == "true"

# Slow VM mode: use JavaScript clicks instead of Playwright clicks
SLOW_VM_MODE = os.getenv("SLOW_VM_MODE", "true").lower() == "true"

# Debug screenshots on failure (disabled by default for performance)
DEBUG_SCREENSHOTS = os.getenv("DEBUG_SCREENSHOTS", "false").lower() == "true"
DEBUG_SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "debug_screenshots")

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
    
    @classmethod
    def _get_clipboard_lock(cls) -> asyncio.Lock:
        """Get or create clipboard lock (lazy init for correct event loop)."""
        if cls._clipboard_lock is None:
            cls._clipboard_lock = asyncio.Lock()
        return cls._clipboard_lock
    
    # Selectors discovered during research
    SELECTORS = {
        "input": 'div[role="textbox"][aria-label="Enter a prompt for Gemini"]',
        "send_btn": '[aria-label="Send message"]',
        "model_btn": '.input-area-switch',
        "new_chat": '[data-test-id="new-chat-button"]',
        "temp_chat": '[aria-label="Temporary chat"]',
        "temp_chat_active": '[aria-label="Temporary chat"].temp-chat-on',
        "sidebar_toggle": '[aria-label="Main menu"]',
        "copy_btn": '[aria-label="Copy"]',
        "menu_panel": '.mat-mdc-menu-panel',
        "menu_item": '.mat-mdc-menu-item'
    }
    
    def __init__(self, worker_id: int = 0):
        super().__init__()
        self.worker_id = worker_id  # For logging
        self._request_count = 0
        self._request_id = None  # Set per-request for log tracing

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
                                log(f"❌ {description}: verification failed", f"Worker {self.worker_id}")
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
        try:
            # Wait for input to be ready (login check)
            await self.page.wait_for_selector(self.SELECTORS["input"], timeout=30000)
            await self._human_delay(500, 1000)
            print("[GeminiWeb] ✅ Logged in and ready")
            
            # Default to Temporary Chat if possible for clean sessions
            await self._enable_temp_chat()
            await self._human_delay(200, 500)
            
            self._initialized = True
            return True
        except Exception as e:
            print(f"[GeminiWeb] ❌ Init failed: {e}")
            return False

    async def _enable_temp_chat(self):
        """Enable temporary chat mode."""
        try:
            temp_btn = self.page.locator(self.SELECTORS["temp_chat"])
            
            # Check if temp chat is already enabled
            temp_active = self.page.locator(self.SELECTORS["temp_chat_active"])
            if await temp_active.count() > 0:
                return
            
            # Expand sidebar if button not visible
            if not await temp_btn.is_visible():
                sidebar_btn = self.page.locator(self.SELECTORS["sidebar_toggle"])
                if await sidebar_btn.is_visible():
                    await sidebar_btn.click()
                    await self._human_delay(300, 500)
                else:
                    return
            
            # Click temp chat button
            if await temp_btn.is_visible():
                temp_active = self.page.locator(self.SELECTORS["temp_chat_active"])
                if await temp_active.count() == 0:
                    await temp_btn.click()
                    await self._human_delay(200, 400)
                    
        except Exception as e:
            log(f"⚠️ Temp chat error: {e}", f"Worker {self.worker_id}")

    async def send_message(self, prompt: str, model: str = None, thinking_level: str = None, use_search: bool = False, images: List[str] = None) -> Dict:
        if not self._initialized: 
            return {"success": False, "error": "Not initialized"}

        try:
            self._generation_in_progress = True
            self._request_count += 1
            self._request_id = uuid.uuid4().hex[:8]  # Short ID for log tracing
            log(f"[{self._request_id}] Request: model={model}, prompt={len(prompt)} chars", f"Worker {self.worker_id}")
            
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
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
                await self._human_delay(1000, 1500)
                self._request_count = 0
            
            # 1. New Chat (starts fresh) - VERIFIED
            async def get_copy_count():
                return await self.page.locator(self.SELECTORS["copy_btn"]).count()
            
            worker_id_for_closure = self.worker_id  # Capture for nested functions
            
            async def verify_chat_cleared(before_count):
                # Give extra time for chat to clear
                await self._human_delay(500, 800)
                after_count = await self.page.locator(self.SELECTORS["copy_btn"]).count()
                # Success if count dropped (ideally to 0, but at least fewer than before)
                return after_count == 0 or after_count < before_count
            
            new_chat_success = await self._verified_click(
                self.SELECTORS["new_chat"],
                "New Chat",
                verify_before=get_copy_count,
                verify_after=verify_chat_cleared,
                timeout=5000
            )
            
            if not new_chat_success:
                log("⚠️ New Chat click failed, proceeding anyway", f"Worker {self.worker_id}")

            
            # 1.5 Enable Temporary Chat
            await self._enable_temp_chat()
            await self._human_delay()

            # 2. Select Model
            if model:
                await self._select_model(model)

            # 3. Enter Prompt
            input_area = self.page.locator(self.SELECTORS["input"])
            await input_area.click()
            await self._human_delay()
            
            # 3.5 Paste Images (if provided)
            if images:
                for img_path in images:
                    await self._paste_image(img_path)
                    await self._human_delay(200, 500)
            
            await input_area.fill(prompt)
            await self._human_delay(300, 600)

            # Capture button count BEFORE sending (to ensure we wait for a NEW one)
            pre_send_count = await self.page.locator(self.SELECTORS["copy_btn"]).count()

            # 4. Click Send - VERIFIED
            worker_id = self.worker_id  # Capture for closure
            
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
                        log(f"⚠️ Send failed: input not cleared ({after_len} chars remain)", f"Worker {worker_id}")
                        return False
                except Exception as e:
                    log(f"⚠️ Send verification error: {e}", f"Worker {worker_id}")
                    return False  # Don't assume success on error
            
            send_success = await self._verified_click(
                self.SELECTORS["send_btn"],
                "Send",
                verify_before=get_input_text,
                verify_after=verify_send_worked,
                timeout=5000
            )
            
            if not send_success:
                log(f"❌ Send button click failed", f"Worker {self.worker_id}")
                return {"success": False, "error": "Send button click failed"}
            
            # 5. Wait for Response (Copy button to appear)
            log(f"Waiting for response...", f"Worker {self.worker_id}")
            await self._human_delay(300, 600)  # Reduced initial wait
            
            # Polling for copy button (Wait until we have MORE buttons than before)
            start_time = time.time()
            max_wait = 180 # Extended for thinking models
            copy_btn = None
            while (time.time() - start_time) < max_wait:
                btns = self.page.locator(self.SELECTORS["copy_btn"])
                current_count = await btns.count()
                
                # We need to find a new button (more than we started with)
                if current_count > pre_send_count:
                    # Get the LAST button (the new one)
                    copy_btn = btns.nth(current_count - 1)
                    if await copy_btn.is_visible():
                        break
                        
                await asyncio.sleep(1)  # Reduced from 2s
            
            if not copy_btn:
                log(f"❌ Timeout after {max_wait}s waiting for response", f"Worker {self.worker_id}")
                return {"success": False, "error": f"Timeout after {max_wait}s waiting for response"}

            # Auto-scroll to ensure copy button is visible
            await self.page.evaluate('''
                () => {
                    const copyButtons = document.querySelectorAll('button[aria-label="Copy"]');
                    if (copyButtons.length > 0) {
                        const lastBtn = copyButtons[copyButtons.length - 1];
                        lastBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                }
            ''')
            await self._human_delay(150, 300)

            # 6. Extraction via Copy Button (with lock to prevent clipboard race condition)
            async with GeminiWebAutomation._get_clipboard_lock():
                await copy_btn.click()
                await self._human_delay(100, 200)
                markdown = await self.page.evaluate("navigator.clipboard.readText()")
            
            self._generation_in_progress = False
            
            if not markdown:
                log(f"⚠️ Clipboard empty, using fallback", f"Worker {self.worker_id}")
                return await self._fallback_extract()

            log(f"✅ Response: {len(markdown)} chars", f"Worker {self.worker_id}")
            return {"success": True, "response": markdown.strip()}

        except Exception as e:
            self._generation_in_progress = False
            log(f"❌ Error: {e}", f"Worker {self.worker_id}")
            
            # Force refresh to reset page state for next request
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=15000)
                self._request_count = 0
            except:
                pass
            
            # Retry extraction once via DOM fallback
            return await self._fallback_extract()

    async def _fallback_extract(self) -> Dict:
        """Fallback extraction via DOM if copy button fails."""
        try:
            await self._human_delay(400, 800) # Give DOM a moment to settle
            text = await self.page.evaluate('''
                () => {
                    const responses = document.querySelectorAll('[data-content-type="response"]');
                    if (responses.length === 0) return null;
                    const last = responses[responses.length - 1];
                    return last.innerText;
                }
            ''')
            if text and len(text) > 20:
                return {"success": True, "response": text.strip()}
        except:
            pass
        return {"success": False, "error": "Extraction failed"}

    async def _select_model(self, model_name: str):
        """Select model from dropdown (Fast, Thinking, Pro)."""
        try:
            # Click the pill
            btn = self.page.locator(self.SELECTORS["model_btn"])
            current = await btn.inner_text()
            if model_name.lower() in current.lower():
                return
            
            await btn.click()
            await self._human_delay(400, 600)
            
            # Select from menu
            items = self.page.locator(self.SELECTORS["menu_item"])
            for i in range(await items.count()):
                item = items.nth(i)
                text = await item.inner_text()
                if model_name.lower() in text.lower():
                    await item.click()
                    print(f"[Worker {self.worker_id}] ✅ Selected model: {model_name}")
                    await self._human_delay(300, 600)
                    return
            # If not found, close menu
            await self.page.keyboard.press("Escape")
            await self._human_delay(100, 300)
        except Exception as e:
            print(f"[Worker {self.worker_id}] ⚠️ Model selection failed: {e}")

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
            input_area = self.page.locator(self.SELECTORS["input"])
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
    SUPPORTED_MODELS = ["thinking", "pro", "fast"]
    
    def __init__(self, worker_count: int = 1, provider: str = "auto"):
        self.worker_count = max(1, worker_count)  # At least 1 worker
        self.provider = provider.lower()
        
        # Array of Gemini Web workers (1 worker = 1 tab)
        self.workers: List[GeminiWebAutomation] = []
        self._worker_busy: List[bool] = []  # Track which workers are busy
        self._worker_lock = asyncio.Lock()  # Lock for worker assignment
        
        self.shared_context = None
        self.playwright = None
        self._initialized = False
        
        self._browser_semaphore = asyncio.Semaphore(worker_count)  # Allow N concurrent requests
        self._last_activity = time.time()
    
    async def _get_available_worker(self, exclude: set = None) -> Tuple[int, GeminiWebAutomation]:
        """Get first available worker (round-robin). Waits if all are busy."""
        if exclude is None:
            exclude = set()
            
        start_time = time.time()
        max_wait = 60
        
        while time.time() - start_time < max_wait:
            async with self._worker_lock:
                for i, busy in enumerate(self._worker_busy):
                    if i in exclude:
                        continue
                    if not busy and self.workers[i]._initialized:
                        self._worker_busy[i] = True
                        return i, self.workers[i]
            await asyncio.sleep(0.5)
        
        log(f"⚠️ Worker assignment timeout", "WorkerPool")
        return None, None
    
    async def _release_worker(self, index: int):
        """Mark worker as available."""
        async with self._worker_lock:
            if 0 <= index < len(self._worker_busy):
                self._worker_busy[index] = False

    async def init(self, cookies: List[Dict]) -> bool:
        """Launch shared browser and N Gemini Web tabs."""
        try:
            self.playwright = await async_playwright().start()
            
            is_headless = os.getenv("HEADLESS", "false").lower() == "true"
            
            # Use GeminiWebAutomation constants (AI Studio code removed)
            browser_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # Anti-throttling: prevent Chrome from suspending background tabs
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
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
            
            self.shared_context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=is_headless,
                args=browser_args,
                viewport={"width": 1920, "height": 1080},
                permissions=["clipboard-read", "clipboard-write"],
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

            # Create N Gemini Web workers (1 worker = 1 tab)
            print(f"[WorkerPool] Creating {self.worker_count} Gemini Web worker(s)...")
            
            workers_ok = 0
            for i in range(self.worker_count):
                print(f"[WorkerPool] Opening tab {i+1}/{self.worker_count}...")
                page = await self.shared_context.new_page()
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
                
                # Stagger tab creation to avoid rate limiting
                if i < self.worker_count - 1:
                    await asyncio.sleep(2)
            
            print(f"[WorkerPool] ✅ {workers_ok}/{self.worker_count} workers ready")
            
            self._initialized = True
            return workers_ok > 0  # Success if at least one works
        except Exception as e:
            print(f"[WorkerPool] Init error: {e}")
            return False

    async def _block_resources(self, route: Route):
        if route.request.resource_type in ["image", "media", "font"]:
            await route.abort()
        else:
            await route.continue_()

    async def send_message(self, prompt: str, model: str = None, thinking_level: str = None, use_search: bool = False, images: List[str] = None) -> Dict:
        """
        Send message with round-robin worker dispatch.
        Supports N concurrent requests (1 per worker).
        """
        # Update activity timestamp
        self._last_activity = time.time()
        
        # No workers available
        if not self.workers:
            log("❌ No workers available", "WorkerPool")
            return {"success": False, "error": "No workers available"}
        
        # Retry configuration
        MAX_RETRIES = min(3, len(self.workers))  # Try up to 3 different workers
        tried_workers = set()
        last_error = None
        
        for attempt in range(MAX_RETRIES):
            # Acquire semaphore (limits concurrent requests to N workers)
            async with self._browser_semaphore:
                
                # Get available worker, excluding already-tried workers
                worker_index, worker = await self._get_available_worker(exclude=tried_workers)
                
                if worker is None:
                    log(f"⚠️ No available workers left to try", "WorkerPool")
                    break
                
                tried_workers.add(worker_index)
                
                try:
                    result = await worker.send_message(prompt, model, thinking_level, use_search, images)
                    
                    # Validate response
                    if result.get("success"):
                        response = result.get("response", "")
                        # Check for empty responses (extraction failed)
                        if not response.strip():
                            log(f"Worker {worker_index+1}: Empty response, retrying...", "WorkerPool")
                            last_error = "Empty response"
                            # Don't call _release_worker here - finally block handles it
                            continue
                        
                        return result
                    else:
                        last_error = result.get('error', 'unknown')
                        log(f"Worker {worker_index+1} failed: {last_error}, retrying...", "WorkerPool")
                        
                except Exception as e:
                    last_error = str(e)
                    log(f"Worker {worker_index+1} exception: {e}, retrying...", "WorkerPool")
                finally:
                    await self._release_worker(worker_index)
                    self._last_activity = time.time()
        
        # All retries exhausted
        log(f"❌ All {MAX_RETRIES} attempts failed. Last error: {last_error}", "WorkerPool")
        return {"success": False, "error": f"All workers failed. Last error: {last_error}"}


    async def close(self):
        # Close all workers
        for w in self.workers:
            await w.close()
        
        if self.shared_context: await self.shared_context.close()
        if self.playwright: await self.playwright.stop()

