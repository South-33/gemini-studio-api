# Gemini Studio API

A lightweight API that automates Gemini Web to provide OpenAI-compatible endpoints. Use Gemini's thinking, pro, or fast models directly from Roo Code, Cline, or any OpenAI-compatible tool.

- **OpenAI Compatible** - Works with Roo Code, Cline, Continue, etc.
- **Simplified Models** - Use `thinking`, `pro`, or `fast`
- **Image Upload** - Send images via OpenAI Vision API format
- **Session Persistence** - Login once via Chrome, stays authenticated forever
- **ngrok Tunnel** - Expose your local API to the internet with a static domain
- **Self-Healing** - Auto-recovers from stuck UI states and errors
- **Discord Alerts** - Get notified on your phone when errors occur
- **Resilient Selectors** - Fallback selectors that survive Google UI changes
- **Request Source Tracing** - Track which app/project sent each request
- **Optional Tab Keepalive** - Prevent idle headless tabs from drifting into stale states

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
| **Model** | `thinking` (recommended) or `pro`, `fast` |

---

## Available Models

| Model | Description |
|-------|-------------|
| `fast` | Quick responses |
| `thinking` | Complex problem solving |
| `pro` | Advanced math & reasoning |

---

## Configuration (.env)

Copy `.env.example` to `.env` and configure:

```env
# Browser
HEADLESS=true           # false to see the browser
WORKER_COUNT=2          # Number of parallel tabs (1 worker = 1 tab)
BROWSER_TIMEOUT=480     # Max seconds for one browser request
RECENT_REQUEST_LIMIT=200  # Number of recent request traces kept in memory
ENABLE_TAB_KEEPALIVE=false  # Keep idle tabs warm/recover stale stop state
TAB_KEEPALIVE_INTERVAL=75   # Seconds between keepalive checks
TAB_IDLE_RECOVER_SECONDS=300  # Only maintain workers idle this long

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
    "model": "thinking",
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
    model="thinking",
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
    model="thinking",
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
  "workers": [
    {"worker_id": 1, "initialized": true, "busy": false, "last_error": null}
  ],
  "errors": {}
}
```

---

## Self-Healing Features

- **Fallback Selectors** - Uses structural selectors (`role`, `contenteditable`) that survive UI text changes
- **Request-Level Retry** - Failed requests automatically retry on different workers
- **Periodic Refresh** - Browser tabs refresh every 10 requests to clear cache
- **Overlay Dismissal** - Clears stuck modals/menus before each request
- **Idle Keepalive (optional)** - Detects/recover stale `Stop response` states while idle
- **Error Tracking** - All errors logged with selector, action, and timestamp
- **Discord Alerts** - Optional notifications with 5-minute cooldown to prevent spam

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
