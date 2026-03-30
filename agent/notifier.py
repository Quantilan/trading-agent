# agent/notifier.py
"""
Telegram notifications — open, close, stop modification, errors.

Events send a photo (candlestick chart) with caption when chart bytes are provided.
Falls back to plain text if chart is None.

Caption structure:
  dd/mm/yyyy HH:MM:SS UTC
  EXCHANGE, SYMBOL

  emoji  ACTION
  ...details...
  [posID / stopID in trade mode]
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .state import Position, PositionSide, PositionStatus

logger = logging.getLogger(__name__)

TELEGRAM_MSG_API   = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"

# Human-readable exchange names
_EXCHANGE_LABELS = {
    'binance':     'Binance USDⓈ-M',
    'bybit':       'Bybit',
    'hyperliquid': 'Hyperliquid',
    'okx':         'OKX Swap',
}


class Notifier:

    def __init__(self, token: str, chat_id: str, mode: str = "paper", exchange: str = ""):
        self.token    = token
        self.chat_id  = chat_id
        self.mode     = mode
        self.exchange = exchange
        self.enabled  = bool(token and chat_id)

    # ─────────────────────────────────────────────────────────────────
    # PUBLIC EVENT METHODS
    # ─────────────────────────────────────────────────────────────────

    async def on_open(self, pos: Position, chart: Optional[bytes] = None) -> None:
        if not self.enabled:
            return
        base   = self._base(pos.symbol)
        emoji  = "🟢" if pos.side == PositionSide.LONG else "🔴"
        paper  = " 📋 PAPER" if self.mode == "paper" else ""
        exlbl  = _EXCHANGE_LABELS.get(self.exchange, self.exchange.upper())

        sl_tp = f"SL: {pos.stop_price}"
        if pos.take_price > 0:
            sl_tp += f"  |  TP: {pos.take_price}"

        lines = [
            self._header(base),
            f"{emoji} <b>{pos.side.value} open{paper}</b>",
            f"isol x{pos.leverage} MARKET",
            f"Size: {pos.amount} {base}",
            f"price: {pos.entry_price}",
            sl_tp,
            f"mode: {self.mode}",
        ]

        if self.mode == "trade":
            lines.append("")
            lines.append(exlbl)
            if pos.position_id and 'paper' not in pos.position_id:
                lines.append(f" posID: {pos.position_id}")
            if pos.stop_id and 'paper' not in pos.stop_id:
                lines.append(f" stopID: {pos.stop_id}")

        await self._dispatch(chart, "\n".join(lines))

    async def on_close(self, pos: Position, chart: Optional[bytes] = None) -> None:
        if not self.enabled:
            return
        base  = self._base(pos.symbol)
        emoji = "✅" if pos.rpnl >= 0 else "❌"
        exlbl = _EXCHANGE_LABELS.get(self.exchange, self.exchange.upper())

        status_map = {
            PositionStatus.CLOSED_SL:  "closed SL",
            PositionStatus.CLOSED_TP:  "closed TP",
            PositionStatus.CLOSED_SIG: "closed",
            PositionStatus.CLOSED_ERR: "closed (error)",
        }
        reason = status_map.get(pos.status, "closed")

        lines = [
            self._header(base),
            f"{emoji} <b>{pos.side.value} {reason}</b>",
            f"rPnL: <b>{pos.rpnl:+.2f} ({pos.rpnl_pct:+.2f}%)</b>",
        ]

        if self.mode == "trade":
            lines.append("")
            lines.append(exlbl)

        await self._dispatch(chart, "\n".join(lines))

    async def on_modify_sl(self, pos: Position, chart: Optional[bytes] = None) -> None:
        """
        pos.stop_price must already contain the NEW stop value before calling.
        """
        if not self.enabled:
            return
        base  = self._base(pos.symbol)
        exlbl = _EXCHANGE_LABELS.get(self.exchange, self.exchange.upper())

        lines = [
            self._header(base),
            f"🟧 <b>{pos.side.value} modify SL</b>",
            f"SL: <b>{pos.stop_price}</b>",
        ]

        if self.mode == "trade":
            lines.append("")
            lines.append(exlbl)
            if pos.stop_id and 'paper' not in pos.stop_id:
                lines.append(f" new stopID: {pos.stop_id}")

        await self._dispatch(chart, "\n".join(lines))

    async def on_error(self, message: str) -> None:
        if not self.enabled:
            return
        await self._send_text(f"⛔️ <b>AGENT ERROR</b>\n{message}")

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
        await self._send_text(text)

    async def on_info(self, message: str) -> None:
        if not self.enabled:
            return
        await self._send_text(f"ℹ️ {message}")

    async def send(self, text: str) -> None:
        """Public method — send arbitrary text."""
        await self._send_text(text)

    # ─────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────────────────────────────

    def _base(self, symbol: str) -> str:
        """DOGEUSDT → DOGE,  ETHUSDC → ETH"""
        for stbc in ('USDT', 'USDC', 'BUSD'):
            if symbol.upper().endswith(stbc):
                return symbol[:-len(stbc)]
        return symbol

    def _header(self, base: str) -> str:
        """First two lines of caption: timestamp + exchange/symbol."""
        exlbl = _EXCHANGE_LABELS.get(self.exchange, self.exchange.upper())
        now   = datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M:%S UTC')
        return f"{now}\n{exlbl}, {base}\n"

    async def _dispatch(self, chart: Optional[bytes], caption: str) -> None:
        """Send photo if chart available, else fall back to text."""
        if chart:
            await self._send_photo(chart, caption)
        else:
            await self._send_text(caption)

    async def _send_photo(self, image_bytes: bytes, caption: str) -> None:
        if not self.enabled:
            return
        try:
            url  = TELEGRAM_PHOTO_API.format(token=self.token)
            data = aiohttp.FormData()
            data.add_field('chat_id',    str(self.chat_id))
            data.add_field('caption',    caption)
            data.add_field('parse_mode', 'HTML')
            data.add_field('photo', image_bytes,
                           filename='chart.png', content_type='image/png')

            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(url, data=data,
                                        timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[Notifier] sendPhoto error {resp.status}: {body[:120]}")
        except Exception as e:
            logger.error(f"[Notifier] _send_photo error: {e}")

    async def _send_text(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            url     = TELEGRAM_MSG_API.format(token=self.token)
            payload = {'chat_id': self.chat_id, 'text': text, 'parse_mode': 'HTML'}
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(url, json=payload,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[Notifier] sendMessage error {resp.status}: {body[:100]}")
        except Exception as e:
            logger.error(f"[Notifier] _send_text error: {e}")
