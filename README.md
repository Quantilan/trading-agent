# Quantilan Trading Agent

**Autonomous Crypto Trading Agent That Runs on Your Infrastructure**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Version](https://img.shields.io/badge/version-1.1.0-success)](#)
[![License: BUSL-1.1](https://img.shields.io/badge/license-BUSL--1.1-orange.svg)](LICENSE)

> ⚡ **v1.1.0** — trailing stop, default TP, GUI improvements, Docker fixes

---

## ⚡ Quick Start

### 🪟 Quick Start on Windows

1. Download ZIP:  
   👉 https://github.com/Quantilan/trading-agent/archive/refs/heads/main.zip

2. Unzip the `trading-agent` folder

3. Double-click `start.bat`

✅ Done! Dependencies install automatically on the first run, then the browser opens at **http://localhost:8080** — configure and start the agent.

### 🐧 Mac / Linux (make)

> `make` comes pre-installed on macOS and most Linux distros. Not available on Windows by default — use `start.bat` instead.

```bash
git clone https://github.com/Quantilan/trading-agent.git
cd trading-agent
make setup   # create .env and state files
make gui     # start Setup GUI — configure and launch agent from browser
```

Open **http://localhost:8080** — configure, test connections, start the agent. That's it.

```bash
make start   # run agent in background (detached)
make logs    # tail live logs
make stop    # stop agent
```
---

## Overview

Quantilan Trading Agent is an autonomous cryptocurrency trading bot that runs on **your** infrastructure (VPS, Mac Mini, home server). You maintain full control of your API keys and funds at all times.

### Key Features

- 🔐 **Self-Custody** — API keys stored only on your device, no withdrawal permission required
- ⚡ **24/7 Trading** — works continuously on your own infrastructure
- 🎛️ **Setup GUI** — browser-based configuration, connection tests, live agent logs
- 🎯 **Multi-Exchange** — Binance, Bybit, Hyperliquid, OKX
- 📊 **Risk Management** — position sizing, stop-loss, ladder take-profit, max positions limit
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
│                              │ ccxt REST API       │
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

### 🖥 VPS / Linux Server (recommended)

**Step 1 — install on the server** (fresh Ubuntu/Debian):

```bash
curl -fsSL https://raw.githubusercontent.com/Quantilan/trading-agent/main/install.sh | bash
```

This installs Docker, clones the repo, builds the image, and prints the next steps.

**Step 2 — open the GUI via SSH tunnel** (on your local machine):

```bash
ssh -L 8080:localhost:8080 user@your-server-ip
```

No firewall port opening needed — the tunnel forwards the GUI securely.  
Not sure of the exact command? Run `make tunnel` on the server.

**Step 3 — configure in your browser:**

```
http://localhost:8080
```

Fill in exchange credentials, Telegram bot, signal source — then click **Start Agent**.

The agent starts immediately in the background. You can close the browser and the GUI — the agent keeps running.

**Optional — manage from terminal:**

```bash
make logs     # watch live output
make stop     # stop agent
make restart  # restart after .env change
```

---

### 🐍 Python directly (local / dev)

```bash
git clone https://github.com/Quantilan/trading-agent.git
cd trading-agent

pip install -r requirements.txt

python setup_gui.py   # opens http://localhost:8080 automatically
# — or edit .env manually, then:
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

### Configuration

| Section | What to fill in |
|---------|----------------|
| **Exchange & Credentials** | Exchange, API key/secret, trading mode (paper/live), virtual balance |
| **Risk Management** | Margin per trade %, leverage, max positions, default SL % |
| **Telegram Bot** | Bot token from @BotFather, your Chat ID from @userinfobot |
| **Signal & Notifications** | Signal source (Quantilan Server / Telegram), license key, parser mode |
| **Chart & Advanced** *(collapsed)* | Chart timeframe (5m/15m/1h/4h) and number of candles (25/50/75/100) for Telegram chart notifications |

### Tests & Status

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

## Updating

```bash
cd ~/trading-agent
git pull
make build
make restart
```

That's it — pulls latest code, rebuilds the image, restarts the agent.

---

## Docker Deployment

### Prerequisites

- Docker + Docker Compose installed  
  *(Ubuntu/Debian: `curl -fsSL https://get.docker.com | sh`)*

### Commands

```bash
make setup      # first-time init: .env + state files
make tunnel     # print SSH tunnel command for remote GUI access
make gui        # start Setup GUI (bound to 127.0.0.1 — access via SSH tunnel)
make start      # start agent in background (auto-restart on crash)
make stop       # stop agent
make restart    # restart agent (reloads .env)
make logs       # tail live logs
make status     # show container status
make build      # rebuild image with cache (fast, for code updates)
make rebuild    # rebuild from scratch (use after requirements.txt changes)
make clean      # remove containers and image
```

> **GUI is bound to `127.0.0.1` only** — it is never exposed publicly.  
> Access it by forwarding port 8080 via SSH tunnel from your local machine.

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
DEFAULT_SL_PCT=2.0        # fallback SL when not in signal
DEFAULT_TP_PCT=5.0        # fallback TP when not in signal (0 = disabled)
TRAILING_STOP=false       # tighten SL as profit grows (off by default — use with own signals)

# ── Entry zone (deferred entries) ──────────────────────────
ENTRY_TOLERANCE=0.1       # ±% tolerance around entry zone
PENDING_ENTRY_TIMEOUT=24  # hours before pending entry expires

# ── LLM parser ─────────────────────────────────────────────
# LLM_PROVIDER=claude
# LLM_API_KEY=sk-ant-...

# ── Chart notifications ────────────────────────────────────
CHART_TF=15m              # 5m | 15m | 1h | 4h
CHART_BARS=50             # 25 | 50 | 75 | 100
```

---

## Personal Bot Commands

```
/start        — agent status, balance, open positions count
/positions    — open positions with P&L (sends PNG image report)
/pending      — deferred entries waiting for price to reach entry zone
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
│   ├── chart.py           # Candlestick chart rendering (Telegram notifications)
│   ├── coins.py           # Supported coins registry per exchange
│   ├── config.py          # Config loader (.env → AgentConfig)
│   ├── daily_secret.py    # Rotating HMAC key from license server
│   ├── license.py         # License validation + device binding
│   ├── main.py            # Agent orchestrator
│   ├── order_executor.py  # Exchange interaction (ccxt)
│   ├── personal_bot.py    # Telegram control bot
│   ├── pnl_image.py       # P&L image report generator (matplotlib)
│   ├── position_monitor.py # SL/TP monitoring — WebSocket (paper) + exchange sync (trade)
│   ├── price_watcher.py   # Deferred entry watcher — WebSocket price monitoring
│   ├── risk_manager.py    # Position sizing, SL/TP logic, ladder TP
│   ├── signal_client.py   # WebSocket signal receiver
│   └── state.py           # JSON state persistence
│
├── gui/
│   ├── templates/
│   │   └── index.html     # Single-page setup UI
│   ├── app.py             # FastAPI backend (REST + polling log stream)
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
- [x] Signal parsing — regex + LLM (Claude), RU/EN/UA
- [x] Forwarded message support (Telegram Premium)
- [x] SL/TP validation and trailing stop protection
- [x] Ladder take-profit — multiple TP levels with partial closes
- [x] Deferred entry zones — wait for price to reach entry range
- [x] P&L image report via /positions
- [x] Browser-based Setup GUI with live connection tests
- [x] Docker Compose deployment
- [x] Trailing stop — auto-tightens SL as profit grows
- [x] Default take-profit % — fallback TP when not in signal
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
