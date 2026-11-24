"""Gemini Studio API.

Default requests go to Gemini Web. Antigravity is an explicit, separate backend.
"""

from __future__ import annotations

import asyncio
import base64
import math
import os
import sys
import tempfile
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agy_pool import AgyPool, AgyQueueFullError, REQUEST_BUDGET_SECONDS
from gemini_models import (
    DEFAULT_MODEL,
    DEFAULT_THINKING_LEVEL,
    MODEL_FAMILIES,
    normalize_model,
    normalize_thinking_level,
)
from worker_pool import WorkerPool


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")


RECENT_REQUEST_LIMIT = 100
ASYNC_JOB_LIMIT = 50
ASYNC_JOB_RETENTION_SECONDS = 3600
IMAGE_TOKEN_ESTIMATE = 300
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 20 * 1024 * 1024

worker_pool: WorkerPool | None = None
agy_pool: AgyPool | None = None
recent_requests: deque[dict[str, Any]] = deque(maxlen=RECENT_REQUEST_LIMIT)
async_jobs: dict[str, dict[str, Any]] = {}


def log(message: str, tag: str = "API") -> None:
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{tag}] {message}", file=sys.stderr, flush=True)


@asynccontextmanager
async def lifespan(_: Starlette):
    global worker_pool, agy_pool

    try:
        idle_seconds = float(os.environ.get("GEMINI_IDLE_SECONDS", "60"))
    except ValueError:
        idle_seconds = 60.0

    worker_pool = WorkerPool(idle_browser_seconds=max(0.0, idle_seconds))
    await worker_pool.init(cleanup_stale_browser=True)

    agy_pool = AgyPool()
    log("Agy ready on demand; CLI is cold until an explicit Agy request")
    log("Gemini Web ready on demand; browser is cold until first request")
    try:
        yield
    finally:
        if agy_pool:
            agy_pool.close()
        if worker_pool:
            await worker_pool.close()


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(
        1,
        math.ceil(len(text) / 4),
        math.ceil(len(text.split()) * 1.3),
    )


def extract_request_source(request: Request, body: dict[str, Any]) -> dict[str, str]:
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    request_id = (
        request.headers.get("x-request-id")
        or body.get("request_id")
        or metadata.get("request_id")
        or uuid.uuid4().hex
    )
    return {
        "request_id": str(request_id),
        "project": str(
            request.headers.get("x-project-name")
            or body.get("project")
            or metadata.get("project")
            or "unknown"
        ),
        "client": str(
            request.headers.get("x-client-name")
            or body.get("client")
            or metadata.get("client")
            or "unknown"
        ),
    }


