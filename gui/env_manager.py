# gui/env_manager.py
"""
Read and write .env file for the trading agent.
Handles token validation and placeholder detection.
"""

import re
from pathlib import Path
from typing import Dict, Tuple

ENV_FILE    = Path(__file__).parent.parent / ".env"
EXAMPLE_FILE = Path(__file__).parent.parent / ".env.example"

# Tokens that indicate the value was not filled in
_PLACEHOLDERS = [
    "your-", "your_", "xxxx-xxxx", "xxxx", "0x...", "sk-ant-xxx",
    "your-api", "your-secret", "your-chat", "your-bot", "add_",
    "example", "changeme", "placeholder",
]

# All recognized .env keys with their defaults
ENV_DEFAULTS: Dict[str, str] = {
    "TG_TOKEN":               "",
    "TG_CHAT_ID":             "",
    "EXCHANGE":               "binance",
    "EXCHANGE_API_KEY":       "",
    "EXCHANGE_SECRET":        "",
    "EXCHANGE_STBC":          "",
    "EXCHANGE_PASSPHRASE":    "",
    "EXCHANGE_WALLET_ADDRESS": "",
    "MARGIN_PCT":             "4.0",
    "LEVERAGE":               "5",
    "MAX_POSITIONS":          "7",
    "MODE":                   "paper",
    "PAPER_BALANCE":          "10000",
    "LICENSE_KEY":            "",
    "SIGNAL_SOURCE":          "telegram",
    "SIGNAL_SERVER":          "wss://signals.quantilan.com",
    "PARSER_MODE":            "regex",
    "CONFIRM_TRADE":          "true",
    "DEFAULT_SL_PCT":         "2.0",
    "LLM_PROVIDER":           "none",
    "LLM_API_KEY":            "",
    "LLM_MODEL":              "",
    "LOG_LEVEL":              "INFO",  # kept for agent runtime, not shown in UI
    "CHART_TF":               "15m",
    "CHART_BARS":             "50",
    "ENTRY_TOLERANCE":        "0.1",  # % tolerance for deferred entry price zone
    "PENDING_ENTRY_TIMEOUT":  "24",   # hours before pending entry is cancelled
}


def is_placeholder(val: str) -> bool:
    """Return True if the value looks like an unfilled placeholder."""
    if not val or not val.strip():
        return True
    lower = val.lower().strip()
    return any(p in lower for p in _PLACEHOLDERS)


def validate_field(key: str, val: str) -> Tuple[str, str]:
    """
    Returns (status, hint) where status is:
      'ok'      — looks valid
      'warn'    — set but cannot fully validate (format unknown)
      'empty'   — not set
      'invalid' — set but clearly wrong format
    """
    if not val or not val.strip():
        return "empty", "Not set"

    if is_placeholder(val):
        return "invalid", "Contains placeholder text from .env.example"

    if key == "TG_TOKEN":
        if re.match(r'^\d{8,12}:[A-Za-z0-9_-]{35,}$', val):
            return "ok", "Valid Telegram bot token format"
        return "invalid", "Expected format: 123456789:ABCdef..."

    if key == "TG_CHAT_ID":
        if re.match(r'^-?\d{5,15}$', val):
            return "ok", "Valid chat ID"
        return "invalid", "Must be a numeric ID (from @userinfobot)"

    if key == "EXCHANGE_API_KEY":
        if len(val) >= 16 and ' ' not in val:
            return "ok", f"Set ({len(val)} chars)"
        return "invalid", "Too short or contains spaces"

    if key == "EXCHANGE_SECRET":
        if len(val) >= 16 and ' ' not in val:
            return "ok", f"Set ({len(val)} chars)"
        return "invalid", "Too short or contains spaces"

    if key == "EXCHANGE_WALLET_ADDRESS":
        if re.match(r'^0x[0-9a-fA-F]{40}$', val):
            return "ok", "Valid Ethereum address format"
        return "invalid", "Expected: 0x followed by 40 hex chars"

    if key == "LICENSE_KEY":
        if re.match(r'^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$', val):
            return "ok", "Valid license key format"
        return "warn", "Set (format not verified)"

    if key == "LLM_API_KEY":
        if val.startswith("sk-ant-") and len(val) > 20:
            return "ok", "Valid Anthropic API key format"
        return "warn", "Set (format not fully verified)"

    return "ok", "Set"


