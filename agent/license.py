# agent/license.py
"""
License checker with device fingerprint binding.

Flow:
  1. On first start — generate device fingerprint
  2. Send license_key + fingerprint to server
  3. Server binds fingerprint to license (first time)
     OR verifies it matches the registered device
  4. Agent is allowed to fetch daily_secret and connect to WSS
  5. Every 6 hours — re-validate to confirm subscription is active

Device fingerprint:
  SHA-256 of MAC + hostname + OS + arch.
  Stable across reboots. No personal data.
"""

import asyncio
import hashlib
import logging
import platform
import time
import uuid
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

LICENSE_SERVER = "https://license.quantilan.com"


class LicenseChecker:

    def __init__(self, license_key: str, check_interval: int = 21600,
                 license_server: str = ""):
        self.license_key    = license_key
        self.check_interval = check_interval
        self.is_valid       = False
        self.expires_at:    Optional[int] = None
        self.plan:          Optional[str] = None
        self._last_check:   int = 0
        self._fingerprint   = get_device_fingerprint()

        base = (license_server or LICENSE_SERVER).rstrip("/")
        self._server_url = f"{base}/v1/validate"

    # ─────────────────────────────────────
    # VALIDATION
    # ─────────────────────────────────────

    async def validate(self) -> bool:
        """Validate license + register device fingerprint."""
        try:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    self._server_url,
                    json={
                        "license_key":        self.license_key,
                        "device_fingerprint": self._fingerprint,
                        "agent_version":      _get_version(),
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()

                    if resp.status == 200 and data.get("valid"):
                        self.is_valid    = True
                        self.expires_at  = data.get("expires_at")
                        self.plan        = data.get("plan")
                        self._last_check = int(time.time())
                        logger.info("✅ [License] Valid")
                        return True

                    self.is_valid = False
                    reason = data.get("reason", "unknown")

                    if reason == "device_mismatch":
                        logger.error(
                            "🚫 [License] Key is bound to a different device. "
                            "Use /reset_device in @qntora_bot to transfer."
                        )
                    elif reason == "expired":
                        logger.error("🚫 [License] Subscription expired.")
                    else:
                        logger.error(f"🚫 [License] Rejected: {reason}")

                    return False

        except Exception as e:
            # Grace period — keep valid if server temporarily unreachable
            elapsed = int(time.time()) - self._last_check
            grace   = self.check_interval * 2

            if self.is_valid and elapsed < grace:
                logger.warning(
                    f"⚠️ [License] Server unreachable, grace period active "
                    f"({elapsed // 3600}h since last check)"
                )
                return True

            logger.error(f"❌ [License] Validation error: {e}")
            return False

    async def start_periodic_check(self) -> None:
        """Background re-validation every check_interval seconds."""
        while True:
            await asyncio.sleep(self.check_interval)
            logger.info("[License] Periodic check...")
            ok = await self.validate()
            if not ok:
                self.is_valid = False
                logger.error("🚫 [License] Periodic check failed — signals blocked")


# ─────────────────────────────────────────
# DEVICE FINGERPRINT
# ─────────────────────────────────────────

def get_device_fingerprint() -> str:
    """
    Stable unique device ID.
    SHA-256 of: MAC address + hostname + OS + architecture.
    Does NOT contain personal data.
    Stable across reboots on the same machine.
    """
    parts = [
        str(uuid.getnode()),    # MAC address as integer
        platform.node(),        # hostname
        platform.system(),      # Windows / Linux / Darwin
        platform.machine(),     # x86_64 / arm64
    ]
    raw = "|".join(p.lower().strip() for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _get_version() -> str:
    try:
        from agent.version import VERSION
        return VERSION
    except Exception:
        return "1.0.0"
