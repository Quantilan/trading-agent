# agent/signal_parser/regex_parser.py
"""
Rule-based signal parser using patterns.json.

Supports actions: open (LONG/SHORT), close (FLAT), modify_sl, modify_tp.
Edit patterns.json to add channel-specific terms or coin aliases.

Examples:
  "открой эфир в лонг"          → LONG  ETH  sl=default
  "buy sol short sl 2% tp 5%"   → SHORT SOL  sl_pct=2 tp_pct=5
  "закрой биток"                 → FLAT  BTC
  "стоп на 1800"                 → MODIFY_SL  new_sl=1800
  "тейк на 3500"                 → MODIFY_TP  new_tp=3500
  "перенеси стоп eth sl 1750"    → MODIFY_SL  ETH  new_sl=1750
"""

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

# Word boundary pattern for Cyrillic + Latin
_WB = r'(?<![а-яёa-z0-9]){}(?![а-яёa-z0-9])'

from agent.state import Signal

logger = logging.getLogger(__name__)

_PATTERNS_FILE = Path(__file__).parent / "patterns.json"


class RegexParser:

    def __init__(self, patterns_path: str = ""):
        path = Path(patterns_path) if patterns_path else _PATTERNS_FILE
        with open(path, encoding="utf-8") as f:
            p = json.load(f)

        self._actions    = p["actions"]
        self._sides      = p["sides"]
        # Build symbol map: sorted longest-first to match "bitcoin cash" before "bitcoin"
        self._symbols: list[tuple[str, str]] = sorted(
            ((k.lower(), v) for k, v in p["symbols"].items()),
            key=lambda x: -len(x[0]),
        )
        self._size_kw = p.get("size_keywords", {})

    # ─────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────

    def parse(self, text: str, default_sl_pct: float = 2.0) -> Optional[Signal]:
        """
        Parse a message into a Signal.
        Returns None if the text doesn't look like a trading command.
        """
        t = text.lower().strip()

        action = self._detect_action(t)
        if not action:
            return None

        symbol = self._detect_symbol(t)

        # open/close require a known symbol; modify can work without (means current pos)
        if action in ("open", "close") and not symbol:
            logger.debug(f"[RegexParser] action={action} but symbol not found: {text[:60]}")
            return None

        side = self._detect_side(t) if action == "open" else None
        if action == "open" and not side:
            logger.debug(f"[RegexParser] open action but side not found: {text[:60]}")
            return None

        sl_pct, sl_abs = self._extract_sl(t)
        tp_pct, tp_abs = self._extract_tp(t)

        # Map to Signal fields
        if action == "open":
            sig_action   = side.upper()          # "LONG" | "SHORT"
            # Use sl_pct if given; skip default if absolute SL price is present
            effective_sl = sl_pct if sl_pct else (0.0 if sl_abs else default_sl_pct / 100)
            new_sl       = 0.0
            new_tp       = 0.0
        elif action == "close":
            sig_action   = "FLAT"
            effective_sl = 0.0
            new_sl       = 0.0
            new_tp       = 0.0
        elif action == "modify_sl":
            sig_action   = "MODIFY_SL"
            effective_sl = 0.0
            new_sl       = sl_abs or 0.0
            new_tp       = 0.0
        else:  # modify_tp
            sig_action   = "MODIFY_TP"
            effective_sl = 0.0
            new_sl       = 0.0
            new_tp       = tp_abs or 0.0

        signal = Signal(
            id         = str(uuid.uuid4())[:8],
            symbol     = symbol,
            action     = sig_action,
            entry      = 0.0,
            entry_type = "market",
            sl_pct     = effective_sl,
            stop_price = sl_abs or 0.0,
            take_price = tp_abs or 0.0,
            tp_pct     = tp_pct or 0.0,
            new_sl     = new_sl,
            new_tp     = new_tp,
            reason     = "telegram_text",
            timestamp  = int(time.time()),
        )

        logger.info(
            f"[RegexParser] ✅ {sig_action} {symbol or '(current)'} "
            f"sl_pct={effective_sl} sl_abs={sl_abs} tp_pct={tp_pct} tp_abs={tp_abs} "
            f"| {text[:60]}"
        )
        return signal

    # ─────────────────────────────────────────
    # DETECTION HELPERS
    # ─────────────────────────────────────────

    def _detect_action(self, text: str) -> Optional[str]:
        """
        Priority: open-with-side > modify_sl > modify_tp > close > bare-open.

        A message that has both a side keyword (long/short) and an open-action keyword
        is unambiguously an open signal — even if it also contains "sl:" or "tp:"
        as part of the listed SL/TP values (common in forwarded channel signals).
        """
        has_open_kw = any(kw in text for kw in self._actions.get("open", []))
        if has_open_kw and self._detect_side(text) is not None:
            return "open"

        for kw in self._actions.get("modify_sl", []):
            if kw in text:
                return "modify_sl"
        for kw in self._actions.get("modify_tp", []):
            if kw in text:
                return "modify_tp"
        for kw in self._actions.get("close", []):
            if kw in text:
                return "close"
        for kw in self._actions.get("open", []):
            if kw in text:
                return "open"
        return None

    def _detect_symbol(self, text: str) -> str:
        """Return base symbol (e.g. 'ETH') or empty string if not found.
        Uses word-boundary check to avoid matching substrings (e.g. 'оп' inside 'стоп').
        """
        for alias, symbol in self._symbols:
            if re.search(_WB.format(re.escape(alias)), text):
                return symbol
        return ""

    def _detect_side(self, text: str) -> Optional[str]:
        """Detect side, checking SHORT keywords first to avoid 'sell' being shadowed by 'long'."""
        for kw in self._sides.get("short", []):
            if re.search(_WB.format(re.escape(kw)), text):
                return "short"
        for kw in self._sides.get("long", []):
            if re.search(_WB.format(re.escape(kw)), text):
                return "long"
        return None

    def _extract_sl(self, text: str) -> Tuple[Optional[float], Optional[float]]:
        """Returns (sl_pct, sl_abs). sl_pct is a decimal (0.02 = 2%)."""
        
        # "sl 2%" / "sl=2%" / "стоп 2%"
        m = re.search(r'(?:sl[:\s=]*|стоп[:\s]*)(\d+\.?\d*)%', text)
        if m:
            return float(m.group(1)) / 100, None
        # "sl 1800" / "sl=1800" (absolute price)
        m = re.search(r'\bsl[:\s=]+(\d+\.?\d*)\b', text)
        if m:
            return None, float(m.group(1))
        # "стоп на 1800"
        m = re.search(r'стоп\s+на\s+(\d+\.?\d*)', text)
        if m:
            return None, float(m.group(1))
        
        # стоп стоплосс stop stoploss:
        pattern = r'(?:стоп[- ]?лосс|стоп|stop[- ]?loss|stoploss|sl)[\s:-]+(\d+\.?\d*)'
        m = re.search(pattern, text)
        if m:
            return None, float(m.group(1))
        return None, None

    def _extract_tp(self, text: str) -> Tuple[Optional[float], Optional[list[float]]]:
        """Returns (tp_pct, tp_abs_list). tp_abs_list is a list of prices."""
        text = text.lower()
        
        # 1. Поиск процентов (обычно один общий профит на позицию)
        # "tp 5%" / "тейк: 5%"
        m_pct = re.search(r'(?:tp|тейк|take[- ]?profit)[\s:-]*(\d+\.?\d*)%', text)
        if m_pct:
            return float(m_pct.group(1)) / 100, None

        # 2. Поиск списка цен (лестница тейков)
        # Ищем ключевое слово, а затем последовательность цифр, разделенных запятыми
        # Примеры: "тейк-профит: 2.973,3.080,3.173" или "tp: 3500, 3600"
        pattern_abs = r'(?:tp|тейк(?:-профит)?|take[- ]?profit)[\s:-]+на?\s*([\d\.,\s]+)'
        m_abs = re.search(pattern_abs, text)
        
        if m_abs:
            raw_values = m_abs.group(1)
            # Разделяем по запятой, убираем пробелы и фильтруем пустые строки
            try:
                # Заменяем возможные пробелы, чтобы корректно распарсить "3 000, 3 100"
                prices = [float(x.strip().replace(' ', '')) for x in raw_values.split(',') if x.strip()]
                if prices:
                    return None, prices
            except ValueError:
                pass # Если в строку попал мусор, который нельзя конвертировать в float

        return None, None
