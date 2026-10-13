import sys
import os

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True, encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(line_buffering=True, encoding='utf-8')

import asyncio
import threading
import base64
import tempfile
import time
import uuid
import math
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# --- Timestamped Logging (use stderr - always unbuffered) ---
def log(msg: str, tag: str = "Server"):
    """Print with timestamp for debugging. Uses stderr for guaranteed immediate output."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    # Use stderr (unbuffered) instead of stdout
    print(f"[{ts}] [{tag}] {msg}", file=sys.stderr, flush=True)

from gemini_models import MODEL_FAMILIES, normalize_model, normalize_thinking_level
from worker_pool import WorkerPool

# Configuration
BROWSER_TIMEOUT = 480
API_TIMEOUT_HEADROOM = 30
QUEUE_TIMEOUT = 1800
RECENT_REQUEST_LIMIT = 200
IMAGE_TOKEN_ESTIMATE = 300

# Global State
worker_pool: Optional[WorkerPool] = None
browser_loop: Optional[asyncio.AbstractEventLoop] = None
browser_thread: Optional[threading.Thread] = None
recent_requests = deque(maxlen=max(20, RECENT_REQUEST_LIMIT))

def run_browser_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

async def init_browser_thread():
    global worker_pool, browser_loop, browser_thread
    browser_loop = asyncio.ProactorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
    browser_thread = threading.Thread(target=run_browser_loop, args=(browser_loop,), daemon=True)
    browser_thread.start()
    
    worker_pool = WorkerPool()
    # Empty cookies - we use persistent browser session instead
    future = asyncio.run_coroutine_threadsafe(worker_pool.init(), browser_loop)
    return future.result(timeout=120)

@asynccontextmanager
async def lifespan(app: FastAPI):
    success = await init_browser_thread()
    if not success:
        if browser_loop:
            browser_loop.call_soon_threadsafe(browser_loop.stop)
        if browser_thread:
            browser_thread.join(timeout=5)
        raise RuntimeError("Worker pool failed to initialize")

    log("Gemini worker ready; concurrent requests will queue", "API")
    try:
        yield
    finally:
        try:
            if worker_pool and browser_loop and browser_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(worker_pool.close(), browser_loop)
                future.result(timeout=15)
        except Exception as exc:
            log(f"Browser cleanup warning: {exc}", "API")
        finally:
            if browser_loop and browser_loop.is_running():
                browser_loop.call_soon_threadsafe(browser_loop.stop)
            if browser_thread:
                browser_thread.join(timeout=5)


app = FastAPI(title="Gemini Studio API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Helpers ---

def extract_request_source(request: Request, body: Dict) -> Dict[str, str]:
    """Extract source labels from headers/body for diagnostics."""
    headers = request.headers
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    project = (
        headers.get("x-project-name")
        or body.get("project")
        or metadata.get("project")
        or "unknown"
    )
    client = (
        headers.get("x-client-name")
        or body.get("client")
        or metadata.get("client")
        or "unknown"
    )
    req_id = (
        headers.get("x-request-id")
        or body.get("request_id")
        or metadata.get("request_id")
        or str(uuid.uuid4())
    )

    return {
        "project": str(project),
        "client": str(client),
        "request_id": str(req_id),
        "ip": request.client.host if request.client else "unknown",
    }


def estimate_text_tokens(text: str) -> int:
    """Rough token estimate for diagnostics/logging only."""
    if not text:
        return 0

    chars = len(text)
    words = len(text.split())
    by_chars = max(1, math.ceil(chars / 4))
    by_words = max(1, math.ceil(words * 1.3))
    return max(by_chars, by_words)


# --- Endpoints ---

@app.get("/v1/models")
async def list_models():
    """List available Gemini Web models."""
    return {
        "data": [{"id": name, "object": "model", "provider": "gemini-web"} for name in MODEL_FAMILIES]
    }

@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """OpenAI-compatible chat completions endpoint."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    if body.get("stream"):
        raise HTTPException(status_code=400, detail="Streaming responses are not supported")
    
    if not worker_pool:
        raise HTTPException(status_code=503, detail="Worker pool not initialized")
    
    source = extract_request_source(request, body)
    trace_id = source["request_id"]

    # Extract fields
    model = body.get("model", "flash")
    messages = body.get("messages", [])
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty array")
    thinking_level_explicit = body.get("thinking_level")
    use_search = body.get("use_search", False)
    if not isinstance(model, str):
        raise HTTPException(status_code=400, detail="model must be a string")
    if thinking_level_explicit is not None and not isinstance(thinking_level_explicit, str):
        raise HTTPException(status_code=400, detail="thinking_level must be a string")
    if not isinstance(use_search, bool):
        raise HTTPException(status_code=400, detail="use_search must be a boolean")

    try:
        base_model = normalize_model(model)
        thinking_level = normalize_thinking_level(thinking_level_explicit or "medium")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Combine messages and extract images
    prompt = ""
    image_paths = []

    def cleanup_image_paths() -> None:
        for path in image_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
    
    for msg in messages:
        if not isinstance(msg, dict):
            cleanup_image_paths()
            raise HTTPException(status_code=400, detail="Each message must be an object")
        content = msg.get("content", "")
        
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    cleanup_image_paths()
                    raise HTTPException(status_code=400, detail="Message content parts must be objects")
                part_type = part.get("type", "text")
                if part_type == "text":
                    text_content = part.get('text', '')
                    if text_content:
                        prompt += str(text_content) + "\n"
                elif part_type == "image_url":
                    image_spec = part.get("image_url")
                    image_url = image_spec.get("url", "") if isinstance(image_spec, dict) else ""
                    if image_url.startswith("data:image/"):
                        try:
                            header, base64_data = image_url.split(",", 1)
                            image_bytes = base64.b64decode(base64_data, validate=True)
                            mime_type = header.split(";")[0].split(":")[1]
                            ext = {
                                "image/png": "png",
                                "image/jpeg": "jpg",
                                "image/gif": "gif",
                                "image/webp": "webp",
                            }.get(mime_type)
                            if not ext:
                                raise ValueError(f"Unsupported image MIME type: {mime_type}")
                            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
                            temp_file.write(image_bytes)
                            temp_file.close()
                            image_paths.append(temp_file.name)
                        except Exception as e:
                            cleanup_image_paths()
                            raise HTTPException(status_code=400, detail=f"Invalid image attachment: {e}") from e
                    else:
                        cleanup_image_paths()
                        raise HTTPException(status_code=400, detail="Only data:image/... image URLs are supported")
                else:
                    cleanup_image_paths()
                    raise HTTPException(status_code=400, detail=f"Unsupported message content type: {part_type}")
        else:
            if content:
                prompt += str(content) + "\n"

    if not prompt.strip() and not image_paths:
        raise HTTPException(status_code=400, detail="Request contains no text or image content")

    trace = {
        "request_id": trace_id,
        "project": source["project"],
        "client": source["client"],
        "ip": source["ip"],
        "model": model,
        "resolved_model": base_model,
        "thinking_level": thinking_level,
        "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    recent_requests.append(trace)
    log(f"[{trace_id}] Source: project={source['project']} client={source['client']} ip={source['ip']}", "API")
    log(f"Model: {base_model} | Thinking: {thinking_level} | Messages: {len(messages)} | Raw: {model}", "API")
    
    # Send to worker
    prompt_chars = len(prompt)
    prompt_tokens_est = estimate_text_tokens(prompt) + (len(image_paths) * IMAGE_TOKEN_ESTIMATE)
    trace["prompt_chars"] = prompt_chars
    trace["prompt_tokens_est"] = prompt_tokens_est
    trace["image_count"] = len(image_paths)

    log(
        f"Prompt stats: chars={prompt_chars} tokens_est={prompt_tokens_est} images={len(image_paths)}",
        "API",
    )
    log("Dispatching to browser thread...", "API")
    coro = worker_pool.send_message(
        prompt, 
        model=base_model, 
        thinking_level=thinking_level, 
        use_search=use_search, 
        images=image_paths,
        request_id=trace_id,
        request_context={
            "project": source["project"],
            "client": source["client"],
            "ip": source["ip"],
            "raw_model": model,
            "message_count": len(messages),
            "image_count": len(image_paths),
            "prompt_tokens_est": prompt_tokens_est,
        },
    )
    future = asyncio.run_coroutine_threadsafe(coro, browser_loop)
    
    retry_budget = 2
    api_timeout = QUEUE_TIMEOUT + (BROWSER_TIMEOUT * retry_budget) + API_TIMEOUT_HEADROOM
    
    try:
        log(
            f"Waiting for queue/browser (queue_timeout={QUEUE_TIMEOUT}s, worker_timeout={BROWSER_TIMEOUT}s, retries={retry_budget}, api_timeout={api_timeout}s)...",
            "API"
        )
        result = await asyncio.get_event_loop().run_in_executor(
            None, 
            lambda: future.result(timeout=api_timeout)
        )
        log("Browser thread returned", "API")
    except TimeoutError:
        try:
            future.cancel()
        except:
            pass
        trace["status"] = "timeout"
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        log(f"❌ BROWSER TIMEOUT after {api_timeout}s - request stuck in browser thread", "API")
        raise HTTPException(status_code=504, detail=f"Browser operation timed out after {api_timeout}s")
    except Exception as e:
        trace["status"] = "error"
        trace["error"] = str(e)
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cleanup_image_paths()

    if not result["success"]:
        trace["status"] = "failed"
        trace["error"] = result.get("error")
        trace["queue_wait_ms"] = result.get("queue_wait_ms")
        trace["attempts"] = result.get("attempts")
        trace["request_logs"] = result.get("request_logs") or []
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        raise HTTPException(status_code=500, detail=result.get("error"))

    content = result["response"] or ""
    completion_tokens_est = estimate_text_tokens(content)
    trace["status"] = "ok"
    trace["queue_wait_ms"] = result.get("queue_wait_ms")
    trace["attempts"] = result.get("attempts")
    trace["response_chars"] = len(content)
    trace["request_logs"] = result.get("request_logs") or []
    trace["response_tokens_est"] = completion_tokens_est
    trace["finished_at"] = datetime.now(timezone.utc).isoformat()
    log(
        f"Got response chars={len(content)} tokens_est={completion_tokens_est}",
        "API",
    )
    
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "request_id": trace_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": prompt_tokens_est,
            "completion_tokens": completion_tokens_est,
            "total_tokens": prompt_tokens_est + completion_tokens_est,
            "prompt_chars": prompt_chars,
            "completion_chars": len(content),
            "token_estimate": True,
        }
    }

