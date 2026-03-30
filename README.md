# Quantilan Trading Agent

**Autonomous Crypto Trading Agent That Runs on Your Infrastructure**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Status](https://img.shields.io/badge/status-active%20development-success)](#)

> ⚡ **First stable version will be ready in May 2026**

---

## Overview

Quantilan Trading Agent is an autonomous cryptocurrency trading bot that runs on **your** infrastructure (VPS, Mac Mini, home server). You maintain full control of your API keys and funds at all times.

### Key Features

- 🔐 **Self-Custody** — API keys stored only on your device, no withdrawal permission required
- ⚡ **24/7 Trading** — works continuously on your own infrastructure
- 🎛️ **Setup GUI** — browser-based configuration, connection tests, live agent logs
- 🎯 **Multi-Exchange** — Binance, Bybit, Hyperliquid, OKX
- 📊 **Risk Management** — position sizing, stop-loss, take-profit, max positions limit
- 💬 **Personal Bot Control** — manage via your own Telegram bot: positions, P&L, equity chart
- 🔍 **Signal Parsing** — forward any Telegram channel to your bot or write commands in plain language
- 🐳 **Docker Compose** — one-command deployment

---

## How It Works

```
┌────────────────────────────────────────────────────┐
│              QUANTILAN SIGNAL SERVER               │
│   Trading strategies → Signal broadcaster          │
│   (verified track record, HMAC-signed signals)     │
└─────────────────────┬──────────────────────────────┘
                      │  WebSocket (wss://signals.quantilan.com)
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
│   API keys stored ONLY on your device              │
└────────────────────────────────────────────────────┘
```

**Two signal modes:**

| Mode | How |
|------|-----|
| **Quantilan Server** | receive HMAC-signed signals via WSS from our strategies |
| **Telegram** | forward any channel to your bot, or type commands in plain language |

---

## Quick Start

### Option A — Docker (recommended for VPS)

```bash
git clone https://github.com/Quantilan/trading-agent.git
cd trading-agent

make setup      # creates .env from template
make gui        # open http://your-server:8080 to configure
make start      # launch agent in background
make logs       # watch live output
```

### Option B — Python directly

```bash
git clone https://github.com/Quantilan/trading-agent.git
cd trading-agent

pip install -r requirements.txt

python setup_gui.py   # open http://localhost:8080 to configure
# — or edit .env manually —
python main.py
```

---

## Setup GUI

The agent ships with a browser-based setup tool. No separate installation needed.

```bash
# Docker
make gui

# Python
python setup_gui.py [--port 8080] [--no-browser]
```

Open **http://localhost:8080** (or your VPS IP).

### Left column — Configuration

| Section | What to fill in |
|---------|----------------|
| **Exchange & Credentials** | Exchange, API key/secret, trading mode (paper/live), virtual balance |
| **Risk Management** | Margin per trade %, leverage, max positions, default SL % |
| **Telegram Bot** | Bot token from @BotFather, your Chat ID from @userinfobot |
| **Signal & Notifications** | Signal source (Quantilan Server / Telegram), license key, parser mode |

### Right column — Tests & Status

| Panel | What it shows |
|-------|--------------|
| **Connection Test** | Exchange connectivity, account balance, position mode |
| **Exchange Order Test** | Full cycle: open → modify SL → close (paper or live) |
| **Telegram Bot Test** | Bot name, connection status, chat ID verification |
| **Signal & License** | License validity, plan name, expiry date |
| **Agent Logs** | Live streaming log output from the running agent |

### Readiness checklist

Before starting the agent, the bottom of the left column shows which tests have passed:

```
✓ Exchange connection
✓ Order placement test
✓ Telegram bot
✓ Signal server & license   ← shown only in Quantilan Server mode
```

Clicking **Start Agent** with unchecked items shows a warning with specific recommendations for each missing test. You can still start anyway.

---

## Docker Deployment

### Prerequisites

- Docker + Docker Compose installed
- Port 8080 open for GUI (can be closed after setup)

### Commands

```bash
make setup      # first-time init: .env + state files
make gui        # start Setup GUI at :8080, stop with Ctrl+C when done
make start      # start agent in background (auto-restart on crash)
make stop       # stop agent
make restart    # restart agent (reloads .env)
make logs       # tail live logs
make status     # show container status
make build      # rebuild image after code changes
make clean      # remove containers and image
```

### Data persistence

All persistent data lives on the host as bind mounts — safe across image rebuilds:

| Host file | Container path | Purpose |
|-----------|---------------|---------|
| `.env` | `/app/.env` | Configuration |
| `logs/` | `/app/logs/` | Log files |
| `agent_state.json` | `/app/agent_state.json` | Open positions, P&L history |

---

## Configuration Reference

All settings are in `.env` (edit manually or via Setup GUI):

```env
# ── Exchange ───────────────────────────────────────────────
EXCHANGE=binance          # binance | bybit | hyperliquid | okx
EXCHANGE_API_KEY=...
EXCHANGE_SECRET=...
# EXCHANGE_PASSPHRASE=    # OKX only
# EXCHANGE_WALLET_ADDRESS=  # Hyperliquid only

# ── Risk management ────────────────────────────────────────
MARGIN_PCT=4.0            # % of free balance per trade
LEVERAGE=5
MAX_POSITIONS=7
DEFAULT_SL_PCT=2.0

# ── Trading mode ───────────────────────────────────────────
MODE=paper                # paper (simulated) | trade (live)
PAPER_BALANCE=10000       # virtual balance for paper mode

# ── Telegram bot ───────────────────────────────────────────
TG_TOKEN=...              # from @BotFather
TG_CHAT_ID=...            # from @userinfobot

# ── Signal source ──────────────────────────────────────────
SIGNAL_SOURCE=server      # server | telegram
LICENSE_KEY=XXXX-XXXX-XXXX-XXXX
SIGNAL_SERVER=wss://signals.quantilan.com

# ── Signal parsing (telegram mode) ────────────────────────
PARSER_MODE=regex         # regex | llm
CONFIRM_TRADE=true
# LLM_PROVIDER=claude
# LLM_API_KEY=sk-ant-...
```

---

## Personal Bot Commands

```
/start        — agent status, balance, open positions count
/positions    — open positions with unrealized P&L
/pnl          — P&L statistics: wins, losses, win rate
/equity       — equity curve chart
/stop         — pause trading (keep positions open)
/resume       — resume trading
/close_all    — close all positions (with confirmation)
/mode         — switch paper ↔ trade (with confirmation)
/autoconfirm  — toggle auto-execute for parsed signals
```

Forward any signal channel message or just type in plain language:

```
buy eth long
sol short sl 2% tp 5%
close btc
stop at 1800
take at 3500
```

---

## Signal Parser

| Mode | Description |
|------|-------------|
| `regex` | Rule-based, covers top 30 coins RU/EN. Fast, free. Edit `agent/signal_parser/patterns.json` to add aliases. |
| `llm` | Claude (Anthropic). Understands any format. Requires `LLM_API_KEY`. |

Supported actions: **LONG / SHORT / FLAT / MODIFY_SL / MODIFY_TP**

---

## Supported Exchanges

| Exchange    | Type            | Credentials needed |
|-------------|-----------------|-------------------|
| Binance     | USDⓈ-M Futures | API Key + Secret |
| Bybit       | Linear Futures  | API Key + Secret |
| Hyperliquid | Perps           | Wallet Address + Private Key + Agent Address (optional) |
| OKX         | Swap            | API Key + Secret + Passphrase |

---

## Project Structure

```
trading-agent/
├── agent/
│   ├── signal_parser/     # Regex patterns & validation rules
│   ├── config.py          # Config loader (.env → AgentConfig)
│   ├── daily_secret.py    # Rotating HMAC key from license server
│   ├── license.py         # License validation + device binding
│   ├── main.py            # Agent orchestrator
│   ├── order_executor.py  # Exchange interaction (ccxt)
│   ├── personal_bot.py    # Telegram control bot
│   ├── risk_manager.py    # Position sizing, SL/TP logic
│   ├── signal_client.py   # WebSocket signal receiver
│   └── state.py           # JSON state persistence
│
├── gui/
│   ├── templates/
│   │   └── index.html     # Single-page setup UI
│   ├── app.py             # FastAPI backend (REST + SSE log stream)
│   └── env_manager.py     # Safe .env read/write
│
├── tests/
│   └── test_agent.py      # Full exchange cycle integration test
│
├── Dockerfile             # Python 3.13-slim image
├── docker-compose.yml     # gui + agent services
├── Makefile               # Deployment shortcuts
├── main.py                # Agent entry point
├── setup_gui.py           # GUI launcher
├── requirements.txt       # Python dependencies
└── .env.example           # Configuration template
```

---

## Subscription Plans

License key required only for **Quantilan strategies**. The agent is open source and free to use with your own signal sources.

| Plan | Signals |
|------|---------|
| 🌱 Start | Your own Telegram channels via personal bot |
| ⚡ Pro | Start + 1 Quantilan strategy (any) |

Get a license key: [@quantilan_bot](https://t.me/quantilan_bot)

---

## Roadmap

- [x] Multi-exchange execution (Binance, Bybit, Hyperliquid, OKX)
- [x] Personal Telegram bot with P&L, equity chart
- [x] Signal parsing — regex + LLM (Claude)
- [x] Forwarded message support (Telegram Premium)
- [x] SL/TP validation and trailing stop protection
- [x] Browser-based Setup GUI with live connection tests
- [x] Docker Compose deployment
- [ ] Strategy marketplace

---

## Security

- ✅ Your API keys are stored **only on your device**
- ✅ Agent runs **on your infrastructure**
- ✅ We have **zero access** to your funds
- ✅ **No withdrawal permission** required
- ✅ All signals from Quantilan server are HMAC-signed and verified locally
- ✅ License is bound to your device fingerprint — keys cannot be shared

---

## Risk Disclaimer

Trading crypto futures involves significant risk of loss. Past performance does not guarantee future results. All trading signals are for informational purposes only and do not constitute financial advice. You are solely responsible for your trading decisions.

---

## Support

Telegram: [@quantilan_support_bot](https://t.me/quantilan_support_bot)