def build_completion_payload(
    *,
    request_id: str,
    model: str,
    backend: str,
    content: str,
    prompt_tokens_est: int,
    prompt_chars: int,
    actual_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completion_tokens_est = estimate_text_tokens(content)

    def valid_count(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    if (
        isinstance(actual_usage, dict)
        and valid_count(actual_usage.get("input_tokens"))
        and valid_count(actual_usage.get("output_tokens"))
    ):
        prompt_tokens = int(actual_usage["input_tokens"])
        completion_tokens = int(actual_usage["output_tokens"])
        total = actual_usage.get("total_tokens")
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(total)
            if valid_count(total)
            else prompt_tokens + completion_tokens,
            "token_estimate": False,
        }
        thinking = actual_usage.get("thinking_tokens")
        if valid_count(thinking):
            usage["thinking_tokens"] = int(thinking)
    else:
        usage = {
            "prompt_tokens": prompt_tokens_est,
            "completion_tokens": completion_tokens_est,
            "total_tokens": prompt_tokens_est + completion_tokens_est,
            "token_estimate": True,
        }

    usage["prompt_chars"] = prompt_chars
    usage["completion_chars"] = len(content)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "backend": backend,
        "request_id": request_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def prune_async_jobs() -> None:
    cutoff = time.time() - ASYNC_JOB_RETENTION_SECONDS
    removable = [
        request_id
        for request_id, job in async_jobs.items()
        if job.get("status") in {"ok", "failed", "error", "timeout"}
        and float(job.get("finished_unix") or 0) < cutoff
    ]
    for request_id in removable:
        async_jobs.pop(request_id, None)

    while len(async_jobs) > ASYNC_JOB_LIMIT:
        terminal = next(
            (
                request_id
                for request_id, job in async_jobs.items()
                if job.get("status") in {"ok", "failed", "error", "timeout"}
            ),
            None,
        )
        if terminal is None:
            break
        async_jobs.pop(terminal, None)


def async_job_response(job: dict[str, Any]) -> JSONResponse:
    status = str(job.get("status") or "started")
    if status == "ok":
        return JSONResponse(status_code=200, content=job["response"])
    if status in {"failed", "error", "timeout"}:
        return JSONResponse(
            status_code=200,
            content={
                "request_id": job["request_id"],
                "status": status,
                "error": str(job.get("error") or "request failed"),
            },
        )
    return JSONResponse(
        status_code=202,
        content={"request_id": job["request_id"], "status": status},
    )


def decode_messages(messages: Any) -> tuple[str, list[str]]:
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty array")

    prompt_parts: list[str] = []
    image_paths: list[str] = []

    def cleanup() -> None:
        for path in image_paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    try:
        for message in messages:
            if not isinstance(message, dict):
                raise HTTPException(status_code=400, detail="Each message must be an object")
            content = message.get("content", "")
            if isinstance(content, str):
                if content:
                    prompt_parts.append(content)
                continue
            if not isinstance(content, list):
                raise HTTPException(
                    status_code=400,
                    detail="Message content must be a string or an array",
                )

            for part in content:
                if not isinstance(part, dict):
                    raise HTTPException(
                        status_code=400,
                        detail="Message content parts must be objects",
                    )
                part_type = part.get("type", "text")
                if part_type == "text":
                    text = part.get("text", "")
                    if not isinstance(text, str):
                        raise HTTPException(
                            status_code=400,
                            detail="Text content must be a string",
                        )
                    if text:
                        prompt_parts.append(text)
                    continue

                if part_type != "image_url":
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsupported message content type: {part_type}",
                    )
                if len(image_paths) >= MAX_IMAGES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"At most {MAX_IMAGES} images are supported",
                    )

                image_spec = part.get("image_url")
                image_url = image_spec.get("url", "") if isinstance(image_spec, dict) else ""
                if not isinstance(image_url, str) or not image_url.startswith("data:image/"):
                    raise HTTPException(
                        status_code=400,
                        detail="Only data:image/... image URLs are supported",
                    )
                try:
                    header, encoded = image_url.split(",", 1)
                    if not header.lower().endswith(";base64"):
                        raise ValueError("Image data URL must use base64 encoding")
                    mime = header.split(";", 1)[0].split(":", 1)[1].lower()
                    extension = {
                        "image/png": "png",
                        "image/jpeg": "jpg",
                        "image/gif": "gif",
                        "image/webp": "webp",
                    }.get(mime)
                    if not extension:
                        raise ValueError(f"Unsupported image MIME type: {mime}")
                    data = base64.b64decode(encoded, validate=True)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError(
                            f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MiB limit"
                        )
                    with tempfile.NamedTemporaryFile(
                        delete=False,
                        suffix=f".{extension}",
                    ) as temp_file:
                        temp_file.write(data)
                        image_paths.append(temp_file.name)
                except HTTPException:
                    raise
                except Exception as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid image attachment: {exc}",
                    ) from exc

        prompt = "\n".join(prompt_parts).strip()
        if not prompt and not image_paths:
            raise HTTPException(
                status_code=400,
                detail="Request contains no text or image content",
            )
        return prompt, image_paths
    except Exception:
        cleanup()
        raise


