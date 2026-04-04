# agent/main.py
"""
Trading Agent entry point.

Flow:
  signal_client -> signal_queue -> signal_processor
                                        |
                                risk_manager (sizing)
                                        |
                                order_executor (ccxt)
                                        |
                                state_manager (save)
                                        |
                                notifier (Telegram)

Security:
  DailySecretManager fetches rotating secret from license server.
  All incoming signals verified with HMAC before processing.
"""

import asyncio
import functools
import logging
import sys
import time
from typing import Optional

from .chart import draw_chart

from .config import load_config, AgentConfig
from .coins import load_coins_list, _COINS_FILE
from .state import (
    StateManager, Signal,
    Position, PositionSide, PositionStatus
)
from .signal_client import SignalClient
from .risk_manager import RiskManager
from .order_executor import OrderExecutor
from .notifier import Notifier
from .license import LicenseChecker, get_device_fingerprint
from .daily_secret import DailySecretManager
from .llm_parser import LLMParser
from .position_monitor import PositionMonitor
from .price_watcher import PriceWatcher


from .logger import setup_logging as _setup_logging

def setup_logging(level: str) -> None:
    _setup_logging(level=level, log_file="agent")


class TradingAgent:

    def __init__(self, config: AgentConfig):
        self.config           = config
        self.running          = False
        self.paused           = False          # set by PersonalBot /stop and /resume
        self.personal_bot     = None           # set by root main.py after creation
        self.signal_queue     = asyncio.Queue()
        self._fingerprint     = get_device_fingerprint()
        self._closing_symbols: set = set()     # guard against double-close race condition

        # Modules
        # State file is scoped by exchange + mode so paper/trade stats never mix.
        # E.g.: state_binance_paper.json, state_hyperliquid_trade.json
        self.state    = StateManager(
            max_positions = config.max_positions,
            state_file    = f"state_{config.exchange}_{config.mode}.json",
        )

        self.executor = OrderExecutor(config)
        self.risk     = RiskManager(config)
        self.notifier = Notifier(config.tg_token, config.tg_chat_id, config.mode, config.exchange)
        self.license  = LicenseChecker(
            config.license_key,
            config.license_check_interval,
            license_server = config.license_server,
        )
        self.daily_secret = DailySecretManager(
            license_server = config.license_server,
            license_key    = config.license_key,
            fingerprint    = self._fingerprint,
        )
        self.client = SignalClient(
            url             = config.signal_server,
            license_key     = config.license_key,
            signal_queue    = self.signal_queue,
            reconnect_delay = config.reconnect_delay,
        )

        # LLM parser — available when PARSER_MODE=llm in personal bot
        self.llm_parser = None
        if config.llm_provider != "none":
            self.llm_parser = LLMParser(
                provider = config.llm_provider,
                api_key  = config.llm_api_key,
                model    = config.llm_model,
            )

        # Position monitor — SL/TP detection (paper: WS candles, trade: exchange sync)
        self.monitor = PositionMonitor(
            executor           = self.executor,
            state              = self.state,
            notifier           = self.notifier,
            mode               = config.mode,
            on_position_closed = self._on_position_closed_by_monitor,
            trailing_stop      = config.trailing_stop,
            on_trailing_stop   = self._on_trailing_stop,
        )

        # Price watcher — deferred entries (WebSocket primary, REST fallback)
        self.price_watcher = PriceWatcher(
            get_ticker        = self.executor.get_ticker,
            on_triggered      = self._on_deferred_entry_triggered,
            get_pro_exchange  = lambda: getattr(self.executor, 'pro_exchange', None),
            tolerance         = config.entry_tolerance / 100,
            timeout_hours     = config.pending_entry_timeout,
        )

    # ─────────────────────────────────────
    # START
    # ─────────────────────────────────────

    async def start(self) -> None:
        self.running = True
        logger = logging.getLogger(__name__)
        logger.info("🚀 Trading Agent starting...")

        # 1. License + daily secret (server mode only)
        if self.config.signal_source == "server":
            valid = await self.license.validate()
            if not valid:
                logger.error("🚫 License invalid. Agent stopped.")
                raise RuntimeError("License validation failed")

            ok = await self.daily_secret.start()
            if not ok:
                logger.error("🚫 Could not fetch daily secret. Agent stopped.")
                raise RuntimeError("Could not fetch daily secret")
        else:
            logger.info("ℹ️  Telegram mode — license check skipped")
            self.license.is_valid = True

        # 2. Connect to exchange
        connected = await self.executor.connect()
        if not connected:
            logger.error("❌ Exchange connection failed. Agent stopped.")
            raise RuntimeError("Exchange connection failed")

        # 2b. Connect ccxt.pro for WebSocket monitoring (paper: SL/TP candles, both modes: price watcher)
        await self.executor.connect_pro()

        # 4. Get balance
        total, free, used = await self.executor.get_balance()
        self.state.update_balance(total, free, used)
        logger.info(f"💰 Balance: {total:.2f}$ (free: {free:.2f}$)")

        # 5. Notify start
        await self.notifier.on_start(
            self.config.exchange, self.config.mode, total,
            positions=self.state.get_open_positions(self.config.mode),
            stats=self.state.get_pnl_stats(self.config.mode),
        )

        # 6. Start background tasks
        await self.monitor.start()
        await self.price_watcher.start()

        tasks = [
            asyncio.create_task(self._process_signals(), name="signal_processor"),
            asyncio.create_task(self._pnl_updater(),     name="pnl_updater"),
        ]
        if self.config.signal_source == "server":
            tasks.append(asyncio.create_task(self.client.start(),                 name="signal_client"))
            tasks.append(asyncio.create_task(self.license.start_periodic_check(), name="license_checker"))

        logger.info("✅ Agent running, waiting for signals...")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        self.running = False
        await self.monitor.stop()
        if self.config.signal_source == "server":
            self.daily_secret.stop()
            await self.client.stop()
        await self.executor.disconnect()
        logging.getLogger(__name__).info("🛑 Agent stopped")

    async def close_all_positions(self) -> int:
        """Close all open positions. Called by PersonalBot /close_all."""
        logger    = logging.getLogger(__name__)
        positions = self.state.get_open_positions()
        closed    = 0
        for pos in positions:
            try:
                price = await self.executor.get_ticker(pos.symbol)
                ok, err = await self.executor.close_position(
                    pos.symbol, pos.amount, pos.side, price
                )
                if ok:
                    closed += 1
                    self.monitor.remove_position(pos.symbol)
                    self.state.close_position(pos.symbol, price, "manual_close")
                    logger.info(f"✅ Closed {pos.symbol} manually")
                else:
                    logger.error(f"❌ Failed to close {pos.symbol}: {err}")
            except Exception as e:
                logger.error(f"❌ close_all error {pos.symbol}: {e}")
        return closed

    def _has_pending_for(self, symbol: str) -> bool:
        """Return True if signal_queue has any pending signal for this symbol."""
        return any(s.symbol == symbol for s in self.signal_queue._queue)

    async def _notify_chart(self, event: str, pos: Position) -> None:
        """Wrapper that calls _notify_with_chart and logs any exception."""
        try:
            await self._notify_with_chart(event, pos)
        except Exception as e:
            logging.getLogger(__name__).error(f"[Notify] chart notification failed ({event}): {e!r}")

    async def _notify(self, text: str) -> None:
        """Route notification — personal_bot if available, else notifier."""
        if self.personal_bot:
            await self.personal_bot.notify(text)
        else:
            await self.notifier.send(text)

    async def _notify_with_chart(self, event: str, pos: Position) -> None:
        """
        Flow:
          1. Short text sent immediately so user sees confirmation right away.
          2. Log: starting OHLCV fetch.
          3. fetch_ohlcv (15 s timeout).
          4. Log: candles received, drawing.
          5. draw_chart() in thread pool — never blocks the event loop.
          6. Full notification WITH chart photo sent (text fallback if build failed).

        event: 'open' | 'close' | 'modify_sl'
        For modify_sl: pos.stop_price must already contain the NEW stop.
        """
        logger = logging.getLogger(__name__)

        base = pos.symbol.upper()
        for s in ('USDT', 'USDC', 'BUSD'):
            if base.endswith(s):
                base = base[:-len(s)]
                break

        # ── Step 1: short immediate confirmation ─────────────────────────────
        if event == 'open':
            side_emoji = "🟢" if pos.side == PositionSide.LONG else "🔴"
            brief = f"{side_emoji} <b>{pos.side.value} {base}</b> @ {pos.entry_price} — 📊 fetching chart..."
        elif event == 'close':
            result_emoji = "✅" if pos.rpnl >= 0 else "❌"
            brief = f"{result_emoji} <b>{base} closed</b> | rPnL: {pos.rpnl:+.2f}$ — 📊 fetching chart..."
        else:  # modify_sl
            brief = f"🟧 <b>{base}</b> SL → {pos.stop_price} — 📊 fetching chart..."
        try:
            await self._notify(brief)
        except Exception as e:
            logger.warning(f"[Notify] brief send failed ({event} {base}): {e}")

        # ── Step 2–5: fetch + render ──────────────────────────────────────────
        # Skip chart if there's another signal pending for the same symbol —
        # avoids double candle fetch on CLOSE+OPEN sequences.
        if self._has_pending_for(pos.symbol):
            logger.info(f"[Chart] {base} {event} — pending signal queued, skipping chart")
            return

        chart: Optional[bytes] = None
        try:
            logger.info(f"[Chart] {base} {event} — fetching {self.config.chart_bars}×{self.config.chart_tf} candles")
            ohlcv = await self.executor.fetch_ohlcv(
                pos.symbol, self.config.chart_tf, self.config.chart_bars
            )
            if ohlcv:
                logger.info(f"[Chart] {base} {event} — {len(ohlcv)} candles received, drawing...")
                chart = await asyncio.get_event_loop().run_in_executor(
                    None,
                    functools.partial(
                        draw_chart, ohlcv, pos, event,
                        self.config.exchange, self.config.chart_tf, base,
                    ),
                )
            else:
                logger.warning(f"[Chart] {base} {event} — no candles returned, sending text only")
        except Exception as e:
            logger.warning(f"[Chart] {pos.symbol} {event}: {e}")

        # ── Step 6: full notification (photo+caption or text fallback) ────────
        try:
            if event == 'open':
                await self.notifier.on_open(pos, chart)
            elif event == 'close':
                await self.notifier.on_close(pos, chart)
            else:  # modify_sl
                await self.notifier.on_modify_sl(pos, chart)
        except Exception as e:
            logger.warning(f"[Notify] full notify failed ({event} {base}): {e}")

    # ─────────────────────────────────────
    # SIGNAL PROCESSING
    # ─────────────────────────────────────

    async def _process_signals(self) -> None:
        logger = logging.getLogger(__name__)

        while self.running:
            try:
                signal: Signal = await asyncio.wait_for(
                    self.signal_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            # Paused — skip new entries
            if self.paused and signal.action.upper() in ("LONG", "SHORT"):
                logger.info(f"⏸ Paused — {signal.symbol} skipped")
                continue

            # License check
            if not self.license.is_valid:
                logger.warning("🚫 License invalid — signal skipped")
                continue

            # Signal signature verification (server mode only)
            if self.config.signal_source == "server":
                raw = getattr(signal, '_raw', None)
                if raw and raw.get("sig"):
                    # Race condition recovery: if hub rotated key between our last
                    # fetch and now, signal.kid won't match our cached kid.
                    # Re-fetch the correct key from server before verifying.
                    signal_kid = raw.get("kid", "")
                    if signal_kid and signal_kid != self.daily_secret.kid:
                        logger.info(
                            f"[Security] kid mismatch "
                            f"(signal={signal_kid} cached={self.daily_secret.kid}) "
                            f"— fetching correct key"
                        )
                        await self.daily_secret.fetch_for_kid(signal_kid)

                    if not self.daily_secret.verify_signal(raw):
                        logger.warning(
                            f"🚫 Bad signature: {signal.symbol} {signal.action} — skipped"
                        )
                        continue

            try:
                await self._handle_signal(signal)
            except Exception as e:
                logger.error(f"❌ Signal error {signal.symbol}: {e}")
                await self._notify(f"❌ Error on {signal.symbol}: {e}")

    async def _handle_signal(self, signal: Signal) -> None:
        """Route signal by action type."""
        logger = logging.getLogger(__name__)
        logger.info(f"📨 Processing: {signal.symbol} {signal.action} | {signal.reason}")

        action = signal.action.upper()

        if action in ("LONG", "SHORT"):
            await self._on_entry(signal)

        elif action == "FLAT":
            await self._on_exit(signal)

        elif action == "MODIFY_SL":
            await self._on_modify_sl(signal)

        elif action == "MODIFY_TP":
            await self._on_modify_tp(signal)

        else:
            logger.warning(f"Unknown action: {action}")

    # ─────────────────────────────────────
    # OPEN POSITION
    # ─────────────────────────────────────

    async def _on_entry(self, signal: Signal) -> None:
        logger = logging.getLogger(__name__)

        # Check if position already open
        existing = self.state.get_position(signal.symbol)
        if existing:
            logger.info(f"[{signal.symbol}] Position already open — skipping")
            return

        # Check position limit
        if self.state.max_positions_reached(self.state.open_positions_count()):
            logger.warning(f"[{signal.symbol}] Max positions reached ({self.config.max_positions})")
            return

        # Deferred entry — register in PriceWatcher and wait for price zone
        if signal.entry_type == "deferred" and signal.entry_min > 0:
            self.price_watcher.register(signal)
            raw_min = signal.entry_min
            raw_max = signal.entry_max if signal.entry_max > signal.entry_min else raw_min
            zone_str = f"{raw_min}" if raw_min == raw_max else f"{raw_min} – {raw_max}"
            await self._notify(
                f"⏳ <b>{signal.symbol} {signal.action}</b> — deferred entry\n"
                f"Waiting for price zone: <b>{zone_str}</b>\n"
                f"Tolerance: ±{self.config.entry_tolerance}%"
            )
            return

        # Get current balance
        total, free, used = await self.executor.get_balance()
        self.state.update_balance(total, free, used)

        # Check if coin is known; if not — validate on exchange and optionally add to coins.json
        base = signal.symbol.replace('USDT','').replace('USDC','').replace('/','').strip().upper()
        if base not in self.executor.coins:
            ms = self.executor._ms(signal.symbol)
            if ms not in self.executor._markets:
                logger.warning(f"[{signal.symbol}] Not found on {self.config.exchange.upper()} with {self.config.stbc} — skipping")
                await self._notify(f"⚠️ <b>{base}</b> not available on {self.config.exchange.upper()} with {self.config.stbc}")
                return
            # Coin exists on exchange but not in coins.json → add it
            try:
                import json as _json
                existing = load_coins_list()
                if base not in existing:
                    existing_sorted = sorted(set(existing + [base]))
                    _COINS_FILE.write_text(_json.dumps(existing_sorted, indent=2, ensure_ascii=False), encoding="utf-8")
                    logger.info(f"[Coins] Added {base} to coins.json")
            except Exception as e:
                logger.warning(f"[Coins] Could not update coins.json: {e}")
            # Re-init coins registry so this symbol gets its params
            self.executor._init_coins()

        # Market params (precision, min_notional, exchange max_amount)
        price_prec, amount_step, min_notional, max_amount = self.executor.get_market_params(signal.symbol)

        # Entry price: use signal.entry if set, otherwise fetch current market price
        entry_price = signal.entry
        if entry_price <= 0:
            entry_price = await self.executor.get_ticker(signal.symbol)
            if entry_price <= 0:
                logger.error(f"[{signal.symbol}] Cannot get ticker price — skipping")
                return

        # Calculate position size
        params, err = self.risk.calculate(
            signal, free, entry_price,
            price_prec, amount_step, min_notional, max_amount
        )
        if err:
            logger.error(f"[{signal.symbol}] RiskManager error: {err}")
            return

        # Set leverage
        await self.executor.set_leverage(signal.symbol, params.leverage)

        # Execute
        pos_id, stop_id, take_ids, err = await self.executor.open_position(params)
        if err and not pos_id:
            logger.error(f"[{signal.symbol}] Open error: {err}")
            await self.notifier.on_error(f"Open {signal.symbol}: {err}")
            return
        if err and pos_id:
            # Position opened but SL failed — executor already closed the position.
            # Notify user and abort (do not save a position without a stop).
            logger.critical(f"[{signal.symbol}] SL placement failed — position was auto-closed: {err}")
            await self.notifier.on_error(
                f"⚠️ {signal.symbol}: position opened but <b>SL failed</b> — position auto-closed.\n{err}"
            )
            return

        # Save position to state
        position = Position(
            symbol       = signal.symbol,
            side         = PositionSide(signal.action),
            entry_price  = entry_price,
            last_price   = entry_price,
            amount       = params.amount,
            volume       = params.volume,
            margin       = params.margin,
            leverage     = params.leverage,
            stop_price   = params.stop_price,
            take_price   = params.take_price,
            
            take_levels      = signal.take_levels,
            take_proportions = signal.take_proportions,
            take_amounts     = [a for _, a in params.take_levels] if params.take_levels else [],

            sl_pct       = params.sl_pct,
            tp_pct       = params.tp_pct,
            open_timestamp = int(__import__('time').time() * 1000),
            position_id  = pos_id or "",
            stop_id      = stop_id or "",
            take_id      = take_ids[0] if take_ids else "",
            take_ids     = take_ids,
            reason       = signal.reason,
            strategy     = signal.strategy,
            mode         = self.config.mode,
        )
        self.state.set_position(position)
        self.monitor.add_position(position)

        logger.info(f"✅ [{signal.symbol}] Position opened {signal.action} @ {entry_price}")
        await self._notify_chart('open', position)

        if err:
            # TP placement failed — position and SL are live, but no take profit
            logger.warning(f"[{signal.symbol}] TP placement error: {err}")
            await self.notifier.on_error(
                f"⚠️ {signal.symbol}: position opened, SL set, but <b>TP failed</b> — set manually.\n{err}"
            )

    # ─────────────────────────────────────
    # EXIT POSITION (by signal)
    # ─────────────────────────────────────

    async def _on_exit(self, signal: Signal) -> None:
        logger = logging.getLogger(__name__)

        # Guard: prevent double-close if monitor fires simultaneously
        if signal.symbol in self._closing_symbols:
            logger.debug(f"[{signal.symbol}] Already closing — skipping signal exit")
            return
        position = self.state.get_position(signal.symbol)
        if not position:
            return

        self._closing_symbols.add(signal.symbol)
        try:
            # Use signal entry price if provided, otherwise fetch current market price
            close_price = signal.entry if signal.entry > 0 else await self.executor.get_ticker(signal.symbol)

            ok, err = await self.executor.close_position(
                signal.symbol, position.amount,
                position.side, close_price
            )
            if not ok:
                await self.notifier.on_error(f"Close {signal.symbol}: {err}")
                return

            position.status           = PositionStatus.CLOSED_SIG
            position.close_price      = close_price
            position.close_timestamp  = int(time.time() * 1000)
            self._calc_rpnl(position)

            self.monitor.remove_position(signal.symbol)
            self.state.remove_position(signal.symbol)
            logger.info(f"⏹ [{signal.symbol}] Closed by signal | rPnL: {position.rpnl:+.2f}$")
            await self._notify_chart('close', position)
        finally:
            self._closing_symbols.discard(signal.symbol)

    # ─────────────────────────────────────
    # MODIFY STOP (by signal)
    # ─────────────────────────────────────

    async def _on_modify_sl(self, signal: Signal) -> None:
        logger = logging.getLogger(__name__)
        position = self.state.get_position(signal.symbol)
        if not position:
            return

        new_stop = signal.new_sl
        if new_stop <= 0:
            return

        price_prec, _, _, _ = self.executor.get_market_params(signal.symbol)
        new_stop = round(new_stop, price_prec)

        new_stop_id, err = await self.executor.modify_stop(
            signal.symbol, position.stop_id,
            position.side, position.amount, new_stop
        )
        if err:
            await self.notifier.on_error(f"ModifySL {signal.symbol}: {err}")
            return

        position.stop_price = new_stop
        if new_stop_id:
            position.stop_id = new_stop_id
        self.state.set_position(position)
        await self._notify_chart('modify_sl', position)

    # ─────────────────────────────────────
    # TRAILING STOP (by monitor)
    # ─────────────────────────────────────

    async def _on_trailing_stop(self, position: Position) -> None:
        """Called by PositionMonitor after trailing stop has moved SL. Notify + chart."""
        await self._notify_chart('modify_sl', position)

    # ─────────────────────────────────────
    # MODIFY TAKE PROFIT (by signal)
    # ─────────────────────────────────────

    async def _on_modify_tp(self, signal: Signal) -> None:
        logger = logging.getLogger(__name__)
        position = self.state.get_position(signal.symbol)
        if not position:
            return

        new_tp = signal.new_tp
        if new_tp <= 0:
            return

        price_prec, _, _, _ = self.executor.get_market_params(signal.symbol)
        new_tp = round(new_tp, price_prec)

        old_tp = position.take_price

        # If position has a TP ladder, cancel all existing levels first,
        # then place a single new TP for the full remaining amount.
        if position.take_ids:
            ms = self.executor._ms(signal.symbol)
            for tid in position.take_ids:
                if tid and 'paper' not in str(tid):
                    try:
                        await self.executor._cancel_take(ms, tid)
                    except Exception as e:
                        logger.warning(f"[{signal.symbol}] cancel ladder TP {tid}: {e}")
            position.take_levels.clear()
            position.take_amounts.clear()
            position.take_proportions.clear()
            position.take_ids.clear()
            position.take_id = ""

        new_take_id, err = await self.executor.modify_take(
            signal.symbol, position.take_id,
            position.side, position.amount, new_tp
        )
        if err:
            await self.notifier.on_error(f"ModifyTP {signal.symbol}: {err}")
            return

        position.take_price = new_tp
        if new_take_id:
            position.take_id = new_take_id
        self.state.set_position(position)
        logger.info(f"🔄 [{signal.symbol}] TP moved {old_tp:.4f} → {new_tp:.4f}")
        await self._notify(
            f"🔄 <b>Take Profit Updated</b>\n"
            f"{signal.symbol}: {old_tp:.4f} → <b>{new_tp:.4f}</b>"
        )

    # ─────────────────────────────────────
    # P&L CALCULATION
    # ─────────────────────────────────────

    def _calc_rpnl(self, position: Position) -> None:
        """Calculate realized P&L for closed position."""
        if position.side == PositionSide.LONG:
            rpnl_pct = (position.close_price - position.entry_price) / position.entry_price
        else:
            rpnl_pct = (position.entry_price - position.close_price) / position.entry_price

        position.rpnl     = round(rpnl_pct * position.volume, 2)
        position.rpnl_pct = round(rpnl_pct * 100, 2)

    # ─────────────────────────────────────
    # PRICE WATCHER CALLBACK
    # ─────────────────────────────────────

    async def _on_deferred_entry_triggered(self, signal: Signal) -> None:
        """Called by PriceWatcher when price enters the entry zone."""
        logger = logging.getLogger(__name__)
        logger.info(f"[PriceWatcher] 🎯 {signal.symbol} entry triggered @ {signal.entry}")
        await self._notify(
            f"🎯 <b>{signal.symbol} {signal.action}</b> — entry zone reached\n"
            f"Price: <b>{signal.entry}</b> — opening position"
        )
        await self._on_entry(signal)

    # ─────────────────────────────────────
    # MONITOR CALLBACK
    # ─────────────────────────────────────

    async def _on_position_closed_by_monitor(
        self, symbol: str, reason: str, close_price: float
    ) -> None:
        """
        Called by PositionMonitor when SL/TP is hit (paper) or position
        disappears from exchange (trade).

        Paper: just update state + notify.
        Trade: the position is already closed on the exchange; we only
               need to cancel any orphan SL/TP orders and sync state.
        """
        logger = logging.getLogger(__name__)

        position = self.state.get_position(symbol)
        if not position:
            return

        # ── Partial TP (ladder) ────────────────────────────────────────
        if reason.startswith('tp_'):
            await self._on_partial_tp(position, reason, close_price)
            return

        # ── Full close (sl / tp / manual) ─────────────────────────────
        # Guard: prevent double-close if signal exit fires simultaneously
        if symbol in self._closing_symbols:
            logger.debug(f"[{symbol}] Already closing — skipping monitor callback")
            return

        self._closing_symbols.add(symbol)
        position.close_price     = close_price
        position.close_timestamp = int(time.time() * 1000)

        if reason == 'sl':
            position.status = PositionStatus.CLOSED_SL
        elif reason == 'tp':
            position.status = PositionStatus.CLOSED_TP
        else:
            position.status = PositionStatus.CLOSED_SIG

        self._calc_rpnl(position)

        # In trade mode the exchange already executed the close; calling
        # close_position() here will silently fail on the market order
        # (position already gone) but will run _cancel_all_orders() to
        # remove any orphan SL or TP orders.
        # Use position.mode (stamped at open) not config.mode — they can diverge
        # if user switches mode while a position is open.
        if position.mode == "trade":
            await self.executor.close_position(
                symbol, position.amount, position.side, close_price
            )

        self.state.remove_position(symbol)
        self._closing_symbols.discard(symbol)
        emoji = "🔴" if reason == "sl" else "🟢" if reason == "tp" else "⏹"
        logger.info(
            f"{emoji} [{symbol}] {reason.upper()} hit @ {close_price:.4f} "
            f"| rPnL: {position.rpnl:+.2f}$"
        )
        await self._notify_chart('close', position)

    # ─────────────────────────────────────
    # PARTIAL TP (ladder)
    # ─────────────────────────────────────

    async def _on_partial_tp(
        self, position: Position, reason: str, close_price: float
    ) -> None:
        """
        Handle one TP ladder level being hit.
        Reduces position amount, removes the level from state, notifies.
        If the last level is hit, fully closes the position.
        """
        logger = logging.getLogger(__name__)
        symbol = position.symbol

        try:
            idx = int(reason[3:])
        except (ValueError, IndexError):
            logger.error(f"[{symbol}] Invalid partial TP reason: {reason}")
            return

        if idx >= len(position.take_levels):
            logger.error(f"[{symbol}] Partial TP index {idx} out of range "
                         f"(take_levels={position.take_levels})")
            return

        closed_amount = (
            position.take_amounts[idx]
            if idx < len(position.take_amounts)
            else 0.0
        )
        if closed_amount <= 0:
            logger.warning(f"[{symbol}] Partial TP_{idx}: zero amount, skipping")
            return

        total_levels  = len(position.take_levels)
        level_number  = idx + 1   # human-readable: TP1, TP2, ...

        # ── Calculate partial rPnL ────────────────────────────────────
        partial_volume = (
            position.volume * (closed_amount / position.amount)
            if position.amount > 0 else 0.0
        )
        if position.side == PositionSide.LONG:
            rpnl_pct = (close_price - position.entry_price) / position.entry_price
        else:
            rpnl_pct = (position.entry_price - close_price) / position.entry_price
        partial_rpnl     = round(rpnl_pct * partial_volume, 2)
        partial_rpnl_pct = round(rpnl_pct * 100, 2)

        # ── Update position state ─────────────────────────────────────
        position.take_levels.pop(0)
        if position.take_amounts:    position.take_amounts.pop(0)
        if position.take_ids:        position.take_ids.pop(0)
        if position.take_proportions: position.take_proportions.pop(0)

        position.amount = round(position.amount - closed_amount, 8)
        position.volume = round(position.volume - partial_volume, 2)
        position.margin = (
            round(position.volume / position.leverage, 2)
            if position.leverage > 0 else position.margin
        )
        position.rpnl = round(position.rpnl + partial_rpnl, 2)   # accumulate
        position.take_price = position.take_levels[0] if position.take_levels else 0.0

        level_label = f"TP{level_number}/{total_levels}"
        logger.info(
            f"🟡 [{symbol}] {level_label} hit @ {close_price} "
            f"| partial rPnL: {partial_rpnl:+.2f}$ ({partial_rpnl_pct:+.2f}%)"
        )

        # ── Last level → fully close ──────────────────────────────────
        if not position.take_levels:
            position.close_price     = close_price
            position.close_timestamp = int(time.time() * 1000)
            position.status          = PositionStatus.CLOSED_TP
            # rpnl already accumulated; set rpnl_pct relative to total original margin
            if position.mode == "trade":
                await self.executor.close_position(
                    symbol, position.amount, position.side, close_price
                )
            self.state.remove_position(symbol)
            logger.info(
                f"🟢 [{symbol}] Last TP level — position fully closed "
                f"| total rPnL: {position.rpnl:+.2f}$"
            )
            await self._notify_chart('close', position)
            return

        # ── Partial: keep position open ───────────────────────────────
        self.state.set_position(position)

        # Notify partial hit
        base = symbol.upper()
        for s in ('USDT', 'USDC', 'BUSD'):
            if base.endswith(s):
                base = base[:-len(s)]
                break
        emoji = "✅" if partial_rpnl >= 0 else "🟡"
        text = (
            f"{emoji} <b>{position.side.value} {base} — {level_label}</b>\n"
            f"@ {close_price}\n"
            f"rPnL: <b>{partial_rpnl:+.2f}$ ({partial_rpnl_pct:+.2f}%)</b>\n"
            f"Remaining: {position.amount:.4f} "
            f"({len(position.take_levels)} level{'s' if len(position.take_levels) != 1 else ''} left)"
        )
        await self._notify(text)

    # ─────────────────────────────────────
    # PNL UPDATER (background cache)
    # ─────────────────────────────────────

    async def _pnl_updater(self) -> None:
        """
        Update unrealized_pnl for all open positions every 30 seconds.
        Personal bot /positions reads this cached value instead of hitting
        the exchange on every request.
        Trade mode: exchange sync already updates uPnL — skip to avoid
        double REST calls.
        """
        logger = logging.getLogger(__name__)
        while self.running:
            await asyncio.sleep(30)
            if self.config.mode == "trade":
                continue   # handled by monitor sync_with_exchange
            positions = self.state.get_open_positions()
            for pos in positions:
                try:
                    price = await self.executor.get_ticker(pos.symbol)
                    if price <= 0:
                        continue
                    if pos.side == PositionSide.LONG:
                        pos.unrealized_pnl = round((price - pos.entry_price) / pos.entry_price * pos.volume, 2)
                    else:
                        pos.unrealized_pnl = round((pos.entry_price - price) / pos.entry_price * pos.volume, 2)
                except Exception as e:
                    logger.debug(f"pnl_updater {pos.symbol}: {e}")


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

def main():

    config = load_config()
    setup_logging(config.log_level)

    agent = TradingAgent(config)

    # Graceful shutdown on Ctrl+C
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, frame):
        logging.getLogger(__name__).info(f"Signal {sig} received, shutting down...")
        loop.call_soon_threadsafe(loop.stop)

    import signal as sig_module
    sig_module.signal(sig_module.SIGINT,  _shutdown)
    sig_module.signal(sig_module.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(agent.start())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
