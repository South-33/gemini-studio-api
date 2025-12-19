# Gemini Studio API

A lightweight API that automates Google AI Studio to provide OpenAI-compatible endpoints. Use Gemini 2.5 Pro/Flash with thinking levels directly from Roo Code, Cursor, or any OpenAI-compatible tool.

## Features

- **OpenAI Compatible** - Works with Cursor, Roo Code, Continue, etc.
- **Thinking Levels** - Control via model name suffix: `-minimal`, `-low`, `-medium`, `-high`
- **Session Persistence** - Login once via Chrome, stays authenticated forever
- **Cloudflare Tunnel** - Expose your local API to the internet for free

---

## 🖥️ Local Server Deployment (Recommended)

Run on your own hardware — no cloud costs, session never expires!

### Quick Start

```bash
# Install dependencies
pip install fastapi uvicorn playwright python-dotenv pydantic
playwright install chromium

# First-time: Login to Google
python setup_session.py  # Chrome opens, log in, then Ctrl+C

# Run the API
python main_bridge.py
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
| `POST /v1/chat/completions` | OpenAI-compatible chat |

---

## Client Setup (Cursor, Roo Code)

| Setting | Value |
|---------|-------|
| **Provider** | OpenAI Compatible |
| **Base URL** | `https://your-tunnel-url.trycloudflare.com/v1` |
| **API Key** | `anything` (ignored) |
| **Model** | `gemini-3-flash-preview` |
| **Streaming** | Disabled ⚠️ |

---

## Model Names

| Model | Description |
|-------|-------------|
| `gemini-3-flash-preview` | Fast responses |
| `gemini-3-flash-preview-minimal` | Minimal thinking |
| `gemini-3-pro-preview` | More capable |

---

## Configuration (.env)

```env
HEADLESS=true              # false to see the browser
```

---

## Files

| File | Purpose |
|------|---------|
| `main_bridge.py` | Main API server (uses persistent session) |
| `test_api.html` | Premium UI for testing the API (OpenAI format) |
| `setup_session.py` | One-time Google login script |
| `autostart.bat` | Auto-start on Windows boot |
| `DEPLOY_LOCAL.md` | Full headless server setup guide |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Session expired | Run `python setup_session.py` and login again |
| 502 Bad Gateway | Make sure API is running (`python main_bridge.py`) |
| Cloudflare can't connect | Use `http://` not `https://` in tunnel command |

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Your Local Server                  │
│  ┌──────────────────────────────────────────┐   │
│  │  main_bridge.py (FastAPI)                │   │
│  │            ↓                             │   │
│  │  Playwright (Chromium)                   │   │
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
