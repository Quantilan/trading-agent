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
import logging
import sys
import time
from typing import Optional

from .config import load_config, AgentConfig
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
        self.state    = StateManager(max_positions=config.max_positions)

        # Restore mode persisted by /mode command (overrides .env)
        if self.state.state.current_mode != config.mode:
            config.mode = self.state.state.current_mode

        self.executor = OrderExecutor(config)
        self.risk     = RiskManager(config)
        self.notifier = Notifier(config.tg_token, config.tg_chat_id, config.mode)
        self.license  = LicenseChecker(
            config.license_key,
            config.license_check_interval,
            server_url = config.signal_server,
        )
        self.daily_secret = DailySecretManager(
            server_url   = config.signal_server,
            license_key  = config.license_key,
            fingerprint  = self._fingerprint,
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

        # 2b. Connect ccxt.pro for paper mode WebSocket monitoring
        if self.config.mode == "paper":
            await self.executor.connect_pro()

        # 4. Get balance
        total, free, used = await self.executor.get_balance()
        self.state.update_balance(total, free, used)
        logger.info(f"💰 Balance: {total:.2f}$ (free: {free:.2f}$)")

        # 5. Notify start
        await self.notifier.on_start(self.config.exchange, self.config.mode, total)

        # 6. Start background tasks
        await self.monitor.start()

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

    async def _notify(self, text: str) -> None:
        """Route notification — personal_bot if available, else notifier."""
        if self.personal_bot:
            await self.personal_bot.notify(text)
        else:
            await self.notifier.send(text)

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

        # Get current balance
        total, free, used = await self.executor.get_balance()
        self.state.update_balance(total, free, used)

        # Market params (precision, min_notional)
        price_prec, amount_step, min_notional = self.executor.get_market_params(signal.symbol)

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
            price_prec, amount_step, min_notional
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

        # Save position to state
        position = Position(
            symbol       = signal.symbol,
            side         = PositionSide(signal.action),
            entry_price  = entry_price,
            amount       = params.amount,
            volume       = params.volume,
            margin       = params.margin,
            leverage     = params.leverage,
            stop_price   = params.stop_price,
            take_price   = params.take_price,
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
        await self.notifier.on_open(position)

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
            await self.notifier.on_close(position)
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

        new_stop_id, err = await self.executor.modify_stop(
            signal.symbol, position.stop_id,
            position.side, position.amount, new_stop
        )
        if err:
            await self.notifier.on_error(f"ModifySL {signal.symbol}: {err}")
            return

        await self.notifier.on_modify_sl(position, new_stop)
        position.stop_price = new_stop
        if new_stop_id:
            position.stop_id = new_stop_id
        self.state.set_position(position)

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

        new_take_id, err = await self.executor.modify_take(
            signal.symbol, position.take_id,
            position.side, position.amount, new_tp
        )
        if err:
            await self.notifier.on_error(f"ModifyTP {signal.symbol}: {err}")
            return

        old_tp = position.take_price
        position.take_price = new_tp
        if new_take_id:
            position.take_id = new_take_id
        self.state.set_position(position)
        logger.info(f"🔄 [{signal.symbol}] TP moved ${old_tp:.4f} → ${new_tp:.4f}")
        await self._notify(
            f"🔄 <b>Take Profit Updated</b>\n"
            f"{signal.symbol}: ${old_tp:.4f} → <b>${new_tp:.4f}</b>"
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

        # Guard: prevent double-close if signal exit fires simultaneously
        if symbol in self._closing_symbols:
            logger.debug(f"[{symbol}] Already closing — skipping monitor callback")
            return
        position = self.state.get_position(symbol)
        if not position:
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
        await self.notifier.on_close(position)

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
