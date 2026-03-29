# agent/price_watcher.py
"""
PriceWatcher — monitors price via exchange REST polling.
When price enters the trigger zone (within tolerance %), fires the entry.

Used for trigger entries from Telegram signals:
  entry_type = "limit" | "stop"

Algorithm:
  - Poll exchange ticker every POLL_INTERVAL seconds for each watched symbol
  - Execute when: abs(current - target) / target <= tolerance
  - Cancel after entry_timeout_hours with no fill
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Dict

from .state import Signal

logger     = logging.getLogger(__name__)
POLL_INTERVAL = 3.0   # seconds between price checks per symbol


@dataclass
class _PendingEntry:
    signal:     Signal
    target:     float          # trigger price
    tolerance:  float          # fraction, e.g. 0.001
    expires_at: int            # unix timestamp


class PriceWatcher:

    def __init__(
        self,
        get_ticker:   Callable[[str], Awaitable[float]],
        on_triggered: Callable[[Signal], Awaitable[None]],
        tolerance:    float = 0.001,
        timeout_hours: int  = 24,
    ):
        self._get_ticker   = get_ticker
        self._on_triggered = on_triggered
        self._tolerance    = tolerance
        self._timeout_sec  = timeout_hours * 3600
        self._pending: Dict[str, _PendingEntry] = {}   # symbol → pending entry
        self._running = False

    def register(self, signal: Signal) -> None:
        """Register a pending trigger entry."""
        if signal.entry <= 0:
            logger.warning(f"[PriceWatcher] {signal.symbol}: entry_price not set — skipped")
            return

        existing = self._pending.get(signal.symbol)
        if existing:
            logger.info(f"[PriceWatcher] {signal.symbol}: replacing existing pending entry")

        self._pending[signal.symbol] = _PendingEntry(
            signal     = signal,
            target     = signal.entry,
            tolerance  = self._tolerance,
            expires_at = int(time.time()) + self._timeout_sec,
        )
        logger.info(
            f"[PriceWatcher] ⏳ {signal.symbol} {signal.action} "
            f"waiting for price @ {signal.entry} (±{self._tolerance*100:.1f}%)"
        )

    def cancel(self, symbol: str) -> None:
        self._pending.pop(symbol, None)

    async def start(self) -> None:
        """Background loop — polls prices for all pending entries."""
        self._running = True
        logger.info("[PriceWatcher] Started")

        while self._running:
            await asyncio.sleep(POLL_INTERVAL)

            if not self._pending:
                continue

            now     = int(time.time())
            symbols = list(self._pending.keys())

            for symbol in symbols:
                entry = self._pending.get(symbol)
                if not entry:
                    continue

                # Check timeout
                if now >= entry.expires_at:
                    logger.info(
                        f"[PriceWatcher] ⏰ {symbol}: entry timed out "
                        f"(target {entry.target}) — cancelled"
                    )
                    self._pending.pop(symbol, None)
                    continue

                # Check price
                try:
                    price = await self._get_ticker(symbol)
                except Exception as e:
                    logger.warning(f"[PriceWatcher] {symbol} ticker error: {e}")
                    continue

                if price <= 0:
                    continue

                deviation = abs(price - entry.target) / entry.target
                if deviation <= entry.tolerance:
                    logger.info(
                        f"[PriceWatcher] 🎯 {symbol}: price {price} hit target "
                        f"{entry.target} (dev={deviation*100:.3f}%) — firing entry"
                    )
                    self._pending.pop(symbol, None)

                    # Mark as market entry and fire
                    entry.signal.entry      = price
                    entry.signal.entry_type = "market"
                    await self._on_triggered(entry.signal)

    def stop(self) -> None:
        self._running = False
