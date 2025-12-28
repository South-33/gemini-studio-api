# 🚀 Headless Old PC Setup Guide

Your old PC has no keyboard/monitor attached, but you can remote in from your laptop.

---

## Step 1: Enable Remote Desktop on Old PC

Before unplugging the monitor, enable RDP:
- Settings → System → Remote Desktop → Enable

---

## Step 2: Transfer the Code

Copy the `gemini-studio-api` folder to your old PC (USB, network, etc.).

---

## Step 3: Remote In & Install Dependencies

From your laptop, RDP into the old PC, then run:

```powershell
pip install fastapi uvicorn playwright python-dotenv pydantic
playwright install chromium
```

---

## Step 4: Login to Google (One-Time)

While remoted in, run:

```powershell
cd path\to\gemini-studio-api
python setup_session.py
```

A Chrome window will open on the remote desktop. Log into Google and go to `aistudio.google.com`. Once you see the prompt box, close the script (Ctrl+C).

**Done!** The session is now saved locally on the old PC.

---

## Step 5: Start the API

```powershell
python main_bridge.py
```

The API runs headlessly using your saved session.

---

## Step 6: ngrok Tunnel (Persistent Public URL)

```powershell
# Install ngrok
winget install ngrok.ngrok

# Authenticate (one-time setup)
# Get your authtoken from: https://dashboard.ngrok.com/get-started/your-authtoken
ngrok config add-authtoken YOUR_AUTHTOKEN

# Claim a free static domain at: https://dashboard.ngrok.com/cloud-edge/domains
# Then run with your static domain:
ngrok http 8000 --domain=YOUR_STATIC_DOMAIN.ngrok-free.app
```

Your domain stays the same forever (e.g., `my-gemini-api.ngrok-free.app`).

---

## Step 7: Auto-Start on Boot

1. `Win+R` → `shell:startup` → Enter
2. Create a shortcut to `autostart.bat`
3. Add your ngrok domain to `.env`: `NGROK_DOMAIN=your-domain.ngrok-free.app`

---

**That's it!** Now close RDP. The old PC runs headlessly as your Gemini API server.
