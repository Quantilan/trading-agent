# agent/signal_client.py
"""
WebSocket client — connects to SignalsHub,
receives signals and puts them into execution queue.

Hub signal format (JSON):
{
    "type":       "signal",
    "action":     "OPEN_LONG | OPEN_SHORT | CLOSE | MODIFY_SL",
    "symbol":     "ETHUSDT",
    "strategy":   "rsi",
    "entry":      0.0,
    "stop_price": 3100.0,
    "take_price": 3400.0,
    "take_levels": [[price, amount], ...],
    "amount":     0.0,
    "entry_type": "market",
    "ts":         1741785600,
    "kid":        "20250331:0",
    "sig":        "<HMAC-SHA256>"
}
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .state import Signal

logger = logging.getLogger(__name__)

# Hub action names → internal action names
_ACTION_MAP = {
    "OPEN_LONG":  "LONG",
    "OPEN_SHORT": "SHORT",
    "CLOSE":      "FLAT",
    "MODIFY_SL":  "MODIFY_SL",
    "MODIFY_TP":  "MODIFY_TP",
}


class SignalClient:
    """
    Persistent WebSocket connection to SignalsHub.
    Reconnects automatically on disconnect.
    """

    def __init__(
        self,
        url:             str,
        license_key:     str,
        signal_queue:    asyncio.Queue,
        reconnect_delay: int = 5,
        version:         str = "",
    ):
        self.url             = url
        self.license_key     = license_key
        self.signal_queue    = signal_queue
        self.reconnect_delay = reconnect_delay
        self.version         = version

        self._running   = False
        self._connected = False
        self._ws        = None
        self._last_ping = 0

    # ─────────────────────────────────────
    # PUBLIC INTERFACE
    # ─────────────────────────────────────

    async def start(self) -> None:
        """Start client with automatic reconnect."""
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.error(f"[SignalClient] Connection error: {e}")
            finally:
                self._connected = False

            if self._running:
                logger.info(f"[SignalClient] Reconnecting in {self.reconnect_delay}s...")
                await asyncio.sleep(self.reconnect_delay)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ─────────────────────────────────────
    # CONNECTION
    # ─────────────────────────────────────

    async def _connect(self) -> None:
        logger.info(f"[SignalClient] Connecting to {self.url}...")

        async with websockets.connect(
            self.url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws

            # Authenticate
            await ws.send(json.dumps({
                "type":        "auth",
                "license_key": self.license_key,
                "version":     self.version,
            }))

            # Wait for auth confirmation
            auth_raw  = await asyncio.wait_for(ws.recv(), timeout=10)
            auth_data = json.loads(auth_raw)

            # Hub sends {"type": "auth_ok", "plan": "...", "expires_at": ...}
            # or        {"type": "auth_fail", "reason": "..."}
            if auth_data.get("type") != "auth_ok":
                reason = auth_data.get("reason", "unknown")
                logger.error(f"[SignalClient] Auth rejected: {reason}")
                raise ConnectionError(f"Auth failed: {reason}")

            plan = auth_data.get("plan", "")
            logger.info(f"✅ [SignalClient] Connected — plan={plan}")
            self._connected = True

            # Main receive loop
            async for raw in ws:
                await self._handle_message(raw)

    # ─────────────────────────────────────
    # MESSAGE HANDLING
    # ─────────────────────────────────────

    async def _handle_message(self, raw: str) -> None:
        try:
            data     = json.loads(raw)
            msg_type = data.get("type", "signal")

            if msg_type == "ping":
                self._last_ping = time.time()
                return

            if msg_type == "signal":
                signal = self._parse_signal(data)
                if signal:
                    await self.signal_queue.put(signal)
                    logger.info(
                        f"📨 [SignalClient] {signal.symbol} {signal.action} "
                        f"strategy={signal.strategy} kid={data.get('kid', '?')}"
                    )

            elif msg_type == "auth_fail":
                logger.error(f"[SignalClient] Server: auth_fail reason={data.get('reason')}")

            elif msg_type == "info":
                logger.info(f"[SignalClient] Server info: {data.get('message', '')}")

            else:
                logger.debug(f"[SignalClient] Unknown type: {msg_type}")

        except json.JSONDecodeError:
            logger.warning(f"[SignalClient] Invalid JSON: {raw[:100]}")
        except Exception as e:
            logger.error(f"[SignalClient] Message handling error: {e}")

    # ─────────────────────────────────────
    # SIGNAL PARSING
    # ─────────────────────────────────────

    def _parse_signal(self, data: dict) -> Optional[Signal]:
        try:
            # Map hub action names → internal action names
            raw_action = data.get("action", "FLAT").upper()
            action     = _ACTION_MAP.get(raw_action, raw_action)

            signal = Signal(
                id          = data.get("id", ""),
                symbol      = data.get("symbol", ""),
                action      = action,
                entry       = float(data.get("entry", 0)),
                stop_price  = float(data.get("stop_price", 0)),    # absolute SL
                take_price  = float(data.get("take_price", 0)),    # absolute TP
                take_levels = data.get("take_levels", []),          # TP ladder
                entry_type  = data.get("entry_type", "market"),
                sl_pct      = float(data.get("sl_pct", 0)),
                tp_pct      = float(data.get("tp_pct", 0)),
                new_sl      = float(data.get("new_sl", data.get("stop_price", 0))),
                reason      = data.get("reason", ""),
                strategy    = data.get("strategy", ""),
                timestamp   = int(data.get("ts", data.get("timestamp", 0))),
                expires     = int(data.get("expires", 0)),
            )
            signal._raw = data   # preserve full raw dict for HMAC verification
            return signal

        except Exception as e:
            logger.error(f"[SignalClient] Signal parse error: {e} | data: {data}")
            return None
