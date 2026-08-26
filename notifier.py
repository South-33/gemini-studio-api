"""
Discord notification module for Gemini Studio API.
Sends alerts when errors occur, with deduplication to prevent spam.
"""

from __future__ import annotations

import os
import time
import hashlib
import asyncio
import json
from datetime import datetime
from typing import Optional, Dict

# Try to import aiohttp, but don't crash if not available
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False


class DiscordNotifier:
    """
    Sends error notifications to Discord with cooldown/deduplication.
    
    Features:
    - Embeds with color-coding (red for errors)
    - Fingerprinting to identify duplicate errors
    - Cooldown to prevent notification spam
    - Optional @mention for phone push notifications
    - Rate limit handling (429 retry)
    """
    
    def __init__(
        self,
        webhook_url: Optional[str] = None,
        cooldown_seconds: int = 300,
        user_id: Optional[str] = None
    ):
        """
        Initialize Discord notifier.
        
        Args:
            webhook_url: Discord webhook URL (from .env DISCORD_WEBHOOK)
            cooldown_seconds: Minimum seconds between same error notifications
            user_id: Discord user ID for @mentions (from .env DISCORD_USER_ID)
        """
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK")
        self.cooldown = cooldown_seconds
        self.user_id = user_id or os.getenv("DISCORD_USER_ID")
        
        # Track last notification time per error fingerprint
        self._last_sent: Dict[str, float] = {}
        # Track occurrence count during cooldown
        self._occurrence_count: Dict[str, int] = {}
        
        # Shared aiohttp session (reused for efficiency)
        self._session: Optional[aiohttp.ClientSession] = None
    
    @property
    def enabled(self) -> bool:
        """Check if notifier is properly configured."""
        return bool(self.webhook_url) and AIOHTTP_AVAILABLE
    
    def _get_fingerprint(self, selector_key: str, action: str) -> str:
        """
        Create a unique fingerprint for this error type.
        Excludes timestamp so same errors are grouped.
        """
        data = f"{selector_key}:{action}"
        return hashlib.md5(data.encode()).hexdigest()[:12]
    
    def _should_notify(self, fingerprint: str) -> bool:
        """
        Check if we should send notification (cooldown check).
        Returns True if not in cooldown period.
        """
        now = time.time()
        
        if fingerprint in self._last_sent:
            elapsed = now - self._last_sent[fingerprint]
            if elapsed < self.cooldown:
                # Still in cooldown - increment occurrence count
                self._occurrence_count[fingerprint] = self._occurrence_count.get(fingerprint, 0) + 1
                return False
        
        # Not in cooldown - reset and allow
        self._last_sent[fingerprint] = now
        return True
    
    def _get_squelched_count(self, fingerprint: str) -> int:
        """Get and reset the count of squelched occurrences."""
        count = self._occurrence_count.get(fingerprint, 0)
        self._occurrence_count[fingerprint] = 0
        return count
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def send_error(
        self,
        error: str,
        selector_key: str,
        action: str,
        worker_id: int = 0,
        diagnostics: Optional[Dict] = None,
    ) -> bool:
        """
        Send error notification to Discord.
        
        Args:
            error: Error message
            selector_key: Which selector failed (e.g., "input", "send_btn")
            action: What action was being attempted (e.g., "init", "send_message")
            worker_id: Which worker encountered the error
            
        Returns:
            True if notification was sent, False if skipped/failed
        """
        if not self.enabled:
            return False
        
        fingerprint = self._get_fingerprint(selector_key, action)
        
        if not self._should_notify(fingerprint):
            return False
        
        # Get count of squelched occurrences since last notification
        squelched = self._get_squelched_count(fingerprint)
        
        # Build embed
        embed = {
            "title": "Gemini API Error",
            "color": 0xFF0000,  # Red
            "fields": [
                {"name": "Worker", "value": str(worker_id), "inline": True},
                {"name": "Selector", "value": f"`{selector_key}`", "inline": True},
                {"name": "Action", "value": action, "inline": True},
                {"name": "Error", "value": error[:1000], "inline": False},  # Truncate if too long
            ],
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "Gemini Studio API"}
        }

        # Add structured diagnostics context when provided
        if diagnostics:
            try:
                diag_text = json.dumps(diagnostics, ensure_ascii=True, separators=(",", ":"))
            except Exception:
                diag_text = str(diagnostics)

            if len(diag_text) > 1000:
                diag_text = diag_text[:997] + "..."

            embed["fields"].append({
                "name": "Diagnostics",
                "value": diag_text,
                "inline": False,
            })
        
        # Add squelched count if there were suppressed occurrences
        if squelched > 0:
            embed["fields"].append({
                "name": "Suppressed",
                "value": f"{squelched} similar errors during cooldown",
                "inline": False
            })
        
        # Build payload
        payload = {"embeds": [embed]}
        
        # Add @mention if user ID configured (triggers phone notification)
        if self.user_id:
            payload["content"] = f"<@{self.user_id}>"
        
        # Send to Discord
        try:
            session = await self._get_session()
            
            async with session.post(self.webhook_url, json=payload) as response:
                if response.status == 429:
                    # Rate limited - get retry_after and wait
                    data = await response.json()
                    retry_after = data.get("retry_after", 1)
                    await asyncio.sleep(retry_after)
                    # Retry once
                    async with session.post(self.webhook_url, json=payload) as retry_response:
                        return retry_response.status in (200, 204)
                
                return response.status in (200, 204)
                
        except Exception as e:
            # Don't crash the app if notification fails
            message = str(e)
            lowered = message.lower()
            if "getaddrinfo failed" in lowered or "name or service not known" in lowered:
                print(f"[Notifier] Failed to send Discord notification (local DNS/network issue): {e}")
            else:
                print(f"[Notifier] Failed to send Discord notification: {e}")
            return False
    
    async def send_recovery(self, message: str = "All workers recovered") -> bool:
        """
        Send recovery notification (green embed).
        Call this when errors are resolved.
        """
        if not self.enabled:
            return False
        
        embed = {
            "title": "Gemini API Recovered",
            "description": message,
            "color": 0x00FF00,  # Green
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "Gemini Studio API"}
        }
        
        payload = {"embeds": [embed]}
        
        try:
            session = await self._get_session()
            async with session.post(self.webhook_url, json=payload) as response:
                return response.status in (200, 204)
        except Exception as e:
            message = str(e)
            lowered = message.lower()
            if "getaddrinfo failed" in lowered or "name or service not known" in lowered:
                print(f"[Notifier] Failed to send recovery notification (local DNS/network issue): {e}")
            return False
    
    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()


# Global notifier instance (initialized lazily)
_notifier: Optional[DiscordNotifier] = None


def get_notifier() -> DiscordNotifier:
    """Get or create the global notifier instance."""
    global _notifier
    if _notifier is None:
        cooldown = 300
        _notifier = DiscordNotifier(cooldown_seconds=cooldown)
    return _notifier


async def notify_error(
    error: str,
    selector_key: str,
    action: str,
    worker_id: int = 0,
    diagnostics: Optional[Dict] = None,
) -> bool:
    """Convenience function to send error notification."""
    return await get_notifier().send_error(error, selector_key, action, worker_id, diagnostics)
