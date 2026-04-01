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
        t = text.lower().strip()

        action = self._detect_action(t)
        if not action: return None

        symbol = self._detect_symbol(t)
        if action in ("open", "close") and not symbol: return None

        # Extraction
        sl_pct, sl_abs = self._extract_sl(t)
        tp_pct, tp_abs = self._extract_tp(t)

        # Action determination
        side = self._detect_side(t) if action == "open" else None
        sig_action = side.upper() if side else action.upper()

        # Final prices
        final_stop = sl_abs if sl_abs > 0 else 0.0
        final_take = tp_abs[0] if tp_abs else 0.0

        # Build Signal
        signal = Signal(
            id          = str(uuid.uuid4())[:8],
            symbol      = symbol,
            action      = sig_action,
            entry       = 0.0,
            entry_type  = "market",
            sl_pct      = sl_pct if sl_pct > 0 else (default_sl_pct / 100 if final_stop == 0 else 0.0),
            stop_price  = final_stop,
            take_price  = final_take,
            take_levels = tp_abs,
            tp_pct      = tp_pct,
            new_sl      = final_stop if sig_action == "MODIFY_SL" else 0.0,
            new_tp      = final_take if sig_action == "MODIFY_TP" else 0.0,
            reason      = "telegram_text",
            timestamp   = int(time.time()),
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

    def _extract_sl(self, text: str) -> Tuple[float, float]:
        """
        Extracts Stop Loss. 
        Matches: 'стоп на 1750', 'стоп ETH 1750', 'Stop-loss: 66000'
        """
        # (?i) -> ignore case
        # (?:стоп[а-яіi\-]*|stop[- ]?loss) -> keywords
        # [\s\w\-:]*? -> optional whitespace
        # ([\d.]+) -> price
        # \s*(%)? -> optional percentage
        
        pattern = r"(?i)(?:стоп[а-яіi\-]*|stop[- ]?loss)[\s\w\-:]*?([\d.]+)\s*(%)?"
        match = re.search(pattern, text)
        
        if not match:
            return 0.0, 0.0

        try:
            val = float(match.group(1))
            is_pct = match.group(2) is not None
            # If it's a percentage (e.g. 2%), return as 0.02
            return (val / 100, 0.0) if is_pct else (0.0, val)
        except (ValueError, IndexError):
            return 0.0, 0.0
        
    def _extract_tp(self, text: str) -> Tuple[float, List[float]]:
        """
        Extracts Take Profit. Returns (pct, [abs_prices]).
        """
        # Key change: Included '-' in the keyword part to handle 'тейк-профіт'
        pattern = r"(?i)(?:тейк[-а-яіi]*|take[- ]?profit|tp)[:\s-]*([\d\s,.\bи\band&%]+)"
        match = re.search(pattern, text)
        
        if not match:
            return 0.0, []

        raw_data = match.group(1).strip()
        
        if "%" in raw_data:
            pct_match = re.search(r"([\d.]+)\s*%", raw_data)
            return (float(pct_match.group(1)) / 100, []) if pct_match else (0.0, [])

        # Normalize list separators
        raw_data = re.sub(r"\s+(?:и|&|and)\s+", ",", raw_data)
        parts = re.split(r"[,\s]+", raw_data)
        
        levels = []
        for part in parts:
            clean = part.replace(',', '.').strip().rstrip('.')
            if not clean: continue
            try:
                levels.append(float(clean))
            except ValueError: continue
                
        return 0.0, levels