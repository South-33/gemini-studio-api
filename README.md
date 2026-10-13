# Gemini Studio API

One OpenAI-compatible API backed by one signed-in Gemini Web tab on the
production Windows server. Requests are serialized through one browser worker.

## Contract

- Model: `flash`
- Thinking: `Medium`, `High`; default `Medium`
- Only these model and thinking values are accepted
- `POST /v1/chat/completions` accepts OpenAI-style `messages`
- `GET /v1/models` lists the single supported model family
- `GET /health` returns 200 only while the worker is on a usable Gemini `/app`
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

Gemini can change numeric model versions in the web UI. The automation matches
the visible family text and does not expose numeric versions through the API.

## Current Gemini UI contract

Last validated on 2026-10-13.

- Chat is `https://gemini.google.com/app` with a visible prompt composer.
- The model button currently uses `data-test-id="bard-mode-menu-button"` and an
  accessible label beginning with `Open mode picker`.
- The closed picker includes the active thinking level when applicable, for
  example `Flash Medium`.
- The API selects only `Medium` or `High`. The automation can still recognize
  Gemini's `Low` UI state so it can switch away from it before a request.
- Responses are returned only from Gemini's Copy control/clipboard. DOM text is
  used for state detection, not as the API response body.
- A Google Accounts sign-in page is not healthy. If the session expires, sign
  in again in the headed production Chromium window.

Each request starts from a verified clean `/app` chat. Long prompts (1,500+
characters) are attached as `prompt.txt` to avoid freezing the editor.
`use_search: true` adds an explicit Search instruction. Caller prompts are sent
without hidden marker or anti-image instructions. The worker retries once only
for Gemini's known transient refusal message. Browser or transport failures are
not silently resent.

## Install and production startup

```powershell
pip install -r requirements.txt
playwright install chromium
powershell -ExecutionPolicy Bypass -File .\ops\windows\install_autostart.ps1
```

The scheduled task `Gemini Studio API` runs `launcher.py` directly at user
sign-in. `launcher.py` owns the API, headed Chromium, and ngrok as one Windows
process tree. The browser profile is stored in `.browser_session/` and must not
be committed.

Headed Chromium needs an interactive desktop. After a server reboot, the server
account must be signed in before the API can run. If RDP disconnect suspends the
browser, run `ops\windows\disconnect_rdp.bat` before closing the RDP client.

## Diagnostics

- `logs\launcher.log`: supervisor lifecycle
- `logs\server.log`: API and browser stream
- `logs\ngrok.log`: tunnel process
- `logs\requests\request_*.json`: canonical per-attempt history

Each retained request record contains the full caller prompt, Gemini response,
model/thinking settings, request source metadata, timing, retry decision,
ready-state result, and browser log. Records rotate at 500 files or 512 MiB.
Failed browser interactions can additionally save a screenshot/text snapshot
under `logs\errors`. All runtime data is Git-ignored.

## Verify

```powershell
python -m py_compile launcher.py main.py gemini_web.py worker_pool.py gemini_models.py request_log.py
python -m unittest discover -s tests -v
```
