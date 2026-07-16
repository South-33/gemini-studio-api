# Gemini Studio API

A lightweight API that automates Gemini Web to provide OpenAI-compatible endpoints. Use Gemini's actual Web UI models (3.5 Flash, 3.1 Pro, 3.1 Flash-Lite) directly from Roo Code, Cline, or any OpenAI-compatible tool.

- **OpenAI Compatible** - Works with Roo Code, Cline, Continue, etc.
- **Web-Aligned Models** - Use exact actual web UI model slugs (e.g. `gemini-3.5-flash`, `gemini-3.1-pro`, `gemini-3.1-flash-lite`) or legacy simplified aliases (`thinking`, `pro`, `flash`, `fast`).
- **Image Upload** - Send images via OpenAI Vision API format
- **Session Persistence** - Login once via Chrome, stays authenticated forever
- **ngrok Tunnel** - Expose your local API to the internet with a static domain
- **Resilient Selectors** - Stable-first fallback selectors for Gemini UI drift
- **Headed Split Windows** - Optional per-worker windows with overlap/tile placement
- **Discord Alerts** - Error notifications with cooldown and diagnostics payload
- **Diagnostics Endpoint** - Worker status, error context, and recent request traces
- **Request Source Tracing** - Track project/client/request-id across logs and diagnostics

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Install browser
playwright install chromium

# Run the API (browser opens - log into Google on first run)
python main.py

# After logging in, set HEADLESS=true in .env for background operation
```

---

## Expose to Internet (ngrok)

```bash
# One-time setup: Create free account at ngrok.com and get auth token
ngrok config add-authtoken YOUR_AUTH_TOKEN

# Optional: Claim a free static domain at dashboard.ngrok.com/domains
ngrok http 8000 --domain=your-subdomain.ngrok-free.app
```

---

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /v1/models` | List available models |
| `POST /v1/chat/completions` | OpenAI-compatible chat |
| `GET /v1/diagnostics` | Worker status, errors, and recent request sources |

---

## Client Setup (Roo Code, Cline)

| Setting | Value |
|---------|-------|
| **Provider** | OpenAI Compatible |
| **Base URL** | `https://your-subdomain.ngrok-free.app/v1` |
| **API Key** | `anything` (ignored) |
| **Model** | `gemini-3.5-flash`, `gemini-3.1-pro`, `gemini-3.1-flash-lite`, `thinking`, `pro`, `flash`, or `fast` |

---

## Available Models

The API directly supports the models and settings present in the Gemini Web UI. You can specify them using their slugified names.

### Web UI Models

| Model ID | Web UI Name | Default Thinking Level | Supported Suffixes |
|----------|-------------|------------------------|--------------------|
| `gemini-3.5-flash` | **3.5 Flash** | `Standard` | `-extended`, `-standard`, `-high`, `-medium`, `-low`, `-minimal` |
| `gemini-3.1-pro` | **3.1 Pro** | *N/A* | *None* |
| `gemini-3.1-flash-lite` | **3.1 Flash-Lite** | *N/A* | *None* |

### Legacy & Simplified Aliases (Supported for Backwards Compatibility)

| Alias | Resolves To Model | Default Thinking Level |
|-------|-------------------|------------------------|
| `thinking` | `gemini-3.5-flash` | `Extended` |
| `flash` | `gemini-3.5-flash` | `Standard` |
| `pro` | `gemini-3.1-pro` | *N/A* |
| `fast` | `gemini-3.1-flash-lite` | *N/A* |

### Controlling Thinking Levels
Only the **3.5 Flash** model supports thinking level configuration. You can select a thinking level by appending a suffix or infix to the model name:
* **Extended Thinking**: `gemini-3.5-flash-extended` (or suffixing `-high`, `-extended`, `:extended`). This will enable the detailed reasoning view in the Web UI.
* **Standard Thinking**: `gemini-3.5-flash-standard` (or suffixing `-standard`, `:standard`). This runs in the normal fast-answer mode.

---

## Configuration (.env)

Copy `.env.example` to `.env` and configure:

```env
# Server
PORT=8000

# Browser
HEADLESS=true             # false to run headed
BROWSER_CHANNEL=chromium  # optional: new Chromium headless mode
WORKER_COUNT=2            # Number of worker tabs
BROWSER_TIMEOUT=480       # Worker timeout seconds
API_TIMEOUT_HEADROOM=30   # API timeout buffer; total API timeout includes retry budget
RECENT_REQUEST_LIMIT=200  # Recent traces stored for /v1/diagnostics

# Headed split-window mode (defaults to true when HEADLESS=false and WORKER_COUNT>=2)
HEADED_SPLIT_WINDOWS=true   # set false to keep worker tabs in one window
HEADED_WINDOW_LAYOUT=overlap  # overlap (default) or tile
HEADED_WINDOW_WIDTH=1366
HEADED_WINDOW_HEIGHT=768
HEADED_WINDOW_OFFSET_X=70
HEADED_WINDOW_OFFSET_Y=45
HEADED_SCREEN_WIDTH=1920      # used by tile layout
HEADED_SCREEN_HEIGHT=1080     # used by tile layout
HEADED_SCREEN_LEFT=0
HEADED_SCREEN_TOP=0

# Performance
LOW_MEMORY_MODE=true
SLOW_VM_MODE=true
DEBUG_SCREENSHOTS=false

# Discord Notifications (optional)
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
DISCORD_USER_ID=123456789  # For @mentions (optional)
DISCORD_COOLDOWN=300       # Seconds between same error alerts
```

