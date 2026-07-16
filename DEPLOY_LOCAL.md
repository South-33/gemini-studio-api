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
pip install -r requirements.txt
playwright install chromium
```

---

## Step 4: Login to Google (One-Time)

While remoted in, run:

```powershell
cd path\to\gemini-studio-api
python main.py
```

A Chrome window will open on the remote desktop (HEADLESS defaults to false). Log into Google and go to `gemini.google.com`. Once logged in, stop the script (Ctrl+C).

Then set `HEADLESS=true` in your `.env` file for background operation.

---

## Step 5: Start the API

```powershell
python main.py
```

On startup the API closes pages restored from the persistent Chrome profile,
then creates exactly the configured number of managed workers. This removes old
`Generating` windows left by an earlier crashed or restarted process.

After startup, verify the live browser/Python state agrees:

```powershell
Invoke-RestMethod http://localhost:8000/v1/diagnostics | ConvertTo-Json -Depth 8
```

Each worker should have an empty `invariant_violations` list.

When using `autostart.bat`, the launcher waits for the API health check before
starting the tunnel or reporting success. Python startup output is retained in
`logs\server.log`; a failed startup prints the last 40 lines and stays open.

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
