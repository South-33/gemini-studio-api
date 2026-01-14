"""
One-time setup script to log in to Gemini.
Run this once with: python setup_login.py

After logging in, the session is saved to .browser_session/main/
and will be copied to all workers on startup.
"""
import asyncio
import os
from playwright.async_api import async_playwright

async def main():
    print("=" * 50)
    print("GEMINI LOGIN SETUP")
    print("=" * 50)
    print()
    print("A browser window will open.")
    print("Please log in to your Google account.")
    print("After you see Gemini's chat interface, press Enter here.")
    print()
    
    session_dir = os.path.join(os.path.dirname(__file__), ".browser_session", "main")
    os.makedirs(session_dir, exist_ok=True)
    
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            session_dir,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://gemini.google.com/")
        
        print("Browser opened. Log in now...")
        input("\n>>> Press Enter after you're logged in and see the Gemini chat... ")
        
        # Close browser to save session
        await context.close()
    
    print()
    print("✅ Session saved to .browser_session/main/")
    print("✅ You can now run: python main.py")
    print()

if __name__ == "__main__":
    asyncio.run(main())
