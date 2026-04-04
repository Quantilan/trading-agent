# agent/personal_bot.py
"""
Personal Telegram Bot — user's own bot for agent control.

The user creates their own bot via @BotFather and puts the token in .env.
This bot is completely independent from the Quantilan server.
It talks directly to the local TradingAgent instance.

Commands:
  /start     — agent status overview
  /positions — open positions with unrealized P&L
  /pnl       — cumulative P&L statistics
  /equity    — equity chart (image)
  /stop      — pause trading (keep positions open)
  /resume    — resume trading
  /close_all — close all positions on exchange (with confirmation)
  /help      — command list
"""

import asyncio
import logging
import time
import uuid
from typing import Optional, TYPE_CHECKING

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.enums.parse_mode import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import BotCommand, MenuButtonCommands

from .signal_parser import RegexParser
from .coins import load_coins_list
from .signal_parser.validator import (
    validate_open_sl, validate_open_tp,
    validate_modify_sl, validate_modify_tp,
)
from .state import Signal, PositionSide

if TYPE_CHECKING:
    from agent.main import TradingAgent

logger = logging.getLogger(__name__)


class PersonalBot:
    """
    Lightweight Telegram bot that talks directly to the local TradingAgent.
    One instance per user — their own bot token, their own chat_id.
    """

    def __init__(self, token: str, owner_chat_id: int, agent: "TradingAgent"):
        self.token         = token
        self.owner_chat_id = owner_chat_id
        self.agent         = agent

        # Inject ThreadedResolver to bypass broken aiodns on Windows.
        # AiohttpSession builds its own TCPConnector from _connector_init dict,
        # so we add 'resolver' there before the first request is made.
        _tg_session = AiohttpSession()
        _tg_session._connector_init['resolver'] = aiohttp.ThreadedResolver()
        self.bot = Bot(token=token, session=_tg_session)
        self.dp  = Dispatcher()

        self._regex_parser  = RegexParser(extra_symbols=load_coins_list())
        self._pending:    dict[str, Signal] = {}      # key → Signal awaiting confirmation
        self._pending_ts: dict[str, float]  = {}      # key → creation timestamp (TTL cleanup)
        self._cmd_last:   dict[str, float]  = {}      # cmd_name → last called timestamp

        self._register_handlers()

    # ─────────────────────────────────────
    # REGISTER
    # ─────────────────────────────────────

    def _register_handlers(self):
        self.dp.message.register(self.cmd_start,       Command("start"))
        self.dp.message.register(self.cmd_help,        Command("help"))
        self.dp.message.register(self.cmd_positions,   Command("positions"))
        self.dp.message.register(self.cmd_pnl,         Command("pnl"))
        self.dp.message.register(self.cmd_equity,      Command("equity"))
        self.dp.message.register(self.cmd_stop,        Command("stop"))
        self.dp.message.register(self.cmd_resume,      Command("resume"))
        self.dp.message.register(self.cmd_close_all,   Command("close_all"))
        self.dp.message.register(self.cmd_autoconfirm,   Command("autoconfirm"))
        self.dp.message.register(self.cmd_mode,          Command("mode"))
        self.dp.message.register(self.cmd_clear_history, Command("clear_history"))
        self.dp.message.register(self.cmd_pending,        Command("pending"))

        # Incoming text / forwarded messages → signal parser
        self.dp.message.register(
            self.on_text_message,
            F.text & ~F.text.startswith("/")
        )
        # Forwarded messages with photo/chart (caption contains signal text)
        self.dp.message.register(
            self.on_text_message,
            (F.photo | F.document) & F.caption
        )

        # Confirmation callbacks
        self.dp.callback_query.register(
            self.cb_mode_switch,    lambda c: c.data.startswith("mode_switch:")
        )
        self.dp.callback_query.register(
            self.cb_close_confirm,        lambda c: c.data == "close_confirm"
        )
        self.dp.callback_query.register(
            self.cb_clear_history_confirm, lambda c: c.data == "clear_history_confirm"
        )
        self.dp.callback_query.register(
            self.cb_cancel,         lambda c: c.data == "cancel"
        )
        self.dp.callback_query.register(
            self.cb_trade_confirm,  lambda c: c.data.startswith("trade_confirm:")
        )
        self.dp.callback_query.register(
            self.cb_trade_cancel,   lambda c: c.data.startswith("trade_cancel:")
        )

    async def _set_commands(self):
        """Set bot commands with emojis and descriptions"""

        commands = [
            BotCommand(command="start",       description="🚀 Agent status"),
            BotCommand(command="positions",   description="📊 Open positions"),
            BotCommand(command="pnl",         description="📈 P&L statistics"),
            BotCommand(command="equity",      description="📉 Equity chart"),
            BotCommand(command="stop",        description="⏸ Pause trading"),
            BotCommand(command="resume",      description="▶️ Resume trading"),
            BotCommand(command="close_all",   description="❌ Close all positions"),
            BotCommand(command="pending",        description="⏳ Pending (deferred) entries"),
            BotCommand(command="autoconfirm", description="⚡ Toggle auto-execute parsed signals"),
            BotCommand(command="mode",          description="🔄 Switch trading mode (paper / trade)"),
            BotCommand(command="clear_history", description="🗑 Clear trade history (monthly reset)"),
            BotCommand(command="help",          description="📖 Command list"),
        ]
        
        await self.bot.set_my_commands(commands)
        
        # Set menu button to open commands list
        await self.bot.set_chat_menu_button(
            chat_id=self.owner_chat_id,
            menu_button=MenuButtonCommands()
        )
        
        logger.info("✅ Bot commands and menu button configured")

    # ─────────────────────────────────────
    # SECURITY — owner only
    # ─────────────────────────────────────

    def _is_owner(self, message: types.Message) -> bool:
        if not message.from_user or message.from_user.is_bot:
            return False
        return message.from_user.id == self.owner_chat_id

    async def _check_owner(self, message: types.Message) -> bool:
        if not self._is_owner(message):
            await message.answer("⛔ Unauthorized.")
            return False
        return True

    # Minimum seconds between repeated calls to the same command
    _COOLDOWNS: dict = {
        "positions": 5.0,
        "pnl":       5.0,
        "equity":   10.0,
        "start":     3.0,
    }

    async def _check_cooldown(self, message: types.Message, cmd: str) -> bool:
        """
        Returns True (proceed) or False (rate-limited, reply sent).
        Silently allows commands not listed in _COOLDOWNS.
        """
        wait = self._COOLDOWNS.get(cmd, 0)
        if wait <= 0:
            return True
        now       = time.time()
        last      = self._cmd_last.get(cmd, 0)
        remaining = wait - (now - last)
        if remaining > 0:
            await message.answer(
                f"⏳ Please wait <b>{remaining:.0f}s</b> before using /{cmd} again.",
                parse_mode=ParseMode.HTML
            )
            return False
        self._cmd_last[cmd] = now
        return True

    def _cleanup_pending(self) -> None:
        """Remove pending confirmation signals older than 5 minutes."""
        cutoff  = time.time() - 300
        expired = [k for k, ts in self._pending_ts.items() if ts < cutoff]
        for k in expired:
            self._pending.pop(k, None)
            self._pending_ts.pop(k, None)

    # ─────────────────────────────────────
    # COMMANDS
    # ─────────────────────────────────────

    async def cmd_start(self, message: types.Message):
        if not await self._check_owner(message):
            return
        if not await self._check_cooldown(message, "start"):
            return

        ag = self.agent
        state = ag.state

        trading_status = "🟢 Active" if ag.running and not ag.paused else "🔴 Paused"
        balance        = state.balance_total
        positions      = state.get_open_positions(mode=ag.config.mode)
        total_pnl      = state.total_realized_pnl

        text = (
            f"🤖 <b>Quantilan Agent</b>\n\n"
            f"Status:   <b>{trading_status}</b>\n"
            f"Exchange: <b>{ag.config.exchange.upper()}</b>\n"
            f"Mode:     <b>{ag.config.mode.upper()}</b>\n\n"
            f"💰 Balance:   <b>${balance:.2f}</b>\n"
            f"📊 Positions: <b>{len(positions)}</b>\n"
            f"📈 Total P&L: <b>${total_pnl:+.2f}</b>\n\n"
            f"Use /help to see all commands."
        )
        await message.answer(text, parse_mode=ParseMode.HTML)

    async def cmd_help(self, message: types.Message):
        if not await self._check_owner(message):
            return
        ac_status  = "ON ⚡" if self.agent.state.state.auto_confirm else "OFF"
        mode       = self.agent.config.mode
        mode_badge = "📋 PAPER" if mode == "paper" else "💰 TRADE"
        await message.answer(
            f"📖 <b>Commands</b>  [{mode_badge}]\n\n"
            "/start         — agent status\n"
            "/positions     — open positions\n"
            "/pnl           — P&L statistics\n"
            "/equity        — equity chart\n"
            "/stop          — pause trading\n"
            "/resume        — resume trading\n"
            "/close_all     — close all positions\n"
            f"/autoconfirm   — toggle auto-execute [{ac_status}]\n"
            "/mode          — switch paper / trade\n"
            "/clear_history — reset monthly stats & equity\n\n"
            "💬 <b>Text commands</b>\n\n"
            "Just write or forward a message:\n"
            "  <code>открой эфир лонг</code>\n"
            "  <code>buy sol short sl 2% tp 5%</code>\n"
            "  <code>закрой биток</code>\n"
            "  <code>стоп на 1800</code>  — move SL\n"
            "  <code>тейк на 3500</code>  — move TP",
            parse_mode=ParseMode.HTML
        )

    async def cmd_autoconfirm(self, message: types.Message):
        if not await self._check_owner(message):
            return
        state = self.agent.state.state
        state.auto_confirm = not state.auto_confirm
        self.agent.state._save()

        if state.auto_confirm:
            await message.answer(
                "⚡ <b>Auto-confirm enabled</b>\n\n"
                "⚠️ Parsed signals will be executed <b>immediately</b> without confirmation.\n\n"
                "You trust the parser to correctly interpret forwarded messages and text commands.\n"
                "Use /autoconfirm again to disable.",
                parse_mode=ParseMode.HTML
            )
        else:
            await message.answer(
                "✅ <b>Auto-confirm disabled</b>\n\n"
                "Parsed signals will require confirmation before execution.",
                parse_mode=ParseMode.HTML
            )

    async def cmd_mode(self, message: types.Message):
        if not await self._check_owner(message):
            return
        current  = self.agent.config.mode
        target   = "trade" if current == "paper" else "paper"
        cur_emoji = "📋" if current == "paper" else "💰"
        tgt_emoji = "💰" if target  == "trade"  else "📋"

        open_pos = self.agent.state.get_open_positions(mode=current)
        pos_warn = ""
        if open_pos and target == "trade":
            pos_warn = f"\n\n⚠️ You have <b>{len(open_pos)} paper position(s)</b> open. They won't be visible in trade mode."

        builder = InlineKeyboardBuilder()
        builder.button(text=f"{tgt_emoji} Switch to {target.upper()}", callback_data=f"mode_switch:{target}")
        builder.button(text="✖ Keep current",                          callback_data="cancel")
        builder.adjust(1)

        await message.answer(
            f"🔄 <b>Trading Mode</b>\n\n"
            f"Current: {cur_emoji} <b>{current.upper()}</b>\n"
            f"Switch to: {tgt_emoji} <b>{target.upper()}</b>{pos_warn}\n\n"
            f"{'📋 Paper — simulated orders, no real funds' if target == 'paper' else '💰 Trade — real orders on the exchange'}",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )

    async def cb_mode_switch(self, callback: types.CallbackQuery):
        if callback.from_user.id != self.owner_chat_id:
            await callback.answer("Unauthorized")
            return
        target = callback.data.split(":")[1]  # "paper" or "trade"
        old    = self.agent.config.mode

        self.agent.config.mode               = target
        self.agent.executor.config.mode      = target
        self.agent.notifier.mode             = target
        self.agent.monitor.mode              = target
        self.agent.state.state.current_mode  = target
        self.agent.state._save()

        emoji = "📋" if target == "paper" else "💰"
        await callback.message.edit_text(
            f"{emoji} <b>Mode switched: {old.upper()} → {target.upper()}</b>\n\n"
            f"All new orders will be {'simulated (paper)' if target == 'paper' else 'executed on the exchange (real)'}.",
            parse_mode=ParseMode.HTML
        )
        await callback.answer()

    async def cmd_positions(self, message: types.Message):
        if not await self._check_owner(message):
            return
        if not await self._check_cooldown(message, "positions"):
            return

        mode      = self.agent.config.mode
        positions = self.agent.state.get_open_positions(mode=mode)

        # Fetch live prices and update unrealized_pnl on each position
        for p in positions:
            try:
                price = await self.agent.executor.get_ticker(p.symbol)
            except Exception:
                price = 0.0
            if price > 0:
                if p.side == PositionSide.LONG:
                    p.unrealized_pnl = round((price - p.entry_price) * p.amount, 2)
                else:
                    p.unrealized_pnl = round((p.entry_price - price) * p.amount, 2)

        # Start balance: first equity snapshot, or estimate from current balance minus rpnl
        state       = self.agent.state
        history     = state.equity_history
        rpnl_total  = state.total_realized_pnl
        if len(history) >= 1:
            start_balance = history[0]
        elif rpnl_total != 0:
            start_balance = round(state.balance_total - rpnl_total, 2)
        else:
            start_balance = state.balance_total

        from .pnl_image import generate_pnl_image
        img_bytes = generate_pnl_image(
            exchange      = self.agent.config.exchange,
            mode          = mode,
            leverage      = self.agent.config.leverage,
            balance       = state.balance_total,
            start_balance = start_balance,
            equity_history= history,
            positions     = positions,
            stbc          = self.agent.config.stbc or "USDT",
        )

        mode_badge = "📋 PAPER" if mode == "paper" else "💰 LIVE"
        n = len(positions)
        caption = (
            f"📊 <b>Positions — {mode_badge}</b>\n"
            f"{n} open position{'s' if n != 1 else ''}"
            if n > 0 else
            f"📊 <b>Positions — {mode_badge}</b>\nNo open positions"
        )

        if img_bytes:
            from aiogram.types import BufferedInputFile
            await message.answer_photo(
                BufferedInputFile(img_bytes, filename="positions.png"),
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            # Fallback: plain text
            if not positions:
                await message.answer("📊 No open positions.")
                return
            lines = [f"📊 <b>Open Positions — {mode_badge}</b>\n"]
            total_upnl = 0.0
            for p in positions:
                upnl = p.unrealized_pnl
                total_upnl += upnl
                sl_str = f"${p.stop_price:.4f}" if p.stop_price > 0 else "—"
                tp_str = f"${p.take_price:.4f}" if p.take_price > 0 else "—"
                lines.append(
                    f"<b>{p.symbol}</b>  {p.side.value}\n"
                    f"  Entry: ${p.entry_price:.4f}  |  Size: {p.amount}\n"
                    f"  SL: {sl_str}  |  TP: {tp_str}\n"
                    f"  uPnL: <b>{upnl:+.2f}$</b>\n"
                )
            lines.append(f"─────────────────\nTotal uPnL: <b>{total_upnl:+.2f}$</b>")
            await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

        # Remind about pending deferred entries if any
        pending = self.agent.price_watcher.pending()
        if pending:
            syms = ", ".join(p.signal.symbol for p in pending)
            await message.answer(
                f"⏳ <b>{len(pending)} deferred entr{'y' if len(pending)==1 else 'ies'} waiting:</b> {syms}\n"
                f"Use /pending for details.",
                parse_mode=ParseMode.HTML,
            )

    async def cmd_pending(self, message: types.Message):
        if not await self._check_owner(message):
            return

        pending = self.agent.price_watcher.pending()
        if not pending:
            await message.answer("⏳ No pending deferred entries.")
            return

        tol = self.agent.config.entry_tolerance
        lines = [f"⏳ <b>Pending entries ({len(pending)})</b>\n"]
        for p in pending:
            raw_min = p.raw_min
            raw_max = p.raw_max
            zone_str = f"{raw_min}" if raw_min == raw_max else f"{raw_min} – {raw_max}"
            ttl_min = max(0, (p.expires_at - int(__import__('time').time())) // 60)
            lines.append(
                f"<b>{p.signal.symbol}</b>  {p.signal.action}\n"
                f"  Zone: <b>{zone_str}</b>  (±{tol}%)\n"
                f"  Expires in: {ttl_min} min\n"
            )

        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_pnl(self, message: types.Message):
        if not await self._check_owner(message):
            return
        if not await self._check_cooldown(message, "pnl"):
            return

        state = self.agent.state
        stats = state.get_pnl_stats(mode=self.agent.config.mode)

        win_rate = (
            stats["wins"] / stats["total"] * 100
            if stats["total"] > 0 else 0
        )

        mode_badge = " 📋 PAPER" if self.agent.config.mode == "paper" else " 💰 LIVE"
        text = (
            f"📈 <b>P&L Statistics{mode_badge}</b>\n\n"
            f"Total trades:  <b>{stats['total']}</b>\n"
            f"Wins / Losses: <b>{stats['wins']} / {stats['losses']}</b>\n"
            f"Win rate:      <b>{win_rate:.1f}%</b>\n\n"
            f"Realized P&L:  <b>${stats['realized']:+.2f}</b>\n"
            f"Unrealized:    <b>${stats['unrealized']:+.2f}</b>\n"
            f"Net P&L:       <b>${stats['realized'] + stats['unrealized']:+.2f}</b>\n\n"
            f"Best trade:    <b>${stats['best']:+.2f}</b>\n"
            f"Worst trade:   <b>${stats['worst']:+.2f}</b>"
        )
        await message.answer(text, parse_mode=ParseMode.HTML)

    async def cmd_equity(self, message: types.Message):
        if not await self._check_owner(message):
            return
        if not await self._check_cooldown(message, "equity"):
            return

        # Try to generate equity chart if matplotlib available
        chart_path = await self._generate_equity_chart()

        if chart_path:
            from aiogram.types import FSInputFile
            photo = FSInputFile(chart_path)
            stats = self.agent.state.get_pnl_stats()
            await message.answer_photo(
                photo,
                caption=(
                    f"📉 <b>Equity Curve</b>\n"
                    f"Net P&L: <b>${stats['realized'] + stats['unrealized']:+.2f}</b>"
                ),
                parse_mode=ParseMode.HTML
            )
        else:
            # Fallback: text equity
            history = self.agent.state.equity_history
            if not history:
                await message.answer("📉 No equity data yet.")
                return
            latest = history[-1]
            start  = history[0]
            change = latest - start
            sign   = "+" if change >= 0 else ""
            await message.answer(
                f"📉 <b>Equity</b>\n\n"
                f"Start:   <b>${start:.2f}</b>\n"
                f"Current: <b>${latest:.2f}</b>\n"
                f"Change:  <b>{sign}${change:.2f}</b>",
                parse_mode=ParseMode.HTML
            )

    async def cmd_stop(self, message: types.Message):
        if not await self._check_owner(message):
            return

        if self.agent.paused:
            await message.answer("⏸ Trading is already paused.")
            return

        self.agent.paused = True

        # Build response — add candle boundary hint if monitor is active
        monitor   = self.agent.monitor
        extra_msg = ""
        if monitor.has_active():
            symbols = monitor.active_symbols()
            secs    = monitor.seconds_to_next_boundary()
            syms_str = ", ".join(symbols) if symbols else "positions"
            extra_msg = (
                f"\n\n⏱ <b>Monitor active</b> for {syms_str}.\n"
                f"If restarting server — wait <b>{secs}s</b> "
                f"(next candle close) to ensure clean state."
            )

        await message.answer(
            f"🛑 <b>Trading paused.</b>\n\n"
            f"Open positions remain open with stop orders active.\n"
            f"Use /resume to restart.{extra_msg}",
            parse_mode=ParseMode.HTML
        )
        logger.info("[PersonalBot] Trading paused by user")

    async def cmd_resume(self, message: types.Message):
        if not await self._check_owner(message):
            return

        if not self.agent.paused:
            await message.answer("▶️ Trading is already active.")
            return

        self.agent.paused = False

        # Refresh daily secret on resume (server mode only)
        if self.agent.config.signal_source == "server":
            await self.agent.daily_secret._fetch()

        await message.answer(
            "▶️ <b>Trading resumed.</b>",
            parse_mode=ParseMode.HTML
        )
        logger.info("[PersonalBot] Trading resumed by user")

    async def cmd_close_all(self, message: types.Message):
        if not await self._check_owner(message):
            return

        positions = self.agent.state.get_open_positions()
        if not positions:
            await message.answer("📊 No open positions to close.")
            return

        builder = InlineKeyboardBuilder()
        builder.button(text="☢️ CLOSE ALL", callback_data="close_confirm")
        builder.button(text="❌ Cancel",    callback_data="cancel")
        builder.adjust(2)

        await message.answer(
            f"⚠️ <b>Close all {len(positions)} position(s)?</b>\n\n"
            f"This will place market orders to close everything.\n"
            f"Action is irreversible.",
            parse_mode=ParseMode.HTML,
            reply_markup=builder.as_markup()
        )

    async def cmd_clear_history(self, message: types.Message):
        if not await self._check_owner(message):
            return

        # Block if there are open positions — close them first
        positions = self.agent.state.get_open_positions()
        if positions:
            syms = ", ".join(p.symbol for p in positions)
            await message.answer(
                f"⚠️ <b>Cannot clear history</b>\n\n"
                f"You have <b>{len(positions)} open position(s)</b>: <code>{syms}</code>\n\n"
                f"Use /close_all first, then run /clear_history again.",
                parse_mode=ParseMode.HTML
            )
            return

        mode  = self.agent.config.mode
        stats = self.agent.state.get_pnl_stats(mode=mode)
        mode_badge = "📋 PAPER" if mode == "paper" else "💰 TRADE"

        builder = InlineKeyboardBuilder()
        builder.button(text="🗑 Yes, clear history", callback_data="clear_history_confirm")
        builder.button(text="❌ Cancel",             callback_data="cancel")
        builder.adjust(1)

        await message.answer(
            f"🗑 <b>Clear trade history?</b>\n\n"
            f"Mode: <b>{mode_badge}</b>\n"
            f"Trades: <b>{stats['total']}</b>  |  "
            f"Wins: <b>{stats['wins']}</b>  |  "
            f"Realized P&L: <b>${stats['realized']:+.2f}</b>\n\n"
            f"This resets: trade count, win rate, P&L stats, equity curve.\n"
            f"<b>This action cannot be undone.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=builder.as_markup()
        )

    # ─────────────────────────────────────
    # INCOMING TEXT / FORWARDED MESSAGES
    # ─────────────────────────────────────

    async def on_text_message(self, message: types.Message):
        """Handle plain text and forwarded messages — parse as trading signal."""
        if not self._is_owner(message):
            return

        # Evict stale pending confirmations (older than 5 minutes)
        self._cleanup_pending()

        text = message.text or message.caption or ""
        if not text:
            return

        signal = await self._parse_text(text)
        if not signal:
            return  # not a trading command — ignore silently

        # Resolve empty symbol for modify actions (use single open position)
        if not signal.symbol and signal.action in ("MODIFY_SL", "MODIFY_TP"):
            positions = self.agent.state.get_open_positions()
            if len(positions) == 1:
                signal.symbol = positions[0].symbol
            elif len(positions) > 1:
                symbols = ", ".join(p.symbol for p in positions)
                await message.answer(
                    f"❓ Which position?\nOpen: <code>{symbols}</code>\n\n"
                    f"Add symbol, e.g.: <code>стоп на 1800 eth</code>",
                    parse_mode=ParseMode.HTML
                )
                return
            else:
                await message.answer("📊 No open positions.")
                return

        # Early check: FLAT / MODIFY_* require an existing open position
        if signal.action in ("FLAT", "MODIFY_SL", "MODIFY_TP") and signal.symbol:
            if not self.agent.state.get_position(signal.symbol):
                await message.answer(
                    f"⚠️ No open position for <b>{signal.symbol}</b>",
                    parse_mode=ParseMode.HTML,
                )
                return

        # Get current price for validation (skip for FLAT — price not needed)
        current_price: Optional[float] = None
        if signal.symbol and signal.action in ("LONG", "SHORT", "MODIFY_SL", "MODIFY_TP"):
            try:
                logger.info(f"[PersonalBot] Fetching current price for {signal.symbol}")
                current_price = await asyncio.wait_for(
                    self.agent.executor.get_ticker(signal.symbol),
                    timeout=5.0,
                )
            except Exception:
                pass
            finally:
                logger.info(f"[PersonalBot] Current price for {signal.symbol}: {current_price}")
                
        # Validate
        error = self._validate_signal(signal, current_price)
        if error:
            await message.answer(
                f"⚠️ <b>Invalid signal</b>\n\n{error}",
                parse_mode=ParseMode.HTML
            )
            return

        conflict_info = self._check_conflict(signal)

        if self.agent.state.state.auto_confirm:
            await self._execute_signal(message, signal, conflict_info)
        else:
            await self._send_confirmation(message, signal, conflict_info, current_price)

    async def _parse_text(self, text: str) -> Optional[Signal]:
        mode = self.agent.config.parser_mode
        if mode == "llm" and self.agent.llm_parser:
            return await self.agent.llm_parser.parse(text)
        return self._regex_parser.parse(text, self.agent.config.default_sl_pct)

    def _validate_signal(self, signal: Signal, price: Optional[float]) -> str:
        action = signal.action

        if action in ("LONG", "SHORT") and price and price > 0:
            if signal.stop_price > 0:
                
                ok, err = validate_open_sl(action, price, signal.stop_price, leverage=self.agent.config.leverage)
                if not ok:
                    return err
            if signal.take_price > 0:
                ok, err = validate_open_tp(action, price, signal.take_price)
                if not ok:
                    return err

        elif action == "MODIFY_SL" and signal.new_sl > 0 and signal.symbol:
            pos = self.agent.state.get_position(signal.symbol)
            if pos:
                ok, err = validate_modify_sl(pos, signal.new_sl)
                if not ok:
                    return err

        elif action == "MODIFY_TP" and signal.new_tp > 0 and signal.symbol:
            pos = self.agent.state.get_position(signal.symbol)
            if pos:
                ok, err = validate_modify_tp(pos, signal.new_tp)
                if not ok:
                    return err

        return ""

    def _check_conflict(self, signal: Signal) -> str:
        if signal.action not in ("LONG", "SHORT") or not signal.symbol:
            return ""
        pos = self.agent.state.get_position(signal.symbol)
        if not pos:
            return ""
        pos_side = pos.side.value
        if pos_side != signal.action:
            upnl = pos.unrealized_pnl or 0.0
            sign = "+" if upnl >= 0 else ""
            return (
                f"⚠️ <b>Conflict:</b> {pos_side} {signal.symbol} is open\n"
                f"uPnL: <b>{sign}${upnl:.2f}</b> | Entry: ${pos.entry_price:.4f}\n"
                f"Executing will <b>close current {pos_side} first</b>."
            )
        return f"ℹ️ {pos_side} {signal.symbol} already open — will be skipped."

    def _format_signal_preview(self, signal: Signal, price: Optional[float]) -> str:
        action = signal.action
        symbol = signal.symbol or "(current position)"

        if action in ("LONG", "SHORT"):
            emoji = "🟢" if action == "LONG" else "🔴"
            header = f"{emoji} <b>{action} {symbol}</b> @ market"
            if price:
                header += f" (~${price:.4f})"
            parts = [header]

            if signal.sl_pct > 0:
                sl_str = f"SL: {signal.sl_pct * 100:.1f}%"
                if price:
                    sl_p = price * (1 - signal.sl_pct) if action == "LONG" else price * (1 + signal.sl_pct)
                    sl_str += f" (~${sl_p:.4f})"
                parts.append(sl_str)
            elif signal.stop_price > 0:
                parts.append(f"SL: ${signal.stop_price:.4f}")

            # --- TP Section (with Ladder & Proportions support) ---
            if signal.tp_pct > 0:
                # Handle percentage-based TP (e.g., "TP: 5%")
                tp_str = f"TP: {signal.tp_pct * 100:.1f}%"
                if price:
                    # Calculate absolute price based on entry price and direction
                    tp_p = price * (1 + signal.tp_pct) if action == "LONG" else price * (1 - signal.tp_pct)
                    tp_str += f" (~${tp_p:.4f})"
                parts.append(tp_str)
            
            elif signal.take_levels and len(signal.take_levels) > 0:
                # Handle multi-level TP ladder (e.g., TP1, TP2, TP3)
                tp_lines = ["TP Ladder:"]
                
                # Get proportions from signal or fallback to equal distribution
                # props example: [0.5, 0.3, 0.2] for 50%, 30%, 20%
                props = getattr(signal, 'take_proportions', [])
                if not props:
                    props = [1.0 / len(signal.take_levels)] * len(signal.take_levels)
                
                # Build a string for each level with its exit weight
                for i, (lvl, prop) in enumerate(zip(signal.take_levels, props), 1):
                    tp_lines.append(f"  TP{i}: ${lvl:.4f} ({prop*100:.0f}%)")
                
                parts.append("\n".join(tp_lines))

            elif signal.take_price > 0:
                # Fallback for single absolute price TP
                parts.append(f"TP: ${signal.take_price:.4f}")

            return "\n".join(parts)

        if action == "FLAT":
            return f"⏹ <b>CLOSE {symbol}</b> @ market"
        if action == "MODIFY_SL":
            return f"🔄 <b>Move SL</b> {symbol} → ${signal.new_sl:.4f}"
        if action == "MODIFY_TP":
            return f"🔄 <b>Move TP</b> {symbol} → ${signal.new_tp:.4f}"
        return f"{action} {symbol}"

    async def _send_confirmation(
        self,
        message: types.Message,
        signal: Signal,
        conflict: str,
        price: Optional[float],
    ) -> None:
        key = str(uuid.uuid4())[:8]
        self._pending[key]    = signal
        self._pending_ts[key] = time.time()

        text = f"📊 <b>Parsed Signal</b>\n\n{self._format_signal_preview(signal, price)}"
        if conflict:
            text += f"\n\n{conflict}"
        text += "\n\n<i>Execute this trade?</i>"

        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Execute", callback_data=f"trade_confirm:{key}")
        builder.button(text="❌ Cancel",  callback_data=f"trade_cancel:{key}")
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())

    async def _execute_signal(
        self,
        source: types.Message,
        signal: Signal,
        conflict: str,
    ) -> None:
        await self.agent.signal_queue.put(signal)
        text = f"⚡ <b>Executing</b>\n\n{self._format_signal_preview(signal, None)}"
        if conflict:
            text += f"\n\n{conflict}"
        await source.answer(text, parse_mode=ParseMode.HTML)

    # ─────────────────────────────────────
    # CALLBACKS
    # ─────────────────────────────────────

    async def cb_close_confirm(self, cb: types.CallbackQuery):
        if cb.from_user.id != self.owner_chat_id:
            await cb.answer("Unauthorized")
            return

        await cb.message.edit_text(
            "⏳ Closing all positions...",
            parse_mode=ParseMode.HTML
        )
        await cb.answer()

        try:
            closed = await self.agent.close_all_positions()
            await cb.message.answer(
                f"✅ <b>Closed {closed} position(s).</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            await cb.message.answer(
                f"❌ Error closing positions: {e}",
                parse_mode=ParseMode.HTML
            )

    async def cb_cancel(self, cb: types.CallbackQuery):
        await cb.message.edit_text("✅ Cancelled.")
        await cb.answer()

    async def cb_clear_history_confirm(self, cb: types.CallbackQuery):
        if cb.from_user.id != self.owner_chat_id:
            await cb.answer("Unauthorized")
            return

        # Double-check: no open positions (could have opened between /clear_history and confirm)
        positions = self.agent.state.get_open_positions()
        if positions:
            await cb.message.edit_text(
                f"⚠️ <b>Cancelled</b> — {len(positions)} position(s) opened since request.\n"
                f"Close them first.",
                parse_mode=ParseMode.HTML
            )
            await cb.answer()
            return

        mode  = self.agent.config.mode
        state = self.agent.state

        # Reset stats for current mode only — other mode data untouched
        state.state.mode_stats[mode] = {
            "total": 0, "wins": 0, "rpnl": 0.0, "best": 0.0, "worst": 0.0
        }
        state.state.equity_history = []
        state._save()

        mode_badge = "📋 PAPER" if mode == "paper" else "💰 TRADE"
        await cb.message.edit_text(
            f"🗑 <b>History cleared</b>  [{mode_badge}]\n\n"
            f"Trade stats and equity curve have been reset.\n"
            f"Starting fresh — equity will rebuild from the next closed trade.",
            parse_mode=ParseMode.HTML
        )
        await cb.answer("✅ Done")
        logger.info(f"[PersonalBot] Trade history cleared for mode={mode}")

    async def cb_trade_confirm(self, cb: types.CallbackQuery):
        if cb.from_user.id != self.owner_chat_id:
            await cb.answer("Unauthorized")
            return

        key    = cb.data.split(":", 1)[1]
        signal = self._pending.pop(key, None)
        self._pending_ts.pop(key, None)
        if not signal:
            await cb.message.edit_text("⏰ Signal expired or already used.")
            await cb.answer()
            return

        await self.agent.signal_queue.put(signal)
        await cb.message.edit_text(
            f"⚡ <b>Executing...</b>\n\n{self._format_signal_preview(signal, None)}",
            parse_mode=ParseMode.HTML
        )
        await cb.answer("✅ Submitted")

    async def cb_trade_cancel(self, cb: types.CallbackQuery):
        key = cb.data.split(":", 1)[1]
        self._pending.pop(key, None)
        self._pending_ts.pop(key, None)
        await cb.message.edit_text("❌ Cancelled.")
        await cb.answer()

    # ─────────────────────────────────────
    # OUTGOING NOTIFICATIONS
    # ─────────────────────────────────────

    async def notify(self, text: str) -> None:
        """Send notification to owner. Called by TradingAgent on events."""
        try:
            await asyncio.wait_for(
                self.bot.send_message(
                    self.owner_chat_id, text,
                    parse_mode=ParseMode.HTML
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[PersonalBot] notify timeout")
        except Exception as e:
            logger.error(f"[PersonalBot] notify error: {e}")

    async def notify_trade_opened(self, symbol: str, side: str,
                                   entry: float, sl: float,
                                   tp: float, amount: float) -> None:
        sign_sl = "↓" if side.upper() == "LONG" else "↑"
        sign_tp = "↑" if side.upper() == "LONG" else "↓"
        await self.notify(
            f"🟢 <b>Position Opened</b>\n\n"
            f"<b>{symbol}  {side.upper()}</b>\n"
            f"Entry: <b>${entry:.4f}</b>  |  Size: {amount}\n"
            f"{sign_sl} SL: ${sl:.4f}\n"
            f"{sign_tp} TP: ${tp:.4f}"
        )

    async def notify_trade_closed(self, symbol: str, side: str,
                                   pnl: float, reason: str) -> None:
        emoji = "✅" if pnl >= 0 else "🔴"
        sign  = "+" if pnl >= 0 else ""
        await self.notify(
            f"{emoji} <b>Position Closed</b>\n\n"
            f"<b>{symbol}  {side.upper()}</b>\n"
            f"P&L: <b>{sign}${pnl:.2f}</b>\n"
            f"Reason: {reason}"
        )

    async def notify_sl_moved(self, symbol: str, old_sl: float, new_sl: float) -> None:
        await self.notify(
            f"🔄 <b>Trailing Stop Updated</b>\n"
            f"{symbol}: ${old_sl:.4f} → <b>${new_sl:.4f}</b>"
        )

    async def notify_error(self, text: str) -> None:
        await self.notify(f"❌ <b>Error</b>\n{text}")

    # ─────────────────────────────────────
    # EQUITY CHART
    # ─────────────────────────────────────

    async def _generate_equity_chart(self) -> Optional[str]:
        """Generate equity curve PNG. Returns path or None if matplotlib missing."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import tempfile

            history = self.agent.state.equity_history
            if len(history) < 2:
                return None

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(history, color="#4CAF50", linewidth=1.5)
            ax.fill_between(range(len(history)), history,
                            min(history), alpha=0.1, color="#4CAF50")
            ax.set_title("Equity Curve", fontsize=14)
            ax.set_xlabel("Trades")
            ax.set_ylabel("Balance ($)")
            ax.grid(True, alpha=0.3)
            fig.tight_layout()

            tmp = tempfile.NamedTemporaryFile(
                suffix=".png", delete=False
            )
            fig.savefig(tmp.name, dpi=100)
            plt.close(fig)
            return tmp.name

        except ImportError:
            return None
        except Exception as e:
            logger.error(f"[PersonalBot] chart error: {e}")
            return None

    # ─────────────────────────────────────
    # RUN
    # ─────────────────────────────────────

    async def run(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self._set_commands()
        logger.info(f"🤖 [PersonalBot] Started for chat_id {self.owner_chat_id}")
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        await self.bot.session.close()
