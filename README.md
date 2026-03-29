# trading-agent

**Autonomous Crypto Trading Agent That Runs on Your Infrastructure**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Status](https://img.shields.io/badge/status-active%20development-success)](#)

> ⚡ **First stable version will be ready in May 2026**

---

## Overview

Quantilan Trading Agent is an autonomous cryptocurrency trading bot that runs on **your** infrastructure (VPS, Mac Mini, home server). You maintain full control of your API keys and funds at all times.

### Key Features

- 🔐 **Self-Custody** — API keys stored only on your device, no withdrawal permission required
- ⚡ **24/7 Trading** — works continuously on your own infrastructure
- 🎯 **Multi-Exchange** — Binance, Bybit, Hyperliquid, OKX
- 📊 **Risk Management** — position sizing, stop-loss, take-profit, max positions limit
- 💬 **Personal Bot Control** — manage via your own Telegram bot: positions, P&L, equity chart
- 🔍 **Signal Parsing** — forward any Telegram channel to your bot or just write in plain language
- 🔧 **Open Source** — full transparency, audit the code yourself

---

## How It Works

```
┌────────────────────────────────────────────────────┐
│              QUANTILAN SIGNAL SERVER               │
│   Trading strategies → Signal broadcaster          │
│   (verified track record, HMAC-signed signals)     │
└─────────────────────┬──────────────────────────────┘
                      │  WebSocket (WSS)
                      ▼
┌────────────────────────────────────────────────────┐
│           YOUR DEVICE (VPS / Mac Mini)             │
│                                                    │
│  ┌──────────────────────────────────────────────┐  │
│  │              Trading Agent                   │  │
│  │                                              │  │
│  │  signal_client  ──►  risk_manager            │  │
│  │  personal_bot   ──►  signal_parser           │  │
│  │                           │                  │  │
│  │                      order_executor          │  │
│  │                           │                  │  │
│  │                      state_manager           │  │
│  └───────────────────────────┼──────────────────┘  │
│                              │ ccxt REST API        │
└──────────────────────────────┼─────────────────────┘
                               ▼
┌────────────────────────────────────────────────────┐
│              YOUR EXCHANGE ACCOUNT                 │
│   Binance  │  Bybit  │  Hyperliquid  │  OKX        │
│                                                    │
│   API keys stored ONLY on your device              │
│   No withdrawal permission required               │
└────────────────────────────────────────────────────┘
```

**Two signal modes in one agent:**

| Mode | How |
|------|-----|
| **Quantilan Server** | receive signed signals via WSS from our strategies |
| **Personal Bot** | forward any Telegram channel to your bot, or write commands in plain language |

---

## Personal Bot Commands

The agent runs your own Telegram bot (created via @BotFather). It gives you full control over the agent and allows natural-language trading.

**Status & monitoring**
```
/start        — agent status, balance, open positions count
/positions    — open positions with unrealized P&L
/pnl          — P&L statistics: wins, losses, win rate, best/worst trade
/equity       — equity curve chart
```

**Trading control**
```
/stop         — pause trading (keep positions open with stops active)
/resume       — resume trading
/close_all    — close all positions (with confirmation)
/mode         — switch between paper and trade mode (with confirmation)
```

**Signal parsing**
```
/autoconfirm  — toggle auto-execute for parsed signals (on/off)
```

Just write or forward a message to your bot:
```
открой эфир лонг
buy sol short sl 2% tp 5%
закрой биток
стоп на 1800          ← move stop-loss
тейк на 3500          ← move take-profit
```

When **auto-confirm is OFF** (default), the bot shows a preview with Execute / Cancel buttons.
When **auto-confirm is ON**, signals execute immediately — useful when forwarding channels.

**Telegram Premium** users can set up message forwarding from any signal channel directly to the bot — no Telethon, no phone auth, no ban risk.

---

## Signal Parser

Two parser modes, configurable via `.env`:

| Mode | Description |
|------|-------------|
| `regex` (default) | Rule-based, uses `agent/signal_parser/patterns.json`. Free. Edit patterns to add your own aliases. |
| `llm` | Claude API. Understands any message format. Requires `LLM_API_KEY`. |

`patterns.json` covers top 30 coins with RU/EN aliases. Supported actions:

| Action | Examples |
|--------|---------|
| **open** | `лонг`, `long`, `buy`, `открой`, `enter` |
| **close** | `закрой`, `фикс`, `close`, `exit` |
| **modify_sl** | `стоп на 1800`, `перенеси стоп`, `sl=1750` |
| **modify_tp** | `тейк на 3500`, `тейк на`, `tp=3500` |

**Validation rules (enforced before execution):**
- LONG: SL must be below entry, TP must be above entry
- SHORT: SL must be above entry, TP must be below entry
- Modify SL: can only trail toward the position (up for LONG, down for SHORT)

---

## Requirements

- Python 3.10+
- VPS / Mac Mini / Home Server (Linux, macOS, Windows)
- Exchange account (Binance / Bybit / Hyperliquid / OKX)
- Telegram bot token (from @BotFather)
- License key from [@quantilan_bot](https://t.me/quantilan_bot) — required for Quantilan strategies

---

## Installation

```bash
git clone https://github.com/Quantilan/trading-agent.git
cd trading-agent

pip install -r requirements.txt

cp .env.example .env
# Edit .env — exchange keys, license key, bot token, risk settings

python main.py
```

---

## Configuration

Key settings in `.env`:

```env
# Exchange
EXCHANGE=binance          # binance | bybit | hyperliquid | okx
EXCHANGE_API_KEY=...
EXCHANGE_SECRET=...

# Risk management
MARGIN_PCT=4.0            # % of free balance per trade
LEVERAGE=5
MAX_POSITIONS=7           # max simultaneous open positions

# Signals
LICENSE_KEY=XXXX-XXXX-XXXX-XXXX
SIGNAL_SERVER=wss://signals.quantilan.com

# Personal bot
TG_TOKEN=...              # your bot token from @BotFather
TG_CHAT_ID=...            # your Telegram ID from @userinfobot

# Signal parsing (optional)
PARSER_MODE=regex         # regex | llm
CONFIRM_TRADE=true
DEFAULT_SL_PCT=2.0

# LLM parser (optional)
# LLM_PROVIDER=claude
# LLM_API_KEY=sk-ant-...

MODE=paper                # paper (test) | trade (live)
```

---

## Supported Exchanges

| Exchange    | Type            | Status |
|-------------|-----------------|--------|
| Binance     | USDⓈ-M Futures | ✅ |
| Bybit       | Linear Futures  | ✅ |
| Hyperliquid | Perps           | ✅ |
| OKX         | Swap            | ✅ |

---

## Project Structure

```
trading-agent/
├── agent/                 # Core Trading Logic
│   ├── signal_parser/     # Regex & Validation rules
│   ├── config.py          # Configuration loader
│   ├── main.py            # Main Orchestrator
│   ├── order_executor.py  # Exchange interaction (CCXT)
│   ├── personal_bot.py    # Telegram interface & Signal listener
│   ├── risk_manager.py    # Position sizing & SL/TP logic
│   └── state.py           # JSON Persistence (positions, P&L)
│
├── gui/                   # Web-based Configuration UI
│   ├── static/            # CSS/JS assets
│   ├── templates/         # HTML (index.html)
│   ├── app.py             # FastAPI Backend (REST API + SSE)
│   └── env_manager.py     # Safe .env reading/writing
│
├── tests/                 # Integration tests
│   └── test_agent.py      # Full exchange cycle test
│
├── main.py                # Agent CLI Entry point
├── setup_gui.py           # GUI Launcher (Uvicorn wrapper)
├── requirements.txt       # Dependencies
└── .env.example           # Template for settings
```
---

## Testing

```bash
# Paper mode — no real orders
python -m tests.test_agent --exchange binance --symbol BTC --mode paper

# Live mode — real orders, use a small position
python -m tests.test_agent --exchange bybit --symbol ETH --mode trade
```

---

## Subscription Plans

License key required only if you want to use **Quantilan strategies** (signals from our server).
The agent itself is open source and free to use with your own signal sources.

| Plan | Signals | Exchanges |
|------|---------|-----------|
| 🌱 Start | Custom (any Telegram channel via personal bot) | All |
| ⚡ Basic | Quantilan strategies | All |
| 🚀 Pro   | Quantilan strategies + priority access | All |

Subscribe via [@quantilan_bot](https://t.me/quantilan_bot)

---

## Roadmap

- [x] Multi-exchange execution (Binance, Bybit, Hyperliquid, OKX)
- [x] Personal Telegram bot with P&L, equity chart
- [x] Signal parsing — regex + LLM (Claude)
- [x] Forwarded message support (Telegram Premium)
- [x] SL/TP validation and trailing stop protection
- [ ] GUI utility (ships with the agent — no separate install)
- [ ] Docker Compose deployment
- [ ] Multi-agent support (run strategies on multiple exchanges simultaneously)

---

## Security

- ✅ Your API keys are stored **only on your device**
- ✅ Agent runs **on your infrastructure**
- ✅ We have **zero access** to your funds
- ✅ **No withdrawal permission** required
- ✅ All signals from Quantilan server are HMAC-signed and verified
- ✅ You can stop the agent or manage positions **directly on the exchange** at any time

---

## Risk Disclaimer

Trading crypto futures involves significant risk of loss. Past performance does not guarantee future results. All trading signals are for informational purposes only and do not constitute financial advice. You are solely responsible for your trading decisions.

Use `/disclaimer` in [@quantilan_bot](https://t.me/quantilan_bot) to read the full risk disclosure.

---

## Support

Telegram: [@quantilan_bot](https://t.me/quantilan_bot)
