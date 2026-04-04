# agent/risk_manager.py
"""
Risk manager — calculates position size for user deposit.
Logic ported from _open_position in coin_processor.py.
"""

import logging
import math
from typing import Tuple

from .config import AgentConfig
from .state import Signal, PositionSide, OrderParams

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Calculates position size based on:
    - current balance
    - risk settings from config
    - signal parameters (sl_pct, tp_pct)
    """

    def __init__(self, config: AgentConfig):
        self.config = config

    # ─────────────────────────────────────
    # PUBLIC INTERFACE
    # ─────────────────────────────────────

    def calculate(
        self,
        signal:        Signal,
        balance:       float,
        price:         float,
        price_precision: int   = 4,
        amount_step:   float   = 0.001,
        min_notional:  float   = 5.0,
        max_amount:    float   = 0.0,
    ) -> Tuple[OrderParams, str]:
        """
        Calculate order parameters.
        Returns (OrderParams, error_message).
        error_message is empty on success.
        """

        try:
            side = PositionSide(signal.action)
        except ValueError:
            return None, f"Invalid side: {signal.action}"

        if balance <= 0:
            return None, "Balance not available"

        if price <= 0:
            return None, "Price not available"

        # ── Margin calculation ───────────
        margin = round(balance * self.config.margin_pct / 100, 2)

        # ── Volume and amount calculation ─
        volume = round(margin * self.config.leverage, 2)

        if volume < min_notional:
            volume = min_notional
            margin = round(volume / self.config.leverage, 2)

        amount = self._floor_to_step(volume / price, amount_step)

        if amount <= 0:
            return None, f"Calculated amount is zero (volume={volume}, price={price}, step={amount_step})"

        # ── Max amount cap (exchange hard limit) ─
        if max_amount > 0 and amount > max_amount:
            logger.warning(
                f"[RiskManager] {signal.symbol} amount {amount} exceeds exchange max {max_amount} — capping"
            )
            amount = self._floor_to_step(max_amount, amount_step)
            volume = round(amount * price, 2)
            margin = round(volume / self.config.leverage, 2)

        # ── SL/TP calculation ────────────
        # Absolute prices from signal take priority (Telegram/LLM signals).
        # Fall back to percentage-based calculation (server signals).
        sl_pct = signal.sl_pct
        tp_pct = signal.tp_pct

        if signal.stop_price > 0:
            stop_price = round(signal.stop_price, price_precision)
            sl_pct     = abs(price - stop_price) / price
        elif sl_pct > 0:
            if side == PositionSide.LONG:
                stop_price = round(price * (1 - sl_pct), price_precision)
            else:
                stop_price = round(price * (1 + sl_pct), price_precision)
        else:
            return None, "No SL defined: set sl_pct or stop_price in signal"

        if signal.take_price > 0:
            take_price = round(signal.take_price, price_precision)
            tp_pct     = abs(take_price - price) / price
        elif tp_pct > 0:
            if side == PositionSide.LONG:
                take_price = round(price * (1 + tp_pct), price_precision)
            else:
                take_price = round(price * (1 - tp_pct), price_precision)
        else:
            take_price = 0.0
            tp_pct     = 0.0

        # ── TP ladder (multiple levels) ──
        take_levels = []
        if signal.take_levels:
            ratios      = signal.take_proportions if signal.take_proportions else [1.0] * len(signal.take_levels)
            amounts     = self.split_amounts(amount, ratios, amount_step)
            take_levels = [
                (round(level[0] if isinstance(level, (list, tuple)) else level, price_precision), a)
                for level, a in zip(signal.take_levels, amounts)
            ]
            take_price = take_levels[0][0]                   # first level for record
            tp_pct     = abs(take_levels[-1][0] - price) / price

        params = OrderParams(
            symbol      = f"{signal.symbol}USDT",
            side        = side,
            amount      = amount,
            volume      = volume,
            margin      = margin,
            leverage    = self.config.leverage,
            stop_price  = stop_price,
            take_price  = take_price,
            sl_pct      = sl_pct,
            tp_pct      = tp_pct,
            entry_price = price,
            take_levels = take_levels,
        )

        if take_levels:
            levels_str = "  ".join(f"{p}×{a}" for p, a in take_levels)
            logger.info(
                f"[RiskManager] {signal.symbol} {side.value} | "
                f"margin: {margin}$ | amount: {amount} | SL: {stop_price} | "
                f"TP ladder: {levels_str}"
            )
        else:
            logger.info(
                f"[RiskManager] {signal.symbol} {side.value} | "
                f"margin: {margin}$ | volume: {volume}$ | "
                f"amount: {amount} (step={amount_step}) | SL: {stop_price} | TP: {take_price}"
            )

        return params, ""

    # ─────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────

    def _floor_to_step(self, amount: float, step: float) -> float:
        """
        Floor amount to the nearest multiple of step (exchange lot size).
        Works correctly for any step: 0.001, 0.01, 1, 5, 10, etc.
        """
        if step <= 0:
            return amount
        # Use integer arithmetic to avoid floating-point drift
        precision = max(0, round(-math.log10(step))) if step < 1 else 0
        return round(math.floor(round(amount / step, 8)) * step, precision)

    def split_amounts(self, total: float, ratios: list, step: float) -> list:
        """
        Split total amount into parts proportional to ratios.
        Each part is floored to step. Last part takes the remainder
        so that sum(parts) == total exactly — no dust.

        Example: split_amounts(1.0, [0.3, 0.3, 0.4], 0.001)
                 → [0.300, 0.300, 0.400]
        """
        if not ratios or total <= 0:
            return []

        parts      = []
        allocated  = 0.0
        total_norm = sum(ratios)

        for i, ratio in enumerate(ratios):
            if i == len(ratios) - 1:
                # Last piece: take exact remainder, then floor to step
                remainder = total - allocated
                part      = self._floor_to_step(remainder, step)
            else:
                part = self._floor_to_step(total * ratio / total_norm, step)

            parts.append(part)
            allocated += part

        return parts
