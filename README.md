# Gemini Studio API

A lightweight API that automates Google AI Studio to provide OpenAI-compatible endpoints. Use Gemini 2.5 Pro/Flash with thinking levels directly from Roo Code, Cursor, or any OpenAI-compatible tool.

## Features

- **OpenAI Compatible** - Works with Cursor, Roo Code, Continue, etc.
- **Thinking Levels** - Control via model name suffix: `-minimal`, `-low`, `-medium`, `-high`
- **Model Selection** - gemini-3-flash-preview, gemini-3-pro-preview
- **Markdown Extraction** - Properly extracts formatted responses via clipboard
- **Session Persistence** - Login once, stays authenticated
- **Multi-Worker** - Handles concurrent requests with multiple browser tabs

---

## Quick Start (Local)

```bash
# Install dependencies
pip install fastapi uvicorn playwright python-dotenv pydantic

# Install browser
playwright install chromium

# Run (first time opens browser for Google login)
python main.py
```

---

## 🚀 Koyeb Deployment (Recommended)

Koyeb offers **4GB RAM / 4 CPU** instances which provide excellent performance for this API.

### 1. Export Cookies from Browser

1. Go to **[aistudio.google.com](https://aistudio.google.com)** and make sure you're logged in
2. Install **Cookie-Editor** extension:
   - [Chrome](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
   - [Firefox](https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/)
3. Click the extension icon → **Export** → **Export as JSON**
4. Save the JSON (you'll need it for the environment variable)

### 2. Test Locally with Docker

```bash
# Build the image
docker build -t gemini-studio-api:test .

# Run with your cookies (paste the JSON in quotes)
docker run -p 8000:8000 \
  -e GEMINI_COOKIES='[{"name":"__Secure-...","value":"..."}]' \
  -e WORKER_COUNT=2 \
  gemini-studio-api:test

# Test the API
curl http://localhost:8000/health
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, say hi back"}'
```

### 3. Push to Docker Hub

```bash
# Login to Docker Hub
docker login

# Tag and push
docker tag gemini-studio-api:test YOUR_USERNAME/gemini-studio-api:latest
docker push YOUR_USERNAME/gemini-studio-api:latest
```

### 4. Deploy to Koyeb

1. Go to [Koyeb Console](https://app.koyeb.com)
2. **Create App** → **Docker**
3. Image: `docker.io/YOUR_USERNAME/gemini-studio-api:latest`
4. Instance: **Medium (4GB RAM / 4 vCPU)** or higher
5. Port: `8000`
6. Add environment variables:
   ```
   PORT=8000
   WORKER_COUNT=4
   HEADLESS=true
   LOW_MEMORY_MODE=false
   SLOW_VM_MODE=false
   GEMINI_COOKIES=[paste your exported JSON here]
   ```
7. Deploy!

### 5. Environment Variables

| Variable | Value | Notes |
|----------|-------|-------|
| `PORT` | `8000` | Koyeb routes to this |
| `WORKER_COUNT` | `4` | One per CPU core |
| `HEADLESS` | `true` | Required for containers |
| `GEMINI_COOKIES` | `[{...}]` | **Required** - JSON from Cookie-Editor |

### 6. Updating & Redeploying

**For code changes:**

```bash
# 1. Rebuild image
docker build -t gemini-studio-api:test .

# 2. Tag for Docker Hub  
docker tag gemini-studio-api:test YOUR_USERNAME/gemini-studio-api:latest

# 3. Push to Docker Hub
docker push YOUR_USERNAME/gemini-studio-api:latest

# 4. In Koyeb dashboard: Redeploy your service (or it auto-pulls on restart)
```

**For cookie updates only:**
- Just update `GEMINI_COOKIES` in Koyeb dashboard → Save → Redeploy
- No rebuild needed!




## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /v1/models` | List available models |
| `POST /v1/chat/completions` | OpenAI-compatible chat |
| `POST /v1/chat` | Simple direct chat |
| `GET /health` | Health check |
| `GET /bandwidth` | Check bandwidth usage |

---

## Roo Code / Cursor Setup

1. **Provider**: OpenAI Compatible
2. **Base URL**: `https://your-app.koyeb.app/v1`
3. **API Key**: `anything` (ignored)
4. **Model**: `gemini-3-flash-preview-minimal`
5. **IMPORTANT**: Disable "Enable streaming" ⚠️

---

## Model Names → Thinking Levels

| Model | Thinking |
|-------|----------|
| `gemini-3-flash-preview` | High (default) |
| `gemini-3-flash-preview-minimal` | Minimal |
| `gemini-3-flash-preview-low` | Low |
| `gemini-3-flash-preview-medium` | Medium |
| `gemini-3-pro-preview` | High |

---

## Configuration (.env)

### For Koyeb (4GB/4CPU)

```env
PORT=8000
WORKER_COUNT=4              # One per CPU (max concurrency)
HEADLESS=true               # Must be true on server
LOW_MEMORY_MODE=false       # Not needed with 4GB RAM
SLOW_VM_MODE=false          # Not needed with 4 CPUs
```

### For Local Development

```env
PORT=8001
WORKER_COUNT=2
HEADLESS=false              # See the browser
LOW_MEMORY_MODE=false
SLOW_VM_MODE=false
```

---

## Session Expiry

Google login sessions typically last **3-6 months** with regular use. If expired:

1. Run locally with `HEADLESS=false`
2. Log in when browser opens
3. Re-upload `.aistudio_data/` folder to your deployment

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Worker pool not initialized" | Check if Chromium installed: `playwright install chromium --with-deps` |
| API not accessible | Check Koyeb deployment logs and health checks |
| Login expired | Re-run locally, login, upload `.aistudio_data/` |
| Timeouts on generation | Check network and try `-minimal` thinking level |
| Container OOM | Reduce `WORKER_COUNT` to 2 |

---

## GCP Deployment (Legacy - Free Tier)

<details>
<summary>Click to expand GCP e2-micro instructions (limited resources)</summary>

The GCP free tier uses e2-micro (1GB RAM) which requires heavy optimizations:

### Settings for GCP e2-micro

```env
PORT=8001
WORKER_COUNT=1           # Only 1 tab due to memory limits
HEADLESS=true
LOW_MEMORY_MODE=true     # Block images/fonts
SLOW_VM_MODE=true        # Use JS clicks, longer delays
BANDWIDTH_LIMIT_MB=900   # Stay under 1GB free tier
```

### Setup Steps

1. Create VM: `e2-micro` in `us-west1` region
2. Allow port 8001 in firewall
3. Install dependencies: `sudo apt install python3.11 python3.11-venv`
4. Run `setup-gcp.sh` script
5. Upload `.aistudio_data/` folder
6. Run with systemd service (see `gemini-api.service`)

**Note**: GCP e2-micro has severe memory limitations. Response times are ~10-30s. Consider Koyeb for better performance.

</details>

---

## Docker Build (Local Testing)

```bash
# Build
docker build -t gemini-studio-api .

# Run (mount session data)
docker run -p 8000:8000 -v $(pwd)/.aistudio_data:/app/.aistudio_data gemini-studio-api
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Server                       │
├─────────────────────────────────────────────────────────┤
│                     WorkerPool                          │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐       │
│  │ Worker 1│ │ Worker 2│ │ Worker 3│ │ Worker 4│       │
│  │ (Tab 1) │ │ (Tab 2) │ │ (Tab 3) │ │ (Tab 4) │       │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘       │
│       └───────────┴───────────┴───────────┘             │
│                        │                                │
│              Shared Browser Context                     │
│                   (Chromium)                            │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
              Google AI Studio (Web)
```
