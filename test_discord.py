"""
Quick test script to verify Discord webhook is working.
Run: python test_discord.py
"""

import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

async def test_webhook():
    webhook_url = os.getenv("DISCORD_WEBHOOK")
    
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK not found in .env")
        print("Add this to your .env file:")
        print("DISCORD_WEBHOOK=https://discord.com/api/webhooks/...")
        return False
    
    print(f"Webhook URL found: {webhook_url[:50]}...")
    
    try:
        import aiohttp
    except ImportError:
        print("ERROR: aiohttp not installed. Run: pip install aiohttp")
        return False
    
    # Test payload - simple embed
    payload = {
        "content": None,
        "embeds": [{
            "title": "Test Notification",
            "description": "If you see this, your Discord webhook is working!",
            "color": 0x00FF00,  # Green
            "footer": {"text": "Gemini Studio API - Test"}
        }]
    }
    
    print("Sending test message...")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json=payload) as response:
            if response.status in (200, 204):
                print("SUCCESS! Check your Discord channel.")
                return True
            else:
                text = await response.text()
                print(f"FAILED! Status: {response.status}")
                print(f"Response: {text}")
                return False

if __name__ == "__main__":
    asyncio.run(test_webhook())
