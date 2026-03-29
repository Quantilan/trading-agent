# agent/notifier.py
"""
Telegram notifications — open, close, stop modification, errors.
"""

import logging
from typing import Optional

import aiohttp

from .state import Position, PositionSide, PositionStatus

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:

    def __init__(self, token: str, chat_id: str, mode: str = "paper"):
        self.token   = token
        self.chat_id = chat_id
        self.mode    = mode
        self.enabled = bool(token and chat_id)

    # ─────────────────────────────────────
    # PUBLIC METHODS
    # ─────────────────────────────────────

    async def on_open(self, pos: Position) -> None:
        if not self.enabled:
            return
        emoji      = "🟢" if pos.side == PositionSide.LONG else "🔴"
        mode_badge = " 📋 PAPER" if self.mode == "paper" else ""
        text = (
            f"{emoji} <b>{pos.side.value} OPEN{mode_badge}</b>\n"
            f"{pos.symbol} | isol x{pos.leverage}\n\n"
            f"Entry:  <b>{pos.entry_price}</b>\n"
            f"SL:     {pos.stop_price}\n"
            f"TP:     {pos.take_price if pos.take_price > 0 else '—'}\n\n"
            f"Size:   {pos.amount} | {pos.volume}$\n"
            f"Margin: {pos.margin}$\n"
            f"Reason: <i>{pos.reason}</i>"
        )
        await self._send(text)

    async def on_close(self, pos: Position) -> None:
        if not self.enabled:
            return
        if pos.rpnl >= 0:
            emoji = "✅"
        else:
            emoji = "❌"

        status_map = {
            PositionStatus.CLOSED_SL:  "SL",
            PositionStatus.CLOSED_TP:  "TP",
            PositionStatus.CLOSED_SIG: "Signal",
            PositionStatus.CLOSED_ERR: "Error",
        }
        close_reason = status_map.get(pos.status, "Closed")

        text = (
            f"{emoji} <b>{pos.side.value} CLOSED {close_reason}</b>\n"
            f"{pos.symbol}\n\n"
            f"Entry:  {pos.entry_price}\n"
            f"Close:  <b>{pos.close_price}</b>\n\n"
            f"rPnL:   <b>{pos.rpnl:+.2f}$ ({pos.rpnl_pct:+.2f}%)</b>"
        )
        await self._send(text)

    async def on_modify_sl(self, pos: Position, new_stop: float) -> None:
        if not self.enabled:
            return
        text = (
            f"🟧 <b>{pos.side.value} MODIFY SL</b>\n"
            f"{pos.symbol}\n\n"
            f"SL: {pos.stop_price} → <b>{new_stop}</b>"
        )
        await self._send(text)

    async def on_error(self, message: str) -> None:
        if not self.enabled:
            return
        await self._send(f"⛔️ <b>AGENT ERROR</b>\n{message}")

    async def on_start(self, exchange: str, mode: str, balance: float) -> None:
        if not self.enabled:
            return
        mode_emoji = "📋" if mode == "paper" else "💰"
        text = (
            f"🚀 <b>Agent started</b>\n\n"
            f"Exchange: {exchange.upper()}\n"
            f"Mode:     {mode_emoji} {mode.upper()}\n"
            f"Balance:  {balance:.2f}$"
        )
        await self._send(text)

    async def on_info(self, message: str) -> None:
        if not self.enabled:
            return
        await self._send(f"ℹ️ {message}")

    # ─────────────────────────────────────
    # SEND
    # ─────────────────────────────────────

    async def send(self, text: str) -> None:
        """Public method — send arbitrary text."""
        await self._send(text)

    async def _send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            url = TELEGRAM_API.format(token=self.token)
            payload = {
                'chat_id':    self.chat_id,
                'text':       text,
                'parse_mode': 'HTML',
            }
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[Notifier] Telegram error {resp.status}: {body[:100]}")
        except Exception as e:
            logger.error(f"[Notifier] Send error: {e}")
