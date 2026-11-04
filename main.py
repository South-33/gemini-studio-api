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
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
PUBLIC_API_TIMEOUT_SECONDS = 290
ASYNC_JOB_TIMEOUT_SECONDS = 2700
RECENT_REQUEST_LIMIT = 200
ASYNC_JOB_LIMIT = 200
IMAGE_TOKEN_ESTIMATE = 300

# Global State
worker_pool: Optional[WorkerPool] = None
browser_loop: Optional[asyncio.AbstractEventLoop] = None
browser_thread: Optional[threading.Thread] = None
recent_requests = deque(maxlen=max(20, RECENT_REQUEST_LIMIT))
async_jobs: Dict[str, Dict[str, Any]] = {}

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


def build_completion_payload(
    *,
    trace_id: str,
    model: str,
    content: str,
    prompt_tokens_est: int,
) -> Dict[str, Any]:
    completion_tokens_est = estimate_text_tokens(content)
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "request_id": trace_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens_est,
            "completion_tokens": completion_tokens_est,
            "total_tokens": prompt_tokens_est + completion_tokens_est,
            "completion_chars": len(content),
            "token_estimate": True,
        },
    }


def remember_async_job(job: Dict[str, Any]) -> None:
    """Keep a bounded in-memory result cache for long-running browser jobs."""
    request_id = str(job["request_id"])
    async_jobs[request_id] = job
    while len(async_jobs) > ASYNC_JOB_LIMIT:
        oldest = next(iter(async_jobs))
        if oldest == request_id and len(async_jobs) == 1:
            break
        async_jobs.pop(oldest, None)


def async_job_response(job: Dict[str, Any]) -> JSONResponse:
    status = str(job.get("status") or "started")
    if status == "ok" and isinstance(job.get("response"), dict):
        return JSONResponse(status_code=200, content=job["response"])
    if status in {"failed", "error", "timeout"}:
        return JSONResponse(status_code=200, content={
            "request_id": job["request_id"],
            "status": status,
            "error": str(job.get("error") or "Gemini request failed"),
        })
    return JSONResponse(status_code=202, content={
        "request_id": job["request_id"],
        "status": status,
    })