### Discord Notifications Setup

Get notified on your phone when errors occur:

1. In Discord: Server Settings > Integrations > Webhooks > New Webhook
2. Copy the webhook URL
3. Add to `.env`: `DISCORD_WEBHOOK=https://discord.com/api/webhooks/...`
4. Test: `python test_discord.py`

---

## API Usage Examples

### Basic Chat Request (curl)

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer anything" \
  -H "X-Project-Name: project-a" \
  -H "X-Client-Name: backend-api" \
  -H "X-Request-ID: req-12345" \
  -d '{
    "model": "gemini-3.5-flash-extended",
    "messages": [
      {"role": "user", "content": "Hello, how are you?"}
    ]
  }'
```

### Python with OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="anything",
    default_headers={
        "X-Project-Name": "project-a",
        "X-Client-Name": "backend-api"
    }
)

response = client.chat.completions.create(
    model="gemini-3.5-flash",
    messages=[{"role": "user", "content": "Write a poem about coding"}]
)
print(response.choices[0].message.content)
```

### Request Source Tagging (Project A vs Project B)

To identify where calls come from, send these headers from each client:

- `X-Project-Name`: Your app/project name (example: `project-a`)
- `X-Client-Name`: Calling service name (example: `api-gateway`)
- `X-Request-ID` (optional): Your own trace ID

Fallback options if headers are hard to add:

- Body fields: `project`, `client`, `request_id`
- Body metadata: `metadata.project`, `metadata.client`

The API logs the source and returns `request_id` in each response. Recent request traces are visible in `GET /v1/diagnostics` under `recent_requests`.

### Image Upload (Vision API)

```python
import base64
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="anything")

with open("image.png", "rb") as f:
    image_b64 = base64.b64encode(f.read()).decode()

response = client.chat.completions.create(
    model="gemini-3.5-flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "What's in this image?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
        ]
    }]
)
print(response.choices[0].message.content)
```

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI server |
| `core.py` | Browser automation with fallback selectors |
| `notifier.py` | Discord webhook notifications |
| `test_discord.py` | Test Discord webhook connection |
| `test_api.html` | Web UI for testing the API |
| `autostart.bat` | Auto-start on Windows boot |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Session expired | Set `HEADLESS=false`, restart, login again |
| 502 Bad Gateway | Make sure API is running (`python main.py`) |
| Selector errors | Check `/v1/diagnostics` for details |
| No Discord alerts | Run `python test_discord.py` to verify webhook |

### Diagnostics Endpoint

Check worker status and errors:

```bash
curl http://localhost:8000/v1/diagnostics
```

Returns:
```json
{
  "status": "ok",
  "pool": {
    "context_page_count": 1,
    "startup_pages_closed": 2,
    "worker_recreation_count": 1,
    "workers": [{
      "worker_id": 1,
      "initialized": true,
      "busy": false,
      "generation_in_progress": false,
      "current_state": {"stop_visible": false, "input_text_len": 0},
      "invariant_violations": []
    }]
  }
}
```

`invariant_violations` is non-empty when the Python worker state disagrees with
the live Gemini page, such as an idle worker whose page still shows **Stop**.

---

## Self-Healing Features

- **Stable-First Selectors** - Uses strict selectors first with bounded fallbacks
- **Request-Level Retry** - Failed requests retry on different workers
- **Periodic Refresh** - Hard refresh every 10 requests to clear stale page state
- **Overlay Dismissal** - Clears stuck overlays before each request
- **Wait-State Telemetry** - Periodic wait logs include stop/send/response/visibility state
- **Stall Watchdog Recovery** - Detects no-output/no-progress generations and recovers before full timeout
- **Verified Recovery** - A refreshed worker is reusable only after the live page proves Stop is gone and input is ready
- **Restored-Page Cleanup** - Closes unmanaged pages restored by the persistent Chrome profile before creating workers
- **Retained-Draft Cleanup** - Clears a prompt left in the composer only after a sent user bubble/start signal is confirmed
- **Headed Window Placement** - Optional CDP-based overlap/tile windows for multi-worker runs
- **Diagnostics Endpoint** - Exposes worker state, live page state, recovery outcomes, invariant violations, and recent request traces
- **Verified Launcher** - `autostart.bat` waits for `/health` and records Python output in `logs/server.log` before reporting success
- **Discord Alerts** - Cooldown-based error notifications with compact diagnostics payload

### Verification

Run the recovery regression suite without launching Chromium:

```powershell
python -m unittest discover -s tests -v
```

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Your Local Server                  │
│  ┌──────────────────────────────────────────┐   │
│  │  main.py (FastAPI)                       │   │
│  │            ↓                             │   │
│  │  core.py (Playwright + Fallback Selectors│   │
│  │            ↓                             │   │
│  │  notifier.py (Discord Alerts)            │   │
│  └──────────────────────────────────────────┘   │
│                      ↓                          │
│  ┌──────────────────────────────────────────┐   │
│  │  ngrok Tunnel (Public HTTPS URL)         │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
                       ↓
              Gemini Web (gemini.google.com)
```
