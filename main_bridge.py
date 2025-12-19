import os
import asyncio
import time
import json
from typing import List, Optional, Dict, Union
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, BrowserContext, Page


# Configuration
SESSION_DIR = os.path.join(os.path.dirname(__file__), ".browser_session")
STUDIO_URL = "https://aistudio.google.com/prompts/new_chat"
KEEPALIVE_INTERVAL = 3600 * 4  # 4 hours

# Global state
playwright_instance = None
browser_context: Optional[BrowserContext] = None
page: Optional[Page] = None
initialized = False


class OpenAIMessage(BaseModel):
    role: str
    content: str


class OpenAIChatRequest(BaseModel):
    model: str
    messages: List[OpenAIMessage]
    stream: bool = False


async def init_browser():
    """Initialize browser with persistent session (works headlessly)."""
    global playwright_instance, browser_context, page, initialized
    
    print(f"[Server] Starting browser with session from: {SESSION_DIR}")
    
    playwright_instance = await async_playwright().start()
    
    # Use persistent context (headless by default)
    browser_context = await playwright_instance.chromium.launch_persistent_context(
        SESSION_DIR,
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
        viewport={"width": 1280, "height": 800},
    )
    
    # Get or create page
    page = browser_context.pages[0] if browser_context.pages else await browser_context.new_page()
    
    # Navigate to AI Studio
    print("[Server] Navigating to AI Studio...")
    await page.goto(STUDIO_URL, wait_until="networkidle", timeout=60000)
    
    # Check if logged in
    try:
        await page.wait_for_selector('textarea[aria-label="Enter a prompt"]', timeout=15000)
        print("[Server] ✅ Session valid - logged in!")
        initialized = True
        return True
    except:
        print("[Server] ❌ Session invalid or expired. Run setup_session.py first.")
        return False


async def session_keepalive():
    """Periodically touch Google to keep session active."""
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        if initialized and page:
            try:
                print("[Keepalive] Refreshing session...")
                await page.goto("https://accounts.google.com/", wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2)
                await page.goto(STUDIO_URL, wait_until="networkidle", timeout=30000)
                print("[Keepalive] ✅ Session refreshed")
            except Exception as e:
                print(f"[Keepalive] ⚠️ Failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    success = await init_browser()
    if success:
        asyncio.create_task(session_keepalive())
    yield
    if browser_context:
        await browser_context.close()
    if playwright_instance:
        await playwright_instance.stop()


app = FastAPI(title="Gemini Studio Local Bridge", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {
        "status": "ok" if initialized else "error",
        "session_dir": SESSION_DIR,
        "initialized": initialized
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: OpenAIChatRequest):
    global page
    
    if not initialized or not page:
        raise HTTPException(status_code=503, detail="Browser not ready. Run setup_session.py first.")
    
    # Combine messages into a single prompt
    prompt = ""
    for msg in request.messages:
        if msg.role == "system":
            prompt += f"[System]: {msg.content}\n"
        elif msg.role == "user":
            prompt += f"{msg.content}\n"
        elif msg.role == "assistant":
            prompt += f"[Previous response]: {msg.content}\n"
    
    print(f"[API] Processing request ({len(prompt)} chars)...")
    
    try:
        # Navigate to new chat
        await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        
        # Wait for textarea
        await page.wait_for_selector('textarea[aria-label="Enter a prompt"]', timeout=10000)
        
        # Type prompt
        await page.evaluate('''(text) => {
            const textarea = document.querySelector('textarea[aria-label="Enter a prompt"]');
            if (textarea) {
                textarea.value = text;
                textarea.dispatchEvent(new Event('input', {bubbles: true}));
                textarea.focus();
            }
        }''', prompt)
        await asyncio.sleep(1)
        
        # Click Run
        await page.evaluate('''() => {
            const btn = document.querySelector('button[aria-label="Run"]');
            if (btn) btn.click();
        }''')
        
        # Wait for generation (poll for Stop -> Run transition)
        print("[API] Waiting for generation...")
        for _ in range(20):
            btn_text = await page.evaluate('''() => {
                const btn = document.querySelector('button[aria-label="Run"]');
                return btn ? btn.innerText : '';
            }''')
            if "Stop" in btn_text:
                break
            await asyncio.sleep(0.5)
        
        for _ in range(300):  # 5 min max
            btn_text = await page.evaluate('''() => {
                const btn = document.querySelector('button[aria-label="Run"]');
                return btn ? btn.innerText : '';
            }''')
            if "Stop" not in btn_text:
                break
            await asyncio.sleep(1)
        
        await asyncio.sleep(1)  # DOM settle
        
        # Extract response
        content = await page.evaluate('''() => {
            const containers = document.querySelectorAll('.chat-turn-container.model');
            if (containers.length === 0) return null;
            const last = containers[containers.length - 1];
            const markdown = last.querySelector('.markdown-body');
            if (markdown) return markdown.innerText.trim();
            const chunks = last.querySelectorAll('ms-text-chunk');
            if (chunks.length > 0) {
                let text = '';
                chunks.forEach(c => text += c.innerText + ' ');
                return text.trim();
            }
            return last.innerText.trim();
        }''')
        
        if not content:
            raise HTTPException(status_code=500, detail="Failed to extract response")
        
        print(f"[API] Got response ({len(content)} chars)")
        
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": len(prompt),
                "completion_tokens": len(content),
                "total_tokens": len(prompt) + len(content)
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    uvicorn.run(app, host="0.0.0.0", port=8000)

