# Gemini Studio API

A lightweight API that automates Google AI Studio to provide OpenAI-compatible endpoints. Use Gemini Pro/Flash with thinking levels directly from Roo Code, Cline, or any OpenAI-compatible tool.

- **Multi-Provider** - Supports both **Google AI Studio** and **Gemini Web** (gemini.google.com)
- **OpenAI Compatible** - Works with Roo Code, Cline, Continue, etc.
- **Thinking Levels** - Control via model name suffix: `-minimal`, `-low`, `-medium`, `-high`
- **Simplified Models** - Use `thinking`, `pro`, or `fast` with Gemini Web (recommended)
- **Image Upload** - Send images via OpenAI Vision API format
- **Session Persistence** - Login once via Chrome, stays authenticated forever
- **ngrok Tunnel** - Expose your local API to the internet with a static domain
- **Self-Healing** - Auto-recovers from stuck UI states and errors

> ⚠️ **Note**: AI Studio has bot detection that may block automation. **Gemini Web models (`thinking`, `pro`, `fast`) are recommended** for reliable operation.

---

## 🖥️ Local Server Deployment

Run on your own hardware — no cloud costs, session never expires!

### Quick Start

```bash
# Install dependencies
pip install fastapi uvicorn playwright python-dotenv pydantic

# Install browser
playwright install chromium

# Run the API (browser opens - log into Google on first run)
python main.py

# After logging in, set HEADLESS=true in .env for background operation
```

### Expose to Internet (ngrok)

```bash
# One-time setup: Create free account at ngrok.com and get auth token
ngrok config add-authtoken YOUR_AUTH_TOKEN

# Optional: Claim a free static domain at dashboard.ngrok.com/domains
# Then run with your domain:
ngrok http 8000 --domain=your-subdomain.ngrok-free.app
```

You'll get a public URL like: `https://your-subdomain.ngrok-free.app`

### Auto-Start on Boot

1. Copy `autostart.bat` shortcut to: `Win+R` → `shell:startup`
2. Enable auto-login: `Win+R` → `netplwiz` → uncheck password requirement

See [DEPLOY_LOCAL.md](DEPLOY_LOCAL.md) for full headless server setup guide.

---

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /v1/models` | List available models |
| `POST /v1/chat/completions` | OpenAI-compatible chat |

---

## Client Setup (Roo Code, Cline)

| Setting | Value |
|---------|-------|
| **Provider** | OpenAI Compatible |
| **Base URL** | `https://your-subdomain.ngrok-free.app/v1` |
| **API Key** | `anything` (ignored) |
| **Model** | `thinking` (recommended) or `pro`, `fast` |

---

## How to Use as an API

This API is fully **OpenAI-compatible** and can be used from any programming language or tool that supports OpenAI's chat completions endpoint.

> **Note**: Replace `http://localhost:8001` in the examples below with:
> - Your ngrok URL: `https://your-subdomain.ngrok-free.app`
> - Or your deployed API URL (if hosted elsewhere)

> **Model Routing**: The API automatically routes to the correct provider based on the model name:
> - Use `thinking`, `pro`, or `fast` → Routes to **Gemini Web**
> - Use `gemini-3-*` models → Routes to **AI Studio**

### Basic Chat Request (curl)

```bash
curl -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer anything" \
  -d '{
    "model": "gemini-3-flash-preview",
    "messages": [
      {"role": "user", "content": "Hello, how are you?"}
    ]
  }'
```

### Python Example

```python
import requests

response = requests.post(
    "http://localhost:8001/v1/chat/completions",
    headers={"Authorization": "Bearer anything"},
    json={
        "model": "gemini-3-flash-preview",
        "messages": [
            {"role": "user", "content": "Explain quantum computing"}
        ]
    }
)
print(response.json()["choices"][0]["message"]["content"])
```

### Using OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8001/v1",
    api_key="anything"  # API key is ignored but required by SDK
)

response = client.chat.completions.create(
    model="gemini-3-flash-preview",
    messages=[
        {"role": "user", "content": "Write a poem about coding"}
    ]
)
print(response.choices[0].message.content)
```

### JavaScript/TypeScript Example

```javascript
const response = await fetch('http://localhost:8001/v1/chat/completions', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer anything'
  },
  body: JSON.stringify({
    model: 'gemini-3-flash-preview',
    messages: [
      { role: 'user', content: 'Hello!' }
    ]
  })
});

const data = await response.json();
console.log(data.choices[0].message.content);
```

### Image Upload (Vision API)

```python
import base64
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8001/v1", api_key="anything")

# Read and encode image
with open("image.png", "rb") as f:
    image_b64 = base64.b64encode(f.read()).decode()

response = client.chat.completions.create(
    model="gemini-3-flash-preview",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"}
                }
            ]
        }
    ]
)
print(response.choices[0].message.content)
```

### Response Format

```json
{
  "id": "chatcmpl-1234567890",
  "object": "chat.completion",
  "created": 1766152800,
  "model": "gemini-3-flash-preview",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! I'm doing well, thank you for asking..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 45,
    "total_tokens": 57
  }
}
```

---

### Available Models
| Model | Description |
|-------|-------------|
| `fast` | Quick responses |
| `thinking` | Complex problem solving |
| `pro` | Advanced math & reasoning |

---

## Configuration (.env)

```env
HEADLESS=true      # false to see the browser
WORKER_COUNT=2     # Number of parallel tabs (1 worker = 1 tab)
```

| Setting | Description |
|---------|-------------|
| `WORKER_COUNT` | Number of concurrent browser tabs. Each can handle one request. |
| `HEADLESS` | Run browser invisibly (recommended for servers) |

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | Main API server |
| `core.py` | Browser automation logic |
| `test_api.html` | Web UI for testing the API |
| `autostart.bat` | Auto-start on Windows boot |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Session expired | Set `HEADLESS=false` in .env, restart, and login again |
| 502 Bad Gateway | Make sure API is running (`python main.py`) |
| ngrok connection refused | Ensure API is running before starting ngrok |
| Extraction failed (500 errors) | Usually auto-recovers. If persistent, restart the API |
| High CPU usage | Normal during generation. Polling is optimized to reduce load |
| Stuck overlay/modal | Auto-dismissed on next request. Hard refresh every 10 requests |

### Automatic Recovery Features

The API includes several self-healing mechanisms:

- **Request-Level Retry**: If a worker fails, automatically retries on a different worker (up to 3 attempts)
- **Response Validation**: Detects empty/invalid responses and triggers retry
- **Chrome Anti-Throttling**: Browser flags prevent Chrome from suspending background tabs during idle
- **Overlay Dismissal**: Automatically clears stuck Angular Material modals/menus before each request
- **Periodic Hard Refresh**: Browser tab refreshes every 10 requests to clear memory/cache buildup
- **Error Recovery**: On failure, the page automatically refreshes to reset state for the next request
- **Optimized Polling**: Reduced polling frequency to minimize CPU pressure on low-resource hosts

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Your Local Server                  │
│  ┌──────────────────────────────────────────┐   │
│  │  main.py (FastAPI)                       │   │
│  │            ↓                             │   │
│  │  core.py (Playwright Automation)         │   │
│  │            ↓                             │   │
│  │  .browser_session/ (saved login)         │   │
│  └──────────────────────────────────────────┘   │
│                      ↓                          │
│  ┌──────────────────────────────────────────┐   │
│  │  ngrok Tunnel                             │   │
│  │  (Public HTTPS URL)                       │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
                       ↓
              Gemini Web (gemini.google.com)
```
