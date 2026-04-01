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
from typing import Optional, Tuple, List

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
        Parse message into a Signal object.
        """
        t = text.lower().strip()

        action = self._detect_action(t)
        if not action:
            return None

        symbol = self._detect_symbol(t)
        if action in ("open", "close") and not symbol:
            return None

        side = self._detect_side(t) if action == "open" else None
        if action == "open" and not side:
            return None

        # Extract numerical values from text
        sl_pct, sl_abs = self._extract_sl(t)
        tp_pct, tp_abs = self._extract_tp(t)

        sig_action = ""
        effective_sl = 0.0
        new_sl = 0.0
        new_tp = 0.0

        if action == "open":
            sig_action = side.upper()
            effective_sl = sl_pct if sl_pct else (0.0 if sl_abs else default_sl_pct / 100)
        elif action == "close":
            sig_action = "FLAT"
        elif action == "modify_sl":
            sig_action = "MODIFY_SL"
            new_sl = sl_abs or 0.0 # Absolute price from text
        elif action == "modify_tp":
            sig_action = "MODIFY_TP"
            new_tp = tp_abs[0] if isinstance(tp_abs, list) else (tp_abs or 0.0)

        # Build final Signal
        signal = Signal(
            id         = str(uuid.uuid4())[:8],
            symbol     = symbol,
            action     = sig_action,
            entry      = 0.0,
            entry_type = "market",
            sl_pct     = effective_sl,
            # Ensure stop_price and take_price get the absolute values for any action
            stop_price = sl_abs or new_sl or 0.0,
            take_price = (tp_abs[0] if isinstance(tp_abs, list) else tp_abs) or new_tp or 0.0,
            take_levels= tp_abs if isinstance(tp_abs, list) else [],
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
        """
        Extracts SL as percentage or absolute price.
        Supports: 'sl 2%', 'стоп eth на 1800', 'Стоп-лосс: 66500'
        """
        
        # 1. Percentage (e.g., 'sl 2%')
        m_pct = re.search(r'(?:sl|стоп|stop[- ]?loss)[\s\w]*?(\d+\.?\d*)%', text)
        if m_pct:
            return float(m_pct.group(1)) / 100, None

        # 2. Absolute Price
        # Using [\s\w:-]*? to skip any chars (spaces, tickers, colons, dashes) 
        # then optional 'на' and finally the price.
        pattern_abs = r'(?:sl|стоп(?:[- ]?лосс)?|stop[- ]?loss)[\s\w:-]*?(?:на\s+)?(\d+\.?\d+)'
        m_abs = re.search(pattern_abs, text)
        
        if m_abs:
            return None, float(m_abs.group(1))
            
        return None, None

    def _extract_tp(self, text: str) -> Tuple[str, Optional[List[float]]]:
        """
        Extracts take-profit levels from text. 
        Handles RU/UA/EN variants and 'dirty' lists with conjunctions.
        """
        # 1. Broad pattern to capture the keyword and everything numeric/delimited that follows
        # Matches: тейки, тейк-профіт, take profit, tp
        pattern = r"(?i)(?:тейк[- ]?проф[иіi]т|тейк[а-яіi]*|take[- ]?profit|tp)[:\s-]*([\d\s,.\bи\band&]+)"
        
        match = re.search(pattern, text)
        if not match:
            return "TP", None

        # raw_data: e.g., "160.2, 170.5 и 185"
        raw_data = match.group(1).strip()
        
        # 2. NORMALIZATION: Replace " и ", " and ", " & " with commas
        # This is the key fix for the 'dirty' text test
        raw_data = re.sub(r"\s+(?:и|&|and)\s+", ",", raw_data)
        
        # 3. Split by any sequence of commas or whitespace
        # This handles "160.2,170.5 185" correctly
        parts = re.split(r"[,\s]+", raw_data)
        
        levels = []
        for part in parts:
            # Standardize decimal separator
            clean_part = part.replace(',', '.').strip()
            if not clean_part:
                continue
            try:
                # Handle cases like "185." at the end of a sentence
                clean_part = clean_part.rstrip('.')
                levels.append(float(clean_part))
            except ValueError:
                # Skip if the part is not a number (e.g. leftover text)
                continue
                
        return "TP", levels if levels else None