def read_env() -> Dict[str, str]:
    """
    Read .env file and return key→value dict.
    Strips inline comments. Returns defaults for missing keys.
    """
    result = dict(ENV_DEFAULTS)

    if not ENV_FILE.exists():
        return result

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, _, rest = line.partition("=")
        key = key.strip()
        # Strip inline comment — only if preceded by whitespace
        value = re.sub(r'\s+#.*$', '', rest).strip()
        # Strip surrounding quotes (single or double) — same as python-dotenv behaviour
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        if key in ENV_DEFAULTS:
            result[key] = value

    return result


def write_env(data: Dict[str, str]) -> None:
    """
    Write config dict to .env, preserving structure of .env.example.
    Unknown keys from data are appended at the end.
    """
    # Build key→value from submitted data, falling back to defaults
    values = dict(ENV_DEFAULTS)
    for k, v in data.items():
        if k in values:
            values[k] = v.strip() if v else ""

    lines = [
        "# ─────────────────────────────────────────────────────────",
        "#  TRADING AGENT — CONFIGURATION",
        "#  Generated by Quantilan Setup GUI",
        "# ─────────────────────────────────────────────────────────",
        "",
        "# ─── Telegram Bot ────────────────────────────────────────",
        f"TG_TOKEN={values['TG_TOKEN']}",
        f"TG_CHAT_ID={values['TG_CHAT_ID']}",
        "",
        "# ─── Exchange ─────────────────────────────────────────────",
        f"EXCHANGE={values['EXCHANGE']}",
        f"EXCHANGE_API_KEY={values['EXCHANGE_API_KEY']}",
        f"EXCHANGE_SECRET={values['EXCHANGE_SECRET']}",
    ]

    if values["EXCHANGE_STBC"]:
        lines.append(f"EXCHANGE_STBC={values['EXCHANGE_STBC']}")
    else:
        lines.append("# EXCHANGE_STBC=USDT  # auto-detected")

    if values["EXCHANGE_PASSPHRASE"]:
        lines.append(f"EXCHANGE_PASSPHRASE={values['EXCHANGE_PASSPHRASE']}")
    else:
        lines.append("# EXCHANGE_PASSPHRASE=  # OKX only")

    if values["EXCHANGE_WALLET_ADDRESS"]:
        lines.append(f"EXCHANGE_WALLET_ADDRESS={values['EXCHANGE_WALLET_ADDRESS']}")
    else:
        lines.append("# EXCHANGE_WALLET_ADDRESS=  # Hyperliquid only")

    lines += [
        "",
        "# ─── Risk management ──────────────────────────────────────",
        f"MARGIN_PCT={values['MARGIN_PCT']}",
        f"LEVERAGE={values['LEVERAGE']}",
        f"MAX_POSITIONS={values['MAX_POSITIONS']}",
        "",
        "# ─── Mode ─────────────────────────────────────────────────",
        f"MODE={values['MODE']}",
        f"PAPER_BALANCE={values['PAPER_BALANCE']}",
        "",
        "# ─── Signal source ────────────────────────────────────────",
        f"LICENSE_KEY={values['LICENSE_KEY']}",
        f"SIGNAL_SOURCE={values['SIGNAL_SOURCE']}",
        f"SIGNAL_SERVER={values['SIGNAL_SERVER']}",
        f"PARSER_MODE={values['PARSER_MODE']}",
        f"CONFIRM_TRADE={values['CONFIRM_TRADE']}",
        f"DEFAULT_SL_PCT={values['DEFAULT_SL_PCT']}",
        "",
        "# ─── LLM parser ────────────────────────────────────────────",
        f"LLM_PROVIDER={values['LLM_PROVIDER']}",
    ]

    if values["LLM_API_KEY"]:
        lines.append(f"LLM_API_KEY={values['LLM_API_KEY']}")
    else:
        lines.append("# LLM_API_KEY=sk-ant-...")

    if values["LLM_MODEL"]:
        lines.append(f"LLM_MODEL={values['LLM_MODEL']}")
    else:
        lines.append("# LLM_MODEL=  # auto")

    lines += [
        "",
        "# ─── Logging & chart ──────────────────────────────────────",
        f"LOG_LEVEL={values['LOG_LEVEL']}",
        f"CHART_TF={values['CHART_TF']}",
        f"CHART_BARS={values['CHART_BARS']}",
        "",
    ]

    ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
