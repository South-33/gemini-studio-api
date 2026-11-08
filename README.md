# Gemini Studio API

One OpenAI-compatible API with two execution backends:

- `gemini` uses the signed-in Gemini Web browser profile and existing browser
  worker.
- `agy` uses the local Antigravity CLI through a bounded parallel process pool.

Requests with no `backend` tag use **Agy-first routing**. The API uses
Antigravity when it can serve the request, then falls back to Gemini Web before
execution when Agy is unavailable, does not expose the requested model/shape,
cannot accept images, or its bounded queue is full. Once an Agy model call has
started, the API does not silently replay it through Gemini Web.

`backend: "agy"` and `backend: "gemini"` remain strict overrides.

The backends initialize independently. If Gemini Web is unavailable but the
Antigravity CLI is healthy, the API stays online and continues serving `agy`
requests. A healthy Agy backend is on the startup critical path; Gemini Web
warms in the background and becomes available when ready. If Agy cannot start,
the service waits for Gemini Web instead. The reverse also remains true.

## Contract

- Gemini Web models: `flash-lite`, `flash`, `pro`, `argon`
- Thinking: `Low`, `Medium`, `High`; default `Medium`
- Antigravity also accepts exact model IDs currently returned by `agy models`
- `fallback_thinking_level` is optional. After the bounded native Retry path,
  a still-failing request can retry on fresh chats at that thinking level while
  keeping the same model family. Fallback requests use at most three browser
  attempts inside one 480-second logical-request budget: the primary level gets
  up to 120 seconds, and any remaining transient retries use the fallback level.
- `POST /v1/chat/completions` accepts OpenAI-style `messages`
- `backend` is optional and must be `gemini` or `agy`. When omitted, routing is
  Agy-first with pre-execution Gemini Web fallback when needed.
- `POST /v1/chat/completions` with `async: true` returns HTTP 202 and a
  `request_id`; poll `GET /v1/chat/completions/{request_id}` until completion
- `GET /v1/endpoint` returns the current public data-plane URL
- `GET /v1/models` lists the four supported model families
- `GET /v1/backends` reports backend availability and the Antigravity models
  currently exposed by the installed CLI
- `GET /health` returns 200 while the service can accept work, including the
  intentional cold-browser state
- `GET /v1/diagnostics` exposes current browser/queue/recovery state

Default Agy-first example:

```powershell
$body = @{
  model = "flash"
  thinking_level = "high"
  messages = @(@{ role = "user"; content = "Reply with hello" })
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/v1/chat/completions `
  -ContentType application/json `
  -Body $body
```

Strict Antigravity example:

```powershell
$body = @{
  backend = "agy"
  model = "flash"
  thinking_level = "medium"
  fallback_thinking_level = "high"
  async = $true
  messages = @(@{ role = "user"; content = "Reply with hello" })
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/v1/chat/completions `
  -ContentType application/json `
  -Body $body
```

Gemini can change numeric model versions in the web UI. The automation ignores
those numbers, matches only the visible family name (`Flash-Lite`, `Flash`,
`Pro`, or `Argon`), and never exposes numeric versions through the API.

## Current Gemini UI contract

Last validated on 2026-11-05 against the authenticated production account.

- Chat is `https://gemini.google.com/app` with a visible prompt composer.
- The model button currently uses `data-test-id="bard-mode-menu-button"` and an
  accessible label beginning with `Open mode picker`.
- The open picker exposes the stable family names `Flash-Lite`, `Flash`, `Pro`,
  and `Argon`. Any numeric version shown beside them is ignored.
- The picker also has a semantic `Thinking level` row. Open that row, then
  select the exact visible label `Low`, `Medium`, or `High`.
- Responses are returned only from Gemini's Copy control/clipboard. DOM text is
  used for state detection, not as the API response body.
- A Google Accounts sign-in page is not healthy. If the session expires, sign
  in again in the headed production Chromium window.

Each request starts from a verified clean `/app` chat. Long prompts (1,500+
characters) are attached as `prompt.txt` to avoid freezing the editor.
`use_search: true` adds an explicit Search instruction. Caller prompts are sent
without hidden marker or anti-image instructions. Requests without a thinking
fallback use at most three browser attempts for Gemini's recognized transient
refusal or empty-terminal generation states. A recognized Gemini refusal first
uses Gemini's own retry/regenerate control when the current UI exposes one;
otherwise recovery recreates the managed chat tab. Requests with
`fallback_thinking_level` use one primary attempt and up to two fallback
attempts, still bounded by the same 480-second logical-request budget. Browser
or unrelated transport failures are not silently resent.

## Antigravity backend

The `agy` backend is a separate path. It does not open Gemini Web or reuse the
browser worker. It starts one fresh Antigravity print-mode conversation per API
request. Up to three requests run in parallel by default; additional requests
wait in a bounded queue. Set `AGY_MAX_CONCURRENCY` to change the parallel limit
from 1 to 8, and `AGY_MAX_QUEUE` to change the waiting limit. The defaults are 3
and 9. Queue wait counts against the request time budget, so a stale queued
request does not start a new model call after its caller budget is exhausted.

The API discovers current Antigravity model IDs with `agy models`. A caller can
use a stable Gemini family such as `flash` plus `thinking_level`; the API then
resolves the current concrete variant such as `gemini-3.8-flash-high`. A caller
can also send an exact Antigravity model ID such as `claude-sonnet-4-6` or
`gpt-oss-120b-medium`. Exact IDs win over `thinking_level`, because those model
IDs define their own reasoning behavior. The API does not pass `--effort`, since
some Antigravity models reject that flag.

