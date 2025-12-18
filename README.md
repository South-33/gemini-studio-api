# Gemini Studio API

A local API that automates Google AI Studio to provide OpenAI-compatible endpoints. Use Gemini 3 Pro/Flash with thinking levels directly from Roo Code, Cursor, or any OpenAI-compatible tool.

## Features

- **OpenAI Compatible** - Works with Cursor, Roo Code, Continue, etc.
- **Thinking Levels** - Control via model name suffix: `-minimal`, `-low`, `-medium`, `-high`
- **Model Selection** - gemini-3-flash-preview, gemini-3-pro-preview
- **Markdown Extraction** - Properly extracts formatted responses via clipboard
- **Session Persistence** - Login once, stays authenticated
- **Bandwidth Limiter** - Auto-stops at 900MB/month (configurable) to stay in GCP free tier

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

## GCP Deployment (Free Tier)

### 1. Create VM (Free Tier Settings)

| Setting | Value |
|---------|-------|
| **Region** | `us-west1 (Oregon)` ⭐ MUST be this for free |
| **Machine type** | `e2-micro` ⭐ MUST be e2-micro |
| **Boot disk** | Ubuntu 22.04, `Standard persistent disk`, 10GB |
| **Firewall** | ✅ Allow HTTP, ✅ Allow HTTPS |

### 2. Create Firewall Rule for Port 8001

Go to: [VPC Network → Firewall](https://console.cloud.google.com/networking/firewalls)

| Field | Value |
|-------|-------|
| Name | `allow-gemini-api` |
| Direction | Ingress |
| Targets | All instances |
| Source IP | `0.0.0.0/0` |
| TCP Ports | `8001` |

### 3. SSH into VM and Setup

```bash
# Install dependencies
sudo apt update && sudo apt install -y git unzip python3.11 python3.11-venv

# Install Playwright dependencies
sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0

# Clone repo
git clone https://github.com/South-33/gemini-studio-api.git
cd gemini-studio-api

# Create venv and install
python3.11 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn playwright python-dotenv pydantic
playwright install chromium
```

### 4. Upload Session Data

The `.aistudio_data/` folder contains your Google login. Upload it via:
- GCP SSH console → Gear icon → Upload file
- Or use SCP: `scp -r .aistudio_data/ USER@VM_IP:~/gemini-studio-api/`

### 5. Create Production .env

```bash
cat > .env << 'EOF'
PORT=8001
WORKER_COUNT=1
HEADLESS=true
LOW_MEMORY_MODE=true
BANDWIDTH_LIMIT_MB=900
EOF
```

### 6. Run the API

```bash
# Activate venv
source venv/bin/activate

# Run
python main.py
```

API available at: `http://YOUR_VM_IP:8001`

### 7. Run as Background Service (Optional)

```bash
# Copy service file
sudo cp gemini-api.service /etc/systemd/system/
sudo nano /etc/systemd/system/gemini-api.service  # Update YOUR_USERNAME

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable gemini-api
sudo systemctl start gemini-api

# Check status
sudo systemctl status gemini-api
```

---

## Updating Code

After making changes locally:

```bash
# On your PC
git add .
git commit -m "Your changes"
git push

# On GCP VM
cd ~/gemini-studio-api
git pull
sudo systemctl restart gemini-api  # If using systemd
```

---

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /v1/models` | List available models |
| `POST /v1/chat/completions` | OpenAI-compatible chat |
| `POST /v1/chat` | Simple direct chat |
| `GET /health` | Health check |
| `GET /bandwidth` | Check bandwidth usage vs limit |

---

## Roo Code / Cursor Setup

1. **Provider**: OpenAI Compatible
2. **Base URL**: `http://YOUR_VM_IP:8001/v1`
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

```env
PORT=8001                  # API port
WORKER_COUNT=1             # Browser tabs (1 for e2-micro)
HEADLESS=true              # Must be true on server
LOW_MEMORY_MODE=true       # Blocks images/fonts to save RAM
BANDWIDTH_LIMIT_MB=900     # Monthly limit (stops API when reached)
```

---

## Bandwidth Protection

The API tracks outbound data and blocks requests at 900MB/month to stay within GCP free tier (1GB limit).

Check usage: `GET /bandwidth`
```json
{
  "status": "ok",
  "month": "2025-12",
  "used_mb": 45.32,
  "limit_mb": 900,
  "remaining_mb": 854.68,
  "percent_used": 5.0
}
```

---

## Session Expiry

Google login sessions typically last **3-6 months** with regular use. If expired:

1. Run locally with `HEADLESS=false`
2. Log in when browser opens
3. Re-upload `.aistudio_data/` folder to GCP

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Worker pool not initialized" | Check if Chromium installed: `playwright install chromium` |
| "Bandwidth limit reached" | Wait until next month or increase `BANDWIDTH_LIMIT_MB` |
| API not accessible | Check firewall rule (port 8001) and VM external IP |
| Login expired | Re-run locally, login, upload `.aistudio_data/` |

---

## Cost Summary (GCP Free Tier)

| Resource | Free Limit | Your Usage |
|----------|------------|------------|
| e2-micro VM | 1 in US region | ✅ |
| Standard disk | 30 GB | 10 GB ✅ |
| Egress | 1 GB/month | Capped at 900MB ✅ |

**Total monthly cost: $0.00** (if you stay in limits)
