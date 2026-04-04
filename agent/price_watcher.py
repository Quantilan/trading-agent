# agent/price_watcher.py
"""
PriceWatcher — waits for price to enter an entry zone, then fires the entry.

Primary:  ccxt.pro watch_ticker (WebSocket) — one shared stream per symbol.
Fallback: REST ticker polling every 3 s — used when ccxt.pro is unavailable.

Entry zone:
  - Single price   → [price * (1 - tol), price * (1 + tol)]
  - Range [lo, hi] → [lo * (1 - tol), hi * (1 + tol)]
  tolerance is loaded from config.entry_tolerance (default 0.1%)

Lifecycle:
  start()    → launches the background watch task
  register() → add a pending entry (replaces existing for same symbol)
  cancel()   → remove by symbol
  stop()     → graceful shutdown
  pending()  → list of all pending entries (for /pending command)
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Awaitable, Dict, List, Optional

from .state import Signal

logger = logging.getLogger(__name__)

_REST_POLL_INTERVAL = 3.0   # seconds — REST fallback poll rate
_WS_TIMEOUT         = 10.0  # seconds — watch_ticker call timeout


@dataclass
class PendingEntry:
    signal:     Signal
    entry_min:  float   # lower price bound (with tolerance applied)
    entry_max:  float   # upper price bound (with tolerance applied)
    raw_min:    float   # original signal entry_min (for display)
    raw_max:    float   # original signal entry_max (for display)
    expires_at: int     # unix timestamp


class PriceWatcher:

    def __init__(
        self,
        get_ticker:   Callable[[str], Awaitable[float]],
        on_triggered: Callable[[Signal], Awaitable[None]],
        get_pro_exchange: Callable[[], Optional[object]],  # returns ccxt.pro instance or None
        tolerance:    float = 0.001,   # fraction e.g. 0.001 = 0.1%
        timeout_hours: int  = 24,
    ):
        self._get_ticker      = get_ticker
        self._on_triggered    = on_triggered
        self._get_pro         = get_pro_exchange
        self._tolerance       = tolerance
        self._timeout_sec     = timeout_hours * 3600
        self._pending: Dict[str, PendingEntry] = {}
        self._running = False
        self._task:   Optional[asyncio.Task] = None

    # ── Public API ───────────────────────────────────────────────

    def register(self, signal: Signal) -> None:
        """Register a pending deferred entry."""
        raw_min = signal.entry_min
        raw_max = signal.entry_max if signal.entry_max > 0 else signal.entry_min

        if raw_min <= 0:
            logger.warning(f"[PriceWatcher] {signal.symbol}: entry_min not set — skipped")
            return

        tol         = self._tolerance
        zone_min    = raw_min * (1 - tol)
        zone_max    = raw_max * (1 + tol)
        expires_at  = int(time.time()) + self._timeout_sec

        if signal.symbol in self._pending:
            logger.info(f"[PriceWatcher] {signal.symbol}: replacing existing pending entry")

        self._pending[signal.symbol] = PendingEntry(
            signal     = signal,
            entry_min  = zone_min,
            entry_max  = zone_max,
            raw_min    = raw_min,
            raw_max    = raw_max,
            expires_at = expires_at,
        )

        if raw_min == raw_max:
            logger.info(
                f"[PriceWatcher] ⏳ {signal.symbol} {signal.action} "
                f"waiting for price @ {raw_min} (±{tol*100:.2f}%)"
            )
        else:
            logger.info(
                f"[PriceWatcher] ⏳ {signal.symbol} {signal.action} "
                f"waiting for price in [{raw_min} – {raw_max}] (±{tol*100:.2f}%)"
            )

    def cancel(self, symbol: str) -> None:
        self._pending.pop(symbol, None)

    def pending(self) -> List[PendingEntry]:
        """Return all pending entries (copy of current state)."""
        return list(self._pending.values())

    async def start(self) -> None:
        self._running = True
        pro = self._get_pro()
        if pro is not None and hasattr(pro, 'watch_ticker'):
            logger.info("[PriceWatcher] Started (WebSocket mode)")
            self._task = asyncio.create_task(self._ws_loop(), name="price_watcher_ws")
        else:
            logger.info("[PriceWatcher] Started (REST polling fallback)")
            self._task = asyncio.create_task(self._rest_loop(), name="price_watcher_rest")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    # ── WebSocket loop ───────────────────────────────────────────

    async def _ws_loop(self) -> None:
        """Watch all pending symbols via ccxt.pro watch_ticker, one task per symbol."""
        symbol_tasks: Dict[str, asyncio.Task] = {}

        while self._running:
            await asyncio.sleep(0.5)

            # Launch tasks for newly registered symbols
            for symbol in list(self._pending.keys()):
                if symbol not in symbol_tasks or symbol_tasks[symbol].done():
                    symbol_tasks[symbol] = asyncio.create_task(
                        self._watch_symbol_ws(symbol),
                        name=f"pw_ws_{symbol}"
                    )

            # Clean up tasks for cancelled/expired symbols
            for symbol in list(symbol_tasks.keys()):
                if symbol not in self._pending and symbol_tasks[symbol].done():
                    del symbol_tasks[symbol]

        # Cancel all on stop
        for t in symbol_tasks.values():
            t.cancel()

    async def _watch_symbol_ws(self, symbol: str) -> None:
        """Watch a single symbol via watch_ticker until triggered or expired/cancelled."""
        pro = self._get_pro()
        if pro is None:
            return

        entry = self._pending.get(symbol)
        if not entry:
            return

        ms = self._ms_symbol(symbol)

        while self._running and symbol in self._pending:
            entry = self._pending.get(symbol)
            if not entry:
                break

            # Timeout check
            if int(time.time()) >= entry.expires_at:
                logger.info(
                    f"[PriceWatcher] ⏰ {symbol}: entry timed out "
                    f"(zone {entry.raw_min}–{entry.raw_max}) — cancelled"
                )
                self._pending.pop(symbol, None)
                break

            try:
                ticker = await asyncio.wait_for(pro.watch_ticker(ms), timeout=_WS_TIMEOUT)
                price  = float(ticker.get('last') or ticker.get('close') or 0)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.warning(f"[PriceWatcher] WS ticker {symbol}: {e} — retrying")
                await asyncio.sleep(2.0)
                continue

            if price <= 0:
                continue

            if entry.entry_min <= price <= entry.entry_max:
                await self._fire(symbol, entry, price)
                break

    # ── REST fallback loop ───────────────────────────────────────

    async def _rest_loop(self) -> None:
        """Poll all pending symbols via REST ticker every _REST_POLL_INTERVAL seconds."""
        while self._running:
            await asyncio.sleep(_REST_POLL_INTERVAL)

            if not self._pending:
                continue

            now = int(time.time())
            for symbol in list(self._pending.keys()):
                entry = self._pending.get(symbol)
                if not entry:
                    continue

                if now >= entry.expires_at:
                    logger.info(
                        f"[PriceWatcher] ⏰ {symbol}: entry timed out — cancelled"
                    )
                    self._pending.pop(symbol, None)
                    continue

                try:
                    price = await self._get_ticker(symbol)
                except Exception as e:
                    logger.warning(f"[PriceWatcher] REST ticker {symbol}: {e}")
                    continue

                if price <= 0:
                    continue

                if entry.entry_min <= price <= entry.entry_max:
                    await self._fire(symbol, entry, price)

    # ── Helpers ──────────────────────────────────────────────────

    async def _fire(self, symbol: str, entry: PendingEntry, price: float) -> None:
        deviation = (price - (entry.raw_min + entry.raw_max) / 2) / ((entry.raw_min + entry.raw_max) / 2) * 100
        logger.info(
            f"[PriceWatcher] 🎯 {symbol}: price {price} hit zone "
            f"[{entry.raw_min}–{entry.raw_max}] (dev={deviation:+.2f}%) — firing entry"
        )
        self._pending.pop(symbol, None)
        entry.signal.entry      = price
        entry.signal.entry_type = "market"
        await self._on_triggered(entry.signal)

    def _ms_symbol(self, symbol: str) -> str:
        """Convert base symbol to ccxt market symbol via the executor's _ms method."""
        # We borrow the same logic used in order_executor._ms()
        # Injected via get_pro_exchange closure — the pro exchange has markets loaded
        pro = self._get_pro()
        if pro and hasattr(pro, 'markets') and pro.markets:
            for ms in [f"{symbol}/USDT:USDT", f"{symbol}/USDC:USDC", f"{symbol}/USDT"]:
                if ms in pro.markets:
                    return ms
        return f"{symbol}/USDT:USDT"
