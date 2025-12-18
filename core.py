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

class AIStudioAutomation:
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
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._initialized = False
        self._owns_browser = False
        # Pending generation tracking (for Koyeb 100s timeout handling)
        self._generation_in_progress = False
        self._pending_result: Optional[Dict] = None

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
            start_time = asyncio.get_event_loop().time()
            max_wait = 300  # 5 minutes max
            
            while (asyncio.get_event_loop().time() - start_time) < max_wait:
                try:
                    btn_text = await button.inner_text(timeout=1000)
                    
                    # Check if "Stop" is gone and we're back to "Run"
                    if "Stop" not in btn_text and "progress_activity" not in btn_text:
                        print("[AIStudio] Generation complete (button shows Run)")
                        await asyncio.sleep(0.3)  # Brief buffer for DOM stability
                        return
                except:
                    pass
                
                await asyncio.sleep(0.2)  # Poll every 200ms
            
            print("[AIStudio] Generation timeout")
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
        try:
            if self.page: await self.page.close()
            if self._owns_browser:
                if self.context: await self.context.close()
                if self.browser: await self.browser.close()
                if self.playwright: await self.playwright.stop()
        except:
            pass

class WorkerPool:
    def __init__(self, worker_count: int = 2):
        self.worker_count = worker_count
        self.workers: List[AIStudioAutomation] = []
        self.available_workers = asyncio.Queue()
        self.shared_context = None
        self.playwright = None
        self._initialized = False
        
        # Request tracking for Koyeb timeout handling
        self._active_requests: Dict[str, int] = {}  # prompt_hash -> worker_idx
        self._pending_results: Dict[str, Dict] = {}  # prompt_hash -> result
        self._result_timestamps: Dict[str, float] = {}  # prompt_hash -> timestamp
        self._lock = asyncio.Lock()
        self.RESULT_TTL = 120  # Results expire after 2 minutes

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
            
            user_data_dir = os.path.join(os.path.dirname(__file__), ".aistudio_data")
            
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

            # Create tabs
            print(f"[WorkerPool] Creating {self.worker_count} worker tabs...")
            tasks = []
            for i in range(self.worker_count):
                print(f"[WorkerPool] Opening tab {i + 1}...")
                page = await self.shared_context.new_page()
                
                print(f"[WorkerPool] Navigating tab {i + 1} to AI Studio...")
                try:
                    await page.goto(AIStudioAutomation.PLAYGROUND_URL, timeout=60000, wait_until="networkidle")
                    print(f"[WorkerPool] ✅ Tab {i + 1} loaded: {page.url}")
                except Exception as nav_err:
                    print(f"[WorkerPool] ⚠️ Tab {i + 1} navigation warning: {nav_err}")
                
                worker = AIStudioAutomation()
                tasks.append(worker.init_with_page(page, self.shared_context))
                self.workers.append(worker)
            
            results = await asyncio.gather(*tasks)
            
            for i, success in enumerate(results):
                if success:
                    self.available_workers.put_nowait((i, self.workers[i]))
            
            self._initialized = True
            return True
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
        
        # New request - get a worker
        worker_handle = await self.available_workers.get()
        idx, worker = worker_handle
        
        async with self._lock:
            self._active_requests[prompt_hash] = idx
        
        try:
            result = await worker.send_message(prompt, model, thinking_level, use_search, images)
            
            async with self._lock:
                # Cache the result in case HTTP timed out and client retries
                self._pending_results[prompt_hash] = result
                self._result_timestamps[prompt_hash] = time.time()
                self._active_requests.pop(prompt_hash, None)
            
            return result
        finally:
            self.available_workers.put_nowait(worker_handle)

    async def close(self):
        for w in self.workers: await w.close()
        if self.shared_context: await self.shared_context.close()
        if self.playwright: await self.playwright.stop()

