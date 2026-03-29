# main.py
"""
Quantilan Trading Agent — entry point.

Starts two components in parallel:
  1. TradingAgent  — connects to signal server, executes trades
  2. PersonalBot   — user's personal Telegram bot for control

Personal bot is optional — only starts if TG_TOKEN is set in .env.
Signal server connection is optional — only if SIGNAL_SERVER is set.
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from agent.config import load_config, SUPPORTED_EXCHANGES
from agent.logger import setup_logging
from agent.main import TradingAgent
from agent.version import VERSION as __version__

_ROOT = Path(__file__).parent


# ── Pre-flight config check ───────────────────────────────────────────────────

def _check_setup() -> bool:
    """
    Validate .env before loading the full config.
    Prints a field-by-field status table and suggests the setup GUI if anything
    is missing or clearly wrong.
    Returns True if all required fields look valid, False otherwise.
    """
    env_file = _ROOT / ".env"

    if not env_file.exists():
        print()
        print("  ⚠️  No .env file found.")
        print()
        print("  Run the setup GUI to configure the agent:")
        print("      python setup_gui.py")
        print()
        return False

    # Read raw values (no dotenv side-effects yet)
    raw: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, rest = line.partition("=")
        val = re.sub(r'\s+#.*$', '', rest).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        raw[key.strip()] = val

    def get(key: str) -> str:
        return raw.get(key, "").strip()

    exchange = get("EXCHANGE") or "binance"
    issues: list[str] = []
    rows:   list[tuple[str, str, str]] = []   # (field, status_icon, hint)

    # ── Telegram ──────────────────────────────────────────────────────────────
    tg_token   = get("TG_TOKEN")
    tg_chat_id = get("TG_CHAT_ID")

    if re.match(r'^\d{8,12}:[A-Za-z0-9_-]{35,}$', tg_token):
        rows.append(("TG_TOKEN",   "✅", "valid format"))
    elif tg_token:
        rows.append(("TG_TOKEN",   "❌", "invalid format — expected 123456789:ABCdef..."))
        issues.append("TG_TOKEN")
    else:
        rows.append(("TG_TOKEN",   "❌", "not set"))
        issues.append("TG_TOKEN")

    if re.match(r'^-?\d{5,15}$', tg_chat_id):
        rows.append(("TG_CHAT_ID", "✅", "valid"))
    elif tg_chat_id:
        rows.append(("TG_CHAT_ID", "❌", "must be a numeric ID (get it from @userinfobot)"))
        issues.append("TG_CHAT_ID")
    else:
        rows.append(("TG_CHAT_ID", "❌", "not set"))
        issues.append("TG_CHAT_ID")

    # ── Exchange ──────────────────────────────────────────────────────────────
    if exchange in SUPPORTED_EXCHANGES:
        rows.append(("EXCHANGE", "✅", SUPPORTED_EXCHANGES[exchange]))
    else:
        rows.append(("EXCHANGE", "❌", f"unknown exchange '{exchange}'"))
        issues.append("EXCHANGE")

    def _cred(key: str, label: str, min_len: int = 16):
        val = get(key)
        if val and len(val) >= min_len and ' ' not in val:
            rows.append((key, "✅", f"set ({len(val)} chars)"))
        elif val:
            rows.append((key, "❌", "too short or contains spaces"))
            issues.append(key)
        else:
            rows.append((key, "❌", "not set"))
            issues.append(key)

    if exchange == "hyperliquid":
        wallet = get("EXCHANGE_WALLET_ADDRESS")
        if re.match(r'^0x[0-9a-fA-F]{40}$', wallet):
            rows.append(("EXCHANGE_WALLET_ADDRESS", "✅", "valid Ethereum address"))
        elif wallet:
            rows.append(("EXCHANGE_WALLET_ADDRESS", "❌", "expected 0x + 40 hex chars"))
            issues.append("EXCHANGE_WALLET_ADDRESS")
        else:
            rows.append(("EXCHANGE_WALLET_ADDRESS", "❌", "not set"))
            issues.append("EXCHANGE_WALLET_ADDRESS")
        _cred("EXCHANGE_SECRET", "private key")
    elif exchange == "okx":
        _cred("EXCHANGE_API_KEY",   "API key")
        _cred("EXCHANGE_SECRET",    "secret")
        passphrase = get("EXCHANGE_PASSPHRASE")
        if passphrase:
            rows.append(("EXCHANGE_PASSPHRASE", "✅", "set"))
        else:
            rows.append(("EXCHANGE_PASSPHRASE", "❌", "required for OKX"))
            issues.append("EXCHANGE_PASSPHRASE")
    else:
        _cred("EXCHANGE_API_KEY", "API key")
        _cred("EXCHANGE_SECRET",  "secret")

    # ── Print table ───────────────────────────────────────────────────────────
    col = max(len(r[0]) for r in rows) + 2
    print()
    print(f"  Quantilan Agent — configuration check")
    print(f"  {'─' * (col + 30)}")
    for field, icon, hint in rows:
        print(f"  {icon}  {field:<{col}} {hint}")
    print(f"  {'─' * (col + 30)}")

    if issues:
        print()
        print("  ⚠️  Some required fields are missing or invalid.")
        print("  Run the setup GUI to fix them:")
        print("      python setup_gui.py")
        print()
        return False

    print()
    return True


async def run():
    # ── Load config ───────────────────────────────────────
    config = load_config()
    setup_logging(level=config.log_level, log_file="agent")

    logger = logging.getLogger(__name__)
    logger.info(f"🚀 Quantilan Trading Agent v{__version__}")
    logger.info(f"   Exchange: {config.exchange.upper()}")
    logger.info(f"   Mode:     {config.mode.upper()}")

    # ── Create agent ──────────────────────────────────────
    agent = TradingAgent(config)

    # ── Create personal bot (optional) ───────────────────
    personal_bot = None
    if config.tg_token and config.tg_chat_id:
        from agent.personal_bot import PersonalBot
        personal_bot = PersonalBot(
            token         = config.tg_token,
            owner_chat_id = config.tg_chat_id,
            agent         = agent,
        )
        # Give agent a reference so it can send notifications
        agent.personal_bot = personal_bot
        logger.info(f"🤖 Personal bot enabled for chat_id {config.tg_chat_id}")
    else:
        logger.info("ℹ️  Personal bot disabled (TG_TOKEN / TG_CHAT_ID not set)")

    # ── Run everything ────────────────────────────────────
    tasks = [asyncio.create_task(agent.start(), name="agent")]

    if personal_bot:
        tasks.append(asyncio.create_task(personal_bot.run(), name="personal_bot"))

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("⛔ Stopped by user")
    except asyncio.CancelledError:
        pass
    except RuntimeError as e:
        logger.error(f"💔 Agent stopped: {e}")
    finally:
        # 1. Stop aiogram polling gracefully before cancelling its task
        if personal_bot:
            try:
                await personal_bot.dp.stop_polling()
            except Exception:
                pass

        # 2. Stop agent (disconnects exchange, stops monitor)
        try:
            await agent.stop()
        except Exception:
            pass

        # 3. Cancel any remaining tasks
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # 4. Close bot session
        if personal_bot:
            try:
                await personal_bot.bot.session.close()
            except Exception:
                pass

        # 5. Let connectors drain before event loop closes (avoids "Unclosed" warnings)
        await asyncio.sleep(0.3)
        logger.info("👋 Agent shutdown complete")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
