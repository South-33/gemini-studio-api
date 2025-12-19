# Session Setup Script
# Run this ONCE in your Daytona environment with a display (VNC/noVNC)
# It opens a visible browser for you to log in manually.
# After login, close this script - the session is saved!
#
# USAGE:
#   python setup_session.py
#
# This creates a persistent browser profile at ./.browser_session/
# When you run the main API, it will use this same folder = already logged in!

import asyncio
import os
import sys
from playwright.async_api import async_playwright

# Persistent session folder - this survives restarts!
SESSION_DIR = os.path.join(os.path.dirname(__file__), ".browser_session")


async def setup_session():
    print("""
╔══════════════════════════════════════════════════════════════╗
║           GEMINI STUDIO API - SESSION SETUP                  ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  A browser window will open.                                 ║
║                                                              ║
║  1. Log into your Google account                             ║
║  2. Go to: aistudio.google.com                               ║
║  3. Make sure you can see the prompt textarea                ║
║  4. Close this script (Ctrl+C) when done                     ║
║                                                              ║
║  Your session will be saved and used by the API!             ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    print(f"[Setup] Session will be saved to: {SESSION_DIR}")
    
    playwright = await async_playwright().start()
    
    # Launch browser with persistent context (NOT headless!)
    context = await playwright.chromium.launch_persistent_context(
        SESSION_DIR,
        headless=False,  # VISIBLE so you can log in
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
        viewport={"width": 1280, "height": 800},
    )
    
    # Open AI Studio
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://aistudio.google.com/")
    
    print("\n[Setup] Browser opened! Please log in to Google and navigate to AI Studio.")
    print("[Setup] When you see the prompt textarea, press Ctrl+C to save and exit.\n")
    
    # Keep running until user stops
    try:
        while True:
            await asyncio.sleep(1)
            
            # Check if logged in
            try:
                textarea = await page.query_selector('textarea[aria-label="Enter a prompt"]')
                if textarea:
                    print("[Setup] ✅ Detected AI Studio login! You can close this now (Ctrl+C).")
                    await asyncio.sleep(5)  # Give them time to see the message
            except:
                pass
                
    except KeyboardInterrupt:
        print("\n[Setup] Saving session...")
    
    await context.close()
    await playwright.stop()
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                    SESSION SAVED! ✅                         ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Your login is now saved to: {SESSION_DIR}
║                                                              ║
║  To use the API:                                             ║
║    python main.py                                            ║
║                                                              ║
║  The API will automatically use your saved session!          ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    asyncio.run(setup_session())
