import asyncio
import os
import random
import time
from typing import List, Dict, Optional, Tuple
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Route

# Low memory mode: block images, fonts, etc.
LOW_MEMORY_MODE = os.getenv("LOW_MEMORY_MODE", "true").lower() == "true"

# Slow VM mode: use JavaScript clicks instead of Playwright clicks (for e2-micro etc)
SLOW_VM_MODE = os.getenv("SLOW_VM_MODE", "true").lower() == "true"

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

    def __init__(self):
        super().__init__()
        self._is_sleeping = False
        self._wake_lock = asyncio.Lock()
        self._idle_lock = asyncio.Lock()
        self._keepalive_task = None
        self._idle_task = None
        self._stop_tasks = False

    async def init_with_page(self, page: Page, context: BrowserContext) -> bool:
        """Initialize with externally provided page (multi-tab mode)."""
        self.page = page
        self.context = context
        self._owns_browser = False
        
        try:
            # Check if we are seeing the playground. 
            # If not, and we are in non-headless mode, wait longer for user to login manually.
            is_headless = os.getenv("HEADLESS", "true").lower() == "true"
            timeout = 15000 if is_headless else 300000 # 5 minutes for manual login
            
            if not is_headless:
                print("[AIStudio] ℹ️ Non-headless mode: If you are not logged in, please log in now in the browser window.")

            await self.page.wait_for_selector('textarea[aria-label="Enter a prompt"]', timeout=timeout)
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
            return result
        except Exception as e:
            print(f"[AIStudio] ⚠️ JS click failed for {description}: {e}")
            return False

    async def _wait_and_extract_pending(self) -> Dict:
        """
        Handle retry after HTTP timeout - wait for any in-progress generation and extract result.
        Called when client retries after Koyeb's 100s timeout.
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
        Handles Koyeb's 100s timeout - if generation is in progress, waits for it.
        """
        if not self._initialized:
            return {"success": False, "error": "Automation not initialized"}

        # Check if there's a pending result from a previous timed-out request
        if self._pending_result:
            result = self._pending_result
            self._pending_result = None
            print("[AIStudio] ✅ Returning cached pending result")
            return result
        
        # Check if generation is already in progress (client retried after timeout)
        if self._generation_in_progress:
            print("[AIStudio] ⏳ Generation already in progress, waiting for it...")
            return await self._wait_and_extract_pending()

        try:
            self._generation_in_progress = True
            
            # 0. Dismiss any popups/tooltips
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            
            # 1. Navigate to new chat via URL (more reliable than clicking)
            print("[AIStudio] Creating new chat session...")
            await self.page.goto(self.PLAYGROUND_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)  # Let page stabilize
            
            # 2. Wait for textarea to be ready
            try:
                await self.page.wait_for_selector('textarea[aria-label="Enter a prompt"]', timeout=15000)
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
            await asyncio.sleep(1)  # Let UI update
            
            # 7. Click Run button using JavaScript
            print("[AIStudio] Generating response...")
            clicked = await self.js_click('button[aria-label="Run"]', "Run button")
            if not clicked:
                # Fallback: try keyboard shortcut
                await self.page.keyboard.press("Control+Enter")
            await asyncio.sleep(1)
            
            # 8. Wait for Generation
            await self._wait_for_generation()
            
            # 9. Extract Markdown
            markdown = await self._extract_markdown()
            
            self._generation_in_progress = False
            
            if not markdown:
                return {"success": False, "error": "Failed to extract markdown response"}
            
            result = {"success": True, "response": markdown}
            # Cache result in case client timed out and retries
            self._pending_result = result
            return result

        except Exception as e:
            self._generation_in_progress = False
            print(f"[AIStudio] Interaction error: {e}")
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
            await asyncio.sleep(0.3)  # Reduced
            
            search_input = self.page.locator('input[placeholder*="Search"], input[aria-label*="Search"]')
            await search_input.fill(model_id)
            await asyncio.sleep(0.3)  # Reduced
            
            model_btn = self.page.locator(f'button:has-text("{model_id}"), button[id*="{model_id}"]').first
            await model_btn.click(timeout=3000)
            await asyncio.sleep(0.2)
            print(f"[AIStudio] ✅ Model {model_id} selected")
        except Exception as e:
            print(f"[AIStudio] ⚠️ Model selection warning: {e}")

    async def _set_thinking_level(self, level: str):
        """Set thinking level from dropdown."""
        try:
            print(f"[AIStudio] Setting thinking level: {level}")
            await self.page.click('mat-select[aria-label="Thinking Level"]', timeout=3000)
            await asyncio.sleep(0.3)  # Reduced from 0.5s
            await self.page.click(f'mat-option:has-text("{level}")', timeout=3000)
            await asyncio.sleep(0.2)  # Reduced from 0.5s
        except Exception as e:
            print(f"[AIStudio] Thinking level warning: {e}")

    async def _paste_image(self, image_path: str):
        """Paste an image via clipboard into AI Studio."""
        try:
            print(f"[AIStudio] Pasting image: {image_path}")
            
            # Read image file as base64
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            import base64
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
            await asyncio.sleep(0.1)
            
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
            
            # Wait for image to appear in the prompt area
            await asyncio.sleep(1.0)  # Give time for upload
            
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
                await asyncio.sleep(0.5)
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
                await asyncio.sleep(0.1)
            
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
                
                await asyncio.sleep(1)  # Poll every 1 second
            
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
                
                await asyncio.sleep(0.1)
            
        except Exception as e:
            print(f"[AIStudio] Wait warning: {e}")


    async def _extract_markdown(self) -> Optional[str]:
        """Extract response as markdown via the 'Copy as markdown' button."""
        try:
            print("[AIStudio] Extracting response...")
            
            # On slow VMs, skip the clipboard method entirely (too many timeouts)
            if SLOW_VM_MODE:
                await asyncio.sleep(1)  # Brief wait for DOM to settle
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
                        await asyncio.sleep(0.2)  # Minimal time for button to appear
                    
                    # Now click the button
                    last_menu = menus.nth(menu_count - 1)
                    await last_menu.click(timeout=3000)
                    print("[AIStudio] Clicked menu button")
                except Exception as click_err:
                    print(f"[AIStudio] Click failed, trying JS: {click_err}")
                    # Force via JavaScript (hover + click)
                    try:
                        await self.page.evaluate('''\n                            (() => {\n                                const containers = document.querySelectorAll('.chat-turn-container.model');\n                                if (containers.length > 0) {\n                                    const last = containers[containers.length - 1];\n                                    last.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));\n                                    setTimeout(() => {\n                                        const btn = last.querySelector('button[aria-label=\"Open options\"]');\n                                        if (btn) btn.click();\n                                    }, 200);\n                                }\n                            })()\n                        ''')
                        await asyncio.sleep(0.3)
                        print("[AIStudio] Clicked via JS")
                    except Exception as js_err:
                        print(f"[AIStudio] JS also failed: {js_err}")
                        return await self._extract_from_dom()
                
                await asyncio.sleep(0.2)  # Wait for menu to appear
                
                # Find and click "Copy as markdown"
                copy_btn = self.page.locator('button:has-text("Copy as markdown")')
                try:
                    if await copy_btn.count() > 0:
                        await copy_btn.first.click(timeout=3000)
                        print("[AIStudio] Clicked 'Copy as markdown'")
                        await asyncio.sleep(0.15)  # Minimal time for clipboard
                        
                        # Read from clipboard
                        markdown = await self.page.evaluate("navigator.clipboard.readText()")
                        if markdown and len(markdown.strip()) > 0:
                            print(f"[AIStudio] ✅ Got {len(markdown)} chars via clipboard")
                            return markdown.strip()
                    else:
                        print("[AIStudio] 'Copy as markdown' not found, pressing Escape")
                        await self.page.keyboard.press("Escape")
                except Exception as copy_err:
                    print(f"[AIStudio] Copy failed: {copy_err}")
                    await self.page.keyboard.press("Escape")
            
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
            await asyncio.sleep(0.5)
            
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
    
    # Selectors discovered during research
    SELECTORS = {
        "input": 'div[role="textbox"][aria-label="Enter a prompt here"]',
        "send_btn": 'button[aria-label="Send message"]',
        "model_btn": 'button.input-area-switch',
        "new_chat": 'button[aria-label="New chat"]',
        "temp_chat": 'button[aria-label="Temporary chat"]',
        "copy_btn": 'button[aria-label="Copy"]',
        "menu_panel": '.mat-mdc-menu-panel',
        "menu_item": 'button.mat-mdc-menu-item'
    }

    async def init_with_page(self, page: Page, context: BrowserContext) -> bool:
        self.page = page
        self.context = context
        try:
            # Wait for input to be ready (login check)
            await self.page.wait_for_selector(self.SELECTORS["input"], timeout=30000)
            print("[GeminiWeb] ✅ Logged in and ready")
            
            # Default to Temporary Chat if possible for clean sessions
            await self._enable_temp_chat()
            
            self._initialized = True
            return True
        except Exception as e:
            print(f"[GeminiWeb] ❌ Init failed: {e}")
            return False

    async def _enable_temp_chat(self):
        """Try to enable temporary chat if it's not already on."""
        try:
            # First check if sidebar is open or needs opening
            # For now, just try to click if visible
            temp_btn = self.page.locator(self.SELECTORS["temp_chat"])
            if await temp_btn.is_visible():
                await temp_btn.click()
                print("[GeminiWeb] ✅ Enabled Temporary Chat")
        except:
            pass

    async def send_message(self, prompt: str, model: str = None, thinking_level: str = None, use_search: bool = False, images: List[str] = None) -> Dict:
        if not self._initialized: return {"success": False, "error": "Not initialized"}
        
        if self._pending_result:
            res = self._pending_result
            self._pending_result = None
            return res

        try:
            self._generation_in_progress = True
            
            # 1. New Chat (starts fresh)
            print("[GeminiWeb] Starting new chat...")
            new_chat_btn = self.page.locator(self.SELECTORS["new_chat"]).first
            if await new_chat_btn.is_visible():
                await new_chat_btn.click()
                await asyncio.sleep(1)

            # 2. Select Model
            if model:
                await self._select_model(model)

            # 3. Enter Prompt
            print(f"[GeminiWeb] Entering prompt...")
            input_area = self.page.locator(self.SELECTORS["input"])
            await input_area.click()
            
            # 3.5 Paste Images (if provided)
            if images:
                for img_path in images:
                    await self._paste_image(img_path)
            
            await input_area.fill(prompt)
            await asyncio.sleep(0.5)

            # 4. Click Send
            await self.page.click(self.SELECTORS["send_btn"])
            
            # 5. Wait for Response (Copy button to appear)
            print("[GeminiWeb] Waiting for response...")
            # We wait for the Copy button of the LAST message to be visible
            # but usually it's better to wait for the generation to stop (no more loading states)
            await asyncio.sleep(2) # Initial wait
            
            # Polling for copy button
            start_time = time.time()
            max_wait = 120
            copy_btn = None
            while (time.time() - start_time) < max_wait:
                btns = self.page.locator(self.SELECTORS["copy_btn"])
                count = await btns.count()
                if count > 0:
                    copy_btn = btns.nth(count - 1)
                    if await copy_btn.is_visible():
                        break
                await asyncio.sleep(1)
            
            if not copy_btn:
                 return {"success": False, "error": "Timeout waiting for copy button"}

            # Auto-scroll to ensure copy button is visible
            print("[GeminiWeb] Scrolling to copy button...")
            await self.page.evaluate('''
                () => {
                    const copyButtons = document.querySelectorAll('button[aria-label="Copy"]');
                    if (copyButtons.length > 0) {
                        const lastBtn = copyButtons[copyButtons.length - 1];
                        lastBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                }
            ''')
            await asyncio.sleep(0.5)  # Wait for scroll to complete

            # 6. Extraction via Copy Button
            print("[GeminiWeb] Extracting markdown...")
            await copy_btn.click()
            await asyncio.sleep(0.5) # Wait for clipboard
            
            markdown = await self.page.evaluate("navigator.clipboard.readText()")
            self._generation_in_progress = False
            
            if not markdown:
                return {"success": False, "error": "Clipboard empty after copy"}

            result = {"success": True, "response": markdown.strip()}
            self._pending_result = result
            return result

        except Exception as e:
            self._generation_in_progress = False
            print(f"[GeminiWeb] Error: {e}")
            # Retry extraction once via DOM fallback
            return await self._fallback_extract()

    async def _fallback_extract(self) -> Dict:
        """Fallback extraction via DOM if copy button fails."""
        try:
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
            await asyncio.sleep(0.5)
            
            # Select from menu
            items = self.page.locator(self.SELECTORS["menu_item"])
            for i in range(await items.count()):
                item = items.nth(i)
                text = await item.inner_text()
                if model_name.lower() in text.lower():
                    await item.click()
                    print(f"[GeminiWeb] ✅ Selected model: {model_name}")
                    await asyncio.sleep(0.5)
                    return
            # If not found, close menu
            await self.page.keyboard.press("Escape")
        except Exception as e:
            print(f"[GeminiWeb] ⚠️ Model selection failed: {e}")

    async def _wait_and_extract_pending(self) -> Dict:
        # Simplified for web: just try to extract if button exists
        btns = self.page.locator(self.SELECTORS["copy_btn"])
        count = await btns.count()
        if count > 0:
            copy_btn = btns.nth(count - 1)
            await copy_btn.click()
            await asyncio.sleep(0.5)
            markdown = await self.page.evaluate("navigator.clipboard.readText()")
            if markdown:
                return {"success": True, "response": markdown.strip()}
        return {"success": False, "error": "Still generating or failed"}

    async def _paste_image(self, image_path: str):
        """Paste an image via clipboard into Gemini Web."""
        try:
            print(f"[GeminiWeb] Pasting image: {image_path}")
            
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            import base64
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
            print(f"[GeminiWeb] ✅ Image pasted")
        except Exception as e:
            print(f"[GeminiWeb] ⚠️ Image paste warning: {e}")

    async def close(self):
        await super().close()

