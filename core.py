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

# Import notifier (won't crash if aiohttp not installed)
from notifier import notify_error

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
    
    # Keyword synonyms for resilient model matching
    # If Google changes "Thinking" to "Deep Thinking", the "think" keyword still matches
    MODEL_KEYWORDS = {
        "thinking": ["think", "reason", "complex", "problem", "deep"],
        "pro": ["pro", "advanced", "longer", "math", "code"],
        "fast": ["fast", "quick", "flash", "answer", "speed"],
        "auto": ["auto", "default", "automatic"],
    }
    
    # Selectors with fallbacks - each key maps to a list of selectors to try in order
    # STRATEGY: Put STABLE structural selectors FIRST, volatile text-based ones LAST
    # Structural (role, contenteditable) > Class-based > aria-label text
    # Last updated: 2026-02-12
    SELECTORS = {
        # INPUT: Most volatile - Google changes aria-label text frequently
        # Priority: structural > class > partial keywords (NO exact text matches)
        "input": [
            'div[role="textbox"][aria-label="Enter a prompt for Gemini"]',  # Current Gemini label
            'div[role="textbox"][contenteditable="true"]',  # STABLE: structural attributes
            '.ql-editor.textarea',  # Class-based (Quill editor)
            '.new-input-ui[role="textbox"]',  # New input UI class fallback
            'div.ql-editor[contenteditable="true"]',  # Quill + structural
            'div[aria-label*="prompt" i][contenteditable="true"]',  # Keyword: "prompt"
            'div[aria-label*="Gemini" i][contenteditable="true"]',  # Keyword: "Gemini"
            'div[aria-label*="Ask" i][contenteditable="true"]',  # Keyword: "Ask"
            'div[aria-label*="Enter" i][contenteditable="true"]',  # Keyword: "Enter"
            'div[aria-label*="message" i][contenteditable="true"]',  # Keyword: "message"
            'div[aria-label*="chat" i][contenteditable="true"]',  # Keyword: "chat"
        ],
        # SEND: Button near input - use class and partial keywords only
        "send_btn": [
            'button[aria-label="Send message"]',  # Current Gemini label
            'button.send-button',  # Class-based - more stable
            'button[aria-label*="Send" i]',  # Keyword: "Send"
            'button[aria-label*="Submit" i]',  # Keyword: "Submit"
            '[aria-label*="Send" i]',  # Any element with Send
        ],
        # MODEL: The dropdown to switch Fast/Pro/Thinking
        "model_btn": [
            'button[aria-label="Open mode picker"]',  # Current Gemini label
            'button.input-area-switch',  # Class-based
            'button.mat-mdc-menu-trigger',  # Angular trigger fallback
            '.input-area-switch',  # Any element with this class
            'button:has-text("Fast")',  # Text-based fallbacks
            'button:has-text("Pro")',
            'button:has-text("Thinking")',
        ],
        # NEW CHAT: Now an <a> tag - use URL and keywords
        "new_chat": [
            'a[aria-label="New chat"]',  # Current Gemini label
            'a[href="/app"]',  # URL-based - very stable!
            '.side-nav-action-button',  # Class-based
            'a[aria-label*="New" i]',  # Keyword: "New"
            'button[aria-label*="New" i]',  # Button fallback with keyword
            'a[aria-label*="chat" i]',  # Keyword: "chat"
        ],
        # TEMP CHAT: Toggle button - keywords only
        "temp_chat": [
            'button[aria-label="Temporary chat"]',  # Current Gemini label
            'button.temp-chat-button',  # Class-based
            '[aria-label*="Temporary" i]',  # Keyword: "Temporary"
            '[aria-label*="temp" i]',  # Keyword: "temp"
        ],
        "temp_chat_active": [
            'button.temp-chat-button.temp-chat-on',  # Class-based active state
            'button[aria-label*="Temporary" i][aria-pressed="true"]',  # aria-pressed
            '[aria-label*="Temporary" i].temp-chat-on',  # Keyword + class
        ],
        # SIDEBAR: Hamburger menu - keywords only
        "sidebar_toggle": [
            'button[aria-label="Main menu"]',  # Current Gemini label
            'button.main-menu-button',  # Class-based fallback
            'button[aria-label*="menu" i]',  # Keyword: "menu"
            'button[aria-label*="Menu" i]',  # Keyword: "Menu" (case variation)
            'button[aria-label*="sidebar" i]',  # Keyword: "sidebar"
            'button[aria-label*="navigation" i]',  # Keyword: "navigation"
        ],
        # COPY: Generic action, unlikely to change
        "copy_btn": [
            'button[aria-label="Copy"]',  # Exact - "Copy" is universal
            'button[aria-label*="Copy" i]',  # Partial fallback
            'button.copy-button',  # Class-based fallback
        ],
        # MENU: Standard accessibility roles - very stable
        "menu_panel": [
            '[role="menu"]',  # STABLE: accessibility standard
            '[role="listbox"]',  # Alternative menu pattern
            '.mat-mdc-menu-panel',  # Angular Material
        ],
        "menu_item": [
            '[role="option"]',  # Listbox pattern (Gemini uses this)
            '[role="menuitem"]',  # Standard menu pattern
            '[role="listitem"]',  # Alternative
            '.mat-mdc-menu-item',  # Angular Material
        ],
        # OVERLAY: Angular CDK internal - stable
        "overlay_backdrop": [
            '.cdk-overlay-backdrop',  # STABLE: Angular CDK
            '.cdk-overlay-backdrop-showing',  # Visible backdrop class
            '.cdk-overlay-container .cdk-overlay-backdrop',  # More specific
        ]
    }
    
    # Error tracking for diagnostics
    _last_errors: Dict[int, Dict] = {}  # worker_id -> {error, selector, action, timestamp}
    
    def __init__(self, worker_id: int = 0):
        super().__init__()
        self.worker_id = worker_id  # For logging
        self._request_count = 0
        self._request_id = None  # Set per-request for log tracing

    def _get_selector(self, key: str) -> str:
        """Get the first selector from a selector group (for backward compatibility)."""
        selectors = self.SELECTORS.get(key, [])
        if isinstance(selectors, list) and len(selectors) > 0:
            return selectors[0]
        elif isinstance(selectors, str):
            return selectors
        return ""
    
    def _get_all_selectors(self, key: str) -> List[str]:
        """Get all selectors for a key as a list."""
        selectors = self.SELECTORS.get(key, [])
        if isinstance(selectors, list):
            return selectors
        elif isinstance(selectors, str):
            return [selectors]
        return []

    async def _resolve_visible_selector(self, key: str, timeout: int = 1500) -> str:
        """Resolve the first visible selector from a selector group."""
        for selector in self._get_all_selectors(key):
            try:
                await self.page.wait_for_selector(selector, state="visible", timeout=timeout)
                return selector
            except:
                continue
        return self._get_selector(key)
    
    async def _find_element(self, key: str, timeout: int = 5000, description: str = None, track_error: bool = True):
        """
        Try all selectors for a key until one finds a visible element.
        Returns (locator, selector_used) or (None, None) if all fail.
        """
        desc = description or key
        selectors = self._get_all_selectors(key)
        
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                # Quick visibility check
                if await locator.is_visible():
                    return locator, selector
            except:
                continue
        
        # None found immediately visible, try waiting for first selector
        if selectors:
            try:
                locator = self.page.locator(selectors[0]).first
                await locator.wait_for(state="visible", timeout=timeout)
                return locator, selectors[0]
            except:
                pass
        
        # Track the failure (optional for non-critical probes)
        if track_error:
            await self._track_error(f"Element not found: {desc}", key, desc)
        return None, None
    
    def _matches_model(self, item_text: str, model_name: str) -> bool:
        """Check if menu item matches requested model using keyword matching."""
        text_lower = item_text.lower()
        model_lower = model_name.lower()
        
        # Direct match first
        if model_lower in text_lower:
            return True
        
        # Keyword fallback
        keywords = self.MODEL_KEYWORDS.get(model_lower, [])
        return any(kw in text_lower for kw in keywords)

    def _get_position_fallback(self, model_name: str, item_count: int) -> int:
        """Get position-based fallback index (cheap to expensive = top to bottom)."""
        model_lower = model_name.lower()

        if item_count <= 0:
            return -1

        if model_lower == "pro":
            return item_count - 1
        if model_lower == "thinking":
            return max(0, item_count - 2)
        if model_lower == "fast":
            return 1 if item_count > 3 else 0
        if model_lower == "auto":
            return 0

        return -1

    async def _is_generation_active(self) -> bool:
        """Check if the UI indicates generation is still running."""
        selectors = list(self._get_all_selectors("send_btn"))
        selectors.append('button[aria-label="Stop response"]')
        selectors.append('button[aria-label*="Stop" i]')
        selectors.append('button:has-text("Stop")')

        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if not await locator.is_visible():
                    continue

                aria_label = await locator.get_attribute("aria-label")
                if aria_label and "stop" in aria_label.lower():
                    return True

                text = await locator.inner_text()
                if text and "stop" in text.lower():
                    return True
            except:
                continue

        return False

    async def _stop_generation_if_active(self, wait_timeout_ms: int = 8000) -> bool:
        """Attempt to stop active generation and wait until UI leaves Stop state."""
        try:
            if not await self._is_generation_active():
                return True

            stop_selectors = [
                'button[aria-label="Stop response"]',
                'button[aria-label*="Stop" i]',
                'button:has-text("Stop")',
            ]

            clicked = False
            for selector in stop_selectors:
                try:
                    locator = self.page.locator(selector).first
                    if not await locator.is_visible():
                        continue
                    await locator.click(timeout=2000)
                    clicked = True
                    break
                except:
                    continue

            if not clicked:
                return False

            end = time.time() + (wait_timeout_ms / 1000)
            while time.time() < end:
                if not await self._is_generation_active():
                    await self._human_delay(120, 250)
                    return True
                await asyncio.sleep(0.25)

            return False
        except:
            return False

    async def _recover_stuck_generation(self) -> bool:
        """Recover worker if previous request left the tab stuck in Stop state."""
        try:
            if not await self._is_generation_active():
                return True

            log("Detected active generation from previous state; recovering", f"Worker {self.worker_id}")

            if await self._stop_generation_if_active(wait_timeout_ms=8000):
                return True

            log("Stop click did not recover state, reloading tab", f"Worker {self.worker_id}")
            await self.page.reload(wait_until="domcontentloaded", timeout=30000)

            for selector in self._get_all_selectors("input"):
                try:
                    await self.page.wait_for_selector(selector, state="visible", timeout=6000)
                    break
                except:
                    continue

            return not await self._is_generation_active()
        except Exception as e:
            log(f"Recovery failed: {e}", f"Worker {self.worker_id}")
            return False

    async def _collect_error_context(self) -> Dict:
        """Collect compact UI state for error diagnostics."""
        context = {
            "request_id": self._request_id,
            "url": "",
            "overlay_count": 0,
            "copy_count": 0,
            "response_count": 0,
            "last_response_len": 0,
            "last_response_tail": "",
            "stop_visible": False,
            "active_button": "",
        }

        if not self.page:
            return context

        try:
            context["url"] = self.page.url
        except:
            pass

        try:
            context["overlay_count"] = await self.page.locator('.cdk-overlay-backdrop').count()
        except:
            pass

        try:
            copy_selector = self._get_selector("copy_btn")
            context["copy_count"] = await self.page.locator(copy_selector).count()
        except:
            pass

        try:
            metrics = await self.page.evaluate('''
                () => {
                    const responses = document.querySelectorAll('[data-content-type="response"]');
                    const last = responses.length ? responses[responses.length - 1] : null;
                    const text = last ? (last.innerText || '') : '';

                    const hasStop = Array.from(document.querySelectorAll('button')).some((btn) => {
                        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                        const inner = (btn.innerText || '').toLowerCase();
                        return label.includes('stop') || inner.includes('stop');
                    });

                    return {
                        response_count: responses.length,
                        last_response_len: text.length,
                        last_response_tail: text.slice(-160),
                        stop_visible: hasStop,
                    };
                }
            ''')

            context["response_count"] = metrics.get("response_count", 0)
            context["last_response_len"] = metrics.get("last_response_len", 0)
            context["last_response_tail"] = metrics.get("last_response_tail", "")
            context["stop_visible"] = metrics.get("stop_visible", False)
        except:
            pass

        selectors = ['button[aria-label="Stop response"]'] + self._get_all_selectors("send_btn")
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if not await locator.is_visible():
                    continue

                aria_label = await locator.get_attribute("aria-label")
                text = await locator.inner_text()
                label = (aria_label or text or "").strip().replace("\n", " ")
                context["active_button"] = label[:80]
                break
            except:
                continue

        return context

    def _format_error_context(self, context: Dict) -> str:
        """Format compact error context for logs/notifications."""
        tail = (context.get("last_response_tail") or "").replace("\n", " ").strip()
        if len(tail) > 120:
            tail = tail[-120:]

        parts = [
            f"req={context.get('request_id') or 'unknown'}",
            f"url={context.get('url') or 'unknown'}",
            f"btn={context.get('active_button') or 'none'}",
            f"stop={context.get('stop_visible')}",
            f"copy={context.get('copy_count', 0)}",
            f"resp={context.get('response_count', 0)}",
            f"resp_len={context.get('last_response_len', 0)}",
            f"overlay={context.get('overlay_count', 0)}",
        ]

        if tail:
            parts.append(f"tail={tail}")

        return " | ".join(parts)
    
    async def _track_error(self, error: str, selector_key: str, action: str):
        """Track error for diagnostics endpoint and send Discord notification."""
        context = await self._collect_error_context()
        context_line = self._format_error_context(context)

        GeminiWebAutomation._last_errors[self.worker_id] = {
            "error": error,
            "selector_key": selector_key,
            "action": action,
            "timestamp": datetime.now().isoformat(),
            "worker_id": self.worker_id,
            "request_id": self._request_id,
            "context": context,
        }
        log(f"Error tracked: {error} (selector: {selector_key}, action: {action})", f"Worker {self.worker_id}")
        log(f"Error context: {context_line}", f"Worker {self.worker_id}")

        try:
            await self._screenshot_on_failure(f"error_{action}")
        except:
            pass
        
        # Send Discord notification (non-blocking, won't crash if fails)
        try:
            await notify_error(error, selector_key, action, self.worker_id, diagnostics=context)
        except Exception as e:
            log(f"Discord notification failed: {e}", f"Worker {self.worker_id}")
    
    @classmethod
    def get_all_errors(cls) -> Dict[int, Dict]:
        """Get all tracked errors (for diagnostics endpoint)."""
        return cls._last_errors.copy()
    
    @classmethod
    def clear_errors(cls):
        """Clear all tracked errors."""
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
            # Wait for input to be ready (login check) - try all selectors
            input_found = False
            selectors = self._get_all_selectors("input")
            
            for selector in selectors:
                try:
                    await self.page.wait_for_selector(selector, timeout=10000)
                    input_found = True
                    log(f"Input found with selector: {selector}", f"Worker {self.worker_id}")
                    break
                except:
                    continue
            
            if not input_found:
                # Last attempt with longer timeout on first selector
                try:
                    await self.page.wait_for_selector(selectors[0], timeout=30000)
                    input_found = True
                except Exception as e:
                    await self._track_error(f"Input not found: {e}", "input", "init")
                    raise e
            
            await self._human_delay(500, 1000)
            print("[GeminiWeb] Logged in and ready")
            
            # Default to Temporary Chat if possible for clean sessions
            await self._enable_temp_chat()
            await self._human_delay(200, 500)
            
            self._initialized = True
            return True
        except Exception as e:
            print(f"[GeminiWeb] Init failed: {e}")
            await self._track_error(str(e), "input", "init")
            return False

    async def _enable_temp_chat(self):
        """Enable temporary chat mode."""
        try:
            # Check if temp chat is already enabled using fallback selectors
            for selector in self._get_all_selectors("temp_chat_active"):
                try:
                    locator = self.page.locator(selector).first
                    if await locator.is_visible():
                        return  # Already enabled
                except:
                    continue
            
            # Try to find temp chat button (non-critical, so avoid immediate Discord error spam)
            temp_btn, _ = await self._find_element(
                "temp_chat",
                timeout=3000,
                description="Temporary chat button",
                track_error=False,
            )
            
            if temp_btn is None:
                # Expand sidebar if button not visible
                sidebar_btn, _ = await self._find_element(
                    "sidebar_toggle",
                    timeout=2500,
                    description="Sidebar toggle",
                    track_error=False,
                )
                if sidebar_btn:
                    await sidebar_btn.click()
                    await self._human_delay(300, 500)
                    # Try again
                    temp_btn, _ = await self._find_element(
                        "temp_chat",
                        timeout=3000,
                        description="Temporary chat button",
                        track_error=False,
                    )
            
            if temp_btn and await temp_btn.is_visible():
                await temp_btn.click()
                await self._human_delay(200, 400)
            else:
                # Temporary chat is optional and may be hidden by UI/account state
                log("Temporary chat toggle not visible; continuing", f"Worker {self.worker_id}")
                    
        except Exception as e:
            log(f"Temp chat error: {e}", f"Worker {self.worker_id}")
            await self._track_error(f"Temp chat toggle failed: {e}", "temp_chat", "enable_temp_chat")

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

            # 0.25 Recover if previous request left tab in Stop state
            recovered = await self._recover_stuck_generation()
            if not recovered:
                reason = "Failed to recover from stuck generation state"
                log(reason, f"Worker {self.worker_id}")
                await self._track_error(reason, "send_btn", "preflight_recover")
                return {"success": False, "error": reason}
            
            # 0.5 Periodic hard refresh to clear browser cache/memory
            if self._request_count >= self.REFRESH_EVERY_N_REQUESTS:
                log(f"Hard refresh (request #{self._request_count})", f"Worker {self.worker_id}")
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
                input_ready = False
                for selector in self._get_all_selectors("input"):
                    try:
                        await self.page.wait_for_selector(selector, state="visible", timeout=6000)
                        input_ready = True
                        break
                    except:
                        continue
                if not input_ready:
                    log("Input not immediately visible after hard refresh; continuing", f"Worker {self.worker_id}")
                await self._human_delay(500, 900)
                self._request_count = 0
            
            # 1. New Chat (starts fresh) - VERIFIED
            async def get_copy_state() -> Tuple[int, str]:
                best_count = 0
                best_selector = self._get_selector("copy_btn")
                for selector in self._get_all_selectors("copy_btn"):
                    try:
                        count = await self.page.locator(selector).count()
                        if count > best_count:
                            best_count = count
                            best_selector = selector
                    except:
                        continue
                return best_count, best_selector

            async def get_copy_count():
                count, _ = await get_copy_state()
                return count
            
            worker_id_for_closure = self.worker_id  # Capture for nested functions
            
            async def verify_chat_cleared(before_count):
                # Give extra time for chat to clear
                await self._human_delay(500, 800)
                after_count, _ = await get_copy_state()
                # Success if count dropped (ideally to 0, but at least fewer than before)
                return after_count == 0 or after_count < before_count

            new_chat_selector = await self._resolve_visible_selector("new_chat", timeout=2000)
            new_chat_success = await self._verified_click(
                new_chat_selector,
                "New Chat",
                verify_before=get_copy_count,
                verify_after=verify_chat_cleared,
                timeout=5000
            )
            
            if not new_chat_success:
                error_msg = "New Chat click failed"
                log(error_msg, f"Worker {self.worker_id}")
                await self._track_error(error_msg, "new_chat", "send_message")
                return {"success": False, "error": error_msg}

            
            # 1.5 Enable Temporary Chat
            await self._enable_temp_chat()
            await self._human_delay()

            # 2. Select Model
            if model:
                await self._select_model(model)

            # 3. Enter Prompt - use fallback selector finding
            input_area, input_selector = await self._find_element("input", timeout=5000, description="Input textbox")
            if not input_area:
                await self._track_error("Input textbox not found", "input", "send_message")
                return {"success": False, "error": "Input textbox not found"}
            
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
            pre_send_count, copy_selector = await get_copy_state()

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
                    # If generation started, send succeeded even if input text lags in UI
                    if await self._is_generation_active():
                        return True

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
                        log(f"Send failed: input not cleared ({after_len} chars remain)", f"Worker {worker_id}")
                        return False
                except Exception as e:
                    log(f"Send verification error: {e}", f"Worker {worker_id}")
                    return False  # Don't assume success on error
            
            send_selector = await self._resolve_visible_selector("send_btn", timeout=2000)
            send_success = await self._verified_click(
                send_selector,
                "Send",
                verify_before=get_input_text,
                verify_after=verify_send_worked,
                timeout=5000
            )
            
            if not send_success:
                log(f"Send button click failed", f"Worker {self.worker_id}")
                await self._track_error("Send button click failed", "send_btn", "send_message")
                return {"success": False, "error": "Send button click failed"}
            
            # 5. Wait for Response (Copy button to appear)
            log(f"Waiting for response...", f"Worker {self.worker_id}")
            await self._human_delay(300, 600)  # Reduced initial wait
            
            # Polling for copy button (Wait until we have MORE buttons than before)
            start_time = time.time()
            max_wait = int(os.getenv("BROWSER_TIMEOUT", "480"))
            idle_timeout = 60
            last_activity = time.time()
            last_length = 0
            copy_btn = None

            async def get_response_length() -> int:
                try:
                    return await self.page.evaluate('''
                        () => {
                            const responses = document.querySelectorAll('[data-content-type="response"]');
                            if (responses.length === 0) return 0;
                            const last = responses[responses.length - 1];
                            return (last.innerText || '').length;
                        }
                    ''')
                except:
                    return 0

            while (time.time() - start_time) < max_wait:
                current_count, active_copy_selector = await get_copy_state()
                
                # We need to find a new button (more than we started with)
                if current_count > pre_send_count:
                    btns = self.page.locator(active_copy_selector)
                    # Get the LAST button (the new one)
                    copy_btn = btns.nth(current_count - 1)
                    if await copy_btn.is_visible():
                        copy_selector = active_copy_selector
                        break

                current_length = await get_response_length()
                if current_length > last_length:
                    last_length = current_length
                    last_activity = time.time()
                else:
                    if await self._is_generation_active():
                        last_activity = time.time()
                    elif (time.time() - last_activity) > idle_timeout:
                        break

                await asyncio.sleep(1)  # Reduced from 2s
            
            if not copy_btn:
                elapsed = int(time.time() - start_time)
                generation_active = await self._is_generation_active()
                if elapsed >= max_wait:
                    timeout_reason = f"Timeout after {max_wait}s waiting for response"
                else:
                    timeout_reason = f"Idle timeout after {idle_timeout}s (no activity)"

                if generation_active:
                    timeout_reason = f"{timeout_reason} (generation still active)"
                    await self._stop_generation_if_active(wait_timeout_ms=6000)

                log(timeout_reason, f"Worker {self.worker_id}")
                await self._track_error(timeout_reason, "copy_btn", "wait_for_response")

                fallback = await self._fallback_extract()
                if fallback.get("success"):
                    return fallback
                return {"success": False, "error": timeout_reason}

            # Auto-scroll to ensure copy button is visible
            await self.page.evaluate(f'''
                () => {{
                    const copyButtons = document.querySelectorAll('{copy_selector}');
                    if (copyButtons.length > 0) {{
                        const lastBtn = copyButtons[copyButtons.length - 1];
                        lastBtn.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                    }}
                }}
            ''')
            await self._human_delay(150, 300)

            # 6. Extraction via Copy Button (with lock to prevent clipboard race condition)
            async with GeminiWebAutomation._get_clipboard_lock():
                await copy_btn.click()
                await self._human_delay(100, 200)
                markdown = await self.page.evaluate("navigator.clipboard.readText()")
            
            self._generation_in_progress = False
            
            if not markdown:
                log(f"Clipboard empty, using fallback extraction", f"Worker {self.worker_id}")
                await self._track_error("Clipboard extraction failed, using DOM fallback", "copy_btn", "extract_response")
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
        finally:
            self._generation_in_progress = False

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
        """Select model from dropdown (Fast, Thinking, Pro) with resilient matching."""
        try:
            # Find model button using fallback selectors
            btn, _ = await self._find_element("model_btn", timeout=3000, description="Model button")
            if not btn:
                log(f"Model button not found, skipping model selection", f"Worker {self.worker_id}")
                await self._track_error("Model button not found", "model_btn", "select_model")
                return
            
            # Check if already selected
            current = await btn.inner_text()
            if self._matches_model(current, model_name):
                log(f"Model '{model_name}' already selected", f"Worker {self.worker_id}")
                return
            
            # Open dropdown
            await btn.click()
            await self._human_delay(400, 600)
            
            # Wait for menu panel to appear
            for selector in self._get_all_selectors("menu_panel"):
                try:
                    await self.page.wait_for_selector(selector, state="visible", timeout=2000)
                    break
                except:
                    continue
            
            # Try each menu_item selector until we find items
            items = None
            used_selector = None
            for selector in self._get_all_selectors("menu_item"):
                try:
                    locator = self.page.locator(selector)
                    count = await locator.count()
                    if count > 0:
                        items = locator
                        used_selector = selector
                        log(f"Found {count} menu items with: {selector}", f"Worker {self.worker_id}")
                        break
                except:
                    continue
            
            if items is None or await items.count() == 0:
                await self.page.keyboard.press("Escape")
                await self._human_delay(100, 300)
                await self._track_error("No menu items found with any selector", "menu_item", "select_model")
                return
            
            # Search through items using keyword matching
            item_count = await items.count()
            found_texts = []
            
            for i in range(item_count):
                item = items.nth(i)
                try:
                    text = await item.inner_text()
                    found_texts.append(text.replace('\n', ' ')[:30])
                    
                    if self._matches_model(text, model_name):
                        await item.click()
                        log(f"Selected model: {model_name} (matched: '{text[:30]}')", f"Worker {self.worker_id}")
                        await self._human_delay(300, 600)
                        return
                except:
                    continue

            # Position fallback if no keyword match (assumes cheap to expensive order)
            fallback_index = self._get_position_fallback(model_name, item_count)
            if 0 <= fallback_index < item_count:
                try:
                    item = items.nth(fallback_index)
                    fallback_text = ""
                    try:
                        fallback_text = await item.inner_text()
                    except:
                        pass
                    await item.click()
                    text_preview = fallback_text.replace("\n", " ")[:30] if fallback_text else "unknown"
                    log(
                        f"Selected model: {model_name} (position: {fallback_index}, item: '{text_preview}')",
                        f"Worker {self.worker_id}"
                    )
                    await self._human_delay(300, 600)

                    warning = (
                        f"Model selection used position fallback for '{model_name}'. "
                        f"Position {fallback_index}/{item_count}. "
                        f"Items: {found_texts}"
                    )
                    if used_selector:
                        warning = f"{warning} Selector: {used_selector}"

                    try:
                        await notify_error(warning, "menu_item", "select_model_fallback", self.worker_id)
                    except Exception as notify_err:
                        log(f"Fallback notification failed: {notify_err}", f"Worker {self.worker_id}")
                    return
                except Exception as click_err:
                    log(f"Position fallback failed: {click_err}", f"Worker {self.worker_id}")

            # No match found - close menu and log what we saw
            await self.page.keyboard.press("Escape")
            await self._human_delay(100, 300)
            log(f"Model '{model_name}' not found. Available: {found_texts}", f"Worker {self.worker_id}")
            await self._track_error(f"Model '{model_name}' not found in menu", "menu_item", "select_model")
            
        except Exception as e:
            log(f"Model selection failed: {e}", f"Worker {self.worker_id}")
            await self._track_error(f"Model selection failed: {e}", "model_btn", "select_model")

    async def _paste_image(self, image_path: str):
        """Paste an image via clipboard into Gemini Web."""
        try:
            log(f"Pasting image: {image_path}", f"Worker {self.worker_id}")
            
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
            
            # Focus input first using fallback selectors
            input_area, _ = await self._find_element("input", timeout=3000, description="Input for image paste")
            if input_area:
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
            log(f"Image pasted", f"Worker {self.worker_id}")
        except Exception as e:
            log(f"Image paste failed: {e}", f"Worker {self.worker_id}")
            await self._track_error(f"Image paste failed: {e}", "input", "paste_image")

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

