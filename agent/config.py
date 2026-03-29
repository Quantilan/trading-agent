# agent/config.py
"""
Agent configuration — reads .env file and validates settings.
"""

import os
import sys
import logging
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
# SUPPORTED EXCHANGES
# ─────────────────────────────────────────

SUPPORTED_EXCHANGES = {
    'binance':      'Binance USDⓈ-M Futures',
    'bybit':        'Bybit Linear Futures',
    'hyperliquid':  'Hyperliquid Perps',
    'okx':          'OKX Swap',
}


# ─────────────────────────────────────────
# CONFIG DATACLASS
# ─────────────────────────────────────────

@dataclass
class AgentConfig:

    # License
    license_key:        str   = ""

    # Signal server
    signal_server:      str   = ""
    signal_source:      str   = "server"  # server | telegram

    # Exchange
    exchange:           str   = "binance"
    api_key:            str   = ""
    api_secret:         str   = ""
    api_passphrase:     str   = ""      # OKX only
    wallet_address:     str   = ""      # Hyperliquid only
    stbc:               str   = ""      # USDT | USDC — auto-detected if empty

    # Risk management
    margin_pct:         float = 4.0     # % of balance per trade
    leverage:           int   = 5       # max leverage
    max_positions:      int   = 7       # max simultaneous positions

    # Trading mode
    mode:               str   = "paper" # paper | trade
    paper_balance:      float = 10000.0 # virtual balance for paper mode (USDT)

    # Telegram
    tg_token:           str   = ""
    tg_chat_id:         int   = 0

    # Signal parsing (forwarded messages / natural language)
    parser_mode:            str   = "regex"  # regex | llm
    confirm_trade:          bool  = True     # ask confirmation before executing parsed signal
    default_sl_pct:         float = 2.0      # default SL % when not specified in signal

    # LLM settings
    llm_provider:           str   = "none"   # none | claude
    llm_api_key:            str   = ""
    llm_model:              str   = ""       # auto if empty

    # Internal
    license_check_interval: int = 21600  # seconds (6 hours)
    reconnect_delay:        int = 5      # seconds between reconnects
    log_level:              str = "INFO"


# ─────────────────────────────────────────
# LOAD AND VALIDATE
# ─────────────────────────────────────────

def load_config() -> AgentConfig:
    """Load config from .env and validate required fields."""

    cfg = AgentConfig(
        license_key         = os.getenv("LICENSE_KEY", ""),
        signal_server       = os.getenv("SIGNAL_SERVER", ""),
        signal_source       = os.getenv("SIGNAL_SOURCE", "server").lower().strip(),
        exchange            = os.getenv("EXCHANGE", "binance").lower().strip(),
        api_key             = os.getenv("EXCHANGE_API_KEY", ""),
        api_secret          = os.getenv("EXCHANGE_SECRET", ""),
        api_passphrase      = os.getenv("EXCHANGE_PASSPHRASE", ""),  # OKX
        wallet_address      = os.getenv("EXCHANGE_WALLET_ADDRESS", ""),
        stbc                = os.getenv("EXCHANGE_STBC", "").upper().strip(),
        margin_pct          = float(os.getenv("MARGIN_PCT", "4.0")),
        leverage            = int(os.getenv("LEVERAGE", "5")),
        max_positions       = int(os.getenv("MAX_POSITIONS", "7")),
        mode                = os.getenv("MODE", "paper").lower().strip(),
        paper_balance       = float(os.getenv("PAPER_BALANCE", "10000") or "10000"),
        tg_token            = os.getenv("TG_TOKEN", ""),
        tg_chat_id          = int(os.getenv("TG_CHAT_ID", "0") or "0"),
        parser_mode             = os.getenv("PARSER_MODE", "regex").lower().strip(),
        confirm_trade           = os.getenv("CONFIRM_TRADE", "true").lower() != "false",
        default_sl_pct          = float(os.getenv("DEFAULT_SL_PCT", "2.0")),
        llm_provider            = os.getenv("LLM_PROVIDER", "none").lower().strip(),
        llm_api_key             = os.getenv("LLM_API_KEY", ""),
        llm_model               = os.getenv("LLM_MODEL", ""),
        log_level           = os.getenv("LOG_LEVEL", "INFO").upper(),
    )

    _validate(cfg)
    # Auto-detect stbc if not set explicitly
    if not cfg.stbc:
        cfg.stbc = "USDC" if cfg.exchange == "hyperliquid" else "USDT"
    return cfg


def _validate(cfg: AgentConfig) -> None:
    """Validate required fields. Exits with message on error."""

    errors = []

    if cfg.signal_source not in ("server", "telegram"):
        errors.append("SIGNAL_SOURCE must be 'server' or 'telegram'")

    if cfg.signal_source == "server":
        if not cfg.license_key:
            errors.append("LICENSE_KEY is required for SIGNAL_SOURCE=server")
        if not cfg.signal_server:
            errors.append("SIGNAL_SERVER is required for SIGNAL_SOURCE=server")

    if cfg.llm_provider not in ("none", "claude"):
        errors.append("LLM_PROVIDER must be 'none' or 'claude'")
    if cfg.llm_provider == "claude" and not cfg.llm_api_key:
        errors.append("LLM_API_KEY is required when LLM_PROVIDER=claude")

    if cfg.exchange not in SUPPORTED_EXCHANGES:
        errors.append(f"EXCHANGE must be one of: {', '.join(SUPPORTED_EXCHANGES)}")

    # Exchange credentials
    if cfg.exchange == 'hyperliquid':
        if not cfg.wallet_address:
            errors.append("EXCHANGE_WALLET_ADDRESS is required for Hyperliquid")
        if not cfg.api_secret:
            errors.append("EXCHANGE_SECRET (private key) is required for Hyperliquid")
    elif cfg.exchange == 'okx':
        if not cfg.api_key:
            errors.append("EXCHANGE_API_KEY is not set")
        if not cfg.api_secret:
            errors.append("EXCHANGE_SECRET is not set")
        if not cfg.api_passphrase:
            errors.append("EXCHANGE_PASSPHRASE is required for OKX")
    else:
        if not cfg.api_key:
            errors.append("EXCHANGE_API_KEY is not set")
        if not cfg.api_secret:
            errors.append("EXCHANGE_SECRET is not set")

    # Risk management
    if not (0.1 <= cfg.margin_pct <= 50.0):
        errors.append("MARGIN_PCT must be between 0.1 and 50.0")

    if not (1 <= cfg.leverage <= 20):
        errors.append("LEVERAGE must be between 1 and 20")

    if cfg.mode not in ("paper", "trade"):
        errors.append("MODE must be 'paper' or 'trade'")

    if errors:
        print("\n❌ Configuration errors:")
        for e in errors:
            print(f"   • {e}")
        print("\nCheck your .env file and restart the agent.\n")
        sys.exit(1)

    # Warnings
    if cfg.mode == "trade" and not cfg.tg_token:
        logger.warning("⚠️  TG_TOKEN not set — notifications disabled")

    if cfg.mode == "paper":
        logger.info("📋 PAPER mode — no real orders will be placed")
