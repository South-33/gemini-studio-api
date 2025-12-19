# Chrome Bridge Mode - Use your existing Chrome session
# This connects to a running Chrome instance instead of managing cookies
#
# SETUP:
# 1. Close Chrome completely
# 2. Relaunch Chrome with: chrome.exe --remote-debugging-port=9222
# 3. Log into Google/AI Studio in that Chrome window
# 4. Run this API - it will connect to your Chrome session
#
# The session stays alive as long as Chrome is running!

import asyncio
import os
from typing import Optional, Dict, List
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


class ChromeBridge:
    """
    Connects to an existing Chrome instance via Chrome DevTools Protocol (CDP).
    Uses your real Chrome session - no cookie management needed!
    """
    
    CDP_URL = "http://127.0.0.1:9222"
    STUDIO_URL = "https://aistudio.google.com/prompts/new_chat"
    
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._initialized = False
    
    async def connect(self) -> bool:
        """Connect to existing Chrome instance."""
        try:
            print("[ChromeBridge] Connecting to Chrome on port 9222...")
            
            self.playwright = await async_playwright().start()
            
            # Connect to Chrome via CDP
            self.browser = await self.playwright.chromium.connect_over_cdp(self.CDP_URL)
            
            # Get the default context (your logged-in session)
            contexts = self.browser.contexts
            if contexts:
                self.context = contexts[0]
                print(f"[ChromeBridge] ✅ Connected! Found {len(contexts)} context(s)")
            else:
                print("[ChromeBridge] ⚠️ No existing context, creating one...")
                self.context = await self.browser.new_context()
            
            # Find or create AI Studio tab
            pages = self.context.pages
            studio_page = None
            
            for p in pages:
                if "aistudio.google.com" in p.url:
                    studio_page = p
                    print(f"[ChromeBridge] Found existing AI Studio tab")
                    break
            
            if not studio_page:
                print("[ChromeBridge] Opening new AI Studio tab...")
                studio_page = await self.context.new_page()
                await studio_page.goto(self.STUDIO_URL, wait_until="networkidle", timeout=60000)
            
            self.page = studio_page
            
            # Verify we're logged in
            try:
                await self.page.wait_for_selector('textarea[aria-label="Enter a prompt"]', timeout=10000)
                print("[ChromeBridge] ✅ Logged in and ready!")
                self._initialized = True
                return True
            except:
                print("[ChromeBridge] ❌ Not logged in. Please log in to AI Studio in Chrome.")
                return False
                
        except Exception as e:
            if "connect" in str(e).lower():
                print(f"""
[ChromeBridge] ❌ Could not connect to Chrome!

Make sure Chrome is running with remote debugging:
1. Close ALL Chrome windows
2. Open Command Prompt/PowerShell and run:
   
   "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222

3. Log into AI Studio in that Chrome window
4. Restart this API
                """)
            else:
                print(f"[ChromeBridge] ❌ Connection error: {e}")
            return False
    
    async def send_message(self, prompt: str, model: str = None, thinking_level: str = None) -> Dict:
        """Send a message using the existing Chrome session."""
        if not self._initialized:
            return {"success": False, "error": "Chrome bridge not connected"}
        
        try:
            # Navigate to new chat
            print("[ChromeBridge] Navigating to new chat...")
            await self.page.goto(self.STUDIO_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)  # Wait for page to stabilize
            
            # Wait for textarea to appear
            await self.page.wait_for_selector('textarea[aria-label="Enter a prompt"]', timeout=10000)
            
            # Type prompt
            print(f"[ChromeBridge] Typing prompt ({len(prompt)} chars)...")
            await self.page.evaluate('''(text) => {
                const textarea = document.querySelector('textarea[aria-label="Enter a prompt"]');
                if (textarea) {
                    textarea.value = text;
                    textarea.dispatchEvent(new Event('input', {bubbles: true}));
                    textarea.focus();
                }
            }''', prompt)
            await asyncio.sleep(1)  # Wait for input to register
            
            # Click Run
            print("[ChromeBridge] Clicking Run...")
            await self.page.evaluate('''() => {
                const btn = document.querySelector('button[aria-label="Run"]');
                if (btn) btn.click();
            }''')
            
            # Wait for generation
            await self._wait_for_generation()
            
            # Wait a bit more for DOM to settle
            await asyncio.sleep(1)
            
            # Extract response
            response = await self._extract_response()
            
            if response:
                return {"success": True, "response": response}
            else:
                return {"success": False, "error": "Failed to extract response"}
                
        except Exception as e:
            print(f"[ChromeBridge] Error: {e}")
            return {"success": False, "error": str(e)}
    
    async def _wait_for_generation(self):
        """Wait for AI Studio to finish generating."""
        print("[ChromeBridge] Waiting for generation...")
        
        # Wait for Stop button to appear (generation started)
        for _ in range(20):
            btn_text = await self.page.evaluate('''() => {
                const btn = document.querySelector('button[aria-label="Run"]');
                return btn ? btn.innerText : '';
            }''')
            if "Stop" in btn_text:
                break
            await asyncio.sleep(0.5)
        
        # Wait for Run button to return (generation complete)
        for _ in range(300):  # 5 min max
            btn_text = await self.page.evaluate('''() => {
                const btn = document.querySelector('button[aria-label="Run"]');
                return btn ? btn.innerText : '';
            }''')
            if "Stop" not in btn_text:
                print("[ChromeBridge] ✅ Generation complete")
                return
            await asyncio.sleep(1)
        
        print("[ChromeBridge] ⚠️ Generation timeout")
    
    async def _extract_response(self) -> Optional[str]:
        """Extract the model's response from the page."""
        await asyncio.sleep(1)  # Increased wait
        
        # Debug: Check what containers exist
        debug_info = await self.page.evaluate('''() => {
            const containers = document.querySelectorAll('.chat-turn-container');
            const modelContainers = document.querySelectorAll('.chat-turn-container.model');
            const allText = document.querySelectorAll('ms-text-chunk');
            return {
                totalContainers: containers.length,
                modelContainers: modelContainers.length,
                textChunks: allText.length,
                url: window.location.href
            };
        }''')
        print(f"[ChromeBridge] Debug: {debug_info}")
        
        text = await self.page.evaluate('''() => {
            const containers = document.querySelectorAll('.chat-turn-container.model');
            if (containers.length === 0) {
                // Try alternative selectors
                const altContainers = document.querySelectorAll('[data-turn-role="model"]');
                if (altContainers.length > 0) {
                    return altContainers[altContainers.length - 1].innerText;
                }
                return null;
            }
            
            const last = containers[containers.length - 1];
            
            // Try markdown-body first
            const markdown = last.querySelector('.markdown-body');
            if (markdown && markdown.innerText.trim().length > 5) {
                return markdown.innerText.trim();
            }
            
            // Try ms-text-chunk
            const chunks = last.querySelectorAll('ms-text-chunk');
            if (chunks.length > 0) {
                let text = '';
                chunks.forEach(c => text += c.innerText + ' ');
                if (text.trim().length > 5) return text.trim();
            }
            
            // Last resort - get all text
            const fullText = last.innerText;
            if (fullText && fullText.trim().length > 10) {
                return fullText.trim();
            }
            
            return null;
        }''')
        
        if text and len(text) > 5:
            print(f"[ChromeBridge] Extracted {len(text)} chars")
            return text
        
        print("[ChromeBridge] Could not extract response")
        return None
    
    async def disconnect(self):
        """Disconnect from Chrome (doesn't close Chrome)."""
        try:
            if self.playwright:
                await self.playwright.stop()
        except:
            pass


# Quick test
async def test_bridge():
    bridge = ChromeBridge()
    try:
        if await bridge.connect():
            print("\n--- Testing message send ---")
            result = await bridge.send_message("Reply with exactly: BRIDGE_OK")
            print(f"\nResult: {result}")
            return result
        else:
            print("Failed to connect")
            return None
    except Exception as e:
        print(f"Error: {e}")
        return None
    finally:
        try:
            await bridge.disconnect()
        except:
            pass


if __name__ == "__main__":
    import sys
    
    # Windows needs ProactorEventLoop for subprocess support (Playwright)
    if sys.platform == 'win32':
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(test_bridge())
        finally:
            loop.close()
    else:
        result = asyncio.run(test_bridge())
    
    print(f"\nFinal: {'SUCCESS' if result and result.get('success') else 'FAILED'}")
