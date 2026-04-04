# gui/app.py
"""
FastAPI web UI for Quantilan Trading Agent setup.

Routes:
  GET  /                      — setup page
  GET  /api/config            — read current .env (secrets masked)
  GET  /api/validate          — validate all fields in current .env
  POST /api/save              — write .env
  POST /api/test/connection   — quick: connect + balance
  POST /api/test/exchange     — full: open → modify SL → close (uses test_agent.py)
  GET  /api/test/stream/{id}  — SSE stream of live test log lines
  POST /api/test/telegram     — send test message via bot
  GET  /api/agent/status      — is agent process running
  POST /api/agent/start       — spawn agent process
  POST /api/agent/stop        — terminate agent process
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Add project root to path so we can import agent/*
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from gui.env_manager import read_env, write_env, validate_field, ENV_DEFAULTS


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, AttributeError):
        return default


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    # On shutdown: terminate agent subprocess so Ctrl+C doesn't hang
    global _agent_proc
    if _agent_proc and _agent_proc.returncode is None:
        try:
            _agent_proc.terminate()
            await asyncio.wait_for(_agent_proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            _agent_proc.kill()
        except Exception:
            pass


app = FastAPI(title="Quantilan Agent Setup", docs_url=None, redoc_url=None, lifespan=_lifespan)

# Static files
_STATIC = Path(__file__).parent / "static"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# ── In-memory state ──────────────────────────────────────────────────────────

# Active SSE test streams: test_id → asyncio.Queue
_active_tests: Dict[str, asyncio.Queue] = {}

# Agent subprocess
_agent_proc: Optional[asyncio.subprocess.Process] = None
_agent_log_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
_agent_paused: bool = False


# ── Request/Response models ──────────────────────────────────────────────────

class SaveRequest(BaseModel):
    fields: Dict[str, str]

class TestConnectionRequest(BaseModel):
    fields: Dict[str, str]

class TestExchangeRequest(BaseModel):
    fields: Dict[str, str]
    symbol: str = "ETH"
    side:   str = "LONG"
    mode:   str = "paper"   # paper | trade

class TestTelegramRequest(BaseModel):
    tg_token:   str
    tg_chat_id: str

class TestSignalRequest(BaseModel):
    signal_source: str
    license_key:   str = ""
    signal_server: str = "wss://signals.quantilan.com"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mask(val: str) -> str:
    """Show first 4 + last 4 chars, rest as asterisks."""
    if not val or len(val) < 10:
        return val
    return val[:4] + "*" * (len(val) - 8) + val[-4:]


def _is_masked(val: str) -> bool:
    """Return True if value looks like a masked secret (contains ****...)."""
    return bool(val and "****" in val)


_SENSITIVE = {
    "EXCHANGE_API_KEY", "EXCHANGE_SECRET", "EXCHANGE_PASSPHRASE",
    "EXCHANGE_WALLET_ADDRESS", "TG_TOKEN", "LICENSE_KEY", "LLM_API_KEY",
}


def _unmask_fields(fields: Dict[str, str]) -> Dict[str, str]:
    """
    Replace masked field values with the real values from .env.
    Called before building config for connection/exchange tests so that
    the user doesn't have to re-enter secrets after the page loads.
    """
    real = read_env()
    result = dict(fields)
    for key in _SENSITIVE:
        if _is_masked(result.get(key, "")):
            result[key] = real.get(key, "")
    return result


def _build_config(fields: Dict[str, str]):
    """Build AgentConfig from form fields dict."""
    from agent.config import AgentConfig
    stbc = fields.get("EXCHANGE_STBC", "").strip()
    ex   = fields.get("EXCHANGE", "binance").lower()
    if not stbc:
        stbc = "USDC" if ex == "hyperliquid" else "USDT"

    return AgentConfig(
        exchange         = ex,
        api_key          = fields.get("EXCHANGE_API_KEY", ""),
        api_secret       = fields.get("EXCHANGE_SECRET", ""),
        api_passphrase   = fields.get("EXCHANGE_PASSPHRASE", ""),
        wallet_address   = fields.get("EXCHANGE_WALLET_ADDRESS", ""),
        stbc             = stbc,
        leverage         = int(fields.get("LEVERAGE", "5") or "5"),
        margin_pct       = float(fields.get("MARGIN_PCT", "4.0") or "4.0"),
        max_positions    = int(fields.get("MAX_POSITIONS", "7") or "7"),
        mode             = fields.get("MODE", "paper"),
        paper_balance    = float(fields.get("PAPER_BALANCE", "10000") or "10000"),
        tg_token         = fields.get("TG_TOKEN", ""),
        tg_chat_id       = _parse_int(fields.get("TG_CHAT_ID", "0")),
        signal_source    = fields.get("SIGNAL_SOURCE", "telegram"),
        signal_server    = fields.get("SIGNAL_SERVER", ""),
        license_key      = fields.get("LICENSE_KEY", ""),
        license_server   = "https://license.quantilan.com",
        llm_provider     = fields.get("LLM_PROVIDER", "none"),
        llm_api_key      = fields.get("LLM_API_KEY", ""),
        llm_model        = fields.get("LLM_MODEL", ""),
    )


class _QueueLogHandler(logging.Handler):
    """Routes log records to an asyncio.Queue for SSE streaming."""

    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self.queue = queue
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        # Strip ANSI codes if any
        import re
        msg = re.sub(r'\x1b\[[0-9;]*m', '', msg)
        item = {"type": "log", "level": record.levelname.lower(), "msg": msg}
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    tpl = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(tpl.read_text(encoding="utf-8"))


@app.get("/api/config")
async def get_config():
    """Return current .env values. Sensitive fields are masked."""
    raw = read_env()
    masked = {}
    sensitive = {"EXCHANGE_API_KEY", "EXCHANGE_SECRET", "EXCHANGE_PASSPHRASE",
                 "EXCHANGE_WALLET_ADDRESS", "TG_TOKEN", "LICENSE_KEY", "LLM_API_KEY"}
    for k, v in raw.items():
        masked[k] = _mask(v) if k in sensitive and v else v

    # Also return raw (unmasked) for validation status
    validation = {k: validate_field(k, v) for k, v in raw.items()
                  if k in {"TG_TOKEN", "TG_CHAT_ID", "EXCHANGE_API_KEY",
                           "EXCHANGE_SECRET", "EXCHANGE_PASSPHRASE",
                           "EXCHANGE_WALLET_ADDRESS", "LICENSE_KEY", "LLM_API_KEY"}}
    return {"fields": masked, "validation": validation, "has_env": (Path(_ROOT / ".env")).exists()}


@app.post("/api/diff")
async def check_diff(req: SaveRequest):
    """
    Compare submitted form fields against existing .env.
    Returns list of changed fields so the UI can prompt before overwriting.
    Sensitive fields: only reports 'masked=True' if value changed, no values exposed.
    Numeric fields: normalized to float before compare (avoids "4" vs "4.0" false positives).
    """
    env_path = Path(_ROOT / ".env")
    if not env_path.exists():
        return {"changed": [], "has_env": False}

    existing = read_env()
    fields   = dict(req.fields)

    _NUMERIC_KEYS = {
        "MARGIN_PCT", "LEVERAGE", "MAX_POSITIONS",
        "PAPER_BALANCE", "DEFAULT_SL_PCT", "CHART_BARS",
    }

    def _norm(key: str, val: str) -> str:
        val = (val or "").strip()
        if key in _NUMERIC_KEYS:
            try:
                return str(float(val))
            except (ValueError, TypeError):
                pass
        return val.lower()

    changed = []
    for key in existing:
        new_val = (fields.get(key) or "").strip()
        old_val = (existing.get(key) or "").strip()

        if key in _SENSITIVE:
            # If the form still contains the masked placeholder, user didn't touch it
            if _is_masked(new_val):
                continue
            if new_val != old_val:
                changed.append({"key": key, "masked": True})
        else:
            if _norm(key, new_val) != _norm(key, old_val):
                changed.append({"key": key, "old": old_val, "new": new_val, "masked": False})

    return {"changed": changed, "has_env": True}


@app.post("/api/save")
async def save_config(req: SaveRequest):
    try:
        # If a field still contains the masked placeholder (user never edited it),
        # restore the real value from the current .env so we never overwrite secrets
        # with their own masks.
        fields   = dict(req.fields)
        existing = read_env()
        for k, v in fields.items():
            if _is_masked(v) and existing.get(k):
                fields[k] = existing[k]
        write_env(fields)
        return {"ok": True, "message": ".env saved successfully"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/test/connection")
async def test_connection(req: TestConnectionRequest):
    """Quick test: connect to exchange and get balance.
    In paper mode connects anonymously (public endpoints only — no API keys needed).
    """
    try:
        from agent.order_executor import OrderExecutor
        fields = _unmask_fields(req.fields)
        mode   = fields.get("MODE", "paper")

        # Paper mode: strip credentials so CCXT uses public endpoints only
        if mode == "paper":
            fields = {**fields, "EXCHANGE_API_KEY": "", "EXCHANGE_SECRET": "",
                      "EXCHANGE_PASSPHRASE": "", "EXCHANGE_WALLET_ADDRESS": ""}

        config   = _build_config(fields)
        executor = OrderExecutor(config)

        ok = await executor.connect()
        if not ok:
            return {"ok": False, "error": f"connect() returned False — check logs for [{config.exchange.upper()}] error details"}

        stbc = config.stbc or "USDT"

        if mode == "paper":
            # Return paper balance; no authenticated balance call needed
            paper_bal = config.paper_balance
            await executor.disconnect()
            return {
                "ok":       True,
                "exchange": config.exchange.upper(),
                "balance":  {"total": paper_bal, "free": paper_bal, "used": 0.0},
                "stbc":     stbc,
                "pos_mode": "paper",
            }

        total, free, used = await executor.get_balance()
        pos_mode = await executor.get_position_mode()

        # Build coins summary for GUI display
        coins_data = {
            sym: {
                "available":    c.available,
                "min_notional": c.min_notional,
                "amount_step":  c.amount_step,
                "max_amount":   c.max_amount,
            }
            for sym, c in executor.coins.items()
        } if executor.coins else {}

        await executor.disconnect()
        return {
            "ok":       True,
            "exchange": config.exchange.upper(),
            "balance":  {"total": round(total, 2), "free": round(free, 2), "used": round(used, 2)},
            "stbc":     stbc,
            "pos_mode": pos_mode,
            "coins":    coins_data,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/test/exchange")
async def start_exchange_test(req: TestExchangeRequest):
    """
    Starts a full exchange test in the background.
    Returns test_id — client polls /api/test/stream/{id} for SSE log lines.
    """
    test_id = str(uuid.uuid4())[:8]
    queue   = asyncio.Queue(maxsize=500)
    _active_tests[test_id] = queue

    # Run test in background task
    asyncio.create_task(_run_exchange_test(test_id, queue, req))
    return {"test_id": test_id}


async def _run_exchange_test(test_id: str, queue: asyncio.Queue, req: TestExchangeRequest):
    """Background task: runs full exchange test and feeds logs to SSE queue."""
    handler = _QueueLogHandler(queue)

    # Add handler only to the root logger — all child loggers propagate up to it,
    # so adding to both root AND children causes every record to appear twice.
    loggers_to_patch = [logging.getLogger()]
    for lg in loggers_to_patch:
        lg.addHandler(handler)

    try:
        from agent.state import PositionSide
        from tests.test_agent import test_exchange, pick_free_symbol

        config = _build_config(_unmask_fields(req.fields))
        config.mode = req.mode

        await queue.put({"type": "start", "msg": f"Starting exchange test on {config.exchange.upper()} [{config.mode.upper()}]..."})

        # Pick a free symbol (avoid collisions with open positions)
        symbol = await pick_free_symbol(
            config, req.symbol,
            candidates=["ETH", "ARB", "TRX", "SOL"]
        )

        side = PositionSide.LONG if req.side.upper() == "LONG" else PositionSide.SHORT
        ok, errors = await test_exchange(config, symbol, side)

        await queue.put({
            "type": "done",
            "ok":   ok,
            "msg":  "✅ All steps passed!" if ok else f"❌ Failed: {errors}",
        })

    except Exception as e:
        await queue.put({"type": "done", "ok": False, "msg": f"💥 Critical error: {e}"})
    finally:
        for lg in loggers_to_patch:
            lg.removeHandler(handler)
        # Give client 30s to drain the queue, then clean up
        await asyncio.sleep(30)
        _active_tests.pop(test_id, None)


@app.get("/api/test/stream/{test_id}")
async def stream_test(test_id: str):
    """SSE endpoint — streams live log lines from exchange test."""
    queue = _active_tests.get(test_id)
    if not queue:
        raise HTTPException(404, "Test not found or expired")

    async def event_gen():
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=60.0)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") == "done":
                    break
            except asyncio.TimeoutError:
                yield 'data: {"type":"ping"}\n\n'

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/test/poll/{test_id}")
async def poll_test(test_id: str):
    """
    Polling endpoint — returns all available log lines since last poll.
    More reliable than SSE over SSH tunnels and reverse proxies.
    Returns {items: [...], done: bool}.
    """
    queue = _active_tests.get(test_id)
    if not queue:
        return {"items": [], "done": True, "expired": True}

    items = []
    done  = False
    # Drain everything currently available (non-blocking)
    while True:
        try:
            item = queue.get_nowait()
            items.append(item)
            if item.get("type") == "done":
                done = True
                break
        except asyncio.QueueEmpty:
            break

    return {"items": items, "done": done}


@app.post("/api/test/telegram")
async def test_telegram(req: TestTelegramRequest):
    """Send a test message to verify bot token + chat_id."""
    try:
        import aiohttp
        token   = req.tg_token.strip()
        chat_id = req.tg_chat_id.strip()

        # If token is masked (loaded from /api/config), fetch real value from .env
        if _is_masked(token):
            token = read_env().get("TG_TOKEN", "").strip()

        if not token or not chat_id:
            return {"ok": False, "error": "Token or chat_id is empty"}

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        async with aiohttp.ClientSession(connector=connector) as session:
            # First: get bot info
            async with session.get(f"https://api.telegram.org/bot{token}/getMe") as r:
                info = await r.json()
                if not info.get("ok"):
                    return {"ok": False, "error": f"Invalid token: {info.get('description')}"}
                bot_name = info["result"].get("username", "?")

            # Then: send test message
            async with session.post(url, json={
                "chat_id": chat_id,
                "text":    "✅ Quantilan Agent — bot connection test successful!",
                "parse_mode": "HTML",
            }) as r:
                resp = await r.json()
                if resp.get("ok"):
                    return {"ok": True, "bot_name": bot_name, "chat_id": chat_id}
                else:
                    return {"ok": False, "error": resp.get("description", "Unknown error")}

    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/test/signal")
async def test_signal(req: TestSignalRequest):
    """Verify license key against Quantilan signal server, or confirm Telegram mode."""
    try:
        source = req.signal_source.strip()

        if source != "server":
            return {"ok": True, "source": "telegram"}

        import aiohttp
        license_key = req.license_key.strip()
        if _is_masked(license_key):
            license_key = read_env().get("LICENSE_KEY", "").strip()

        if not license_key:
            return {"ok": False, "error": "License key is empty"}

        # License API is always on license.quantilan.com — independent of SIGNAL_SERVER (WSS)
        base_url = "https://license.quantilan.com"

        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"{base_url}/v1/validate",
                json={"license_key": license_key},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status >= 500:
                    return {"ok": False, "error": f"Signal server unavailable (HTTP {r.status}) — server may not be running yet"}
                if r.status == 404:
                    return {"ok": False, "error": "Endpoint not found — check Signal Server URL"}
                ct = r.headers.get("Content-Type", "")
                if "application/json" not in ct:
                    return {"ok": False, "error": f"Server returned unexpected response (HTTP {r.status}) — check Signal Server URL"}

                resp = await r.json()
                if resp.get("valid"):
                    import datetime
                    expires_ts = resp.get("expires_at", 0)
                    valid_until = datetime.datetime.fromtimestamp(expires_ts).strftime("%Y-%m-%d") if expires_ts else "—"
                    return {
                        "ok":          True,
                        "source":      "server",
                        "plan":        resp.get("plan", "—"),
                        "valid_until": valid_until,
                    }
                else:
                    reason = resp.get("reason", "invalid_license")
                    messages = {
                        "key_not_found":   "License key not found",
                        "status_revoked":  "License has been revoked",
                        "status_expired":  "License has expired",
                        "expired":         "License has expired",
                        "device_mismatch": "License is bound to a different device",
                    }
                    return {"ok": False, "error": messages.get(reason, reason)}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Agent process management ──────────────────────────────────────────────────

@app.get("/api/agent/status")
async def agent_status():
    global _agent_proc, _agent_paused
    running = _agent_proc is not None and _agent_proc.returncode is None
    if not running:
        _agent_paused = False   # reset if process died
    return {"running": running, "paused": _agent_paused, "pid": _agent_proc.pid if running else None}


@app.post("/api/agent/start")
async def agent_start():
    global _agent_proc, _agent_log_queue
    if _agent_proc and _agent_proc.returncode is None:
        return {"ok": False, "error": "Agent is already running"}

    # Flush old logs
    while not _agent_log_queue.empty():
        try:
            _agent_log_queue.get_nowait()
        except Exception:
            break

    try:
        python = sys.executable
        main   = str(_ROOT / "main.py")
        _agent_proc = await asyncio.create_subprocess_exec(
            python, main,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(_ROOT),
        )
        _agent_paused = False
        asyncio.create_task(_read_agent_output())
        return {"ok": True, "pid": _agent_proc.pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/agent/pause")
async def agent_pause():
    global _agent_proc, _agent_paused
    if not _agent_proc or _agent_proc.returncode is not None:
        return {"ok": False, "error": "Agent is not running"}
    if _agent_paused:
        return {"ok": False, "error": "Already paused"}
    try:
        _agent_proc.stdin.write(b"pause\n")
        await _agent_proc.stdin.drain()
        _agent_paused = True
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/agent/resume")
async def agent_resume():
    global _agent_proc, _agent_paused
    if not _agent_proc or _agent_proc.returncode is not None:
        return {"ok": False, "error": "Agent is not running"}
    if not _agent_paused:
        return {"ok": False, "error": "Not paused"}
    try:
        _agent_proc.stdin.write(b"resume\n")
        await _agent_proc.stdin.drain()
        _agent_paused = False
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/agent/stop")
async def agent_stop():
    global _agent_proc, _agent_paused
    if not _agent_proc or _agent_proc.returncode is not None:
        return {"ok": False, "error": "Agent is not running"}
    try:
        _agent_proc.terminate()
        await asyncio.wait_for(_agent_proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        _agent_proc.kill()
    _agent_paused = False
    return {"ok": True}


@app.get("/api/agent/logs")
async def agent_logs():
    """SSE stream of agent stdout lines."""
    async def gen():
        while True:
            try:
                line = await asyncio.wait_for(_agent_log_queue.get(), timeout=30.0)
                yield f"data: {json.dumps({'msg': line})}\n\n"
            except asyncio.TimeoutError:
                yield 'data: {"type":"ping"}\n\n'
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _read_agent_output():
    global _agent_proc
    if not _agent_proc or not _agent_proc.stdout:
        return
    async for raw in _agent_proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        try:
            _agent_log_queue.put_nowait(line)
        except asyncio.QueueFull:
            pass  # drop oldest — don't block
