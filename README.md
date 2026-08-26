# Gemini Studio API

A small OpenAI-compatible API backed by one logged-in Gemini Web tab. Requests
queue, run in clean chats, copy Gemini's response, and retry once on a newly
created tab after a concrete browser failure.

## Run

1. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Run `python main.py` and sign in to Gemini in the opened browser. Copy
   `.env.example` to `.env` only if Discord failure alerts are wanted.

3. On the Windows server, run `autostart.bat`. Its foreground supervisor owns
   the API, Chromium, and ngrok as one process tree. Press Ctrl+C once or close
   the terminal to stop all of them. Output is written to `logs\server.log`
   and `logs\ngrok.log`.

The login profile is stored in `.browser_session/` and must never be committed.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Readiness; 503 unless the Gemini tab is usable |
| `GET` | `/v1/models` | Stable families: `flash`, `flash-lite`, `pro` |
| `POST` | `/v1/chat/completions` | OpenAI-compatible completion |
| `GET` | `/v1/diagnostics` | Queue, worker, UI, and recovery state |

```powershell
$body = @{
  model = "flash"
  messages = @(@{ role = "user"; content = "Reply with hello" })
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/v1/chat/completions `
  -ContentType application/json -Body $body
```

Numeric model names remain accepted when their family is clear, but clients
should use `flash`, `flash-lite`, or `pro`. Append `-extended` or `-standard` to
Flash, or send `thinking_level`, to choose the reasoning mode.

## Configuration

Operational settings are intentionally fixed in code for this deployment:
port 8000, the production ngrok domain, visible Chromium, one worker, and the
tested timeout/retry policy. `.env` is optional and contains only Discord alert
delivery values. Concurrent calls wait in an in-process queue;
`/v1/diagnostics` exposes `active_request`, `queued_requests`, and the last
ready-state reset. Each serialized request verifies its model, sends, waits,
copies the response, and resets to a clean Temporary Chat before the next
queued request can start.

Prompts at or above 1,500 characters are attached as `prompt.txt` to avoid
freezing Gemini's editor. For those attached prompts, explicit
`use_search: true` or standalone `google`, `search`, or `web` wording leaves a
short Search instruction in the composer.

## Operations

- `stress_test.html` submits concurrent queue checks. It remembers the last API
  root and is preloaded with the production tunnel.
- Check `logs\server.log` for API/browser failures and `logs\ngrok.log` for
  tunnel failures.
- If disconnecting RDP suspends Chromium, run `disconnect_rdp.bat` or use the
  included virtual-display installer.
- Healthy idle tabs are never refreshed because of age or request count.

Gemini's maintained UI inventory is [GEMINI_WEBSITE.md](GEMINI_WEBSITE.md).
Update it and a focused test whenever the site changes.

## Verify

```powershell
python -m unittest discover -s tests -v
python -m py_compile launcher.py main.py gemini_web.py worker_pool.py gemini_models.py notifier.py
```
