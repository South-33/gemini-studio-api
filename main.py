import os
import asyncio
import threading
import base64
import tempfile
import json
import time
from typing import List, Optional, Dict, Union, AsyncGenerator
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
from dotenv import load_dotenv


# Load .env file
load_dotenv()

from core import WorkerPool
from cookie_loader import get_cookies

# Configuration
WORKER_COUNT = int(os.getenv("WORKER_COUNT", "2"))

# Bandwidth Limiter - prevents exceeding GCP free tier (1GB/month)
BANDWIDTH_LIMIT_MB = int(os.getenv("BANDWIDTH_LIMIT_MB", "900"))  # 900MB = safe margin
BANDWIDTH_LIMIT_BYTES = BANDWIDTH_LIMIT_MB * 1024 * 1024
BANDWIDTH_FILE = os.path.join(os.path.dirname(__file__), ".bandwidth_usage")

def get_current_month() -> str:
    """Get current month as YYYY-MM string."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m")

def load_bandwidth_usage() -> tuple:
    """Load bandwidth usage from file. Returns (month, bytes_used)."""
    try:
        if os.path.exists(BANDWIDTH_FILE):
            with open(BANDWIDTH_FILE, "r") as f:
                data = f.read().strip().split(",")
                return data[0], int(data[1])
    except:
        pass
    return get_current_month(), 0

def save_bandwidth_usage(month: str, bytes_used: int):
    """Save bandwidth usage to file."""
    try:
        with open(BANDWIDTH_FILE, "w") as f:
            f.write(f"{month},{bytes_used}")
    except:
        pass

def add_bandwidth(response_bytes: int) -> bool:
    """Add to bandwidth counter. Returns False if limit exceeded."""
    current_month = get_current_month()
    saved_month, bytes_used = load_bandwidth_usage()
    
    # Reset counter if new month
    if saved_month != current_month:
        bytes_used = 0
    
    bytes_used += response_bytes
    save_bandwidth_usage(current_month, bytes_used)
    
    return bytes_used < BANDWIDTH_LIMIT_BYTES

def get_bandwidth_status() -> dict:
    """Get current bandwidth usage status."""
    current_month = get_current_month()
    saved_month, bytes_used = load_bandwidth_usage()
    if saved_month != current_month:
        bytes_used = 0
    
    return {
        "month": current_month,
        "used_mb": round(bytes_used / (1024 * 1024), 2),
        "limit_mb": BANDWIDTH_LIMIT_MB,
        "remaining_mb": round((BANDWIDTH_LIMIT_BYTES - bytes_used) / (1024 * 1024), 2),
        "percent_used": round((bytes_used / BANDWIDTH_LIMIT_BYTES) * 100, 1)
    }

# Global State
worker_pool: Optional[WorkerPool] = None
browser_loop: Optional[asyncio.AbstractEventLoop] = None
browser_thread: Optional[threading.Thread] = None

def run_browser_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

async def init_browser_thread(cookies):
    global worker_pool, browser_loop, browser_thread
    browser_loop = asyncio.ProactorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
    browser_thread = threading.Thread(target=run_browser_loop, args=(browser_loop,), daemon=True)
    browser_thread.start()
    
    worker_pool = WorkerPool(worker_count=WORKER_COUNT)
    future = asyncio.run_coroutine_threadsafe(worker_pool.init(cookies), browser_loop)
    return future.result(timeout=120)

@asynccontextmanager
async def lifespan(app: FastAPI):
    cookies, error = get_cookies()
    if error:
        print(f"[Server] ⚠️ Cookie warning: {error}")
        print("[Server] Proceeding without cookies (manual login might be required).")
    
    success = await init_browser_thread(cookies or [])
    if success:
        print("[Server] ✅ AI Studio Worker Pool Ready")
    
    yield
    if worker_pool:
        future = asyncio.run_coroutine_threadsafe(worker_pool.close(), browser_loop)
        future.result(timeout=10)

app = FastAPI(title="Gemini Studio API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Models ---

class ChatRequest(BaseModel):
    message: str
    model: str = "gemini-3-flash-preview"
    thinking_level: Optional[str] = "High"
    use_search: bool = False
    images: Optional[List[str]] = None  # Base64 strings

# OpenAI Compat Models
class OpenAIMessage(BaseModel):
    role: str
    content: Union[str, List[Dict]]  # Support both text and multipart (Vision API)
    
    class Config:
        extra = "allow"

class OpenAIChatRequest(BaseModel):
    model: str
    messages: List[OpenAIMessage]
    temperature: Optional[float] = 1.0
    stream: bool = False
    # Custom fields
    thinking_level: Optional[str] = None
    use_search: Optional[bool] = False
    # Accept any extra fields clients might send (max_tokens, top_p, etc.)
    
    class Config:
        extra = "allow"

# --- Endpoints ---

def parse_model_and_thinking(model_name: str) -> tuple:
    """
    Parse thinking level from model name suffix.
    Examples:
        gemini-3-flash-preview -> (gemini-3-flash-preview, High)
        gemini-3-flash-preview-minimal -> (gemini-3-flash-preview, Minimal)
        gemini-3-pro-preview-low -> (gemini-3-pro-preview, Low)
    """
    thinking_levels = ["minimal", "low", "medium", "high"]
    
    for level in thinking_levels:
        if model_name.lower().endswith(f"-{level}"):
            # Remove the suffix and return
            base_model = model_name[:-(len(level) + 1)]
            return base_model, level.capitalize()
    
    # No suffix found, default to High
    return model_name, "High"

@app.get("/v1/models")
async def list_models():
    return {
        "data": [
            {"id": "gemini-3-pro-preview", "object": "model"},
            {"id": "gemini-3-flash-preview", "object": "model"},
            {"id": "gemini-3-flash-preview-minimal", "object": "model"},
            {"id": "gemini-3-flash-preview-low", "object": "model"},
            {"id": "gemini-3-flash-preview-high", "object": "model"},
            {"id": "gemini-flash-latest", "object": "model"},
        ]
    }

@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """OpenAI-compatible endpoint with SSE streaming support for Koyeb timeout handling."""
    # Check bandwidth limit FIRST
    bw_status = get_bandwidth_status()
    if bw_status["remaining_mb"] <= 0:
        raise HTTPException(
            status_code=503, 
            detail=f"Bandwidth limit reached ({bw_status['limit_mb']}MB/month). Resets next month."
        )
    
    body = await request.json()
    stream = body.get("stream", False)
    print(f"[DEBUG] Model: {body.get('model')} | Messages: {len(body.get('messages', []))} | Stream: {stream}")
    
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
                    prompt += f"{role.capitalize()}: {part.get('text', '')}\n"
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
                            print(f"[DEBUG] Failed to decode image: {e}")
        else:
            prompt += f"{role.capitalize()}: {content}\n"
    
    # If streaming requested, use SSE with heartbeat
    if stream:
        return StreamingResponse(
            stream_with_heartbeat(prompt, base_model, thinking_level, use_search, image_paths, model),
            media_type="text/event-stream"
        )
    
    # Non-streaming: regular response
    coro = worker_pool.send_message(prompt, model=base_model, thinking_level=thinking_level, 
                                     use_search=use_search, images=image_paths)
    future = asyncio.run_coroutine_threadsafe(coro, browser_loop)
    
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, future.result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for img_path in image_paths:
            try: os.unlink(img_path)
            except: pass

    if not result["success"]:
        raise HTTPException(status_code=500, detail=result.get("error"))

    content = result["response"] or ""
    
    response_data = {
        "id": f"chatcmpl-studio-{id(result)}",
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
    
    response_size = len(json.dumps(response_data).encode('utf-8'))
    add_bandwidth(response_size)
    
    return response_data


async def stream_with_heartbeat(prompt: str, base_model: str, thinking_level: str, 
                                 use_search: bool, image_paths: List[str], model: str) -> AsyncGenerator[str, None]:
    """
    Stream response with SSE heartbeat to keep Koyeb connection alive.
    Sends empty comments every 15 seconds while waiting for generation.
    """
    HEARTBEAT_INTERVAL = 15  # seconds
    
    # Start generation in background
    coro = worker_pool.send_message(prompt, model=base_model, thinking_level=thinking_level,
                                     use_search=use_search, images=image_paths)
    future = asyncio.run_coroutine_threadsafe(coro, browser_loop)
    
    result = None
    start_time = time.time()
    
    try:
        while result is None:
            try:
                # Check if result is ready (non-blocking with short timeout)
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: future.result(timeout=1)
                )
            except Exception as e:
                if "timeout" in str(e).lower() or "TimeoutError" in str(type(e).__name__):
                    # Not ready yet, send heartbeat
                    elapsed = time.time() - start_time
                    if int(elapsed) % HEARTBEAT_INTERVAL == 0 and int(elapsed) > 0:
                        # Send SSE comment as heartbeat (keeps connection alive)
                        yield ": heartbeat\n\n"
                        print(f"[Stream] Heartbeat sent at {int(elapsed)}s")
                    await asyncio.sleep(1)
                else:
                    # Real error
                    error_chunk = {
                        "id": f"chatcmpl-error",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": f"Error: {str(e)}"}, "finish_reason": "stop"}]
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
    finally:
        # Clean up temp files
        for img_path in image_paths:
            try: os.unlink(img_path)
            except: pass
    
    # Got result, stream it
    if result and result.get("success"):
        content = result.get("response", "")
        
        # Send the content as a single chunk (AI Studio doesn't give us streaming)
        chunk = {
            "id": f"chatcmpl-studio-{id(result)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None
            }]
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        
        # Send finish chunk
        finish_chunk = {
            "id": f"chatcmpl-studio-{id(result)}",
            "object": "chat.completion.chunk", 
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }
        yield f"data: {json.dumps(finish_chunk)}\n\n"
    else:
        error_msg = result.get("error", "Unknown error") if result else "No result"
        error_chunk = {
            "id": "chatcmpl-error",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": f"Error: {error_msg}"}, "finish_reason": "stop"}]
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
    
    yield "data: [DONE]\n\n"
    
    # Track bandwidth
    add_bandwidth(len(content) if result and result.get("success") else 100)


@app.post("/v1/chat")
async def direct_chat(request: ChatRequest):
    """Simple direct endpoint with image support."""
    if not worker_pool:
        raise HTTPException(status_code=503, detail="Worker pool not initialized")
    
    image_paths = []
    if request.images:
        for img_base64 in request.images:
            try:
                if "," in img_base64:
                    header, data = img_base64.split(",", 1)
                    ext = header.split(";")[0].split("/")[1]
                else:
                    data = img_base64
                    ext = "png"
                
                image_bytes = base64.b64decode(data)
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
                temp_file.write(image_bytes)
                temp_file.close()
                image_paths.append(temp_file.name)
            except Exception as e:
                print(f"[DEBUG] Failed to decode image: {e}")

    coro = worker_pool.send_message(
        request.message, 
        model=request.model, 
        thinking_level=request.thinking_level,
        use_search=request.use_search,
        images=image_paths
    )
    future = asyncio.run_coroutine_threadsafe(coro, browser_loop)
    
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, future.result)
    finally:
        # Clean up temp files
        for path in image_paths:
            try: os.unlink(path)
            except: pass
            
    return result

@app.get("/health")
async def health():
    return {"status": "ok", "workers": WORKER_COUNT}

@app.get("/bandwidth")
async def bandwidth():
    """Check current bandwidth usage against GCP free tier limit."""
    status = get_bandwidth_status()
    return {
        "status": "ok" if status["remaining_mb"] > 0 else "LIMIT_REACHED",
        **status,
        "warning": "API will block requests when limit is reached" if status["percent_used"] > 80 else None
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
