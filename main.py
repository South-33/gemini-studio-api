import sys
import os

# Force unbuffered output (critical for Windows/ngrok)
# Method 1: Set environment variable (for subprocesses)
os.environ['PYTHONUNBUFFERED'] = '1'
# Method 2: Reconfigure stdout/stderr
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(line_buffering=True)

import asyncio
import threading
import base64
import tempfile
import json
import time
from datetime import datetime
from typing import List, Optional, Dict, Union
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# --- Timestamped Logging (use stderr - always unbuffered) ---
def log(msg: str, tag: str = "Server"):
    """Print with timestamp for debugging. Uses stderr for guaranteed immediate output."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    # Use stderr (unbuffered) instead of stdout
    print(f"[{ts}] [{tag}] {msg}", file=sys.stderr, flush=True)

# Load .env file
load_dotenv()

from core import WorkerPool

# Configuration
WORKER_COUNT = int(os.getenv("WORKER_COUNT", "1"))
PROVIDER = os.getenv("PROVIDER", "auto").lower()  # "auto", "aistudio", or "gemini-web"

# Global State
worker_pool: Optional[WorkerPool] = None
browser_loop: Optional[asyncio.AbstractEventLoop] = None
browser_thread: Optional[threading.Thread] = None

def run_browser_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

async def init_browser_thread():
    global worker_pool, browser_loop, browser_thread
    browser_loop = asyncio.ProactorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
    browser_thread = threading.Thread(target=run_browser_loop, args=(browser_loop,), daemon=True)
    browser_thread.start()
    
    worker_pool = WorkerPool(worker_count=WORKER_COUNT, provider=PROVIDER)
    # Empty cookies - we use persistent browser session instead
    future = asyncio.run_coroutine_threadsafe(worker_pool.init([]), browser_loop)
    return future.result(timeout=120)

@asynccontextmanager
async def lifespan(app: FastAPI):
    success = await init_browser_thread()
    if success:
        log(f"✅ Multi-Worker Pool Ready ({WORKER_COUNT} workers)")
    
    yield
    if worker_pool:
        future = asyncio.run_coroutine_threadsafe(worker_pool.close(), browser_loop)
        future.result(timeout=10)


app = FastAPI(title="Gemini Studio API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Models ---

class OpenAIMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    role: str
    content: Union[str, List[Dict]]  # Support both text and multipart (Vision API)

class OpenAIChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    model: str
    messages: List[OpenAIMessage]
    temperature: Optional[float] = 1.0
    stream: bool = False  # Ignored, but accepted for compatibility
    thinking_level: Optional[str] = None
    use_search: Optional[bool] = False

# --- Helpers ---

def parse_model_and_thinking(model_name: str) -> tuple:
    """
    Parse thinking level from model name suffix.
    Returns (model_for_provider, thinking_level)
    
    Gemini Web models: thinking, pro, fast
    AI Studio models: gemini-3-flash-preview, gemini-3-pro-preview, etc.
    """
    # Check if it's a Gemini Web model
    gemini_web_models = ["thinking", "pro", "fast"]
    for gw_model in gemini_web_models:
        if gw_model in model_name.lower():
            # Gemini Web - capitalize the model name
            return model_name.capitalize() if model_name.lower() in gemini_web_models else model_name, "High"
    
    # AI Studio model parsing
    thinking_levels = ["minimal", "low", "medium", "high"]
    
    for level in thinking_levels:
        if model_name.lower().endswith(f"-{level}"):
            base_model = model_name[:-(len(level) + 1)]
            return base_model, level.capitalize()
    
    return model_name, "High"

# --- Endpoints ---

@app.get("/v1/models")
async def list_models():
    """List available Gemini Web models."""
    return {
        "data": [
            {"id": "fast", "object": "model", "provider": "gemini-web"},
            {"id": "thinking", "object": "model", "provider": "gemini-web"},
            {"id": "pro", "object": "model", "provider": "gemini-web"},
        ]
    }

@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """OpenAI-compatible chat completions endpoint."""
    body = await request.json()
    
    if not worker_pool:
        raise HTTPException(status_code=503, detail="Worker pool not initialized")
    
    # Extract fields
    model = body.get("model", "gemini-3-flash-preview")
    messages = body.get("messages", [])
    thinking_level_explicit = body.get("thinking_level")
    use_search = body.get("use_search", False)

    # Parse thinking level from model name
    base_model, thinking_level = parse_model_and_thinking(model)
    if thinking_level_explicit:
        thinking_level = thinking_level_explicit

    log(f"Model: {base_model} | Thinking: {thinking_level} | Messages: {len(messages)}", "API")

    # Combine messages and extract images
    prompt = ""
    image_paths = []
    
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        
        if isinstance(content, list):
            for part in content:
                part_type = part.get("type", "text")
                if part_type == "text":
                    text_content = part.get('text', '')
                    if text_content:
                        prompt += text_content + "\n"
                elif part_type == "image_url":
                    image_url = part.get("image_url", {}).get("url", "")
                    if image_url.startswith("data:image/"):
                        try:
                            header, base64_data = image_url.split(",", 1)
                            image_bytes = base64.b64decode(base64_data)
                            mime_type = header.split(";")[0].split(":")[1]
                            ext = mime_type.split("/")[1]
                            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
                            temp_file.write(image_bytes)
                            temp_file.close()
                            image_paths.append(temp_file.name)
                        except Exception as e:
                            log(f"Failed to decode image: {e}", "API")
        else:
            if content:
                prompt += content + "\n"
    
    # Send to worker
    log(f"Dispatching to browser thread...", "API")
    coro = worker_pool.send_message(
        prompt, 
        model=base_model, 
        thinking_level=thinking_level, 
        use_search=use_search, 
        images=image_paths
    )
    future = asyncio.run_coroutine_threadsafe(coro, browser_loop)
    
    # CRITICAL: Use timeout to prevent infinite blocking
    # The lambda with timeout prevents run_in_executor from blocking forever
    BROWSER_TIMEOUT = 300  # 5 minutes max for generation
    
    try:
        log(f"Waiting for browser thread (timeout={BROWSER_TIMEOUT}s)...", "API")
        result = await asyncio.get_event_loop().run_in_executor(
            None, 
            lambda: future.result(timeout=BROWSER_TIMEOUT)
        )
        log(f"Browser thread returned", "API")
    except TimeoutError:
        log(f"❌ BROWSER TIMEOUT after {BROWSER_TIMEOUT}s - request stuck in browser thread", "API")
        raise HTTPException(status_code=504, detail=f"Browser operation timed out after {BROWSER_TIMEOUT}s")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for img_path in image_paths:
            try: os.unlink(img_path)
            except: pass

    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("error"))

    content = result["response"] or ""
    log(f"Got response ({len(content)} chars)", "API")
    
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
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

@app.get("/health")
async def health():
    return {"status": "ok", "workers": WORKER_COUNT}

if __name__ == "__main__":
    import uvicorn
    import sys
    # Note: Windows event loop policy is set automatically in Python 3.8+
    # The deprecated set_event_loop_policy warning can be ignored
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