def ready_worker_count() -> int:
    if not worker_pool or not worker_pool._initialized:
        return 0

    ready = 0
    for worker in worker_pool.workers:
        try:
            if (
                worker
                and worker._initialized
                and worker.page
                and not worker.page.is_closed()
                and str(worker.page.url).startswith("https://gemini.google.com/app")
            ):
                ready += 1
        except Exception:
            continue
    return ready


def public_request_trace(trace: Dict) -> Dict:
    """Return operational metadata without exposing the caller IP."""
    return {
        key: value
        for key, value in trace.items()
        if key != "ip"
    }


@app.get("/health")
async def health(response: Response):
    ready_workers = ready_worker_count()
    is_ready = ready_workers > 0
    response.status_code = 200 if is_ready else 503
    return {
        "status": "ok" if is_ready else "degraded",
        "ready": is_ready,
        "workers": 1,
        "ready_workers": ready_workers,
    }


@app.get("/v1/diagnostics")
async def diagnostics():
    if not worker_pool:
        return {"status": "not_initialized"}

    pool_diag = await worker_pool.get_live_diagnostics()
    return {
        "status": "ok" if ready_worker_count() else "degraded",
        "pool": pool_diag,
        "recent_requests": [public_request_trace(trace) for trace in recent_requests],
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
