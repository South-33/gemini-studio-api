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
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
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
from agy_pool import AgyPool, AgyQueueFullError, AGY_REQUEST_TOTAL_BUDGET_SECONDS
from worker_pool import WorkerPool

# Configuration
PUBLIC_API_TIMEOUT_SECONDS = 290
ASYNC_JOB_TIMEOUT_SECONDS = 2700
RECENT_REQUEST_LIMIT = 200
ASYNC_JOB_LIMIT = 200
IMAGE_TOKEN_ESTIMATE = 300
ROOT = Path(__file__).resolve().parent
ENDPOINT_STATE = ROOT / "runtime_endpoint.json"
CONTROL_PLANE_URL = "https://believable-unplagiarised-josette.ngrok-free.dev"

# Global State
worker_pool: Optional[WorkerPool] = None
agy_pool: Optional[AgyPool] = None
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
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=120)
    except asyncio.TimeoutError:
        future.cancel()
        return False
    except asyncio.CancelledError:
        future.cancel()
        raise


def stop_browser_runtime() -> None:
    if browser_loop and browser_loop.is_running():
        browser_loop.call_soon_threadsafe(browser_loop.stop)
    if browser_thread:
        browser_thread.join(timeout=5)


async def initialize_gemini_backend() -> bool:
    """Initialize Gemini Web without making a healthy Agy backend wait on it."""

    try:
        ready = await init_browser_thread()
    except asyncio.CancelledError:
        stop_browser_runtime()
        raise
    except Exception as exc:
        log(f"Gemini Web backend startup failed: {exc}", "API")
        stop_browser_runtime()
        return False

    if ready:
        log("Gemini worker ready; concurrent requests will queue", "API")
        return True

    log("Gemini Web backend unavailable; Antigravity may still serve requests", "API")
    stop_browser_runtime()
    return False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global agy_pool
    agy_pool = AgyPool()
    try:
        agy_ready = await asyncio.to_thread(agy_pool.init)
    except Exception as exc:
        agy_ready = False
        log(f"Antigravity CLI backend startup failed: {exc}", "API")

    if agy_ready:
        log(
            f"Antigravity CLI backend ready; concurrency={agy_pool.max_concurrency} queue={agy_pool.max_queue}",
            "API",
        )
    else:
        log("Antigravity CLI backend unavailable", "API")

    browser_init_task: Optional[asyncio.Task] = None
    if agy_ready:
        browser_init_task = asyncio.create_task(initialize_gemini_backend())
    else:
        browser_ready = await initialize_gemini_backend()
        if not browser_ready:
            agy_pool.close()
            raise RuntimeError("No model backend could initialize")

    try:
        yield
    finally:
        if agy_pool:
            agy_pool.close()
        if browser_init_task and not browser_init_task.done():
            browser_init_task.cancel()
            try:
                await browser_init_task
            except asyncio.CancelledError:
                pass
        try:
            if worker_pool and browser_loop and browser_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(worker_pool.close(), browser_loop)
                future.result(timeout=15)
        except Exception as exc:
            log(f"Browser cleanup warning: {exc}", "API")
        finally:
            stop_browser_runtime()


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
    prompt_chars: int = 0,
    backend: str = "gemini",
    actual_usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    completion_tokens_est = estimate_text_tokens(content)
    usage: Dict[str, Any]
    if isinstance(actual_usage, dict) and isinstance(actual_usage.get("input_tokens"), int):
        prompt_tokens = int(actual_usage.get("input_tokens") or 0)
        completion_tokens = int(actual_usage.get("output_tokens") or 0)
        total_tokens = int(actual_usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "thinking_tokens": int(actual_usage.get("thinking_tokens") or 0),
            "cache_read_tokens": int(actual_usage.get("cache_read_tokens") or 0),
            "prompt_chars": prompt_chars,
            "completion_chars": len(content),
            "token_estimate": False,
        }
    else:
        usage = {
            "prompt_tokens": prompt_tokens_est,
            "completion_tokens": completion_tokens_est,
            "total_tokens": prompt_tokens_est + completion_tokens_est,
            "prompt_chars": prompt_chars,
            "completion_chars": len(content),
            "token_estimate": True,
        }
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "backend": backend,
        "request_id": trace_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


def gemini_backend_ready() -> bool:
    if not worker_pool or not worker_pool._initialized:
        return False
    if browser_loop is None:
        return True
    is_running = getattr(browser_loop, "is_running", None)
    return bool(callable(is_running) and is_running())


