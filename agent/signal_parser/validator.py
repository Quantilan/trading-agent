# agent/signal_parser/validator.py
"""
Pre-execution validation for parsed signals.

Rules:
  OPEN  LONG  — SL must be below entry, TP must be above entry
  OPEN  SHORT — SL must be above entry, TP must be below entry
  MODIFY_SL   — SL can only move toward the position (trail up for LONG, trail down for SHORT)
  MODIFY_TP   — TP must remain on the profitable side of entry
"""

from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.state import Position, PositionSide


def validate_open_sl(side: str, entry: float, sl: float) -> Tuple[bool, str]:
    """Validate stop-loss price for a new position."""
    if sl <= 0:
        return True, ""
    if side == "LONG" and sl >= entry:
        return False, (
            f"SL ${sl:.4f} must be <b>below</b> entry ${entry:.4f} for LONG"
        )
    if side == "SHORT" and sl <= entry:
        return False, (
            f"SL ${sl:.4f} must be <b>above</b> entry ${entry:.4f} for SHORT"
        )
    return True, ""


def validate_open_tp(side: str, entry: float, tp: float) -> Tuple[bool, str]:
    """Validate take-profit price for a new position."""
    if tp <= 0:
        return True, ""
    if side == "LONG" and tp <= entry:
        return False, (
            f"TP ${tp:.4f} must be <b>above</b> entry ${entry:.4f} for LONG"
        )
    if side == "SHORT" and tp >= entry:
        return False, (
            f"TP ${tp:.4f} must be <b>below</b> entry ${entry:.4f} for SHORT"
        )
    return True, ""


def validate_modify_sl(position: "Position", new_sl: float) -> Tuple[bool, str]:
    """
    Trailing stop validation — SL can only move in the direction of the position:
      LONG  → SL must go UP   (lock in more profit / reduce loss)
      SHORT → SL must go DOWN (lock in more profit / reduce loss)
    """
    if new_sl <= 0:
        return False, "New SL price must be > 0"

    from agent.state import PositionSide
    cur = position.stop_price

    if position.side == PositionSide.LONG:
        if new_sl <= cur:
            return False, (
                f"For LONG, new SL ${new_sl:.4f} must be <b>above</b> "
                f"current SL ${cur:.4f} (trail up only)"
            )
    elif position.side == PositionSide.SHORT:
        if new_sl >= cur:
            return False, (
                f"For SHORT, new SL ${new_sl:.4f} must be <b>below</b> "
                f"current SL ${cur:.4f} (trail down only)"
            )
    return True, ""


def validate_modify_tp(position: "Position", new_tp: float) -> Tuple[bool, str]:
    """TP must stay on the profitable side of the entry price."""
    if new_tp <= 0:
        return False, "New TP price must be > 0"

    from agent.state import PositionSide
    entry = position.entry_price

    if position.side == PositionSide.LONG and new_tp <= entry:
        return False, (
            f"For LONG, new TP ${new_tp:.4f} must be <b>above</b> entry ${entry:.4f}"
        )
    if position.side == PositionSide.SHORT and new_tp >= entry:
        return False, (
            f"For SHORT, new TP ${new_tp:.4f} must be <b>below</b> entry ${entry:.4f}"
        )
    return True, ""