All CLI processes run from one sterile runtime workspace with `--sandbox` and
slash-command expansion disabled. The workspace contains only two static API
agent definitions: one with no tools and one with web search only. CLI log
output is sent to the OS null device so request logs are never persisted. Prompts are
never put on the Windows command line or written to `prompt.txt`; every request
is one NDJSON user event over stdin with `--input-format stream-json`. This also
avoids creating a new workspace-cache entry per API request. All CLI stdin/stdout
pipes use UTF-8 explicitly so non-ASCII prompts do not depend on the Windows
process code page.

Normal requests use the tiny `api-model` agent with no tools. `use_search: true`
uses the separate `api-search` agent with only `search_web`. Each CLI
process also receives session-only `--settings` that override the user's global
Antigravity preferences: permissions use `request-review`; writes, commands,
unsandboxed execution, MCP, and browser actuation are denied; G1 credit fallback
is disabled; `modelProvider` is reset to the signed-in Antigravity default so a
personal `GEMINI_API_KEY` provider cannot silently change billing; and arbitrary
URL reads are denied even for search-enabled requests. The custom agents expose
no local-file tools. The user's persistent Antigravity settings are not modified.
Headless stream-json calls do not use interactive prompt history, so the API does
not override `historySize` or trim the user's normal CLI history.
Personal status-line and terminal-title scripts are disabled for API sessions so
headless calls cannot execute decorative shell commands from CLI preferences.

The custom API agents explicitly set `inheritCustomizations: false` and
`inheritMcp: false`, with no skills. This keeps inherited global/workspace
rules, skills, hooks, and MCP configuration out of API conversations without
requiring the user's normal Antigravity setup to be disabled. MCP tool use is
also denied by the session policy.

Antigravity output is read from `--output-format stream-json`; only the final
`result` event becomes the API response. When Antigravity reports token usage, the OpenAI-style
response exposes those real counts with `token_estimate: false`. Image
attachments are not yet supported by the `agy` backend. Gemini Web continues to
handle them.

Tool events are audited before a response is accepted. A normal request fails
if Antigravity attempts any tool. A `use_search: true` request fails if it tries
anything outside the search allowlist or if `search_web` does not complete
successfully. Successful tool names are retained in request diagnostics.

For a request with `fallback_thinking_level`, Antigravity retries only when the
first successful CLI response is one of Gemini's known transient refusal
messages. This fallback is only supported with stable Gemini family aliases,
where the fallback resolves to a different concrete model variant. A
CLI/process/quota error is returned directly instead of blindly spending
another request.

Stable Gemini family aliases tolerate CLI model rotation. If a cached concrete
slug is rejected with `invalid model selection` before any model tokens are
spent, the pool refreshes `agy models` and retries once with the new slug. Exact
model IDs never get this substitution. A CLI `print_timeout` is always treated
as a timeout; any partial response is discarded and synchronous callers receive
HTTP 504 rather than a misleading partial success.

If all active Antigravity slots and the waiting queue are full, new `agy`
requests are rejected immediately with HTTP 429 and `Retry-After: 5` instead of
creating more CLI processes.

## Install and production startup

```powershell
pip install -r requirements.txt
playwright install chromium
powershell -ExecutionPolicy Bypass -File .\ops\windows\install_autostart.ps1
```

The installer creates a `Gemini Studio API.lnk` shortcut in the signed-in user's
Explorer Startup folder. Windows auto-login starts `launcher.py` when the real
desktop shell opens. `launcher.py` owns the API, headed Chromium, Cloudflare
Quick Tunnel, and ngrok as one Windows process tree. Neither Task Scheduler nor
the HKCU `Run` key is used for persistent production autostart: both were
observed to relaunch the service during SSH maintenance on this host.

The fixed ngrok URL is the control plane. `GET /v1/endpoint` on that URL
publishes the current Cloudflare Quick Tunnel URL, which is the preferred data
plane. Quick Tunnel hostnames change when the launcher restarts. Clients should
rediscover and retry the data plane, with the fixed ngrok URL as fallback. Long
Gemini work should use the async submit/poll API so tunnel changes do not cancel
browser work.
If this installer is run over SSH and no healthy launcher is already running,
it may create a short-lived interactive bootstrap task only to start the initial
launcher in the desktop session. That task is deleted immediately and is not a
production autostart mechanism.
The browser profile is stored in `.browser_session/` and must not be committed.

Headed Chromium needs an interactive desktop. The production host is configured
to provide that desktop automatically after reboot. After five minutes without
API requests, Chromium closes completely; the next request starts it again with
the same persistent profile. If an RDP disconnect suspends the browser, run
`ops\windows\disconnect_rdp.bat` before closing the RDP client.

## Diagnostics

- `logs\launcher.log`: supervisor lifecycle
- `logs\server.log`: API and browser stream
- `logs\ngrok.log`: tunnel process
- `logs\requests\request_*.json`: canonical per-attempt history

Each retained request record contains the full caller prompt, Gemini response,
model/thinking settings, request source metadata, timing, retry decision,
ready-state result, and browser log. Records rotate at 500 files or 512 MiB.
Failed browser interactions can additionally save a screenshot/text snapshot
under `logs\errors`. Refusal snapshots also include the visible actions near
the last response so Retry/regenerate UI changes can be diagnosed without
opening the screenshot. All runtime data is Git-ignored.

## Verify

```powershell
python -m py_compile launcher.py main.py gemini_web.py worker_pool.py gemini_models.py request_log.py agy_cli.py agy_pool.py
python -m unittest discover -s tests -v
```
