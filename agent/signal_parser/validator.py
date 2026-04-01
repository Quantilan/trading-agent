# agent/signal_parser/validator.py
"""
Pre-execution validation for parsed signals.
Updated: 
1. Added Liquidation Price safety (10% buffer).
2. Added Market Price check to prevent instant execution.
3. Supports Spot-like behavior for Leverage=1.
"""

from typing import Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.state import Position

# --- Settings ---
MAX_SL_PCT = 0.50      # SL can't be more than 50% away from entry
LIQ_BUFFER_PCT = 0.15  # 15% safety margin from the distance between Entry and Liq


def calculate_liq_price(side: str, entry: float, leverage: int) -> float:
    """
    Calculates basic liquidation price. 
    If leverage is 1, liquidation for LONG is 0 (Spot behavior).
    """
    if leverage <= 0:
        return 0.0
    
    # If leverage is 1, it's effectively a spot position.
    price = entry * (1 + 1 / leverage)
    if side == "LONG":
        price = entry * (1 - 1 / leverage)
    
    return round(price, 8)


def validate_sl_logic(side: str, entry: float, sl: float, leverage: int, current_price: float) -> Tuple[bool, str]:
    if sl <= 0:
        return True, ""

    # 1. Проверка на максимальное расстояние (MAX_SL_PCT)
    # Если на LONG стоп ниже входа более чем на 50% — отсекаем
    if side == "LONG":
        if (entry - sl) / entry > MAX_SL_PCT:
            return False, f"SL ${sl:.4f} is more than {MAX_SL_PCT*100}% below entry ${entry:.4f}"
    else: # SHORT
        if (sl - entry) / entry > MAX_SL_PCT:
            return False, f"SL ${sl:.4f} is more than {MAX_SL_PCT*100}% above entry ${entry:.4f}"

    # 2. Проверка стороны рынка (чтобы не закрыться мгновенно)
    if side == "LONG":
        if sl >= current_price:
            return False, f"SL ${sl:.4f} must be below market ${current_price:.4f}"
    else:
        if sl <= current_price:
            return False, f"SL ${sl:.4f} must be above market ${current_price:.4f}"

    # 3. Проверка ликвидации с буфером 10%
    liq = calculate_liq_price(side, entry, leverage)
    if side == "LONG":
        # Дистанция от входа до ликвы. Буфер — 10% от этой дистанции.
        min_safe_sl = liq + (entry - liq) * LIQ_BUFFER_PCT
        if sl <= min_safe_sl:
            return False, f"SL ${sl:.4f} too close to LIQ ${liq:.2f}. Min safe: ${min_safe_sl:.2f}"
    else:
        max_safe_sl = liq - (liq - entry) * LIQ_BUFFER_PCT
        if sl >= max_safe_sl:
            return False, f"SL ${sl:.4f} too close to LIQ ${liq:.2f}. Max safe: ${max_safe_sl:.2f}"

    return True, ""

def validate_open_sl(side: str, entry: float, sl: float, leverage: int) -> Tuple[bool, str]:
    """
    Validation for a NEW position (where current price = entry).
    """
    return validate_sl_logic(side, entry, sl, leverage, current_price=entry)


def validate_open_tp(side: str, entry: float, tp: float) -> Tuple[bool, str]:
    """Validate TP for new position."""
    if tp <= 0:
        return True, ""
    if side == "LONG" and tp <= entry:
        return False, f"TP ${tp:.4f} must be above entry ${entry:.4f}"
    if side == "SHORT" and tp >= entry:
        return False, f"TP ${tp:.4f} must be below entry ${entry:.4f}"
    return True, ""


def validate_modify_sl(position: "Position", new_sl: float) -> Tuple[bool, str]:
    """
    Validation for changing SL on an EXISTING position.
    Uses actual market price and position's leverage.
    """
    return validate_sl_logic(
        side=position.side.value, 
        entry=position.entry_price, 
        sl=new_sl, 
        leverage=position.leverage, 
        current_price=position.last_price
    )


def validate_modify_tp(position: "Position", new_tp: float) -> Tuple[bool, str]:
    """
    TP must stay on the profitable side of entry AND market price.
    """
    if new_tp <= 0:
        return False, "New TP price must be > 0"

    entry = position.entry_price
    side = position.side.value

    if side == "LONG":
        if new_tp <= entry:
            return False, f"For LONG, new TP ${new_tp:.4f} must be above entry ${entry:.4f}"
        if new_tp <= position.last_price:
            return False, f"For LONG, new TP ${new_tp:.4f} must be above market ${position.last_price:.4f}"
    else: # SHORT
        if new_tp >= entry:
            return False, f"For SHORT, new TP ${new_tp:.4f} must be below entry ${entry:.4f}"
        if new_tp >= position.last_price:
            return False, f"For SHORT, new TP ${new_tp:.4f} must be below market ${position.last_price:.4f}"
            
    return True, ""