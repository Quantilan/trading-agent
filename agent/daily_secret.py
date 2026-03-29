# agent/daily_secret.py
"""
Daily rotating secret manager.

Key rotation:
  - Server stores DAILY_SECRET in DB, rotates at 00:00 UTC
  - Agent fetches new secret at 00:05 UTC + on startup + at 12:05 UTC (backup)
  - Secret stored IN MEMORY ONLY during runtime
  - Without valid license -> cannot fetch -> signals rejected

Signal verification:
  HMAC-SHA256(DAILY_SECRET, signal_id:symbol:action:timestamp)
"""

import asyncio
import hashlib
import hmac as hmac_module
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class DailySecretManager:

    def __init__(self, server_url: str, license_key: str, fingerprint: str):
        # Normalize URL: wss:// -> https://
        self._server      = (server_url
                             .replace("wss://", "https://")
                             .replace("ws://",  "http://")
                             .rstrip("/"))
        self._license_key = license_key
        self._fingerprint = fingerprint

        # In-memory only — never written to disk
        self._secret:      Optional[str] = None
        self._secret_date: Optional[str] = None
        self._running      = False

    # ─────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────

    async def start(self) -> bool:
        """Fetch on startup, then schedule daily rotation."""
        ok = await self._fetch()
        if not ok:
            return False
        self._running = True
        asyncio.create_task(self._rotation_loop(), name="daily_secret_rotation")
        return True

    def stop(self) -> None:
        self._running  = False
        self._secret   = None   # wipe from memory
        self._secret_date = None

    def verify_signal(self, signal: dict) -> bool:
        """Verify HMAC-SHA256 signature. Must be called before processing."""
        if not self._secret:
            logger.warning("[DailySecret] No secret in memory — cannot verify")
            return False

        sig = signal.get("sig", "")
        if not sig:
            logger.warning("[DailySecret] Signal missing signature — rejected")
            return False

        payload = (
            f"{signal.get('id','')}:"
            f"{signal.get('symbol','')}:"
            f"{signal.get('action','')}:"
            f"{signal.get('timestamp','')}"
        )
        expected = hmac_module.new(
            self._secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

        ok = hmac_module.compare_digest(expected, sig)
        if not ok:
            logger.warning(
                f"[DailySecret] Bad signature: "
                f"{signal.get('symbol')} {signal.get('action')} — rejected"
            )
        return ok

    @property
    def has_secret(self) -> bool:
        return bool(self._secret)

    # ─────────────────────────────────────
    # FETCH
    # ─────────────────────────────────────

    async def _fetch(self) -> bool:
        """Request today's secret from license server."""
        url = f"{self._server}/v1/get_daily_secret"
        try:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url,
                    json={
                        "license_key":        self._license_key,
                        "device_fingerprint": self._fingerprint,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()

                    if resp.status == 200 and data.get("ok"):
                        self._secret      = data["secret"]   # memory only
                        self._secret_date = data.get("date", _today())
                        logger.info(f"✅ [DailySecret] Updated for {self._secret_date}")
                        return True

                    reason = data.get("reason", "unknown")
                    logger.error(f"❌ [DailySecret] Fetch failed: {reason}")
                    if reason in ("expired", "revoked", "device_mismatch"):
                        self._secret = None
                    return False

        except Exception as e:
            # Network error — keep today's secret if we have it
            if self._secret and self._secret_date == _today():
                logger.warning(f"⚠️ [DailySecret] Server unreachable, using cached secret: {e}")
                return True
            logger.error(f"❌ [DailySecret] Network error, no valid secret: {e}")
            self._secret = None
            return False

    # ─────────────────────────────────────
    # ROTATION LOOP
    # ─────────────────────────────────────

    async def _rotation_loop(self) -> None:
        """Wait until next fetch time (00:05 or 12:05 UTC), then rotate."""
        while self._running:
            next_time  = _next_fetch_time()
            sleep_secs = (next_time - datetime.now(timezone.utc)).total_seconds()
            logger.info(
                f"[DailySecret] Next rotation at "
                f"{next_time.strftime('%Y-%m-%d %H:%M UTC')} "
                f"(in {sleep_secs/3600:.1f}h)"
            )
            await asyncio.sleep(max(sleep_secs, 1))
            if not self._running:
                break
            logger.info("[DailySecret] Rotating...")
            await self._fetch()


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _next_fetch_time() -> datetime:
    """Next 00:05 or 12:05 UTC — two fetches per day for reliability."""
    now    = datetime.now(timezone.utc)
    today  = now.replace(hour=0,  minute=5, second=0, microsecond=0)
    midday = now.replace(hour=12, minute=5, second=0, microsecond=0)
    return min(
        today   + timedelta(days=1) if now >= today   else today,
        midday  + timedelta(days=1) if now >= midday  else midday,
    )
