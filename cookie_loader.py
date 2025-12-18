import os
import json
import base64

def get_cookies():
    """
    Get cookies from environment variable GEMINI_COOKIES.
    Returns: (cookies_list, error_message)
    """
    cookies_str = os.getenv("GEMINI_COOKIES")
    if not cookies_str:
        return None, "GEMINI_COOKIES environment variable not set"
    
    try:
        # Try as raw JSON first
        cookies = json.loads(cookies_str)
        return cookies, None
    except json.JSONDecodeError:
        try:
            # Try as base64 encoded JSON
            decoded = base64.b64decode(cookies_str).decode('utf-8')
            cookies = json.loads(decoded)
            return cookies, None
        except Exception as e:
            return None, f"Failed to parse GEMINI_COOKIES: {str(e)}"
