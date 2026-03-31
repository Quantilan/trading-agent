# agent/daily_secret.py
"""
Daily rotating secret manager.

Fetches the current signing secret from the License Server.
Rotation schedule: 00:05 UTC and 12:05 UTC.
Secret stored IN MEMORY ONLY — never written to disk.

Signal verification:
  Hub signs with: HMAC-SHA256(secret, canonical_json)
  where canonical_json = json.dumps(all_fields_except_sig, sort_keys=True, separators=(",",":"))

Race condition recovery:
  If a signal arrives with a kid that doesn't match the cached kid, the agent
  calls fetch_for_kid(kid) to get the correct secret for that slot, then re-verifies.
"""

import asyncio
import hashlib
import hmac as hmac_module
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class DailySecretManager:

    def __init__(self, license_server: str, license_key: str, fingerprint: str):
        self._server      = (license_server or "https://license.quantilan.com").rstrip("/")
        self._license_key = license_key
        self._fingerprint = fingerprint

        # In-memory only — never written to disk
        self._secret: Optional[str] = None
        self._kid:    Optional[str] = None   # "YYYYMMDD:0" or "YYYYMMDD:12"
        self._running = False

    # ─────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────

    async def start(self) -> bool:
        """Fetch on startup, then schedule rotation at 00:05 / 12:05 UTC."""
        ok = await self._fetch()
        if not ok:
            return False
        self._running = True
        asyncio.create_task(self._rotation_loop(), name="daily_secret_rotation")
        return True

    def stop(self) -> None:
        self._running = False
        self._secret  = None   # wipe from memory
        self._kid     = None

    @property
    def has_secret(self) -> bool:
        return bool(self._secret)

    @property
    def kid(self) -> Optional[str]:
        return self._kid

    def verify_signal(self, raw: dict) -> bool:
        """
        Verify HMAC-SHA256 signature of a hub signal.

        Hub computes:
          canonical = json.dumps({all fields except "sig"}, sort_keys=True, separators=(",",":"))
          sig = HMAC-SHA256(secret, canonical)
        """
        if not self._secret:
            logger.warning("[DailySecret] No secret in memory — cannot verify")
            return False

        sig = raw.get("sig", "")
        if not sig:
            logger.warning("[DailySecret] Signal missing signature — rejected")
            return False

        canonical = json.dumps(
            {k: v for k, v in raw.items() if k != "sig"},
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hmac_module.new(
            self._secret.encode(),
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()

        ok = hmac_module.compare_digest(expected, sig)
        if not ok:
            logger.warning(
                f"[DailySecret] Bad signature: "
                f"{raw.get('symbol')} {raw.get('action')} kid={raw.get('kid')} — rejected"
            )
        return ok

    async def fetch_for_kid(self, kid: str) -> bool:
        """
        Race condition recovery: fetch the hub signing secret for a specific kid.
        Called when signal.kid != self.kid (key rotated between hub refresh and agent refresh).
        """
        logger.info(f"[DailySecret] kid mismatch — re-fetching for kid={kid}")
        url = f"{self._server}/v1/get_daily_secret"
        try:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url,
                    json={
                        "license_key":        self._license_key,
                        "device_fingerprint": self._fingerprint,
                        "kid":                kid,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()

            if resp.status == 200 and data.get("ok"):
                self._secret = data["secret"]
                self._kid    = data.get("kid", kid)
                logger.info(f"[DailySecret] Re-fetched OK for kid={self._kid}")
                return True

            reason = data.get("reason", "unknown")
            logger.error(f"[DailySecret] fetch_for_kid failed: {reason}")
            return False

        except Exception as e:
            logger.error(f"[DailySecret] fetch_for_kid error: {e}")
            return False

    # ─────────────────────────────────────
    # FETCH
    # ─────────────────────────────────────

    async def _fetch(self) -> bool:
        """Request current secret from license server."""
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
                self._secret = data["secret"]
                self._kid    = data.get("kid")
                logger.info(f"✅ [DailySecret] Updated — kid={self._kid}")
                return True

            reason = data.get("reason", "unknown")
            logger.error(f"❌ [DailySecret] Fetch failed: {reason}")
            if reason in ("expired", "revoked", "device_mismatch"):
                self._secret = None
                self._kid    = None
            return False

        except Exception as e:
            # Network error — keep current secret if we have it
            if self._secret:
                logger.warning(f"⚠️ [DailySecret] Server unreachable, using cached secret: {e}")
                return True
            logger.error(f"❌ [DailySecret] Network error, no valid secret: {e}")
            return False

    # ─────────────────────────────────────
    # ROTATION LOOP
    # ─────────────────────────────────────

    async def _rotation_loop(self) -> None:
        """Wait until 00:05 or 12:05 UTC, then fetch fresh secret."""
        while self._running:
            next_time  = _next_fetch_time()
            sleep_secs = (next_time - datetime.now(timezone.utc)).total_seconds()
            logger.info(
                f"[DailySecret] Next rotation at "
                f"{next_time.strftime('%Y-%m-%d %H:%M UTC')} "
                f"(in {sleep_secs / 3600:.1f}h)"
            )
            await asyncio.sleep(max(sleep_secs, 1))
            if not self._running:
                break
            logger.info("[DailySecret] Rotating...")
            await self._fetch()


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def _next_fetch_time() -> datetime:
    """Return the next 00:05 or 12:05 UTC datetime."""
    now    = datetime.now(timezone.utc)
    today  = now.replace(hour=0,  minute=5, second=0, microsecond=0)
    midday = now.replace(hour=12, minute=5, second=0, microsecond=0)
    return min(
        today   + timedelta(days=1) if now >= today   else today,
        midday  + timedelta(days=1) if now >= midday  else midday,
    )
