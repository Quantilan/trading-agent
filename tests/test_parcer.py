import pytest
from agent.signal_parser.regex_parser import RegexParser

@pytest.fixture
def parser():
    """Initializes the parser instance for testing."""
    return RegexParser()

def test_tp_ladder_extraction(parser):
    """Verifies parsing of multiple take-profit levels (ladder)."""
    text = "тейк-профіт: 2.973,3.080,3.173"
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
    
# ── Entry parsing tests ───────────────────────────────────────────────────────

def test_entry_single_price(parser):
    """Single entry price → entry_min == entry_max == price (point zone)."""
    text = "BTC long\nentry price: 68000\nstop-loss: 66500"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.entry_min == 68000.0
    assert signal.entry_max == 68000.0   # point zone: min == max
    assert signal.entry_type == "deferred"

def test_entry_range(parser):
    """Entry range → entry_min/entry_max set correctly (lower/upper)."""
    text = "заходжу в inj long\nточка входу: 2.927 - 2.702\nтейк-профіт: 2.973,3.080,3.173\nстоп-лосс: 2.608"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.symbol == "INJ"
    assert signal.action == "LONG"
    assert signal.entry_min == pytest.approx(2.702, abs=0.001)
    assert signal.entry_max == pytest.approx(2.927, abs=0.001)
    assert signal.entry_type == "deferred"
    assert signal.take_levels == [2.973, 3.08, 3.173]
    assert signal.stop_price == pytest.approx(2.608, abs=0.001)

def test_entry_range_dash_variants(parser):
    """Entry range with em-dash separator."""
    text = "ETH SHORT\nentry: 2100 – 2050\nstop-loss: 2200"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.entry_min == pytest.approx(2050.0)
    assert signal.entry_max == pytest.approx(2100.0)

def test_entry_keyword_enter(parser):
    """'enter:' keyword."""
    text = "SOL LONG enter: 145.5 stop-loss: 135"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.entry_min == pytest.approx(145.5)
    assert signal.entry_type == "deferred"

def test_entry_keyword_vkhod(parser):
    """Russian 'вход' keyword."""
    text = "BTC лонг вход: 65000 стоп на 63000"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.entry_min == pytest.approx(65000.0)
    assert signal.entry_type == "deferred"

def test_no_entry_gives_market(parser):
    """No entry price → entry_type market, entry_min == 0."""
    text = "ETH long стоп на 1800"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.entry_min == 0.0
    assert signal.entry_type == "market"

def test_entry_mid_used_as_entry(parser):
    """For a range, signal.entry should be the midpoint."""
    text = "BTC LONG entry: 68000 - 70000 stop-loss: 65000"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.entry == pytest.approx(69000.0)


# ── sl/tp shorthand parsing ───────────────────────────────────────────────────

def test_sl_shorthand_pct(parser):
    """'sl: 1%' shorthand must parse to 1%, not fall back to default 2%."""
    text = "long xrp sl: 1% tp: 3%"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.symbol == "XRP"
    assert signal.action == "LONG"
    assert signal.sl_pct == pytest.approx(0.01)
    assert signal.tp_pct == pytest.approx(0.03)

def test_sl_shorthand_no_space(parser):
    """'sl:1%' without space still parses correctly."""
    text = "short btc sl:2% tp:4%"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.action == "SHORT"
    assert signal.sl_pct == pytest.approx(0.02)
    assert signal.tp_pct == pytest.approx(0.04)

def test_sl_shorthand_space_no_colon(parser):
    """'sl 1.5%' (space, no colon) parses correctly."""
    text = "long eth sl 1.5% tp 5%"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.sl_pct == pytest.approx(0.015)
    assert signal.tp_pct == pytest.approx(0.05)


if __name__ == "__main__":
    pytest.main([__file__])