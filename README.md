# Gemini Studio API

A lightweight API that automates Google AI Studio to provide OpenAI-compatible endpoints. Use Gemini 2.5 Pro/Flash with thinking levels directly from Roo Code, Cursor, or any OpenAI-compatible tool.

- **Multi-Provider** - Supports both **Google AI Studio** and **Gemini Web** (gemini.google.com)
- **OpenAI Compatible** - Works with Cursor, Roo Code, Continue, etc.
- **Thinking Levels** - Control via model name suffix: `-minimal`, `-low`, `-medium`, `-high`
- **Simplified Models** - Use `pro`, `thinking`, or `fast` with Gemini Web
- **Image Upload** - Send images via OpenAI Vision API format (Supported on both providers)
- **Session Persistence** - Login once via Chrome, stays authenticated forever
- **Cloudflare Tunnel** - Expose your local API to the internet for free

---

## 🖥️ Local Server Deployment

Run on your own hardware — no cloud costs, session never expires!

### Quick Start

```bash
# Install dependencies
pip install fastapi uvicorn playwright python-dotenv pydantic

# Install browser
playwright install chromium

# First-time: Login to Google
python setup_session.py  # Chrome opens, log in, then Ctrl+C

# Run the API
python main.py
```

### Expose to Internet (Cloudflare Tunnel)

```bash
cloudflared tunnel --url http://localhost:8000
```

You'll get a public URL like: `https://random-words.trycloudflare.com`

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

## Client Setup (Cursor, Roo Code)

| Setting | Value |
|---------|-------|
| **Provider** | OpenAI Compatible |
| **Base URL** | `https://your-tunnel-url.trycloudflare.com/v1` |
| **API Key** | `anything` (ignored) |
| **Model** | `gemini-3-flash-preview` |

---

## How to Use as an API

This API is fully **OpenAI-compatible** and can be used from any programming language or tool that supports OpenAI's chat completions endpoint.

> **Note**: Replace `http://localhost:8001` in the examples below with:
> - Your Cloudflare Tunnel URL: `https://your-tunnel-url.trycloudflare.com`
> - Or your deployed API URL (if hosted on Convex, Railway, etc.)

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

### AI Studio Models
| Model | Description |
|-------|-------------|
| `gemini-3-flash-preview` | Fast responses (default: High thinking) |
| `gemini-3-flash-preview-minimal` | Minimal thinking |
| `gemini-3-flash-preview-low` | Low thinking |
| `gemini-3-flash-preview-medium` | Medium thinking |
| `gemini-3-flash-preview-high` | High thinking |
| `gemini-3-pro-preview` | More capable model |

### Gemini Web Models (gemini.google.com)
| Model | Description |
|-------|-------------|
| `fast` | Quick responses |
| `thinking` | Complex problem solving |
| `pro` | Advanced math & reasoning |

> **Auto-Routing**: By default (`PROVIDER=auto`), the API automatically routes requests to the correct provider based on model name. Use `thinking`, `pro`, or `fast` to hit Gemini Web; use `gemini-3-*` models to hit AI Studio.

---

## Configuration (.env)

```env
HEADLESS=true      # false to see the browser
WORKER_COUNT=1     # Ignored in dual-provider mode
PROVIDER=auto      # "auto" (default), "aistudio", or "gemini-web"
```

| Provider Mode | Behavior |
|---------------|----------|
| `auto` | Opens both tabs, routes by model name |
| `aistudio` | Only uses AI Studio |
| `gemini-web` | Only uses Gemini Web |

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | Main API server |
| `core.py` | AI Studio automation logic |
| `setup_session.py` | One-time Google login script |
| `test_api.html` | Web UI for testing the API |
| `autostart.bat` | Auto-start on Windows boot |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Session expired | Run `python setup_session.py` and login again |
| 502 Bad Gateway | Make sure API is running (`python main.py`) |
| Cloudflare can't connect | Use `http://` not `https://` in tunnel command |

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
│  │  Cloudflare Tunnel                        │   │
│  │  (Public HTTPS URL)                       │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
                       ↓
              Google AI Studio (Web)
```
