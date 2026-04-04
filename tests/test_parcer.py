import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent.signal_parser.regex_parser import RegexParser
from agent.llm_parser import LLMParser

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


# ── Additional regex tests ────────────────────────────────────────────────────

def test_flat_close_signal(parser):
    """'close btc' produces a FLAT signal."""
    signal = parser.parse("close btc")
    assert signal is not None
    assert signal.action == "FLAT"
    assert signal.symbol == "BTC"


def test_modify_tp_parsing(parser):
    """MODIFY_TP correctly maps price to take_price."""
    signal = parser.parse("тейк на ETH 2100")
    assert signal is not None
    assert signal.action == "MODIFY_TP"
    assert signal.symbol == "ETH"
    assert signal.take_price == pytest.approx(2100.0)


def test_hashtag_symbol(parser):
    """Symbol with # prefix is parsed correctly."""
    signal = parser.parse("🔥 SHORT #DOGE стоп 0.16")
    assert signal is not None
    assert signal.symbol == "DOGE"
    assert signal.action == "SHORT"
    assert signal.stop_price == pytest.approx(0.16)


def test_ukrainian_long_signal(parser):
    """Full Ukrainian-language LONG signal parses correctly."""
    text = "відкривай лонг #SOL\nвхід по ринку\nстоп на 145"
    signal = parser.parse(text)
    assert signal is not None
    assert signal.symbol == "SOL"
    assert signal.action == "LONG"
    assert signal.stop_price == pytest.approx(145.0)


def test_cyrillic_alias_bitcoin(parser):
    """'биток' alias resolves to BTC."""
    signal = parser.parse("лонг биток стоп 60000")
    assert signal is not None
    assert signal.symbol == "BTC"


def test_tp_only_no_sl(parser):
    """TP present but no SL — stop_price stays 0."""
    signal = parser.parse("long ETH тейк 2500")
    assert signal is not None
    assert signal.take_price == pytest.approx(2500.0)
    assert signal.stop_price == 0.0


def test_non_signal_returns_none(parser):
    """Random non-trading text returns None."""
    signal = parser.parse("Привет всем! Как дела?")
    assert signal is None


def test_short_signal_english(parser):
    """Plain English short signal."""
    signal = parser.parse("sell BNB stop-loss 550")
    assert signal is not None
    assert signal.action == "SHORT"
    assert signal.symbol == "BNB"
    assert signal.stop_price == pytest.approx(550.0)


def test_sl_tp_pct_only_sl(parser):
    """Only SL% shorthand, no TP%."""
    signal = parser.parse("long link sl: 2%")
    assert signal is not None
    assert signal.symbol == "LINK"
    assert signal.sl_pct == pytest.approx(0.02)
    assert signal.tp_pct == 0.0


# ── LLM parser tests (mocked, no real API calls) ─────────────────────────────

_SAMPLE_JSON = {
    "action":      "LONG",
    "symbol":      "ETH",
    "entry_type":  "market",
    "entry_price": 3200.0,
    "stop_price":  3100.0,
    "take_price":  3400.0,
    "take_levels": [3400.0, 3600.0],
    "confidence":  0.92,
}


def _make_claude_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    return resp


def _make_groq_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    return resp


@pytest.mark.asyncio
async def test_llm_claude_parses_long():
    """LLMParser with claude provider returns correct Signal from mocked response."""
    parser = LLMParser(provider="claude", api_key="fake-key")
    with patch("anthropic.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(return_value=_make_claude_response(_SAMPLE_JSON))
        signal = await parser.parse("buy eth long, entry 3200, sl 3100, tp 3400/3600")

    assert signal is not None
    assert signal.symbol == "ETH"
    assert signal.action == "LONG"
    assert signal.entry == pytest.approx(3200.0)
    assert signal.stop_price == pytest.approx(3100.0)
    assert signal.take_levels == [3400.0, 3600.0]


@pytest.mark.asyncio
async def test_llm_groq_parses_long():
    """LLMParser with groq provider returns correct Signal from mocked response."""
    parser = LLMParser(provider="groq", api_key="fake-key")
    with patch("groq.AsyncGroq") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(
            return_value=_make_groq_response(_SAMPLE_JSON)
        )
        signal = await parser.parse("buy eth long, entry 3200, sl 3100, tp 3400")

    assert signal is not None
    assert signal.symbol == "ETH"
    assert signal.action == "LONG"
    assert signal.stop_price == pytest.approx(3100.0)


@pytest.mark.asyncio
async def test_llm_low_confidence_returns_none():
    """Signal with confidence < 0.65 is discarded."""
    parser = LLMParser(provider="claude", api_key="fake-key")
    payload = {**_SAMPLE_JSON, "confidence": 0.4}
    with patch("anthropic.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(return_value=_make_claude_response(payload))
        signal = await parser.parse("some text")

    assert signal is None


@pytest.mark.asyncio
async def test_llm_null_action_returns_none():
    """action=null (non-signal message) returns None."""
    parser = LLMParser(provider="claude", api_key="fake-key")
    payload = {**_SAMPLE_JSON, "action": None}
    with patch("anthropic.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(return_value=_make_claude_response(payload))
        signal = await parser.parse("have you seen the BTC price lately?")

    assert signal is None


@pytest.mark.asyncio
async def test_llm_bad_json_returns_none():
    """Malformed JSON from LLM is handled gracefully."""
    parser = LLMParser(provider="claude", api_key="fake-key")
    bad_resp = MagicMock()
    bad_resp.content = [MagicMock(text="not valid json at all")]
    with patch("anthropic.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(return_value=bad_resp)
        signal = await parser.parse("some text")

    assert signal is None


@pytest.mark.asyncio
async def test_llm_groq_strips_markdown_fences():
    """Groq response wrapped in ```json ... ``` fences is parsed correctly."""
    parser = LLMParser(provider="groq", api_key="fake-key")
    fenced = f"```json\n{json.dumps(_SAMPLE_JSON)}\n```"
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=fenced))]
    with patch("groq.AsyncGroq") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=resp)
        signal = await parser.parse("eth long sl 3100 tp 3400")

    assert signal is not None
    assert signal.symbol == "ETH"


@pytest.mark.asyncio
async def test_llm_provider_none_returns_none():
    """Provider 'none' always returns None without any API call."""
    parser = LLMParser(provider="none", api_key="")
    signal = await parser.parse("long btc sl 60000")
    assert signal is None


if __name__ == "__main__":
    pytest.main([__file__])