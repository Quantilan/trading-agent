# agent/llm_parser.py
"""
LLM-based signal parser for Telegram channel messages.

Supported providers:
  none   — disabled (no parsing)
  claude — Anthropic Claude (claude-haiku-4-5-20251001 by default)

The LLM receives raw message text and returns a structured Signal or None.
"""

import json
import logging
import time
import uuid
from typing import Optional

from .state import Signal

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = {
    "claude": "claude-haiku-4-5-20251001",
}

_SYSTEM_PROMPT = """\
You are a trading signal parser. Extract futures trading signals from messages.
Return ONLY valid JSON. No explanations, no markdown, just the JSON object.

Schema:
{
  "action":      "LONG" | "SHORT" | "FLAT" | null,
  "symbol":      "ETH",
  "entry_type":  "market" | "limit" | "stop",
  "entry_price": <float or null>,
  "stop_price":  <float or null>,
  "take_price":  <float or null>,
  "take_levels": [<float>, <float>, ...],
  "confidence":  <0.0 to 1.0>
}

Rules:
- action=null if the message is NOT a trading signal
- entry_type="market" if no specific entry price is mentioned
- entry_type="limit" if entry price is BELOW current context (buy dip / sell rally)
- entry_type="stop" if entry price is ABOVE current for LONG or BELOW for SHORT (breakout)
- stop_price: the stop-loss price level
- take_levels: ALL take-profit levels as a list [TP1, TP2, TP3, ...] — position split equally
- take_price: same as take_levels[0] if multiple, or single TP price; null if none
- confidence: your certainty that this is a valid actionable trade signal
"""


class LLMParser:

    def __init__(self, provider: str, api_key: str, model: str = ""):
        self.provider = provider.lower()
        self.api_key  = api_key
        self.model    = model or _DEFAULT_MODEL.get(self.provider, "")

    async def parse(self, text: str) -> Optional[Signal]:
        """Parse a message text into a Signal. Returns None if not a signal."""
        if self.provider == "none" or not text.strip():
            return None

        if self.provider == "claude":
            return await self._parse_claude(text)

        logger.warning(f"[LLMParser] Unknown provider: {self.provider}")
        return None

    async def _parse_claude(self, text: str) -> Optional[Signal]:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=self.api_key)

            response = await client.messages.create(
                model=self.model,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )

            raw = response.content[0].text.strip()
            data = json.loads(raw)

        except json.JSONDecodeError as e:
            logger.warning(f"[LLMParser] JSON parse error: {e} | raw: {raw[:100]}")
            return None
        except Exception as e:
            logger.error(f"[LLMParser] Claude API error: {e}")
            return None

        if not data.get("action"):
            return None

        confidence = float(data.get("confidence", 0))
        if confidence < 0.65:
            logger.info(f"[LLMParser] Low confidence {confidence:.2f} — skipped: {text[:60]}")
            return None

        symbol = (data.get("symbol") or "").upper().strip()
        if not symbol:
            return None

        action = data["action"].upper()
        if action not in ("LONG", "SHORT", "FLAT"):
            return None

        entry_price  = float(data["entry_price"]) if data.get("entry_price") else 0.0
        stop_price   = float(data["stop_price"])  if data.get("stop_price")  else 0.0
        take_price   = float(data["take_price"])  if data.get("take_price")  else 0.0
        entry_type   = data.get("entry_type", "market")
        take_levels  = [float(p) for p in data.get("take_levels", []) if p]

        # If multiple levels, take_price = first level
        if take_levels and not take_price:
            take_price = take_levels[0]

        signal = Signal(
            id          = str(uuid.uuid4())[:8],
            symbol      = symbol,
            action      = action,
            entry       = entry_price,
            entry_type  = entry_type,
            stop_price  = stop_price,
            take_price  = take_price,
            take_levels = take_levels,
            reason      = "telegram",
            timestamp   = int(time.time()),
        )

        tp_info = f"TP ladder {take_levels}" if take_levels else f"TP {take_price}"
        logger.info(
            f"[LLMParser] ✅ {symbol} {action} | entry_type={entry_type} "
            f"entry={entry_price} SL={stop_price} {tp_info} conf={confidence:.2f}"
        )
        return signal
