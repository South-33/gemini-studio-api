# Gemini Studio API

One OpenAI-compatible API backed by one signed-in Gemini Web browser profile on
the production Windows server. Requests are serialized through one browser
worker.

## Contract

- Models: `flash-lite`, `flash`, `pro`, `argon`
- Thinking: `Low`, `Medium`, `High`; default `Medium`
- Only these model and thinking values are accepted
- `fallback_thinking_level` is optional. After the bounded native Retry path,
  a still-failing request can retry once on a fresh chat at that thinking level
  while keeping the same model family. Fallback requests are bounded to two
  browser attempts: up to 60 seconds on the primary level and up to 210 seconds
  on the fallback level.
- `POST /v1/chat/completions` accepts OpenAI-style `messages`
- `POST /v1/chat/completions` with `async: true` returns HTTP 202 and a
  `request_id`; poll `GET /v1/chat/completions/{request_id}` until completion
- `GET /v1/endpoint` returns the current public data-plane URL
- `GET /v1/models` lists the four supported model families
- `GET /health` returns 200 while the service can accept work, including the
  intentional cold-browser state
- `GET /v1/diagnostics` exposes current browser/queue/recovery state

Example:

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
`fallback_thinking_level` use one primary attempt and one fallback attempt.
Browser or unrelated transport failures are not silently resent.

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
python -m py_compile launcher.py main.py gemini_web.py worker_pool.py gemini_models.py request_log.py
python -m unittest discover -s tests -v
```