def agy_backend_ready() -> bool:
    return bool(agy_pool and agy_pool._initialized)


def select_request_backend(
    *,
    requested_backend: str,
    model: str,
    thinking_level: str,
    fallback_thinking_level: Optional[str],
    has_images: bool,
) -> tuple[str, str, str]:
    """Resolve the execution backend and concrete API model.

    ``auto`` means Agy-first. Fallback to Gemini Web happens only before any
    model call starts, for unsupported request shape/model/backend availability.
    """

    agy_model = model.strip().lower()
    if not agy_model:
        raise HTTPException(status_code=400, detail="model must not be empty")

    if requested_backend == "agy":
        if not agy_backend_ready():
            detail = agy_pool.diagnostics().get("last_error") if agy_pool else "not initialized"
            raise HTTPException(status_code=503, detail=f"Antigravity CLI backend is unavailable: {detail}")
        if has_images:
            raise HTTPException(status_code=400, detail="Antigravity CLI backend does not support image attachments yet")
        if not agy_pool.is_model_available(agy_model, thinking_level):
            raise HTTPException(
                status_code=400,
                detail=f"Antigravity CLI does not currently expose {agy_model}/{thinking_level}",
            )
        if fallback_thinking_level and agy_model not in MODEL_FAMILIES:
            raise HTTPException(
                status_code=400,
                detail="fallback_thinking_level requires a stable Gemini family alias such as 'flash'",
            )
        if fallback_thinking_level and not agy_pool.is_model_available(agy_model, fallback_thinking_level):
            raise HTTPException(
                status_code=400,
                detail=f"Antigravity CLI does not currently expose fallback {agy_model}/{fallback_thinking_level}",
            )
        return "agy", agy_model, "explicit"

    if requested_backend == "gemini":
        if not gemini_backend_ready():
            raise HTTPException(status_code=503, detail="Gemini Web backend is not initialized")
        try:
            return "gemini", normalize_model(model), "explicit"
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # No backend tag: prefer Agy when it can serve the request as-is.
    agy_can_serve = (
        agy_backend_ready()
        and not has_images
        and agy_pool.is_model_available(agy_model, thinking_level)
        and (not fallback_thinking_level or agy_model in MODEL_FAMILIES)
        and (
            not fallback_thinking_level
            or agy_pool.is_model_available(agy_model, fallback_thinking_level)
        )
    )
    if agy_can_serve:
        return "agy", agy_model, "agy_preferred"

    try:
        gemini_model = normalize_model(model)
    except ValueError as exc:
        if not agy_backend_ready() and not gemini_backend_ready():
            raise HTTPException(status_code=503, detail="No model backend is available") from exc
        raise HTTPException(
            status_code=400,
            detail=f"No available backend can serve model {model!r}",
        ) from exc

    if gemini_backend_ready():
        if has_images:
            reason = "images_require_gemini"
        elif not agy_backend_ready():
            reason = "agy_unavailable"
        else:
            reason = "agy_request_unsupported"
        return "gemini", gemini_model, reason

    if has_images:
        raise HTTPException(status_code=503, detail="Image requests require the Gemini Web backend, which is unavailable")
    if agy_backend_ready():
        raise HTTPException(
            status_code=400,
            detail=f"Antigravity CLI cannot serve {agy_model}/{thinking_level} and Gemini Web is unavailable",
        )
    raise HTTPException(status_code=503, detail="No model backend is available")


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
            "backend": job.get("backend"),
            "status": status,
            "error": str(job.get("error") or "Model request failed"),
        })
    return JSONResponse(status_code=202, content={
        "request_id": job["request_id"],
        "backend": job.get("backend"),
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
    prompt_chars: int,
    backend: str,
) -> None:
    """Finish a queued backend request after the submit HTTP request returned."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: future.result(timeout=api_timeout))
    except TimeoutError:
        try:
            future.cancel()
        except Exception:
            pass
        job["status"] = "timeout"
        job["error"] = f"{backend} operation timed out after {api_timeout}s"
        trace["status"] = "timeout"
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        log(f"[{job['request_id']}] Async {backend} timeout after {api_timeout}s", "API")
        return
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        trace["status"] = "error"
        trace["error"] = str(exc)
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        log(f"[{job['request_id']}] Async {backend} error: {exc}", "API")
        return
    finally:
        cleanup_image_paths()

    trace["queue_wait_ms"] = result.get("queue_wait_ms")
    trace["attempts"] = result.get("attempts")
    trace["effective_thinking_level"] = result.get("effective_thinking_level")
    trace["request_logs"] = result.get("request_logs") or []
    trace["prompt_transport"] = result.get("prompt_transport")
    trace["agy_model"] = result.get("agy_model")
    trace["agy_usage"] = result.get("agy_usage")
    trace["agy_tools_used"] = result.get("agy_tools_used") or []
    trace["finished_at"] = datetime.now(timezone.utc).isoformat()

    if not result.get("success"):
        error = str(result.get("error") or "Gemini request failed")
        timed_out = bool(result.get("timed_out"))
        job["status"] = "timeout" if timed_out else "failed"
        job["error"] = error
        trace["status"] = "timeout" if timed_out else "failed"
        trace["error"] = error
        log(
            f"[{job['request_id']}] Async request {'timed out' if timed_out else 'failed'}: {error}",
            "API",
        )
        return

    content = str(result.get("response") or "")
    response = build_completion_payload(
        trace_id=str(job["request_id"]),
        model=model,
        content=content,
        prompt_tokens_est=prompt_tokens_est,
        prompt_chars=prompt_chars,
        backend=backend,
        actual_usage=result.get("agy_usage"),
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


@app.get("/v1/backends")
async def list_backends():
    """Report the execution backends without changing the existing model list."""
    agy_diag = agy_pool.diagnostics() if agy_pool else {
        "available": False,
        "active_request": False,
        "queued_requests": 0,
        "models": [],
        "last_error": "not initialized",
    }
    return {
        "default_policy": "agy-first",
        "data": [
            {
                "id": "gemini",
                "available": gemini_backend_ready(),
                "transport": "gemini-web",
            },
            {
                "id": "agy",
                "available": bool(agy_diag.get("available")),
                "transport": "antigravity-cli",
                "models": agy_diag.get("models") or [],
                "model_ids": agy_diag.get("model_ids") or [],
                "max_concurrency": int(agy_diag.get("max_concurrency") or 0),
                "max_queue": int(agy_diag.get("max_queue") or 0),
                "active_requests": int(agy_diag.get("active_requests") or 0),
                "active_request": bool(agy_diag.get("active_request")),
                "queued_requests": int(agy_diag.get("queued_requests") or 0),
                "last_error": agy_diag.get("last_error"),
            },
        ]
    }


@app.get("/v1/endpoint")
async def endpoint_discovery():
    """Return the current fast data plane behind the stable ngrok control URL."""
    payload: Dict[str, Any] = {
        "version": 1,
        "data_plane_url": CONTROL_PLANE_URL,
        "control_plane_url": CONTROL_PLANE_URL,
        "transport": "ngrok-fallback",
    }
    try:
        candidate = json.loads(ENDPOINT_STATE.read_text(encoding="utf-8"))
        data_plane = str(candidate.get("data_plane_url") or "").rstrip("/")
        if (
            data_plane == CONTROL_PLANE_URL
            or data_plane.endswith(".trycloudflare.com")
            or data_plane.startswith("http://127.0.0.1:")
            or data_plane.startswith("http://localhost:")
        ):
            payload.update({
                "data_plane_url": data_plane,
                "control_plane_url": str(candidate.get("control_plane_url") or CONTROL_PLANE_URL).rstrip("/"),
                "transport": str(candidate.get("transport") or "unknown"),
                "updated_at": candidate.get("updated_at"),
            })
    except (OSError, ValueError, TypeError):
        pass
    return payload

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

    backend_raw = body.get("backend")
    if backend_raw is None:
        requested_backend = "auto"
    else:
        if not isinstance(backend_raw, str):
            raise HTTPException(status_code=400, detail="backend must be a string")
        requested_backend = backend_raw.strip().lower()
        if requested_backend not in {"gemini", "agy"}:
            raise HTTPException(status_code=400, detail="backend must be 'gemini' or 'agy'")
    
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

    try:
        backend, base_model, backend_reason = select_request_backend(
            requested_backend=requested_backend,
            model=model,
            thinking_level=thinking_level,
            fallback_thinking_level=fallback_thinking_level,
            has_images=bool(image_paths),
        )
    except HTTPException:
        cleanup_image_paths()
        raise

    trace = {
        "request_id": trace_id,
        "project": source["project"],
        "client": source["client"],
        "ip": source["ip"],
        "backend_requested": requested_backend,
        "backend": backend,
        "backend_reason": backend_reason,
        "model": model,
        "resolved_model": base_model,
        "thinking_level": thinking_level,
        "fallback_thinking_level": fallback_thinking_level,
        "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    recent_requests.append(trace)
    log(
        f"[{trace_id}] Source: project={source['project']} client={source['client']} ip={source['ip']} "
        f"backend={backend} requested={requested_backend} reason={backend_reason}",
        "API",
    )
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

    def submit_gemini_request(gemini_model: str):
        log("Dispatching to Gemini Web browser thread...", "API")
        coro = worker_pool.send_message(
            prompt,
            model=gemini_model,
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
        return asyncio.run_coroutine_threadsafe(coro, browser_loop)

    if backend == "agy":
        agy_timeout = (
            AGY_REQUEST_TOTAL_BUDGET_SECONDS
            if respond_async
            else min(AGY_REQUEST_TOTAL_BUDGET_SECONDS, max(1, PUBLIC_API_TIMEOUT_SECONDS - 15))
        )
        log(f"Dispatching to Antigravity CLI queue (budget={agy_timeout}s)...", "API")
        try:
            future = agy_pool.submit(
                prompt,
                model=base_model,
                thinking_level=thinking_level,
                fallback_thinking_level=fallback_thinking_level,
                use_search=use_search,
                timeout_seconds=agy_timeout,
            )
        except AgyQueueFullError as exc:
            gemini_spill_model = None
            if requested_backend == "auto" and gemini_backend_ready():
                try:
                    gemini_spill_model = normalize_model(model)
                except ValueError:
                    gemini_spill_model = None

            if gemini_spill_model:
                backend = "gemini"
                base_model = gemini_spill_model
                backend_reason = "agy_queue_full"
                trace["backend"] = backend
                trace["resolved_model"] = base_model
                trace["backend_reason"] = backend_reason
                log(f"[{trace_id}] Agy queue full; spilling untagged request to Gemini Web", "API")
                future = submit_gemini_request(base_model)
            else:
                cleanup_image_paths()
                trace["status"] = "rejected"
                trace["error"] = str(exc)
                trace["finished_at"] = datetime.now(timezone.utc).isoformat()
                raise HTTPException(
                    status_code=429,
                    detail=str(exc),
                    headers={"Retry-After": "5"},
                ) from exc
    else:
        future = submit_gemini_request(base_model)

    if respond_async:
        job: Dict[str, Any] = {
            "request_id": trace_id,
            "backend": backend,
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
            prompt_chars=prompt_chars,
            backend=backend,
        ))
        job["_task"] = task
        log(f"[{trace_id}] Async request accepted; poll /v1/chat/completions/{trace_id}", "API")
        return async_job_response(job)

    api_timeout = PUBLIC_API_TIMEOUT_SECONDS
    
    try:
        log(f"Waiting for queued {backend} request (api_timeout={api_timeout}s)...", "API")
        result = await asyncio.get_event_loop().run_in_executor(
            None, 
            lambda: future.result(timeout=api_timeout)
        )
        log(f"{backend} backend returned", "API")
    except TimeoutError:
        try:
            future.cancel()
        except:
            pass
        trace["status"] = "timeout"
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        log(f"{backend.upper()} TIMEOUT after {api_timeout}s", "API")
        raise HTTPException(
            status_code=504,
            detail=f"{backend} request failed: API request timed out after {api_timeout}s",
        )
    except Exception as e:
        trace["status"] = "error"
        trace["error"] = str(e)
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cleanup_image_paths()

    if not result["success"]:
        timed_out = bool(result.get("timed_out"))
        trace["status"] = "timeout" if timed_out else "failed"
        trace["error"] = result.get("error")
        trace["queue_wait_ms"] = result.get("queue_wait_ms")
        trace["attempts"] = result.get("attempts")
        trace["effective_thinking_level"] = result.get("effective_thinking_level")
        trace["request_logs"] = result.get("request_logs") or []
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        raise HTTPException(status_code=504 if timed_out else 500, detail=result.get("error"))

    content = result["response"] or ""
    completion_tokens_est = estimate_text_tokens(content)
    trace["status"] = "ok"
    trace["queue_wait_ms"] = result.get("queue_wait_ms")
    trace["attempts"] = result.get("attempts")
    trace["effective_thinking_level"] = result.get("effective_thinking_level")
    trace["response_chars"] = len(content)
    trace["request_logs"] = result.get("request_logs") or []
    trace["prompt_transport"] = result.get("prompt_transport")
    trace["agy_model"] = result.get("agy_model")
    trace["agy_usage"] = result.get("agy_usage")
    trace["agy_tools_used"] = result.get("agy_tools_used") or []
    trace["response_tokens_est"] = completion_tokens_est
    trace["finished_at"] = datetime.now(timezone.utc).isoformat()
    log(
        f"Got response chars={len(content)} tokens_est={completion_tokens_est}",
        "API",
    )
    
    return build_completion_payload(
        trace_id=trace_id,
        model=model,
        content=content,
        prompt_tokens_est=prompt_tokens_est,
        prompt_chars=prompt_chars,
        backend=backend,
        actual_usage=result.get("agy_usage"),
    )


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

    # FastAPI and Playwright run on different event-loop threads. Keep /health
    # thread-safe and cheap by reading only cached pool state. Deep browser
    # inspection belongs in /v1/diagnostics.
    if worker_pool._browser_state != "warm":
        return 0
    return sum(1 for worker in worker_pool.workers if worker and worker._initialized)


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
    gemini_active = bool(worker_pool and worker_pool._initialized and worker_pool._active_request)
    queued_requests = int(getattr(worker_pool, "_queued_requests", 0) or 0) if worker_pool else 0
    browser_state = getattr(worker_pool, "_browser_state", "unavailable") if worker_pool else "unavailable"
    pool_ready = bool(worker_pool and worker_pool._initialized)
    gemini_ready = gemini_backend_ready() and (
        ready_workers > 0
        or gemini_active
        or browser_state in {"cold", "starting"}
    )
    agy_diag = agy_pool.diagnostics() if agy_pool else {
        "available": False,
        "active_requests": 0,
        "queued_requests": 0,
    }
    agy_ready = bool(agy_diag.get("available"))
    agy_active = int(agy_diag.get("active_requests") or 0)
    is_ready = gemini_ready or agy_ready
    active_request = gemini_active or agy_active > 0
    response.status_code = 200 if is_ready else 503
    return {
        "status": "busy" if active_request else ("ok" if is_ready else "degraded"),
        "ready": is_ready,
        "workers": 1,
        "ready_workers": ready_workers,
        "active_request": active_request,
        "queued_requests": queued_requests,
        "browser_state": browser_state,
        "backends": {
            "gemini": {
                "ready": gemini_ready,
                "active": gemini_active,
                "queued_requests": queued_requests,
            },
            "agy": {
                "ready": agy_ready,
                "active_requests": agy_active,
                "queued_requests": int(agy_diag.get("queued_requests") or 0),
            },
        },
    }


async def get_live_pool_diagnostics() -> Dict[str, Any]:
    """Run Playwright-backed diagnostics on the browser's owning event loop."""
    if not worker_pool:
        return {}
    if not browser_loop or not browser_loop.is_running():
        return worker_pool.get_diagnostics()

    future = asyncio.run_coroutine_threadsafe(
        worker_pool.get_live_diagnostics(),
        browser_loop,
    )
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=8.0)
    except asyncio.TimeoutError:
        future.cancel()
        fallback = worker_pool.get_diagnostics()
        fallback["live_diagnostics_error"] = "browser diagnostics timed out"
        return fallback
    except Exception as exc:
        fallback = worker_pool.get_diagnostics()
        fallback["live_diagnostics_error"] = str(exc)
        return fallback


@app.get("/v1/diagnostics")
async def diagnostics():
    pool_diag = await get_live_pool_diagnostics() if worker_pool else {
        "initialized": False,
        "browser_state": "unavailable",
    }
    ready_workers = ready_worker_count()
    browser_state = pool_diag.get("browser_state")
    gemini_ready = bool(
        gemini_backend_ready()
        and (
            ready_workers
            or worker_pool._active_request
            or browser_state in {"cold", "starting"}
        )
    )
    agy_diag = agy_pool.diagnostics() if agy_pool else {
        "available": False,
        "last_error": "not initialized",
    }
    is_ready = gemini_ready or bool(agy_diag.get("available"))
    return {
        "status": "ok" if is_ready else "degraded",
        "pool": pool_diag,
        "agy": agy_diag,
        "recent_requests": [public_request_trace(trace) for trace in recent_requests],
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
