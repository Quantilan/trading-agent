# agent/state.py
"""
Agent state — positions, signals, balance.
In-memory with JSON file persistence.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)

STATE_FILE = "agent_state.json"   # legacy default; prefer exchange-scoped names


# ─────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────

class PositionSide(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    FLAT  = "FLAT"

class PositionStatus(str, Enum):
    OPEN        = "OPEN"
    CLOSED_SL   = "CLOSED_SL"
    CLOSED_TP   = "CLOSED_TP"
    CLOSED_SIG  = "CLOSED_SIG"   # closed by signal
    CLOSED_ERR  = "CLOSED_ERR"   # closed due to error


# ─────────────────────────────────────────
# POSITION
# ─────────────────────────────────────────

@dataclass
class Position:
    symbol:         str
    side:           PositionSide
    entry_price:    float
    amount:         float           # in base currency
    volume:         float           # in USDT
    margin:         float           # margin in USDT
    leverage:       int
    last_price:     float           # current market price

    stop_price:     float           # current stop price
    take_price:     float           # take profit (0 = none)
    sl_pct:         float           # SL % from entry
    tp_pct:         float           # TP % from entry

    open_timestamp: int   = 0
    close_timestamp:int   = 0
    close_price:    float = 0.0
    status:         PositionStatus = PositionStatus.OPEN

    # Exchange order IDs
    position_id:    str   = ""
    stop_id:        str   = ""
    take_id:        str   = ""                        # first TP id (single or ladder[0])
    take_ids:       list  = field(default_factory=list)  # all TP ids (ladder)

    # P&L
    rpnl:           float = 0.0     # realized P&L in USDT
    rpnl_pct:       float = 0.0     # in % of margin
    unrealized_pnl: float = 0.0     # unrealized P&L (updated externally)

    reason:         str   = ""       # entry reason from signal
    strategy:       str   = ""       # strategy
    mode:           str   = "paper"  # "paper" | "trade"


# ─────────────────────────────────────────
# ORDER PARAMETERS (risk manager output)
# ─────────────────────────────────────────

@dataclass
class OrderParams:
    """Order parameters after risk manager calculation."""
    symbol:      str
    side:        PositionSide
    amount:      float    # quantity in base currency
    volume:      float    # volume in USDT
    margin:      float    # margin in USDT
    leverage:    int
    stop_price:  float
    take_price:  float    # first TP price (0 = none)
    sl_pct:      float
    tp_pct:      float
    entry_price: float
    take_levels: list = field(default_factory=list)  # [(price, amount), ...] for ladder TP


# ─────────────────────────────────────────
# SIGNAL (incoming from server)
# ─────────────────────────────────────────

@dataclass
class Signal:
    id:         str
    symbol:     str
    action:     str           # LONG | SHORT | FLAT | MODIFY_SL | MODIFY_TP
    entry:      float = 0.0
    sl_pct:     float = 0.0
    tp_pct:     float = 0.0
    new_sl:     float = 0.0   # for MODIFY_SL action
    new_tp:     float = 0.0   # for MODIFY_TP action
    reason:     str   = ""
    strategy:   str   = ""
    # Telegram/LLM signal fields
    entry_type:   str   = "market"  # market | limit | stop
    stop_price:   float = 0.0       # absolute SL price (0 = use sl_pct)
    take_price:   float = 0.0       # absolute TP price (0 = use tp_pct)
    take_levels:  list  = field(default_factory=list)  # [price1, price2, ...] ladder TP
    timestamp:  int   = 0
    expires:    int   = 0     # signal expiry unix timestamp


# ─────────────────────────────────────────
# AGENT STATE
# ─────────────────────────────────────────

@dataclass
class AgentState:
    # Open positions: symbol → Position
    positions:      Dict[str, Position] = field(default_factory=dict)

    # Balance
    balance:        float = 0.0
    free:           float = 0.0
    used:           float = 0.0

    # Per-mode statistics: {"paper": {...}, "trade": {...}}
    mode_stats: Dict[str, dict] = field(default_factory=lambda: {
        "paper": {"total": 0, "wins": 0, "rpnl": 0.0, "best": 0.0, "worst": 0.0},
        "trade": {"total": 0, "wins": 0, "rpnl": 0.0, "best": 0.0, "worst": 0.0},
    })

    # Equity history (balance after each trade)
    equity_history: List[float] = field(default_factory=list)

    # Internal
    last_signal_id: str        = ""
    is_running:     bool       = True
    max_positions:  int        = 7
    auto_confirm:   bool       = False   # execute parsed signals without confirmation
    current_mode:   str        = "paper" # active trading mode, persisted across restarts
    monitor_ts:     Dict[str, int] = field(default_factory=dict)  # symbol → last processed candle ts


# ─────────────────────────────────────────
# STATE MANAGER
# ─────────────────────────────────────────

class StateManager:
    """Manages agent state with file persistence."""

    def __init__(self, max_positions: int = 7, state_file: str = STATE_FILE):
        self._file  = state_file
        self.state  = AgentState(max_positions=max_positions)
        self._load()
        logger.info(f"State file: {self._file}")

    # ── Positions ────────────────────────

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.state.positions.get(symbol)

    def set_position(self, position: Position) -> None:
        self.state.positions[position.symbol] = position
        self._save()

    def remove_position(self, symbol: str) -> Optional[Position]:
        pos = self.state.positions.pop(symbol, None)
        if pos:
            ms = self.state.mode_stats.setdefault(pos.mode, {
                "total": 0, "wins": 0, "rpnl": 0.0, "best": 0.0, "worst": 0.0
            })
            ms["total"] += 1
            if pos.rpnl > 0:
                ms["wins"] += 1
            ms["rpnl"]  = round(ms["rpnl"] + pos.rpnl, 2)
            if pos.rpnl > ms["best"]:
                ms["best"] = pos.rpnl
            if pos.rpnl < ms["worst"]:
                ms["worst"] = pos.rpnl
            self._save()
        return pos

    def close_position(self, symbol: str, close_price: float, reason: str) -> Optional[Position]:
        """Close position with P&L calculation. Called on manual close."""
        pos = self.state.positions.get(symbol)
        if not pos:
            return None
        pos.close_price     = close_price
        pos.close_timestamp = int(time.time() * 1000)
        pos.status          = PositionStatus.CLOSED_SIG

        if pos.side == PositionSide.LONG:
            rpnl_pct = (close_price - pos.entry_price) / pos.entry_price
        else:
            rpnl_pct = (pos.entry_price - close_price) / pos.entry_price
        pos.rpnl     = round(rpnl_pct * pos.volume, 2)
        pos.rpnl_pct = round(rpnl_pct * 100, 2)

        return self.remove_position(symbol)

    def get_open_positions(self, mode: Optional[str] = None) -> List[Position]:
        """Return open positions, optionally filtered by mode."""
        positions = list(self.state.positions.values())
        if mode:
            positions = [p for p in positions if p.mode == mode]
        return positions

    def has_position(self, symbol: str) -> bool:
        return symbol in self.state.positions

    def open_positions_count(self) -> int:
        return len(self.state.positions)

    def max_positions_reached(self, count: int) -> bool:
        return count >= self.state.max_positions

    # ── Balance ─────────────────────────

    def update_balance(self, balance: float, free: float, used: float) -> None:
        self.state.balance = balance
        self.state.free    = free
        self.state.used    = used
        if balance > 0:
            self.state.equity_history.append(round(balance, 2))
            if len(self.state.equity_history) > 1000:
                self.state.equity_history = self.state.equity_history[-1000:]

    @property
    def balance_total(self) -> float:
        return self.state.balance

    @property
    def total_realized_pnl(self) -> float:
        return self.state.mode_stats.get(self.state.current_mode, {}).get("rpnl", 0.0)

    @property
    def equity_history(self) -> List[float]:
        return self.state.equity_history

    # ── Statistics ──────────────────────

    @property
    def win_rate(self) -> float:
        ms = self.state.mode_stats.get(self.state.current_mode, {})
        total = ms.get("total", 0)
        if total == 0:
            return 0.0
        return round(ms.get("wins", 0) / total * 100, 1)

    def get_pnl_stats(self, mode: Optional[str] = None) -> dict:
        m   = mode or self.state.current_mode
        ms  = self.state.mode_stats.get(m, {"total": 0, "wins": 0, "rpnl": 0.0, "best": 0.0, "worst": 0.0})
        unrealized = sum(p.unrealized_pnl for p in self.state.positions.values() if p.mode == m)
        losses = ms["total"] - ms["wins"]
        return {
            "total":      ms["total"],
            "wins":       ms["wins"],
            "losses":     losses,
            "realized":   round(ms["rpnl"], 2),
            "unrealized": round(unrealized, 2),
            "best":       ms["best"],
            "worst":      ms["worst"],
        }

    # ── Persistence ─────────────────────

    def _save(self) -> None:
        try:
            data = {
                'positions':      {s: asdict(p) for s, p in self.state.positions.items()},
                'mode_stats':     self.state.mode_stats,
                'equity_history': self.state.equity_history[-200:],
                'last_signal_id': self.state.last_signal_id,
                'auto_confirm':   self.state.auto_confirm,
                'current_mode':   self.state.current_mode,
                'monitor_ts':     self.state.monitor_ts,
            }
            with open(self._file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"State save error: {e}")

    def _load(self) -> None:
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file) as f:
                data = json.load(f)

            for symbol, p in data.get('positions', {}).items():
                p['side']   = PositionSide(p['side'])
                p['status'] = PositionStatus(p['status'])
                self.state.positions[symbol] = Position(**p)

            self.state.equity_history = data.get('equity_history', [])
            self.state.last_signal_id = data.get('last_signal_id', '')
            self.state.auto_confirm   = data.get('auto_confirm', False)
            self.state.current_mode   = data.get('current_mode', 'paper')
            self.state.monitor_ts     = data.get('monitor_ts', {})

            # Load per-mode stats; migrate legacy flat stats to "paper" if needed
            if 'mode_stats' in data:
                ms = data['mode_stats']
                for m in ('paper', 'trade'):
                    ms.setdefault(m, {"total": 0, "wins": 0, "rpnl": 0.0, "best": 0.0, "worst": 0.0})
                self.state.mode_stats = ms
            elif 'total_trades' in data:
                # Backward compat: migrate old global stats to "paper"
                self.state.mode_stats["paper"] = {
                    "total": data.get('total_trades', 0),
                    "wins":  data.get('winning_trades', 0),
                    "rpnl":  data.get('total_rpnl', 0.0),
                    "best":  data.get('best_trade', 0.0),
                    "worst": data.get('worst_trade', 0.0),
                }

            if self.state.positions:
                logger.info(f"📂 Restored {len(self.state.positions)} positions from state file")

        except Exception as e:
            logger.error(f"State load error: {e}")
