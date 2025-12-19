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

## Step 6: Cloudflare Tunnel (Public URL)

```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create gemini-local
cloudflared tunnel run --url http://localhost:8000 gemini-local
```

---

## Step 7: Auto-Start on Boot

1. `Win+R` → `shell:startup` → Enter
2. Create a shortcut to a `.bat` that runs:
   ```batch
   python main_bridge.py
   cloudflared tunnel run gemini-local
   ```

---

**That's it!** Now close RDP. The old PC runs headlessly as your Gemini API server.
