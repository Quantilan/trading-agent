import pytest
from agent.signal_parser.regex_parser import RegexParser

@pytest.fixture
def parser():
    """Initializes the parser instance for testing."""
    return RegexParser()

def test_tp_ladder_extraction(parser):
    """Verifies parsing of multiple take-profit levels (ladder)."""
    text = "тейк-профит: 2.973,3.080,3.173"
    _, tp_list = parser._extract_tp(text)
    assert tp_list == [2.973, 3.08, 3.173]

def test_full_signal_long(parser):
    """Verifies full parsing of a LONG signal including emojis and hashtags."""
    raw_text = (
        "🚀 ВХОД В ЛОНГ #BTC\n"
        "Цена входа: 68000\n"
        "Стоп-лосс: 66500"
    )
    signal = parser.parse(raw_text)
    assert signal is not None
    assert signal.symbol == "BTC"
    assert signal.action == "LONG"
    assert signal.stop_price == 66500.0

def test_modify_sl_parsing(parser):
    """Verifies that MODIFY_SL correctly maps the new price to stop_price."""
    raw_text = "перенеси стоп ETH на 1750.5"
    signal = parser.parse(raw_text)
    assert signal is not None
    assert signal.action == "MODIFY_SL"
    assert signal.symbol == "ETH"
    # This was failing (0.0 != 1750.5) before the parse method fix
    assert signal.stop_price == 1750.5

def test_dirty_text_parsing(parser):
    """Checks if the parser handles messy real-world Telegram messages."""
    text = "Ребята, заходим в #SOL! Вход по рынку. стоп на 145.5, тейки 160.2, 170.5 и 185"
    signal = parser.parse(text)
    
    assert signal is not None
    assert signal.symbol == "SOL"
    assert signal.stop_price == 145.5
    # Verification of the TP ladder in the signal object
    assert signal.take_levels == [160.2, 170.5, 185.0]
    
if __name__ == "__main__":
    pytest.main([__file__])