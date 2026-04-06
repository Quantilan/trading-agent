# agent/coins.py
"""
Coin registry — validates the project coin list against exchange markets.

coins.json  (root, user-editable)  — list of base symbols the user trades.
            Created automatically from the built-in top-30 list on first run.
            NOT overwritten on software updates.
"""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_ROOT       = Path(__file__).parent.parent
_COINS_FILE = _ROOT / "coins.json"

_BUILTIN_COINS: List[str] = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "TRX", "DOT",
    "LINK", "POL", "LTC", "BCH", "NEAR", "APT", "UNI", "ICP", "ARB", "OP",
    "INJ", "SUI", "ATOM", "HBAR", "AAVE", "TON", "ETC", "LDO", "ENA", "PEPE",
    "TIA", "TAO", "ONDO",
]


@dataclass
class CoinInfo:
    symbol:        str    # "BTC"
    market_symbol: str    # "BTC/USDT:USDT"
    available:     bool   # found in exchange markets with the configured stablecoin
    price_prec:    int    # decimal digits for price (e.g. 2 → price rounded to 0.01)
    amount_step:   float  # minimum lot increment (floor amount to multiples of this)
    min_notional:  float  # minimum order value in stablecoin
    max_amount:    float  # maximum single order amount (0 = exchange has no limit)


def load_coins_list() -> List[str]:
    """
    Load user coin list from coins.json.
    If coins.json does not exist, create it from default_coins.json (or the built-in list)
    and return its contents.
    """
    if _COINS_FILE.exists():
        try:
            data = json.loads(_COINS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                coins = [str(c).upper().strip() for c in data if c]
                logger.debug(f"[Coins] Loaded {len(coins)} coins from {_COINS_FILE.name}")
                return coins
        except Exception as e:
            logger.warning(f"[Coins] Failed to read coins.json: {e} — using defaults")

    # Create coins.json from built-in list
    sorted_default = sorted([str(c).upper().strip() for c in _BUILTIN_COINS if c])
    _COINS_FILE.write_text(
        json.dumps(sorted_default, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(f"[Coins] Created {_COINS_FILE.name} with {len(sorted_default)} coins")
    return sorted_default


def build_registry(
    coins:   List[str],
    markets: dict,
    stbc:    str,
) -> Dict[str, "CoinInfo"]:
    """
    Match each coin against loaded exchange markets and return a registry.

    Args:
        coins:   base symbols, e.g. ["BTC", "ETH"]
        markets: ccxt markets dict (exchange.markets after load_markets())
        stbc:    stablecoin, e.g. "USDT"

    Returns:
        dict  symbol → CoinInfo  (both available and unavailable coins are included)
    """
    registry: Dict[str, CoinInfo] = {}

    def _price_digits(val, default: int = 2) -> int:
        """Convert ccxt price precision float (e.g. 0.01) to decimal digit count (2)."""
        try:
            return int(abs(round(math.log10(float(val))))) if val else default
        except Exception:
            return default

    available = 0
    for coin in coins:
        ms     = f"{coin}/{stbc}:{stbc}"
        market = markets.get(ms)

        if market:
            precision    = market.get("precision") or {}
            limits       = market.get("limits")    or {}
            amt_limits   = limits.get("amount")    or {}
            cost_limits  = limits.get("cost")      or {}

            price_prec   = _price_digits(precision.get("price"))
            amount_step  = float(precision.get("amount") or 0.001)
            min_notional = float(cost_limits.get("min")  or 5.0)
            max_amount   = float(amt_limits.get("max")   or 0.0)

            registry[coin] = CoinInfo(
                symbol        = coin,
                market_symbol = ms,
                available     = True,
                price_prec    = price_prec,
                amount_step   = amount_step,
                min_notional  = min_notional,
                max_amount    = max_amount,
            )
            available += 1
        else:
            registry[coin] = CoinInfo(
                symbol        = coin,
                market_symbol = ms,
                available     = False,
                price_prec    = 2,
                amount_step   = 0.001,
                min_notional  = 5.0,
                max_amount    = 0.0,
            )

    logger.info(
        f"[Coins] {available}/{len(coins)} available on exchange with {stbc}"
    )
    return registry
