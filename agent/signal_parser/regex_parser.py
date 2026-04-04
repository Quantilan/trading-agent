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

    def __init__(self, patterns_path: str = "", extra_symbols: list = None):
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

        # Merge extra coins from coins.json (user list).
        # We only add coins not already covered by patterns.json aliases.
        # No alternative names for user-added coins — they must use the exact ticker.
        if extra_symbols:
            existing_canonical = {v for _, v in self._symbols}
            added = 0
            for coin in extra_symbols:
                coin = str(coin).upper().strip()
                if coin and coin not in existing_canonical:
                    self._symbols.append((coin.lower(), coin))
                    added += 1
            if added:
                # Re-sort to keep longest-first matching intact
                self._symbols.sort(key=lambda x: -len(x[0]))
                logger.debug(f"[Parser] Added {added} extra coins from coins.json")

    # ─────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────

    def parse(self, text: str, default_sl_pct: float = 2.0, default_tp_pct: float = 0.0) -> Optional[Signal]:
        # 1. Basic cleanup
        t = text.lower().strip()

        # 2. Action detection
        action = self._detect_action(t)
        if not action: return None

        # 3. Symbol detection
        symbol = self._detect_symbol(t)
        if action in ("open", "close") and not symbol: return None

        # 4. Numerical data extraction (entry / SL / TP)
        entry_min, entry_max = self._extract_entry(t)
        sl_pct, sl_abs = self._extract_sl(t)
        tp_pct, tp_abs = self._extract_tp(t)

        # 5. TP Ladder proportions calculation
        proportions = []
        if isinstance(tp_abs, list) and tp_abs:
            count = len(tp_abs)
            if count == 1:
                proportions = [1.0]
            elif count == 2:
                proportions = [0.6, 0.4]
            elif count == 3:
                proportions = [0.5, 0.3, 0.2]
            else:
                proportions = [0.4, 0.2] + [0.4 / (count - 2)] * (count - 2)

        # 6. ACTION MAPPING (The fix for "Unknown action")
        if action == "open":
            side = self._detect_side(t)
            sig_action = side.upper() if side else "LONG"
        elif action == "close":
            sig_action = "FLAT" # Fixes "CLOSE" vs "FLAT" mismatch
        elif action == "modify_sl":
            sig_action = "MODIFY_SL"
        elif action == "modify_tp":
            sig_action = "MODIFY_TP"
        else:
            sig_action = action.upper()

        # 7. Final prices setup
        final_stop = sl_abs if sl_abs > 0 else 0.0
        final_take = tp_abs[0] if (isinstance(tp_abs, list) and tp_abs) else 0.0
        
        # 8. Build Signal object
        # entry_min set → deferred entry (wait for price)
        # entry_min == 0 → immediate market entry
        entry_mid = round((entry_min + entry_max) / 2, 8) if entry_min > 0 else 0.0

        signal = Signal(
            id               = str(uuid.uuid4())[:8],
            symbol           = symbol,
            action           = sig_action,
            entry            = entry_mid,
            entry_type       = "market" if entry_min == 0 else "deferred",
            entry_min        = entry_min,
            entry_max        = entry_max if entry_max > 0 else entry_min,
            sl_pct           = sl_pct if sl_pct > 0 else (default_sl_pct / 100 if final_stop == 0 else 0.0),
            stop_price       = final_stop,
            take_price       = final_take,
            tp_pct           = tp_pct if tp_pct > 0 else (default_tp_pct / 100 if final_take == 0 and not isinstance(tp_abs, list) else 0.0),
            take_levels      = tp_abs if isinstance(tp_abs, list) else [],
            take_proportions = proportions,
            new_sl           = final_stop if sig_action == "MODIFY_SL" else 0.0,
            new_tp           = final_take if sig_action == "MODIFY_TP" else 0.0,
            reason           = "telegram_text",
            timestamp        = int(time.time()),
        )

        # 9. YOUR ORIGINAL LOGGER (The one you asked to keep)
        logger.info(
            f"Parsed signal: {signal.symbol} {signal.action} "
            f"SL:{signal.stop_price or signal.sl_pct} "
            f"TP:{signal.take_levels or signal.take_price} "
            f"Props:{signal.take_proportions}"
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

    def _extract_entry(self, text: str) -> Tuple[float, float]:
        """
        Extract entry price or range from signal text.
        Returns (entry_min, entry_max).
        Single price  → (price, 0.0)
        Range X - Y   → (min(X,Y), max(X,Y))
        Not found     → (0.0, 0.0)

        Matches patterns like:
          entry: 2.927 - 2.702
          entry price: 68000
          точка входу: 2.927 - 2.702
          вход: 1.25
          enter: 2050
        """
        # Keywords: EN + UA + RU
        # UA: вхід (nom) / входу (gen, used in "точка входу") / вхідна (adj)
        pattern = (
            r"(?i)"
            r"(?:entry\s*(?:price|point|zone)?|точка\s*вход[уаіia]?|вхід[а-яіa]*|вход[аеиу]?|enter)"
            r"\s*[:\-]?\s*"
            r"([\d.]+)"
            r"(?:\s*[-–—]\s*([\d.]+))?"
        )
        match = re.search(pattern, text)
        if not match:
            return 0.0, 0.0

        try:
            v1 = float(match.group(1))
            v2 = float(match.group(2)) if match.group(2) else 0.0
        except (ValueError, TypeError):
            return 0.0, 0.0

        if v2 > 0:
            return min(v1, v2), max(v1, v2)
        return v1, 0.0