class WorkerPool:
    """Dual-provider worker pool with auto-routing based on model name."""
    
    # Models that route to Gemini Web
    GEMINI_WEB_MODELS = ["thinking", "pro", "fast"]
    
    def __init__(self, worker_count: int = 1, provider: str = "auto"):
        self.worker_count = worker_count
        self.provider = provider.lower()  # "auto", "aistudio", or "gemini-web"
        
        # Separate workers for each provider
        self.aistudio_worker: Optional[AIStudioAutomation] = None
        self.geminiweb_worker: Optional[GeminiWebAutomation] = None
        
        self.shared_context = None
        self.playwright = None
        self._initialized = False
        
        # Request tracking for Koyeb timeout handling
        self._active_requests: Dict[str, str] = {}  # prompt_hash -> provider
        self._pending_results: Dict[str, Dict] = {}  # prompt_hash -> result
        self._result_timestamps: Dict[str, float] = {}  # prompt_hash -> timestamp
        self._lock = asyncio.Lock()
        self.RESULT_TTL = 120  # Results expire after 2 minutes
        
        # Idle/Keepalive settings
        self.IDLE_TIMEOUT_MINUTES = int(os.getenv("IDLE_TIMEOUT_MINUTES", "30"))
        self.KEEPALIVE_HOURS = float(os.getenv("KEEPALIVE_HOURS", "4"))
        self._last_activity = time.time()
        self._is_sleeping = False
        self._wake_lock = asyncio.Lock()
        self._idle_lock = asyncio.Lock()
        self._tasks_started = False
        self._keepalive_task = None
        self._idle_task = None

    def _route_model(self, model: str) -> str:
        """Determine which provider to use based on model name."""
        if self.provider in ["aistudio", "gemini-web"]:
            return self.provider  # Fixed provider mode
        
        # Auto-routing based on model name
        if model:
            model_lower = model.lower()
            for gw_model in self.GEMINI_WEB_MODELS:
                if gw_model in model_lower:
                    return "gemini-web"
        return "aistudio"  # Default

    def _hash_prompt(self, prompt: str) -> str:
        """Create a hash of the prompt for tracking."""
        import hashlib
        # Use first 1000 chars to avoid hashing huge prompts
        return hashlib.md5(prompt[:1000].encode()).hexdigest()[:16]

    async def _cleanup_stale(self):
        """Remove expired results."""
        now = time.time()
        stale_keys = [k for k, ts in self._result_timestamps.items() 
                      if now - ts > self.RESULT_TTL]
        for key in stale_keys:
            self._pending_results.pop(key, None)
            self._result_timestamps.pop(key, None)
            self._active_requests.pop(key, None)

    async def _keepalive_loop(self):
        """Background task to visit Google occasionally to keep session alive."""
        print(f"[WorkerPool] 🕒 Keepalive background task started (every {self.KEEPALIVE_HOURS}h)")
        while not self._tasks_started: await asyncio.sleep(1) # Wait for init
        
        while True:
            try:
                await asyncio.sleep(self.KEEPALIVE_HOURS * 3600)
                
                # Check if we are busy
                async with self._lock:
                    if len(self._active_requests) > 0:
                        print("[WorkerPool] ⏳ Skipping keepalive, system busy")
                        continue
                
                print("[WorkerPool] 💤 Visiting Google for session keepalive...")
                async with self._wake_lock: # Ensure we don't clash with wake/sleep
                    temp_page = await self.shared_context.new_page()
                    try:
                        await temp_page.goto("https://www.google.com", timeout=30000)
                        await asyncio.sleep(2)
                        print("[WorkerPool] ✅ Keepalive visit complete")
                    finally:
                        await temp_page.close()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[WorkerPool] ⚠️ Keepalive error: {e}")
                await asyncio.sleep(60)

    async def _idle_monitor(self):
        """Background task to close tabs when idle."""
        print(f"[WorkerPool] 💤 Idle monitor started (timeout: {self.IDLE_TIMEOUT_MINUTES}m)")
        while True:
            try:
                await asyncio.sleep(60) # Check every minute
                
                now = time.time()
                idle_time = (now - self._last_activity) / 60
                
                if idle_time >= self.IDLE_TIMEOUT_MINUTES and not self._is_sleeping:
                    async with self._idle_lock:
                        # Double check under lock
                        async with self._lock:
                            if len(self._active_requests) > 0:
                                continue
                        
                        print(f"[WorkerPool] 🛌 System idle for {idle_time:.1f}m. Entering sleep mode...")
                        
                        # Close all worker pages but keep context (and cookies) alive
                        for w in self.workers:
                            try:
                                if w.page:
                                    await w.page.close()
                                    w.page = None
                                    w._initialized = False
                            except: pass
                        
                        self._is_sleeping = True
                        print("[WorkerPool] ✅ Sleep mode active (tabs closed)")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[WorkerPool] ⚠️ Idle monitor error: {e}")

    async def _wake_up(self):
        """Re-open worker pages if sleeping."""
        async with self._wake_lock:
            if not self._is_sleeping:
                return
            
            print("[WorkerPool] ☕ Waking up from sleep mode...")
            tasks = []
            for i, worker in enumerate(self.workers):
                print(f"[WorkerPool] Re-opening tab {i + 1}...")
                page = await self.shared_context.new_page()
                try:
                    target_url = AIStudioAutomation.PLAYGROUND_URL if self.provider == "aistudio" else GeminiWebAutomation.URL
                    await page.goto(target_url, timeout=60000, wait_until="networkidle")
                except Exception as e:
                    print(f"[WorkerPool] ⚠️ Wake-up tab {i+1} warning: {e}")
                
                tasks.append(worker.init_with_page(page, self.shared_context))
            
            results = await asyncio.gather(*tasks)
            
            # Reset queue with initialized workers
            while not self.available_workers.empty():
                self.available_workers.get_nowait()
                
            for i, success in enumerate(results):
                if success:
                    self.available_workers.put_nowait((i, self.workers[i]))
            
            self._is_sleeping = False
            self._last_activity = time.time()
            print("[WorkerPool] ✅ System fully awake")

    async def _wake_up_worker(self, provider: str):
        """Wake up a specific provider's worker."""
        async with self._wake_lock:
            if provider == "aistudio" and self.aistudio_worker:
                if self.aistudio_worker.page is None:
                    page = await self.shared_context.new_page()
                    await page.goto(AIStudioAutomation.PLAYGROUND_URL, timeout=60000, wait_until="networkidle")
                    await self.aistudio_worker.init_with_page(page, self.shared_context)
            elif provider == "gemini-web" and self.geminiweb_worker:
                if self.geminiweb_worker.page is None:
                    page = await self.shared_context.new_page()
                    await page.goto(GeminiWebAutomation.URL, timeout=60000, wait_until="networkidle")
                    await self.geminiweb_worker.init_with_page(page, self.shared_context)

    async def init(self, cookies: List[Dict]) -> bool:
        """Launch shared browser and N tabs."""
        try:
            self.playwright = await async_playwright().start()
            
            is_headless = os.getenv("HEADLESS", "true").lower() == "true"
            
            browser_args = AIStudioAutomation.BROWSER_ARGS.copy()
            if is_headless:
                browser_args.extend(AIStudioAutomation.HEADLESS_ARGS)
            if LOW_MEMORY_MODE:
                browser_args.extend(AIStudioAutomation.LOW_MEMORY_ARGS)
            
            # Use .browser_session as requested for persistence
            user_data_dir = os.path.join(os.path.dirname(__file__), ".browser_session")
            
            self.shared_context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=is_headless,
                args=browser_args,
                user_agent=AIStudioAutomation.USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                permissions=["clipboard-read", "clipboard-write"],
            )
            
            # Inject cookies once
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

            # Create dual-provider tabs
            print("[WorkerPool] Creating dual-provider workers (AI Studio + Gemini Web)...")
            
            # Tab 1: AI Studio
            print("[WorkerPool] Opening AI Studio tab...")
            page1 = await self.shared_context.new_page()
            try:
                await page1.goto(AIStudioAutomation.PLAYGROUND_URL, timeout=60000, wait_until="networkidle")
                print(f"[WorkerPool] ✅ AI Studio tab loaded: {page1.url}")
            except Exception as e:
                print(f"[WorkerPool] ⚠️ AI Studio navigation warning: {e}")
            
            self.aistudio_worker = AIStudioAutomation()
            aistudio_ok = await self.aistudio_worker.init_with_page(page1, self.shared_context)
            
            # Tab 2: Gemini Web
            print("[WorkerPool] Opening Gemini Web tab...")
            page2 = await self.shared_context.new_page()
            try:
                await page2.goto(GeminiWebAutomation.URL, timeout=60000, wait_until="networkidle")
                print(f"[WorkerPool] ✅ Gemini Web tab loaded: {page2.url}")
            except Exception as e:
                print(f"[WorkerPool] ⚠️ Gemini Web navigation warning: {e}")
            
            self.geminiweb_worker = GeminiWebAutomation()
            geminiweb_ok = await self.geminiweb_worker.init_with_page(page2, self.shared_context)
            
            print(f"[WorkerPool] Workers ready - AI Studio: {aistudio_ok}, Gemini Web: {geminiweb_ok}")
            
            self._initialized = True
            return aistudio_ok or geminiweb_ok  # Success if at least one works
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
        Send message with proper request tracking for Koyeb timeout handling.
        If this prompt was already submitted and is pending, wait for that result.
        """
        prompt_hash = self._hash_prompt(prompt)
        
        async with self._lock:
            await self._cleanup_stale()
            
            # Update activity
            self._last_activity = time.time()
            
            # Handle sleep mode
            if self._is_sleeping:
                print("[WorkerPool] 🛌 System is sleeping, triggering wake up...")
                await self._wake_up()

            # Check if we have a cached result for this exact prompt
            if prompt_hash in self._pending_results:
                result = self._pending_results.pop(prompt_hash)
                self._result_timestamps.pop(prompt_hash, None)
                self._active_requests.pop(prompt_hash, None)
                print(f"[WorkerPool] ✅ Returning cached result for {prompt_hash}")
                return result
            
            # Check if this prompt is already being processed
            if prompt_hash in self._active_requests:
                worker_idx = self._active_requests[prompt_hash]
                worker = self.workers[worker_idx]
                print(f"[WorkerPool] ⏳ Request {prompt_hash} already in progress on worker {worker_idx}")
                
                # Wait for the existing generation
                result = await worker._wait_and_extract_pending()
                
                # Cache it
                self._pending_results[prompt_hash] = result
                self._result_timestamps[prompt_hash] = time.time()
                self._active_requests.pop(prompt_hash, None)
                
                return result
        # Route to correct provider based on model
        target_provider = self._route_model(model)
        print(f"[WorkerPool] Routing to {target_provider} for model '{model}'")
        
        # Get the appropriate worker
        if target_provider == "gemini-web":
            worker = self.geminiweb_worker
            if not worker or not worker._initialized:
                await self._wake_up_worker("gemini-web")
                worker = self.geminiweb_worker
        else:
            worker = self.aistudio_worker
            if not worker or not worker._initialized:
                await self._wake_up_worker("aistudio")
                worker = self.aistudio_worker
        
        if not worker:
            return {"success": False, "error": f"Worker for {target_provider} not available"}
        
        async with self._lock:
            self._active_requests[prompt_hash] = target_provider
        
        try:
            result = await worker.send_message(prompt, model, thinking_level, use_search, images)
            
            async with self._lock:
                self._pending_results[prompt_hash] = result
                self._result_timestamps[prompt_hash] = time.time()
                self._active_requests.pop(prompt_hash, None)
            
            return result
        finally:
            self._last_activity = time.time()

    async def close(self):
        # Cancel background tasks
        if self._idle_task: self._idle_task.cancel()
        if self._keepalive_task: self._keepalive_task.cancel()
        
        if self.aistudio_worker: await self.aistudio_worker.close()
        if self.geminiweb_worker: await self.geminiweb_worker.close()
        if self.shared_context: await self.shared_context.close()
        if self.playwright: await self.playwright.stop()