async def finish_async_job(
    *,
    job: Dict[str, Any],
    trace: Dict[str, Any],
    future,
    api_timeout: float,
    cleanup_image_paths,
    model: str,
    prompt_tokens_est: int,
) -> None:
    """Finish a browser request after the submit HTTP request has returned."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: future.result(timeout=api_timeout))
    except TimeoutError:
        try:
            future.cancel()
        except Exception:
            pass
        job["status"] = "timeout"
        job["error"] = f"Browser operation timed out after {api_timeout}s"
        trace["status"] = "timeout"
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        log(f"[{job['request_id']}] Async browser timeout after {api_timeout}s", "API")
        return
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        trace["status"] = "error"
        trace["error"] = str(exc)
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        log(f"[{job['request_id']}] Async browser error: {exc}", "API")
        return
    finally:
        cleanup_image_paths()

    trace["queue_wait_ms"] = result.get("queue_wait_ms")
    trace["attempts"] = result.get("attempts")
    trace["effective_thinking_level"] = result.get("effective_thinking_level")
    trace["request_logs"] = result.get("request_logs") or []
    trace["finished_at"] = datetime.now(timezone.utc).isoformat()

    if not result.get("success"):
        error = str(result.get("error") or "Gemini request failed")
        job["status"] = "failed"
        job["error"] = error
        trace["status"] = "failed"
        trace["error"] = error
        log(f"[{job['request_id']}] Async request failed: {error}", "API")
        return

    content = str(result.get("response") or "")
    response = build_completion_payload(
        trace_id=str(job["request_id"]),
        model=model,
        content=content,
        prompt_tokens_est=prompt_tokens_est,
    )
    job["status"] = "ok"
    job["response"] = response
    trace["status"] = "ok"
    trace["response_chars"] = len(content)
    trace["response_tokens_est"] = estimate_text_tokens(content)
    log(f"[{job['request_id']}] Async request complete chars={len(content)}", "API")


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
    respond_async = body.get("async", False)
    if not isinstance(respond_async, bool):
        raise HTTPException(status_code=400, detail="async must be a boolean")
    
    if not worker_pool:
        raise HTTPException(status_code=503, detail="Worker pool not initialized")
    
    source = extract_request_source(request, body)
    trace_id = source["request_id"]
    if respond_async:
        existing_job = async_jobs.get(trace_id)
        if existing_job is not None:
            log(f"[{trace_id}] Reusing existing async job status={existing_job.get('status')}", "API")
            return async_job_response(existing_job)

    # Extract fields
    model = body.get("model", "flash")
    messages = body.get("messages", [])
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty array")
    thinking_level_explicit = body.get("thinking_level")
    fallback_thinking_level_explicit = body.get("fallback_thinking_level")
    use_search = body.get("use_search", False)
    if not isinstance(model, str):
        raise HTTPException(status_code=400, detail="model must be a string")
    if thinking_level_explicit is not None and not isinstance(thinking_level_explicit, str):
        raise HTTPException(status_code=400, detail="thinking_level must be a string")
    if fallback_thinking_level_explicit is not None and not isinstance(fallback_thinking_level_explicit, str):
        raise HTTPException(status_code=400, detail="fallback_thinking_level must be a string")
    if not isinstance(use_search, bool):
        raise HTTPException(status_code=400, detail="use_search must be a boolean")

    try:
        base_model = normalize_model(model)
        thinking_level = normalize_thinking_level(thinking_level_explicit or "medium")
        fallback_thinking_level = (
            normalize_thinking_level(fallback_thinking_level_explicit)
            if fallback_thinking_level_explicit is not None
            else None
        )
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
        "fallback_thinking_level": fallback_thinking_level,
        "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    recent_requests.append(trace)
    log(f"[{trace_id}] Source: project={source['project']} client={source['client']} ip={source['ip']}", "API")
    log(
        f"Model: {base_model} | Thinking: {thinking_level}"
        f" | Fallback: {fallback_thinking_level or '-'} | Messages: {len(messages)} | Raw: {model}",
        "API",
    )
    
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
        fallback_thinking_level=fallback_thinking_level,
        use_search=use_search, 
        images=image_paths,
        request_id=trace_id,
        request_context={
            "project": source["project"],
            "client": source["client"],
            "ip": source["ip"],
            "raw_model": model,
            "fallback_thinking_level": fallback_thinking_level,
            "message_count": len(messages),
            "image_count": len(image_paths),
            "prompt_tokens_est": prompt_tokens_est,
        },
    )
    future = asyncio.run_coroutine_threadsafe(coro, browser_loop)

    if respond_async:
        job: Dict[str, Any] = {
            "request_id": trace_id,
            "status": "started",
            "started_at": trace["started_at"],
        }
        remember_async_job(job)
        task = asyncio.create_task(finish_async_job(
            job=job,
            trace=trace,
            future=future,
            api_timeout=ASYNC_JOB_TIMEOUT_SECONDS,
            cleanup_image_paths=cleanup_image_paths,
            model=model,
            prompt_tokens_est=prompt_tokens_est,
        ))
        job["_task"] = task
        log(f"[{trace_id}] Async request accepted; poll /v1/chat/completions/{trace_id}", "API")
        return async_job_response(job)

    api_timeout = PUBLIC_API_TIMEOUT_SECONDS
    
    try:
        log(
            f"Waiting for queued browser request (api_timeout={api_timeout}s)...",
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
        log(f"BROWSER TIMEOUT after {api_timeout}s - request stuck in browser thread", "API")
        raise HTTPException(
            status_code=504,
            detail=f"Gemini request failed: API request timed out after {api_timeout}s",
        )
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
        trace["effective_thinking_level"] = result.get("effective_thinking_level")
        trace["request_logs"] = result.get("request_logs") or []
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        raise HTTPException(status_code=500, detail=result.get("error"))

    content = result["response"] or ""
    completion_tokens_est = estimate_text_tokens(content)
    trace["status"] = "ok"
    trace["queue_wait_ms"] = result.get("queue_wait_ms")
    trace["attempts"] = result.get("attempts")
    trace["effective_thinking_level"] = result.get("effective_thinking_level")
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


@app.get("/v1/chat/completions/{request_id}")
async def get_chat_completion(request_id: str):
    """Poll a long-running completion without holding an idle tunnel request."""
    job = async_jobs.get(request_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown async request_id")
    return async_job_response(job)

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
    active_request = bool(worker_pool and worker_pool._initialized and worker_pool._active_request)
    browser_state = getattr(worker_pool, "_browser_state", "unavailable") if worker_pool else "unavailable"
    pool_ready = bool(worker_pool and worker_pool._initialized)
    is_ready = pool_ready and (
        ready_workers > 0
        or active_request
        or browser_state in {"cold", "starting"}
    )
    response.status_code = 200 if is_ready else 503
    return {
        "status": "busy" if active_request else ("ok" if is_ready else "degraded"),
        "ready": is_ready,
        "workers": 1,
        "ready_workers": ready_workers,
        "active_request": active_request,
        "browser_state": browser_state,
    }


@app.get("/v1/diagnostics")
async def diagnostics():
    if not worker_pool:
        return {"status": "not_initialized"}

    pool_diag = await worker_pool.get_live_diagnostics()
    ready_workers = ready_worker_count()
    browser_state = pool_diag.get("browser_state")
    is_ready = bool(
        worker_pool._initialized
        and (
            ready_workers
            or worker_pool._active_request
            or browser_state in {"cold", "starting"}
        )
    )
    return {
        "status": "ok" if is_ready else "degraded",
        "pool": pool_diag,
        "recent_requests": [public_request_trace(trace) for trace in recent_requests],
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