def cleanup_files(paths: list[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


async def run_backend(
    *,
    backend: str,
    prompt: str,
    images: list[str],
    model: str,
    thinking_level: str | None,
    use_search: bool,
    request_id: str,
) -> dict[str, Any]:
    if backend == "gemini":
        if not worker_pool:
            return {
                "success": False,
                "backend_unavailable": True,
                "error": "Gemini worker is unavailable",
            }
        return await worker_pool.send_message(
            prompt,
            model=model,
            thinking_level=thinking_level,
            use_search=use_search,
            images=images,
            request_id=request_id,
        )

    if not agy_pool or not agy_pool.is_ready():
        return {
            "success": False,
            "backend_unavailable": True,
            "error": "Antigravity backend is unavailable",
        }

    try:
        future = agy_pool.submit(
            prompt,
            model=model,
            use_search=use_search,
            timeout_seconds=REQUEST_BUDGET_SECONDS,
        )
    except AgyQueueFullError as exc:
        return {"success": False, "queue_full": True, "error": str(exc)}
    except RuntimeError as exc:
        return {
            "success": False,
            "backend_unavailable": True,
            "error": str(exc),
        }
    return await asyncio.wrap_future(future)


async def wait_for_client_disconnect(request: Request) -> bool:
    """Wait for ASGI http.disconnect after the JSON body has been consumed."""
    receive = getattr(request, "receive", None)
    if not callable(receive):
        return False
    while True:
        try:
            message = await receive()
        except Exception:
            return False
        message_type = message.get("type")
        if message_type == "http.disconnect":
            return True
        if message_type != "http.request":
            return False


async def finish_async_job(
    *,
    job: dict[str, Any],
    task: asyncio.Task[dict[str, Any]],
    trace: dict[str, Any],
    model: str,
    backend: str,
    prompt_tokens_est: int,
    prompt_chars: int,
    image_paths: list[str],
) -> None:
    try:
        result = await task
        if not result.get("success"):
            error = str(result.get("error") or "request failed")
            job["status"] = "timeout" if result.get("timed_out") else "failed"
            job["error"] = error
            trace["status"] = job["status"]
            trace["error"] = error
            return

        content = result.get("response")
        if not isinstance(content, str):
            raise RuntimeError("Backend returned a non-text response")

        response = build_completion_payload(
            request_id=job["request_id"],
            model=model,
            backend=backend,
            content=content,
            prompt_tokens_est=prompt_tokens_est,
            prompt_chars=prompt_chars,
            actual_usage=result.get("agy_usage"),
        )
        job["status"] = "ok"
        job["response"] = response
        trace["status"] = "ok"
        trace["response_chars"] = len(content)
        trace["attempts"] = result.get("attempts")
        trace["queue_wait_ms"] = result.get("queue_wait_ms")
        if backend == "gemini" and worker_pool:
            worker_timing = getattr(worker_pool, "_last_request_timing", None)
            if worker_timing and worker_timing.get("request_id") == job["request_id"]:
                trace["worker_timing_ms"] = dict(worker_timing)
    except asyncio.CancelledError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        trace["status"] = "error"
        trace["error"] = str(exc)
    finally:
        cleanup_files(image_paths)
        finished = datetime.now(timezone.utc).isoformat()
        job["finished_at"] = finished
        job["finished_unix"] = time.time()
        trace["finished_at"] = finished
        started_unix = trace.get("started_unix")
        if isinstance(started_unix, (int, float)):
            trace["duration_ms"] = int((time.time() - started_unix) * 1000)
        prune_async_jobs()


async def list_models() -> dict[str, Any]:
    return {
        "data": [
            {"id": model, "object": "model", "provider": "gemini-web"}
            for model in MODEL_FAMILIES
        ],
        "default": {
            "model": DEFAULT_MODEL,
            "thinking_level": DEFAULT_THINKING_LEVEL,
        },
    }


async def list_backends() -> dict[str, Any]:
    if agy_pool and not agy_pool.is_ready():
        await asyncio.to_thread(agy_pool.init)
    return {
        "default": "gemini",
        "gemini": {
            "available": bool(worker_pool and worker_pool._initialized),
            "models": list(MODEL_FAMILIES),
            "thinking_levels": ["Low", "Medium", "High"],
        },
        "agy": agy_pool.diagnostics()
        if agy_pool
        else {"available": False, "models": [], "last_error": "not initialized"},
    }


async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    if body.get("stream"):
        raise HTTPException(status_code=400, detail="Streaming is not supported")

    respond_async = body.get("async", False)
    if not isinstance(respond_async, bool):
        raise HTTPException(status_code=400, detail="async must be a boolean")
    use_search = body.get("use_search", False)
    if not isinstance(use_search, bool):
        raise HTTPException(status_code=400, detail="use_search must be a boolean")

    backend = body.get("backend", "gemini")
    if not isinstance(backend, str):
        raise HTTPException(status_code=400, detail="backend must be a string")
    backend = backend.strip().lower()
    if backend not in {"gemini", "agy"}:
        raise HTTPException(status_code=400, detail="backend must be 'gemini' or 'agy'")

    source = extract_request_source(request, body)
    request_id = source["request_id"]
    prune_async_jobs()
    if respond_async and request_id in async_jobs:
        return async_job_response(async_jobs[request_id])

    model_value = body.get("model")
    thinking_value = body.get("thinking_level")
    if model_value is not None and not isinstance(model_value, str):
        raise HTTPException(status_code=400, detail="model must be a string")
    if thinking_value is not None and not isinstance(thinking_value, str):
        raise HTTPException(status_code=400, detail="thinking_level must be a string")

    if backend == "gemini":
        try:
            model = normalize_model(model_value or DEFAULT_MODEL)
            thinking_level = normalize_thinking_level(
                thinking_value or DEFAULT_THINKING_LEVEL
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        if not model_value or not model_value.strip():
            if agy_pool and not agy_pool.is_ready():
                await asyncio.to_thread(agy_pool.init)
            available = agy_pool.available_models() if agy_pool else []
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Agy requests require an exact model ID",
                    "models": available,
                },
            )
        if thinking_value is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "thinking_level is a Gemini Web setting; "
                    "Agy reasoning is part of its exact model ID"
                ),
            )
        model = model_value.strip()
        thinking_level = None
        if not agy_pool:
            raise HTTPException(status_code=503, detail="Antigravity backend is unavailable")
        if not agy_pool.is_ready() and not await asyncio.to_thread(agy_pool.init):
            raise HTTPException(
                status_code=503,
                detail=agy_pool.diagnostics().get("last_error")
                or "Antigravity backend is unavailable",
            )
        if not agy_pool.has_model(model):
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Unknown Agy model ID {model!r}",
                    "models": agy_pool.available_models(),
                },
            )

    prompt, image_paths = decode_messages(body.get("messages"))
    if backend == "agy" and image_paths:
        cleanup_files(image_paths)
        raise HTTPException(
            status_code=400,
            detail="Image attachments are supported only by the Gemini Web backend",
        )

    prompt_chars = len(prompt)
    prompt_tokens_est = estimate_text_tokens(prompt) + (
        len(image_paths) * IMAGE_TOKEN_ESTIMATE
    )
    started_unix = time.time()
    trace = {
        "request_id": request_id,
        "project": source["project"],
        "client": source["client"],
        "backend": backend,
        "model": model,
        "thinking_level": thinking_level,
        "image_count": len(image_paths),
        "prompt_chars": prompt_chars,
        "status": "started",
        "started_unix": started_unix,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    recent_requests.append(trace)
    log(
        f"[{request_id}] {backend} model={model} "
        f"thinking={thinking_level or '-'} images={len(image_paths)} "
        f"chars={prompt_chars}"
    )

    task = asyncio.create_task(
        run_backend(
            backend=backend,
            prompt=prompt,
            images=image_paths,
            model=model,
            thinking_level=thinking_level,
            use_search=use_search,
            request_id=request_id,
        )
    )

    if respond_async:
        if len(async_jobs) >= ASYNC_JOB_LIMIT and not any(
            job.get("status") in {"ok", "failed", "error", "timeout"}
            for job in async_jobs.values()
        ):
            task.cancel()
            cleanup_files(image_paths)
            raise HTTPException(status_code=429, detail="Async job capacity is full")

        job: dict[str, Any] = {
            "request_id": request_id,
            "backend": backend,
            "status": "started",
            "started_at": trace["started_at"],
        }
        async_jobs[request_id] = job
        job["_finalizer"] = asyncio.create_task(
            finish_async_job(
                job=job,
                task=task,
                trace=trace,
                model=model,
                backend=backend,
                prompt_tokens_est=prompt_tokens_est,
                prompt_chars=prompt_chars,
                image_paths=image_paths,
            )
        )
        return async_job_response(job)

    disconnect_task: asyncio.Task[bool] | None = None
    try:
        if backend != "gemini":
            result = await task
        else:
            disconnect_task = asyncio.create_task(wait_for_client_disconnect(request))
            done, _ = await asyncio.wait(
                {task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                result = await task
            elif disconnect_task.result():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                trace["status"] = "cancelled"
                trace["error"] = "Client disconnected"
                trace["finished_at"] = datetime.now(timezone.utc).isoformat()
                trace["duration_ms"] = int((time.time() - started_unix) * 1000)
                log(f"[{request_id}] Client disconnected; Gemini work cancelled")
                return JSONResponse({"detail": "Client disconnected"}, status_code=499)
            else:
                result = await task
    except asyncio.CancelledError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        trace["status"] = "cancelled"
        trace["finished_at"] = datetime.now(timezone.utc).isoformat()
        trace["duration_ms"] = int((time.time() - started_unix) * 1000)
        raise
    finally:
        if disconnect_task is not None and not disconnect_task.done():
            disconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect_task
        cleanup_files(image_paths)

    trace["queue_wait_ms"] = result.get("queue_wait_ms")
    trace["attempts"] = result.get("attempts")
    trace["effective_thinking_level"] = result.get("effective_thinking_level")
    trace["finished_at"] = datetime.now(timezone.utc).isoformat()
    trace["duration_ms"] = int((time.time() - started_unix) * 1000)
    if backend == "gemini" and worker_pool:
        worker_timing = getattr(worker_pool, "_last_request_timing", None)
        if worker_timing and worker_timing.get("request_id") == request_id:
            trace["worker_timing_ms"] = dict(worker_timing)

    if not result.get("success"):
        error = str(result.get("error") or "request failed")
        trace["status"] = "timeout" if result.get("timed_out") else "failed"
        trace["error"] = error
        if result.get("queue_full"):
            raise HTTPException(
                status_code=429,
                detail=error,
                headers={"Retry-After": "5"},
            )
        if result.get("backend_unavailable"):
            raise HTTPException(status_code=503, detail=error)
        if result.get("timed_out"):
            raise HTTPException(status_code=504, detail=error)
        raise HTTPException(status_code=500, detail=error)

    content = result.get("response")
    if not isinstance(content, str):
        raise HTTPException(status_code=500, detail="Backend returned a non-text response")

    trace["status"] = "ok"
    trace["response_chars"] = len(content)
    return build_completion_payload(
        request_id=request_id,
        model=model,
        backend=backend,
        content=content,
        prompt_tokens_est=prompt_tokens_est,
        prompt_chars=prompt_chars,
        actual_usage=result.get("agy_usage"),
    )


async def get_chat_completion(request_id: str):
    prune_async_jobs()
    job = async_jobs.get(request_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown async request_id")
    return async_job_response(job)


async def health(response: Response) -> dict[str, Any]:
    browser = worker_pool.get_diagnostics() if worker_pool else {}
    ready = bool(
        worker_pool
        and worker_pool._initialized
        and browser.get("browser_state") not in {"error", "auth_required"}
    )
    response.status_code = 200 if ready else 503
    return {
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "default_backend": "gemini",
        "browser_state": browser.get("browser_state", "unavailable"),
        "active_request": bool(browser.get("active_request")),
        "queued_requests": int(browser.get("queued_requests") or 0),
        "agy_available": bool(agy_pool and agy_pool.is_ready()),
    }


async def diagnostics() -> dict[str, Any]:
    browser = (
        await worker_pool.get_live_diagnostics()
        if worker_pool
        else {"initialized": False, "browser_state": "unavailable"}
    )
    return {
        "gemini": browser,
        "agy": agy_pool.diagnostics()
        if agy_pool
        else {"available": False, "models": []},
        "recent_requests": list(recent_requests),
        "async_jobs": {
            request_id: {
                key: value
                for key, value in job.items()
                if not key.startswith("_") and key != "response"
            }
            for request_id, job in async_jobs.items()
        },
    }

def _as_http_response(value: Any) -> Response:
    return value if isinstance(value, Response) else JSONResponse(value)


async def _models_http(_: Request) -> Response:
    return _as_http_response(await list_models())


async def _backends_http(_: Request) -> Response:
    return _as_http_response(await list_backends())


async def _chat_http(request: Request) -> Response:
    return _as_http_response(await chat_completions(request))


async def _chat_status_http(request: Request) -> Response:
    return _as_http_response(
        await get_chat_completion(str(request.path_params["request_id"]))
    )


async def _health_http(_: Request) -> Response:
    response = Response()
    payload = await health(response)
    return JSONResponse(payload, status_code=response.status_code)


async def _diagnostics_http(_: Request) -> Response:
    return _as_http_response(await diagnostics())


async def _http_exception_handler(_: Request, exc: HTTPException) -> Response:
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


app = Starlette(
    routes=[
        Route("/v1/models", _models_http, methods=["GET"]),
        Route("/v1/backends", _backends_http, methods=["GET"]),
        Route("/v1/chat/completions", _chat_http, methods=["POST"]),
        Route(
            "/v1/chat/completions/{request_id}",
            _chat_status_http,
            methods=["GET"],
        ),
        Route("/health", _health_http, methods=["GET"]),
        Route("/v1/diagnostics", _diagnostics_http, methods=["GET"]),
    ],
    lifespan=lifespan,
    exception_handlers={HTTPException: _http_exception_handler},
)
