import pytest
import sys
from unittest.mock import MagicMock


from agent.signal_parser.validator import (
    calculate_liq_price,
    validate_sl_logic,
    validate_modify_sl
)
from agent.state import Position, PositionSide

# 1. Test math
def test_liquidation_math():
    # Long 10x: 100 * (1 - 0.1) = 90
    assert calculate_liq_price("LONG", 100.0, 10) == 90.0
    # Short 10x: 100 * (1 + 0.1) = 110
    assert calculate_liq_price("SHORT", 100.0, 10) == 110.0
    # Long 1x (Spot): 100 * (1 - 1) = 0
    assert calculate_liq_price("LONG", 100.0, 1) == 0.0
    # Short 1x: 100 * (1 + 1) = 200
    assert calculate_liq_price("SHORT", 100.0, 1) == 200.0

# 2. Side test (Current Price)
def test_sl_vs_market_price():
    # LONG: 
    ok, _ = validate_sl_logic("LONG", 100.0, 95.0, 10, current_price=100.0)
    assert ok is True
    
    # LONG: 
    ok, msg = validate_sl_logic("LONG", 100.0, 105.0, 10, current_price=100.0)
    assert ok is False
    assert "below market" in msg

# 3. test liquidation buffer
def test_liquidation_buffer():
    # SL 92 — ОК
    ok, _ = validate_sl_logic("LONG", 100.0, 92.0, 10, current_price=98.0)
    assert ok is True
    
    # SL 90.5 — Error
    ok, msg = validate_sl_logic("LONG", 100.0, 90.5, 10, current_price=98.0)
    assert ok is False
    assert "too close to LIQ" in msg

# 4. test modify SL
def test_modify_sl_flexibility():
    
    pos = MagicMock(spec=Position)
    pos.side = MagicMock()
    pos.side.value = "LONG"
    pos.entry_price = 100.0
    pos.leverage = 10
    pos.stop_price = 95.0
    pos.last_price = 102.0

    # SL 92 — ОК
    ok, _ = validate_modify_sl(pos, new_sl=92.0)
    assert ok is True

# 5. test max SL
def test_max_sl_limit():
    # Long: Entry 100, SL 40  
    ok, msg = validate_sl_logic("LONG", 100.0, 40.0, 1, current_price=90.0)
    
    assert ok is False
    
    assert "more than" in msg.lower()
    assert "below entry" in msg.lower()

if __name__ == "__main__":
    pytest.main([__file__, "-v"])