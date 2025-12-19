# Gemini Studio API

A lightweight API that automates Google AI Studio to provide OpenAI-compatible endpoints. Use Gemini 2.5 Pro/Flash with thinking levels directly from Roo Code, Cursor, or any OpenAI-compatible tool.

## Features

- **OpenAI Compatible** - Works with Cursor, Roo Code, Continue, etc.
- **Thinking Levels** - Control via model name suffix: `-minimal`, `-low`, `-medium`, `-high`
- **Image Upload** - Send images via OpenAI Vision API format
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

## Model Names

| Model | Description |
|-------|-------------|
| `gemini-3-flash-preview` | Fast responses (default: High thinking) |
| `gemini-3-flash-preview-minimal` | Minimal thinking |
| `gemini-3-flash-preview-low` | Low thinking |
| `gemini-3-flash-preview-medium` | Medium thinking |
| `gemini-3-flash-preview-high` | High thinking |
| `gemini-3-pro-preview` | More capable model |

---

## Configuration (.env)

```env
HEADLESS=true      # false to see the browser
WORKER_COUNT=1     # Number of concurrent browser tabs
```

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
