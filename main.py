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
import uuid
import math
import pathlib
from collections import deque
from datetime import datetime, timezone
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
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "480"))
API_TIMEOUT_HEADROOM = int(os.getenv("API_TIMEOUT_HEADROOM", "30"))
RECENT_REQUEST_LIMIT = int(os.getenv("RECENT_REQUEST_LIMIT", "200"))
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
    
    worker_pool = WorkerPool(worker_count=WORKER_COUNT, provider=PROVIDER)
    # Empty cookies - we use persistent browser session instead
    future = asyncio.run_coroutine_threadsafe(worker_pool.init([]), browser_loop)
    return future.result(timeout=120)

@asynccontextmanager
async def lifespan(app: FastAPI):
    success = await init_browser_thread()
    if success:
        log(f"✅ Multi-Worker Pool Ready ({WORKER_COUNT} workers)")
    else:
        raise RuntimeError("Worker pool failed to initialize")
    
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
    
    Gemini Web models: thinking, pro, fast, flash
    AI Studio models: gemini-3-flash-preview, gemini-3-pro-preview, etc.
    """
    model_lower = model_name.lower()
    
    # Check for explicit suffixes like model-high, model-medium, etc.
    thinking_levels = ["minimal", "low", "medium", "high"]
    for level in thinking_levels:
        if model_lower.endswith(f"-{level}"):
            base = model_name[:-(len(level) + 1)]
            if base.lower() == "thinking":
                return "Flash", level.capitalize()
            return base, level.capitalize()
            
    # Default mappings if no explicit suffix is provided
    if model_lower == "thinking":
        return "Flash", "Extended"
    elif model_lower == "flash":
        return "Flash", "Standard"
    elif model_lower == "fast":
        return "Fast", None
    elif model_lower == "pro":
        return "Pro", None
        
    return model_name, None


def extract_request_source(request: Request, body: Dict) -> Dict[str, str]:
    """Extract source labels from headers/body for diagnostics."""
    headers = request.headers
    project = (
        headers.get("x-project-name")
        or body.get("project")
        or (body.get("metadata") or {}).get("project")
        or "unknown"
    )
    client = (
        headers.get("x-client-name")
        or body.get("client")
        or (body.get("metadata") or {}).get("client")
        or "unknown"
    )
    req_id = (
        headers.get("x-request-id")
        or body.get("request_id")
        or (body.get("metadata") or {}).get("request_id")
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


ERROR_LOG_DIR = pathlib.Path(__file__).parent / "logs" / "errors"
ERROR_LOG_MAX_FILES = 200


def write_error_transaction_log(
    trace: Dict,
    result: Dict,
    recent_req_snapshot: List[Dict],
) -> None:
    """Write a self-contained error report for a failed request to disk.

    Each file captures:
    - Request metadata (model, prompt size, source, timestamps)
    - Per-attempt worker log lines from all retry attempts
    - The last few recent requests for context (did subsequent requests succeed?)

    Files are named:  {timestamp}_{request_id}.log
    Capped at ERROR_LOG_MAX_FILES; oldest files are deleted when the cap is hit.
    """
    try:
        ERROR_LOG_DIR.mkdir(parents=True, exist_ok=True)

        # Rotate: delete oldest files if over cap
        existing = sorted(ERROR_LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
        while len(existing) >= ERROR_LOG_MAX_FILES:
            old_log = existing.pop(0)
            prefix = old_log.stem
            old_log.unlink(missing_ok=True)
            # Delete any HTML or PNG files associated with this transaction ID
            for related in ERROR_LOG_DIR.glob(f"{prefix}*"):
                try:
                    related.unlink(missing_ok=True)
                except Exception as rotate_err:
                    log(f"Failed to delete related diagnostic file {related.name}: {rotate_err}", "API")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        req_id = trace.get("request_id", "unknown")
        filename = ERROR_LOG_DIR / f"{ts}_{req_id}.log"

        lines = []
        sep = "=" * 72
        thin = "-" * 72

        lines.append(sep)
        lines.append("GEMINI-STUDIO-API  ERROR TRANSACTION REPORT")
        lines.append(sep)
        lines.append(f"Request ID  : {req_id}")
        lines.append(f"Started     : {trace.get('started_at', '')}")
        lines.append(f"Finished    : {trace.get('finished_at', '')}")
        lines.append(f"Source      : project={trace.get('project','')} client={trace.get('client','')} ip={trace.get('ip','')}")
        lines.append(f"Model       : {trace.get('model', '')}")
        lines.append(f"Prompt      : chars={trace.get('prompt_chars', 0)} tokens_est={trace.get('prompt_tokens_est', 0)} images={trace.get('image_count', 0)}")
        lines.append(f"Error       : {result.get('error', 'unknown')}")
        lines.append("")

        # Prompt preview (first 3000 chars) — enough to identify what failed
        prompt_preview = trace.get("prompt_preview", "")
        if prompt_preview:
            lines.append(thin)
            lines.append("PROMPT PREVIEW (first 3000 chars)")
            lines.append(thin)
            lines.append(prompt_preview)
            lines.append("")

        # Save full prompt as a sidecar .prompt.txt so it can be replayed
        prompt_full = trace.get("prompt_full", "")
        if prompt_full:
            prompt_path = ERROR_LOG_DIR / f"{ts}_{req_id}.prompt.txt"
            try:
                prompt_path.write_text(prompt_full, encoding="utf-8")
                lines.append(f"Full prompt saved to: {prompt_path.name}")
                lines.append("")
            except Exception as pe:
                lines.append(f"(Failed to save full prompt: {pe})")
                lines.append("")

        attempt_logs = result.get("attempt_logs") or []
        if attempt_logs:
            lines.append(thin)
            lines.append("WORKER ATTEMPT LOGS (all retry attempts)")
            lines.append(thin)
            lines.extend(attempt_logs)
            lines.append("")
        else:
            lines.append("[No per-attempt logs captured]")
            lines.append("")

        lines.append(thin)
        lines.append("RECENT REQUEST CONTEXT (last 6 requests before/including this one)")
        lines.append(thin)
        for r in recent_req_snapshot[-6:]:
            status = r.get("status", "?")
            marker = ">>> FAILED <<<" if r.get("request_id") == req_id else (
                "  OK" if status == "ok" else f"  {status.upper()}"
            )
            lines.append(
                f"  {marker}  [{r.get('request_id','?')}] "
                f"model={r.get('model','?')} "
                f"chars={r.get('prompt_chars','?')} "
                f"tokens={r.get('prompt_tokens_est','?')} "
                f"status={status} "
                f"started={r.get('started_at','')} "
                f"finished={r.get('finished_at','')}"
            )
        lines.append("")
        lines.append(sep)

        filename.write_text("\n".join(lines), encoding="utf-8")
        log(f"Error transaction log written: {filename.name}", "API")
    except Exception as exc:
        log(f"Failed to write error transaction log: {exc}", "API")

# --- Endpoints ---

@app.get("/v1/models")
async def list_models():
    """List available Gemini Web models."""
    return {
        "data": [
            {"id": "fast", "object": "model", "provider": "gemini-web"},
            {"id": "flash", "object": "model", "provider": "gemini-web"},
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
    
    source = extract_request_source(request, body)
    trace_id = source["request_id"]
    trace = {
        "request_id": trace_id,
        "project": source["project"],
        "client": source["client"],
        "ip": source["ip"],
        "model": body.get("model", ""),
        "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    recent_requests.append(trace)

    # Extract fields
    model = body.get("model", "gemini-3-flash-preview")
    messages = body.get("messages", [])
    thinking_level_explicit = body.get("thinking_level")
    use_search = body.get("use_search", False)

    # Parse thinking level from model name
    base_model, thinking_level = parse_model_and_thinking(model)
    if thinking_level_explicit:
        thinking_level = thinking_level_explicit

    log(f"[{trace_id}] Source: project={source['project']} client={source['client']} ip={source['ip']}", "API")
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
    prompt_chars = len(prompt)
    prompt_tokens_est = estimate_text_tokens(prompt) + (len(image_paths) * IMAGE_TOKEN_ESTIMATE)
    trace["prompt_chars"] = prompt_chars
    trace["prompt_tokens_est"] = prompt_tokens_est
    trace["image_count"] = len(image_paths)
    # Store prompt for error log / retry — full prompt so we can replay it if needed
    trace["prompt_preview"] = prompt[:3000]
    trace["prompt_full"] = prompt

    log(
        f"Prompt stats: chars={prompt_chars} tokens_est={prompt_tokens_est} images={len(image_paths)}",
        "API",
    )
    log(f"Dispatching to browser thread...", "API")
    coro = worker_pool.send_message(
        prompt, 
        model=base_model, 
        thinking_level=thinking_level, 
        use_search=use_search, 
        images=image_paths,
        request_id=trace_id,
    )
    future = asyncio.run_coroutine_threadsafe(coro, browser_loop)
    
    retry_budget = min(3, max(1, WORKER_COUNT))
    api_timeout = (BROWSER_TIMEOUT * retry_budget) + API_TIMEOUT_HEADROOM
    
    try:
        log(
            f"Waiting for browser thread (worker_timeout={BROWSER_TIMEOUT}s, retries={retry_budget}, api_timeout={api_timeout}s)...",
            "API"
        )
        result = await asyncio.get_event_loop().run_in_executor(
            None, 
            lambda: future.result(timeout=api_timeout)
        )
        log(f"Browser thread returned", "API")
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
        for img_path in image_paths:
            try: os.unlink(img_path)
            except: pass

    if not result["success"]:
        trace["status"] = "failed"
        trace["error"] = result.get("error")
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_error_transaction_log(trace, result, list(recent_requests))
        raise HTTPException(status_code=500, detail=result.get("error"))

    content = result["response"] or ""
    completion_tokens_est = estimate_text_tokens(content)
    trace["status"] = "ok"
    trace["response_chars"] = len(content)
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

@app.get("/health")
async def health():
    return {"status": "ok", "workers": WORKER_COUNT}


@app.get("/v1/diagnostics")
async def diagnostics():
    if not worker_pool:
        return {"status": "not_initialized"}

    pool_diag = worker_pool.get_diagnostics()
    return {
        "status": "ok",
        "pool": pool_diag,
        "recent_requests": list(recent_requests),
    }

if __name__ == "__main__":
    import uvicorn
    import sys
    # Note: Windows event loop policy is set automatically in Python 3.8+
    # The deprecated set_event_loop_policy warning can be ignored
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
