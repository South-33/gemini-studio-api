import os
import json
import base64

def get_cookies():
    """
    Get cookies from:
    1. GEMINI_COOKIES environment variable (JSON or base64)
    2. cookies.json file in the project directory (fallback)
    Returns: (cookies_list, error_message)
    """
    # Try environment variable first
    cookies_str = os.getenv("GEMINI_COOKIES")
    if cookies_str:
        try:
            # Try as raw JSON first
            cookies = json.loads(cookies_str)
            print(f"[Cookies] ✅ Loaded {len(cookies)} cookies from GEMINI_COOKIES env")
            return cookies, None
        except json.JSONDecodeError:
            try:
                # Try as base64 encoded JSON
                decoded = base64.b64decode(cookies_str).decode('utf-8')
                cookies = json.loads(decoded)
                print(f"[Cookies] ✅ Loaded {len(cookies)} cookies from base64 GEMINI_COOKIES")
                return cookies, None
            except Exception as e:
                return None, f"Failed to parse GEMINI_COOKIES: {str(e)}"
    
    # Try cookies.json file as fallback
    cookies_file = os.path.join(os.path.dirname(__file__), "cookies.json")
    if os.path.exists(cookies_file):
        try:
            with open(cookies_file, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            print(f"[Cookies] ✅ Loaded {len(cookies)} cookies from cookies.json")
            return cookies, None
        except Exception as e:
            return None, f"Failed to read cookies.json: {str(e)}"
    
    return None, "No cookies found. Set GEMINI_COOKIES env or create cookies.json file"

