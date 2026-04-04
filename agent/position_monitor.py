# agent/position_monitor.py
"""
Position Monitor — tracks open positions for SL/TP detection.

Paper mode  — WebSocket candle monitoring via ccxt.pro:
  Phase 1: watch_trades from open_timestamp → synthetic first candle
           (tracks H/L only from entry, no pre-entry noise)
  Phase 2: watch_ohlcv('1m') → closed candles, check H/L vs SL/TP

Trade mode  — Periodic exchange sync via REST:
  - Poll every SYNC_INTERVAL seconds
  - Detect positions closed externally (SL/TP hit, or user via web/app)
  - Cancel orphan SL or TP orders
  - On startup: full sync to reconcile state with exchange

Graceful shutdown:
  - has_active()              → True if positions are being monitored
  - seconds_to_next_boundary() → seconds until next 1m candle close
  PersonalBot /stop waits this long before stopping so monitor_ts is
  always saved at a clean candle boundary.

State persistence:
  AgentState.monitor_ts: Dict[str, int]  symbol → last processed candle ts
  Saved on every closed candle; used for gap-fill on restart.
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Callable, Awaitable

from .state import StateManager, Position, PositionSide, PositionStatus

logger = logging.getLogger(__name__)

# Callback type: (symbol, reason, close_price) → None
ClosedCallback    = Callable[[str, str, float], Awaitable[None]]
# Callback type: (position,) → None  — called after trailing SL is moved
TrailingCallback  = Callable[['Position'], Awaitable[None]]

# Trailing stop constants
_TRAIL_TRIGGER  = 1.5    # upnl must be ≥ 1.5 × sl_pct to start trailing
_TRAIL_DISTANCE = 0.618  # new SL is placed at 0.618 × sl_pct from current price


class PositionMonitor:

    # Trade mode: sync with exchange every N seconds
    SYNC_INTERVAL = 5 * 60   # 5 minutes

    def __init__(
        self,
        executor,               # OrderExecutor
        state: StateManager,
        notifier,               # Notifier (for direct send if personal_bot not set)
        mode: str,              # "paper" | "trade"
        on_position_closed: ClosedCallback,
        trailing_stop: bool = False,
        on_trailing_stop: Optional[TrailingCallback] = None,
    ):
        self.executor           = executor
        self.state              = state
        self.notifier           = notifier
        self.mode               = mode
        self.on_position_closed = on_position_closed
        self.trailing_stop      = trailing_stop
        self.on_trailing_stop   = on_trailing_stop

        self._running    = False
        self._tasks:     Dict[str, asyncio.Task] = {}   # paper: symbol → task
        self._sync_task: Optional[asyncio.Task]  = None  # trade: sync loop

    # ─────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────

    async def start(self) -> None:
        self._running = True

        if self.mode == "trade":
            # Sync immediately on startup to catch any closes during downtime
            await self.sync_with_exchange()
            self._sync_task = asyncio.create_task(
                self._sync_loop(), name="exchange_sync"
            )
            logger.info("📡 PositionMonitor started (trade — exchange sync every "
                        f"{self.SYNC_INTERVAL}s)")
        else:
            # Paper: restore monitoring for positions that survived a restart
            for pos in self.state.get_open_positions(mode="paper"):
                resume_ts = self.state.state.monitor_ts.get(pos.symbol, 0)
                self._start_paper_task(pos, resume_ts)
            logger.info(f"📡 PositionMonitor started (paper — {len(self._tasks)} positions)")

    async def stop(self) -> None:
        self._running = False

        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)

        logger.info("📡 PositionMonitor stopped")

    def add_position(self, position: Position) -> None:
        """Call this right after a new position is saved to state."""
        if self.mode == "paper":
            self._start_paper_task(position, resume_ts=0)
        # trade mode: next sync cycle picks it up automatically

    def remove_position(self, symbol: str) -> None:
        """Call when a position is manually closed (so we stop monitoring it)."""
        task = self._tasks.pop(symbol, None)
        if task and not task.done():
            task.cancel()
        self.state.state.monitor_ts.pop(symbol, None)

    def has_active(self) -> bool:
        """True if any position is currently being monitored."""
        active_tasks = any(not t.done() for t in self._tasks.values())
        active_sync  = self._sync_task is not None and not self._sync_task.done()
        return active_tasks or active_sync

    def active_symbols(self) -> list:
        return [s for s, t in self._tasks.items() if not t.done()]

    def seconds_to_next_boundary(self) -> int:
        """Seconds until the next 1-minute candle boundary."""
        now_ms      = int(time.time() * 1000)
        next_min_ms = (now_ms // 60_000 + 1) * 60_000
        return max(0, (next_min_ms - now_ms) // 1000)

    # ─────────────────────────────────────
    # PAPER MODE — WebSocket monitoring
    # ─────────────────────────────────────

    def _start_paper_task(self, position: Position, resume_ts: int) -> None:
        symbol = position.symbol
        if symbol in self._tasks and not self._tasks[symbol].done():
            return   # already running
        task = asyncio.create_task(
            self._monitor_symbol(position, resume_ts),
            name=f"monitor_{symbol}"
        )
        self._tasks[symbol] = task
        logger.debug(f"[{symbol}] Monitor task created (resume_ts={resume_ts})")

    async def _monitor_symbol(self, position: Position, resume_ts: int) -> None:
        symbol = position.symbol
        if getattr(position, 'last_price', 0) <= 0:
            position.last_price = position.entry_price
        
        logger.info(f"🔍 [{symbol}] Monitor started. Initial last_price: {position.last_price}")

        try:
            # ── Restart gap-fill ──────────────────────────────────────
            if resume_ts > 0:
                # Normal restart: gap-fill from last known closed candle
                hit = await self._gap_fill_check(position, since_ts=resume_ts)
                if hit:
                    return
            elif position.open_timestamp > 0:
                # Restarted with no closed candles recorded yet (Phase 1 was in progress)
                now_ms         = int(time.time() * 1000)
                first_boundary = _next_minute_ms(position.open_timestamp)

                if now_ms >= first_boundary:
                    # Agent was down long enough that at least one full candle closed.
                    # Gap-fill from the candle that contains the entry (boundary - 60s)
                    # so we catch any SL/TP hits while we were offline.
                    hit = await self._gap_fill_check(
                        position, since_ts=first_boundary - 60_000
                    )
                    if hit:
                        return
                else:
                    # Still within the first minute — just spot-check current price
                    price = await self.executor.get_ticker(symbol)
                    if price > 0:
                        reason = self._check_hit(position, price, price)
                        if reason:
                            fully_closed = await self._handle_hit(position, reason, price)
                            if fully_closed:
                                return

            # ── Phase 1: watch_trades → synthetic first candle ───────
            next_boundary = _next_minute_ms(position.open_timestamp)
            if int(time.time() * 1000) < next_boundary:
                hit = await self._phase1_trades(position, next_boundary)
                if hit:
                    return

            # ── Phase 2: watch_ohlcv → closed 1m candles ─────────────
            await self._phase2_ohlcv(position)

        except asyncio.CancelledError:
            logger.debug(f"[{symbol}] Monitor cancelled")
        except Exception as e:
            logger.error(f"[{symbol}] Monitor error: {e}", exc_info=True)
        finally:
            self._tasks.pop(symbol, None)

    # ── Phase 1 ──────────────────────────────────────────────────────

    async def _phase1_trades(self, position: Position, boundary_ms: int) -> bool:
        """
        Watch individual trades from entry until the first minute boundary.
        Builds a synthetic candle (H/L only, post-entry).
        Returns True if SL/TP was hit.
        """
        symbol     = position.symbol
        ccxt_sym   = self.executor._ms(symbol)
        high = low = 0.0

        logger.info(f"[{symbol}] Phase 1 — watching trades until "
                    f"{boundary_ms - int(time.time()*1000)}ms boundary")

        pro = self._pro_exchange()
        if pro is None:
            logger.warning(f"[{symbol}] ccxt.pro unavailable — skipping Phase 1")
            return False

        try:
            while self._running and int(time.time() * 1000) < boundary_ms:
                try:
                    trades = await asyncio.wait_for(
                        pro.watch_trades(ccxt_sym), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                for trade in trades:
                    price = float(trade.get('price') or 0)
                    if price <= 0:
                        continue
                    position.last_price = price
                    if high == 0.0:
                        high = low = price
                    else:
                        high = max(high, price)
                        low  = min(low,  price)

                    reason = self._check_hit(position, high, low)
                    if reason:
                        fully_closed = await self._handle_hit(position, reason, price)
                        if fully_closed:
                            return True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{symbol}] Phase 1 error: {e}")

        # Save phase-1 candle timestamp (the closed candle = boundary - 60s)
        closed_candle_ts = boundary_ms - 60_000
        self.state.state.monitor_ts[symbol] = closed_candle_ts
        self.state._save()
        logger.info(f"[{symbol}] Phase 1 complete. Synthetic H={high:.4f} L={low:.4f}")
        return False

    # ── Phase 2 ──────────────────────────────────────────────────────

    async def _phase2_ohlcv(self, position: Position) -> None:
        """
        Watch 1m closed candles and check SL/TP on each.
        Saves monitor_ts after every processed candle.
        """
        symbol   = position.symbol
        ccxt_sym = self.executor._ms(symbol)
        last_ts  = self.state.state.monitor_ts.get(symbol, 0)

        logger.info(f"[{symbol}] Phase 2 — watching ohlcv 1m (last_ts={last_ts})")

        pro = self._pro_exchange()
        if pro is None:
            logger.warning(f"[{symbol}] ccxt.pro unavailable — Phase 2 fallback to polling")
            await self._phase2_poll_fallback(position)
            return

        try:
            while self._running:
                candles = await pro.watch_ohlcv(ccxt_sym, '1m')
                now_ms  = int(time.time() * 1000)

                for ts, o, h, l, c, v in candles:
                    # Only process closed candles we haven't seen yet
                    if ts <= last_ts:
                        continue
                    if (ts + 60_000) > now_ms:
                        continue   # candle still open
                    position.last_price = c
                    last_ts = ts
                    self.state.state.monitor_ts[symbol] = ts
                    self.state._save()

                    reason = self._check_hit(position, h, l)
                    logger.debug(f"[{symbol}] Candle ts={ts} H={h} L={l} — {reason or 'ok'}")

                    if reason:
                        close_price = _hit_price(position, reason)
                        fully_closed = await self._handle_hit(position, reason, close_price)
                        if fully_closed:
                            return
                    else:
                        await self._check_trailing_stop(position, c)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{symbol}] Phase 2 error: {e}")

    async def _phase2_poll_fallback(self, position: Position) -> None:
        """
        Fallback for when ccxt.pro is unavailable:
        Poll REST ohlcv every 65 seconds (after candle close).
        """
        symbol   = position.symbol
        ccxt_sym = self.executor._ms(symbol)
        last_ts  = self.state.state.monitor_ts.get(symbol, 0)

        logger.info(f"[{symbol}] Phase 2 fallback — polling REST every 65s")

        while self._running:
            await asyncio.sleep(65)
            try:
                candles = await self.executor.exchange.fetch_ohlcv(
                    ccxt_sym, '1m', limit=3
                )
                now_ms = int(time.time() * 1000)
                for ts, o, h, l, c, v in candles:
                    if ts <= last_ts or (ts + 60_000) > now_ms:
                        continue
                    last_ts = ts
                    self.state.state.monitor_ts[symbol] = ts
                    self.state._save()

                    reason = self._check_hit(position, h, l)
                    if reason:
                        close_price = _hit_price(position, reason)
                        fully_closed = await self._handle_hit(position, reason, close_price)
                        if fully_closed:
                            return
                    else:
                        await self._check_trailing_stop(position, c)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{symbol}] Poll fallback error: {e}")

    # ── Gap fill on restart ───────────────────────────────────────────

    async def _gap_fill_check(self, position: Position, since_ts: int) -> bool:
        """
        Fetch all closed 1m candles since last saved ts and check SL/TP.
        Returns True if a hit is found.
        """
        symbol   = position.symbol
        ccxt_sym = self.executor._ms(symbol)
        logger.info(f"[{symbol}] Gap-fill from {since_ts}")

        try:
            candles = await self.executor.exchange.fetch_ohlcv(
                ccxt_sym, '1m', since=since_ts, limit=1500
            )
            now_ms = int(time.time() * 1000)

            for ts, o, h, l, c, v in candles:
                if ts <= since_ts or (ts + 60_000) > now_ms:
                    continue

                reason = self._check_hit(position, h, l)
                if reason:
                    close_price = _hit_price(position, reason)
                    logger.info(f"[{symbol}] Gap-fill HIT: {reason} @ candle {ts}")
                    fully_closed = await self._handle_hit(position, reason, close_price)
                    if fully_closed:
                        return True
                    # partial TP — update ts and continue checking remaining candles

                # Update last known ts even without a hit
                self.state.state.monitor_ts[symbol] = ts

            if candles:
                self.state._save()

            logger.info(f"[{symbol}] Gap-fill complete — no SL/TP hit")

        except Exception as e:
            logger.error(f"[{symbol}] Gap-fill error: {e}")

        return False

    # ─────────────────────────────────────
    # TRADE MODE — Exchange sync
    # ─────────────────────────────────────

    async def _sync_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.SYNC_INTERVAL)
            await self.sync_with_exchange()

    async def sync_with_exchange(self) -> None:
        """
        Compare our state with the exchange:
        - Position still open on exchange → update unrealized PnL
        - Position NOT on exchange → closed externally (SL/TP/manual)
          → detect close price + reason, cancel orphan orders, update state
        """
        logger.info("🔄 Syncing with exchange...")
        try:
            trade_positions = self.state.get_open_positions(mode="trade")
            if not trade_positions:
                return

            exchange_positions = await self.executor.fetch_open_positions()
            # Build lookup: base symbol → exchange position data
            ex_by_symbol: Dict[str, dict] = {}
            for ep in exchange_positions:
                raw_sym = ep.get('symbol', '')  # e.g. "BTC/USDT:USDT"
                base    = raw_sym.split('/')[0] if '/' in raw_sym else raw_sym
                ex_by_symbol[base] = ep

            for pos in trade_positions:
                if pos.symbol in ex_by_symbol:
                    # Still open — update unrealized PnL from exchange
                    ep   = ex_by_symbol[pos.symbol]
                    upnl = ep.get('unrealizedPnl')
                    if upnl is not None:
                        pos.unrealized_pnl = float(upnl)

                    # Trailing stop check (trade mode): estimate current price from mark or PnL
                    if self.trailing_stop:
                        mark = float(ep.get('markPrice') or
                                     ep.get('info', {}).get('markPrice') or 0)
                        if mark <= 0 and upnl is not None and pos.amount > 0:
                            # Fallback: price from unrealized PnL
                            # PnL = (mark - entry) × amount  (base currency contracts)
                            upnl_val = float(upnl)
                            mark = (pos.entry_price + upnl_val / pos.amount
                                    if pos.side == PositionSide.LONG
                                    else pos.entry_price - upnl_val / pos.amount)
                        if mark > 0:
                            await self._check_trailing_stop(pos, mark)

                    # Detect partial TP fill: amount on exchange has decreased
                    if pos.take_levels and pos.take_amounts:
                        raw = ep.get('contracts') or ep.get('amount') or ep.get('size')
                        if raw is not None:
                            ex_amount = abs(float(raw))
                            if ex_amount > 0 and ex_amount < pos.amount * 0.99:
                                await self._handle_partial_tp_trade(pos, ex_amount)
                else:
                    # Gone from exchange — closed externally
                    close_price, reason = await self._detect_close_reason(pos)
                    logger.info(f"[{pos.symbol}] Externally closed: {reason} @ {close_price:.4f}")
                    await self._handle_hit(pos, reason, close_price)

        except Exception as e:
            logger.error(f"sync_with_exchange error: {e}")

    async def _handle_partial_tp_trade(self, pos: Position, ex_amount: float) -> None:
        """
        A partial TP fill detected in trade mode: exchange amount < our tracked amount.

        Determines how many TP levels were filled by accumulating take_amounts from
        index 0 until the cumulative amount matches (pos.amount - ex_amount).
        Fires on_position_closed for each filled level in order.

        This correctly handles the gap case where multiple levels are filled between
        two sync cycles (e.g. price jumped from below TP1 to above TP2).
        """
        remaining_to_explain = round(pos.amount - ex_amount, 8)
        if remaining_to_explain <= 0:
            return

        accumulated = 0.0
        filled_indices = []
        for i, ta in enumerate(pos.take_amounts):
            if accumulated >= remaining_to_explain * 0.99:
                break
            filled_indices.append(i)
            accumulated = round(accumulated + ta, 8)

        if not filled_indices:
            logger.warning(
                f"[{pos.symbol}] Partial TP: could not match any level "
                f"(exchange: {ex_amount:.4f}, our: {pos.amount:.4f})"
            )
            return

        logger.info(
            f"[{pos.symbol}] Partial TP: {len(filled_indices)} level(s) filled "
            f"(exchange: {ex_amount:.4f}, closed: {remaining_to_explain:.4f})"
        )

        # Fire callback for each filled level (always index 0 since each callback
        # pops the front of the arrays).
        for _ in filled_indices:
            try:
                # After each callback, pos.take_levels[0] is the level that was hit
                tp_price = pos.take_levels[0] if pos.take_levels else pos.take_price
                await self.on_position_closed(pos.symbol, 'tp_0', tp_price)
            except Exception as e:
                logger.error(f"[{pos.symbol}] partial TP trade callback error: {e}")
                break
            # If position was fully closed (last level), stop
            if not self.state.has_position(pos.symbol):
                break

    async def _detect_close_reason(self, position: Position):
        """
        Inspect recent closed orders to find the fill that closed this position.
        Returns (close_price, reason) where reason is 'sl', 'tp', or 'manual'.
        """
        ccxt_sym = self.executor._ms(position.symbol)
        try:
            orders = await self.executor.exchange.fetch_closed_orders(ccxt_sym, limit=10)
            for order in reversed(orders):
                if order.get('status') != 'closed':
                    continue
                fill_price = float(order.get('average') or order.get('price') or 0)
                if fill_price <= 0:
                    continue
                otype = (order.get('type') or '').lower()
                if 'stop' in otype:
                    return fill_price, 'sl'
                if 'take_profit' in otype or ('limit' in otype and order.get('reduceOnly')):
                    return fill_price, 'tp'
        except Exception as e:
            logger.warning(f"[{position.symbol}] detect_close_reason: {e}")

        # Fallback: current price, unknown reason
        price = await self.executor.get_ticker(position.symbol)
        return price, 'manual'

    # ─────────────────────────────────────
    # HIT HANDLING
    # ─────────────────────────────────────

    async def _handle_hit(
        self, position: Position, reason: str, close_price: float
    ) -> bool:
        """
        Delegate to agent callback. Returns True if position was fully closed
        (monitoring should stop), False if partial TP (monitoring continues).
        """
        symbol = position.symbol
        if reason.startswith('tp_'):
            logger.info(f"🟡 [{symbol}] {reason} hit @ {close_price:.4f}")
        else:
            emoji = "🔴" if reason == "sl" else "🟢" if reason == "tp" else "⏹"
            logger.info(f"{emoji} [{symbol}] {reason.upper()} hit @ {close_price:.4f}")

        try:
            await self.on_position_closed(symbol, reason, close_price)
        except Exception as e:
            logger.error(f"[{symbol}] on_position_closed callback error: {e}")

        # Position still in state → partial TP, monitoring continues
        fully_closed = not self.state.has_position(symbol)
        if fully_closed:
            self._tasks.pop(symbol, None)
            self.state.state.monitor_ts.pop(symbol, None)
        return fully_closed

    # ─────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────

    async def _check_trailing_stop(self, position: Position, close_price: float) -> bool:
        """
        Trail the stop-loss on each closed candle.

        Algorithm:
          - Compute original SL distance as % of entry (from position.sl_pct, or
            derived from abs stop if sl_pct == 0).
          - Trigger when unrealized PnL % ≥ 1.5 × sl_pct.
          - New SL placed at 0.618 × sl_pct distance from current candle close.
          - Only move SL toward the position (never widen it).
          - Notifies user with chart on each adjustment.

        Returns True if SL was moved.
        """
        if not self.trailing_stop:
            return False

        # Determine original SL % from entry
        sl_pct = position.sl_pct
        if sl_pct <= 0 and position.stop_price > 0 and position.entry_price > 0:
            sl_pct = abs(position.entry_price - position.stop_price) / position.entry_price * 100
        if sl_pct <= 0:
            return False

        # Unrealized PnL % relative to entry price
        if position.side == PositionSide.LONG:
            upnl_pct = (close_price - position.entry_price) / position.entry_price * 100
        else:
            upnl_pct = (position.entry_price - close_price) / position.entry_price * 100

        if upnl_pct <= 0:
            return False   # Position underwater — don't trail

        # Determine trailing distance:
        #   Phase 1 (any profit):     trail at original sl_pct — follow price, protect gains
        #   Phase 2 (profit ≥ 1.5×): tighten to 0.618 × sl_pct — lock in more
        if upnl_pct >= _TRAIL_TRIGGER * sl_pct:
            trail_pct = _TRAIL_DISTANCE * sl_pct   # e.g. 2% → 1.236%
        else:
            trail_pct = sl_pct                      # regular distance

        price_prec, _, _, _ = self.executor.get_market_params(position.symbol)
        trail_dist = trail_pct / 100

        if position.side == PositionSide.LONG:
            new_sl = round(close_price * (1.0 - trail_dist), price_prec)
            if new_sl <= position.stop_price:
                return False   # No improvement
            if new_sl >= close_price:
                return False   # Sanity
        else:
            new_sl = round(close_price * (1.0 + trail_dist), price_prec)
            if new_sl >= position.stop_price:
                return False
            if new_sl <= close_price:
                return False

        logger.info(
            f"[{position.symbol}] 🔒 Trailing stop: {position.stop_price} → {new_sl} "
            f"(upnl {upnl_pct:.2f}%, dist {trail_pct:.3f}%"
            + (" [tightened]" if upnl_pct >= _TRAIL_TRIGGER * sl_pct else "") + ")"
        )

        # Update SL on exchange (trade) or in state only (paper)
        if self.mode == "trade":
            new_stop_id, err = await self.executor.modify_stop(
                position.symbol, position.stop_id,
                position.side, position.amount, new_sl
            )
            if err:
                logger.warning(f"[{position.symbol}] Trailing stop modify failed: {err}")
                return False
            position.stop_price = new_sl
            if new_stop_id:
                position.stop_id = new_stop_id
        else:
            position.stop_price = new_sl

        self.state._save()

        if self.on_trailing_stop:
            try:
                await self.on_trailing_stop(position)
            except Exception as e:
                logger.error(f"[{position.symbol}] trailing stop notify error: {e}")

        return True

    def _check_hit(
        self, position: Position, high: float, low: float
    ) -> Optional[str]:
        """
        Return 'sl', 'tp', 'tp_N' (ladder level N), or None.
        SL is checked first (more conservative).
        For ladder TP, levels are checked lowest-index first (closest to entry).
        """
        sl = position.stop_price
        if sl > 0:
            if position.side == PositionSide.LONG  and low  <= sl: return 'sl'
            if position.side == PositionSide.SHORT and high >= sl: return 'sl'

        # Ladder TP: check each level in order
        if position.take_levels and position.take_amounts:
            for i, tp_price in enumerate(position.take_levels):
                if position.side == PositionSide.LONG  and high >= tp_price: return f'tp_{i}'
                if position.side == PositionSide.SHORT and low  <= tp_price: return f'tp_{i}'
        elif position.take_price > 0:
            tp = position.take_price
            if position.side == PositionSide.LONG  and high >= tp: return 'tp'
            if position.side == PositionSide.SHORT and low  <= tp: return 'tp'

        return None

    def _pro_exchange(self):
        """
        Return a ccxt.pro exchange instance (for watch_* calls).
        The executor creates it lazily on first request.
        Returns None if ccxt.pro is not available.
        """
        return getattr(self.executor, 'pro_exchange', None)


# ─────────────────────────────────────────
# MODULE-LEVEL HELPERS
# ─────────────────────────────────────────

def _next_minute_ms(ts_ms: int) -> int:
    """Next closed 1m candle boundary after ts_ms."""
    return (ts_ms // 60_000 + 1) * 60_000


def _hit_price(position: Position, reason: str) -> float:
    """
    Best-estimate close price when a hit is detected from a candle range.
    Uses the exact SL/TP level stored on the position.
    """
    if reason == 'sl':
        return position.stop_price if position.stop_price > 0 else position.entry_price
    if reason == 'tp':
        return position.take_price if position.take_price > 0 else position.entry_price
    if reason.startswith('tp_'):
        try:
            idx = int(reason[3:])
            if idx < len(position.take_levels):
                return position.take_levels[idx]
        except (ValueError, IndexError):
            pass
        return position.take_price if position.take_price > 0 else position.entry_price
    return position.entry_price
