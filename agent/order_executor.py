# agent/order_executor.py
"""
Order executor via ccxt.

Supported exchanges:
  - Binance USDM-M Futures  (ccxt: binanceusdm)
  - Bybit Linear Futures
  - Hyperliquid Perps
  - OKX Swap

Exchange params (rate limits, timeouts, options) are loaded from
exchanges.json in the project root. Edit that file to tune without
touching code.

Position mode:
  connect() checks and auto-switches to One-Way mode.
  Binance:     fapiprivate_get/post_positionside_dual
  Bybit:       privatePostV5PositionSwitchMode (mode=0)
  OKX:         privatePostAccountSetPositionMode (posMode=net_mode)
  Hyperliquid: always One-Way
"""

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
import ccxt.async_support as ccxt

try:
    import ccxt.pro as ccxtpro
    _HAS_PRO = True
except ImportError:
    ccxtpro   = None  # type: ignore
    _HAS_PRO  = False

from .config import AgentConfig
from .state import PositionSide, OrderParams

logger = logging.getLogger(__name__)


def _make_session() -> aiohttp.ClientSession:
    """
    Create an aiohttp session with ThreadedResolver.

    When aiodns is installed aiohttp uses it by default, but c-ares (aiodns)
    sometimes fails DNS resolution on Windows while the standard socket resolver works.
    ThreadedResolver calls socket.getaddrinfo in a thread pool — same path as curl.
    """
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    return aiohttp.ClientSession(connector=connector)


SAFE_CLOSE_ERRORS = [
    "reduce only order would increase position",
    "reduceonly order is rejected",
    "-2022",                           # Binance: ReduceOnly Order is rejected
    "110017",                          # Bybit: position is zero, cannot fix reduce-only qty
    "51169",                           # OKX: no position in this direction to reduce/close
    "no open",
    "order does not exist",
    "position does not exist",
    "position size is zero",           # Hyperliquid
    "insufficient",
]

# Exchange parameters — loaded from exchanges.json in project root
_EXCHANGES_FILE = Path(__file__).parent.parent / "exchanges.json"

_EXCHANGE_DEFAULTS: dict = {
    "binance": {
        "ccxt_id": "binanceusdm",
        "enableRateLimit": True, "rateLimit": 100, "timeout": 15000,
        "options": {"adjustForTimeDifference": True, "defaultType": "future",
                    "fetchCurrencies": False,   # skip /sapi/v1/capital/config/getall (spot endpoint)
                    "cache": False, "warmup": False}
    },
    "bybit": {
        "ccxt_id": "bybit",
        "enableRateLimit": True, "rateLimit": 100, "timeout": 15000,
        "options": {"adjustForTimeDifference": True, "defaultType": "linear",
                    "fetchCurrencies": False,
                    "cache": False}
    },
    "hyperliquid": {
        "ccxt_id": "hyperliquid",
        "enableRateLimit": True, "rateLimit": 100, "timeout": 15000,
        "options": {}
    },
    "okx": {
        "ccxt_id": "okx",
        "enableRateLimit": True, "rateLimit": 100, "timeout": 15000,
        "options": {"adjustForTimeDifference": True, "defaultType": "swap",
                    "cache": False}
    },
}


def _load_exchange_params() -> dict:
    """Load exchange params from exchanges.json, fallback to built-in defaults."""
    if _EXCHANGES_FILE.exists():
        try:
            data = json.loads(_EXCHANGES_FILE.read_text())
            logger.info(f"[Executor] Loaded exchange params from {_EXCHANGES_FILE.name}")
            return data
        except Exception as e:
            logger.warning(f"[Executor] exchanges.json read error: {e} — using defaults")
    return _EXCHANGE_DEFAULTS


_EXCHANGE_PARAMS = _load_exchange_params()


class OrderExecutor:

    def __init__(self, config: AgentConfig):
        self.config        = config
        self.exchange:     Optional[ccxt.Exchange] = None
        self.pro_exchange: Optional[object]        = None   # ccxt.pro instance (watch_*)
        self._session:     Optional[aiohttp.ClientSession] = None
        self._pro_session: Optional[aiohttp.ClientSession] = None
        self._semaphore    = asyncio.Semaphore(3)
        self._markets      = {}
        self.is_one_way    = False  # set after connect()

    # ─────────────────────────────────────
    # CONNECT
    # ─────────────────────────────────────

    async def connect(self) -> bool:
        """
        Connect to exchange, load markets,
        verify/set One-Way mode, set Isolated margin.
        """
        try:
            self.exchange = self._create_exchange()
            await self.exchange.load_markets()
            self._markets = self.exchange.markets
            logger.info(f"✅ [Executor] Connected to {self.config.exchange.upper()}")
        except Exception as e:
            logger.error(f"❌ [Executor] Connection failed [{type(e).__name__}]: {e}")
            return False

        if self.config.mode == 'paper':
            self.is_one_way = True
            return True

        # Check and set One-Way mode
        ok = await self._ensure_one_way_mode()
        if not ok:
            logger.error(
                f"❌ [Executor] Could not set One-Way mode on {self.config.exchange}. "
                f"Please switch manually in exchange settings and restart."
            )
            return False

        return True

    async def disconnect(self) -> None:
        if self.pro_exchange:
            try:
                await self.pro_exchange.close()
            except Exception:
                pass
            self.pro_exchange = None
        if self._pro_session:
            try:
                await self._pro_session.close()
            except Exception:
                pass
            self._pro_session = None
        if self.exchange:
            await self.exchange.close()
            self.exchange = None
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def connect_pro(self) -> bool:
        """
        Create a ccxt.pro exchange instance for WebSocket watch_* calls.
        Used by PositionMonitor in paper mode (Phase 1 + Phase 2).
        Safe to call even if ccxt.pro is not installed — returns False and logs a warning.
        """
        if not _HAS_PRO:
            logger.warning(
                "[Executor] ccxt.pro not installed — paper mode monitor falls back to REST polling. "
                "Install with: pip install ccxt[pro]"
            )
            return False
        try:
            ex_key  = self.config.exchange
            params  = _EXCHANGE_PARAMS.get(ex_key, _EXCHANGE_DEFAULTS.get(ex_key, {}))
            ccxt_id = params.get("ccxt_id", ex_key)

            self._pro_session = _make_session()
            ccxt_params = {
                "enableRateLimit": params.get("enableRateLimit", True),
                "rateLimit":       params.get("rateLimit", 100),
                "timeout":         params.get("timeout", 15000),
                "options":         dict(params.get("options", {})),
                "session":         self._pro_session,
            }
            if ex_key == "hyperliquid":
                ccxt_params["walletAddress"] = self.config.wallet_address
                ccxt_params["privateKey"]    = self.config.api_secret
                if self.config.api_key:
                    ccxt_params["apiKey"] = self.config.api_key  # agent address (optional)
            elif ex_key == "okx":
                ccxt_params["apiKey"]   = self.config.api_key
                ccxt_params["secret"]   = self.config.api_secret
                ccxt_params["password"] = self.config.api_passphrase
            else:
                ccxt_params["apiKey"] = self.config.api_key
                ccxt_params["secret"] = self.config.api_secret

            pro_class = getattr(ccxtpro, ccxt_id, None)
            if not pro_class:
                logger.warning(f"[Executor] ccxt.pro has no class for '{ccxt_id}'")
                return False

            self.pro_exchange = pro_class(ccxt_params)
            if self.pro_exchange.has.get("fetchCurrencies"):
                self.pro_exchange.has["fetchCurrencies"] = False
            logger.info(f"[Executor] ccxt.pro connected ({ccxt_id})")
            return True

        except Exception as e:
            logger.error(f"[Executor] connect_pro failed: {e}")
            return False

    def _create_exchange(self) -> ccxt.Exchange:
        """
        Build ccxt exchange instance from exchanges.json params.
        """
        ex_key  = self.config.exchange
        params  = _EXCHANGE_PARAMS.get(ex_key, _EXCHANGE_DEFAULTS.get(ex_key, {}))
        ccxt_id = params.get("ccxt_id", ex_key)

        # Build ccxt params dict
        self._session = _make_session()
        ccxt_params = {
            "enableRateLimit": params.get("enableRateLimit", True),
            "rateLimit":       params.get("rateLimit", 100),
            "timeout":         params.get("timeout", 15000),
            "options":         dict(params.get("options", {})),
            "session":         self._session,   # ThreadedResolver — bypasses broken aiodns on Windows
        }

        # Credentials
        if ex_key == "hyperliquid":
            ccxt_params["walletAddress"] = self.config.wallet_address
            ccxt_params["privateKey"]    = self.config.api_secret
            if self.config.api_key:
                ccxt_params["apiKey"] = self.config.api_key  # agent address (optional)
        elif ex_key == "okx":
            ccxt_params["apiKey"]   = self.config.api_key
            ccxt_params["secret"]   = self.config.api_secret
            ccxt_params["password"] = self.config.api_passphrase
        else:
            ccxt_params["apiKey"] = self.config.api_key
            ccxt_params["secret"] = self.config.api_secret

        exchange_class = getattr(ccxt, ccxt_id, None)
        if not exchange_class:
            raise ValueError(f"ccxt has no exchange '{ccxt_id}' (config key: '{ex_key}')")

        logger.info(f"[Executor] Using ccxt class: {ccxt_id}")
        ex = exchange_class(ccxt_params)

        # Disable currency fetching via has dict — options.fetchCurrencies is only
        # respected by Binance; other exchanges (Bybit) need the has flag patched directly.
        if ex.has.get("fetchCurrencies"):
            ex.has["fetchCurrencies"] = False

        return ex


    # ─────────────────────────────────────
    # POSITION MODE — CHECK AND SET
    # ─────────────────────────────────────

    async def _ensure_one_way_mode(self) -> bool:
        """
        Check current position mode.
        If Hedge/Dual — switch to One-Way/Single automatically.
        Returns True if One-Way is confirmed.
        """
        ex = self.config.exchange

        try:
            if ex == 'binance':
                return await self._binance_ensure_one_way()

            elif ex == 'bybit':
                return await self._bybit_ensure_one_way()

            elif ex == 'okx':
                return await self._okx_ensure_one_way()

            elif ex == 'hyperliquid':
                # Hyperliquid is always netting (one-way) — no action needed
                logger.info("✅ [Executor] Hyperliquid: always One-Way (netting)")
                self.is_one_way = True
                return True

        except Exception as e:
            logger.error(f"❌ [Executor] ensure_one_way_mode error: {e}")
            return False

        return True

    async def _binance_ensure_one_way(self) -> bool:
        """
        Binance: check dualSidePosition.
        false = One-Way ✅  |  true = Hedge ❌ → switch
        """
        try:
            res = await self.exchange.fapiprivate_get_positionside_dual()
            is_dual = res.get('dualSidePosition', False)

            if not is_dual:
                logger.info("✅ [Executor] Binance: One-Way mode confirmed")
                self.is_one_way = True
                return True

            # Switch to One-Way
            logger.info("⚠️  [Executor] Binance in Hedge mode — switching to One-Way...")
            await self.exchange.fapiprivate_post_positionside_dual({
                "dualSidePosition": "false"
            })
            logger.info("✅ [Executor] Binance: switched to One-Way mode")
            self.is_one_way = True
            return True

        except Exception as e:
            logger.error(f"❌ [Executor] Binance set One-Way error: {e}")
            return False

    async def _bybit_ensure_one_way(self) -> bool:
        """
        Bybit: switch to MergedSingle (One-Way) mode.
        POST /v5/position/switch-mode  mode=0

        Error codes:
          110025 = position mode not modified (already One-Way) → OK
          110026 = have open positions, cannot switch → FAIL
        """
        try:
            coin = self.config.stbc or "USDT"
            await self.exchange.privatePostV5PositionSwitchMode({
                "category": "linear",
                "coin":     coin,
                "mode":     0,   # 0 = MergedSingle (One-Way)
            })
            logger.info("✅ [Executor] Bybit: switched to One-Way mode")
            self.is_one_way = True
            return True

        except Exception as e:
            err_str = str(e).lower()
            # 110025 = already in One-Way → not an error
            if "110025" in err_str or "already" in err_str or "mode not modified" in err_str:
                logger.info("✅ [Executor] Bybit: already in One-Way mode")
                self.is_one_way = True
                return True
            # 110026 = has open positions → cannot switch
            if "110026" in err_str or "open position" in err_str:
                logger.error(
                    "❌ [Executor] Bybit: cannot switch to One-Way — "
                    "close all positions first, then restart."
                )
                return False
            # 10003 = API key invalid signature — can also happen on Unified Trading Accounts
            # where switch-mode is not supported (UTA is always One-Way).
            # We try to verify by fetching a private endpoint; if that also fails, the key is bad.
            if "10003" in err_str or "api key is invalid" in err_str:
                try:
                    info = await self.exchange.privateGetV5AccountInfo({})
                    uta = info.get("result", {}).get("unifiedMarginStatus", 0)
                    if uta in (1, 2, 3, 4):   # any UTA level
                        logger.info(
                            f"✅ [Executor] Bybit Unified Account (UTA level {uta}) — "
                            "One-Way mode is the default, skipping switch."
                        )
                        self.is_one_way = True
                        return True
                except Exception:
                    pass
                logger.error(
                    "❌ [Executor] Bybit API key rejected (10003). "
                    "Check that the API secret is copied correctly and the key is active."
                )
                return False
            logger.error(f"❌ [Executor] Bybit set One-Way error: {e}")
            return False

    async def _okx_ensure_one_way(self) -> bool:
        """
        OKX: check current posMode and switch to net_mode (One-Way) if needed.
        GET  /api/v5/account/config       → current posMode
        POST /api/v5/account/set-position-mode → posMode: net_mode

        posMode values:
          net_mode        = One-Way ✅
          long_short_mode = Hedge   ❌ → switch
        """
        try:
            # Check current mode
            res      = await self.exchange.privateGetAccountConfig()
            info     = res.get('data', [{}])[0]
            pos_mode = info.get('posMode', '')

            if pos_mode == 'net_mode':
                logger.info("✅ [Executor] OKX: already in net_mode (One-Way)")
                self.is_one_way = True
                return True

            logger.info(f"⚠️  [Executor] OKX in '{pos_mode}' — switching to net_mode...")
            set_res = await self.exchange.privatePostAccountSetPositionMode({
                "posMode": "net_mode"
            })
            # OKX returns code "0" on success
            code = set_res.get('code', '')
            if str(code) == '0':
                logger.info("✅ [Executor] OKX: switched to net_mode (One-Way)")
                self.is_one_way = True
                return True
            else:
                msg = set_res.get('msg', 'unknown error')
                logger.error(f"❌ [Executor] OKX set One-Way failed: {msg}")
                return False

        except Exception as e:
            err_str = str(e).lower()
            if "already" in err_str or "no need" in err_str:
                logger.info("✅ [Executor] OKX: already in One-Way mode")
                self.is_one_way = True
                return True
            logger.error(f"❌ [Executor] OKX set One-Way error: {e}")
            return False

    async def get_position_mode(self) -> str:
        """
        Returns current position mode string for logging/UI.
        'one-way' or 'hedge' or 'unknown'
        """
        ex = self.config.exchange
        try:
            if ex == 'binance':
                res  = await self.exchange.fapiprivate_get_positionside_dual()
                dual = res.get('dualSidePosition', False)
                return 'hedge' if dual else 'one-way'

            elif ex == 'bybit':
                # Bybit returns mode in position list
                return 'one-way' if self.is_one_way else 'unknown'

            elif ex == 'okx':
                res      = await self.exchange.privateGetAccountConfig()
                pos_mode = res.get('data', [{}])[0].get('posMode', 'unknown')
                return 'one-way' if pos_mode == 'net_mode' else 'hedge'

            elif ex == 'hyperliquid':
                return 'one-way'

        except Exception as e:
            logger.warning(f"[Executor] get_position_mode error: {e}")

        return 'unknown'

    # ─────────────────────────────────────
    # OPEN POSITIONS (exchange)
    # ─────────────────────────────────────

    async def fetch_open_positions(self) -> list:
        """
        Returns raw ccxt position dicts for all positions with non-zero size.
        Used by PositionMonitor (trade mode) to detect externally closed positions.
        Each dict includes at minimum: 'symbol', 'contracts', 'unrealizedPnl'.
        """
        try:
            positions = await self.exchange.fetch_positions()
            return [
                p for p in positions
                if float(p.get('contracts') or p.get('contractSize') or 0) > 0
            ]
        except Exception as e:
            logger.warning(f"[Executor] fetch_open_positions: {e}")
            return []

    async def fetch_open_symbols(self) -> set:
        """
        Returns a set of base-coin symbols (e.g. {'ETH', 'BTC'}) that
        currently have open futures positions on the exchange.
        """
        try:
            positions = await self.exchange.fetch_positions()
            result = set()
            for p in positions:
                contracts = float(p.get('contracts') or p.get('contractSize') or 0)
                if contracts > 0:
                    ms   = p.get('symbol', '')           # e.g. "ETH/USDT:USDT"
                    base = ms.split('/')[0] if '/' in ms else ms
                    result.add(base)
            return result
        except Exception as e:
            logger.warning(f"[Executor] fetch_open_symbols: {e}")
            return set()

    # ─────────────────────────────────────
    # BALANCE AND PRICE
    # ─────────────────────────────────────

    async def get_balance(self) -> Tuple[float, float, float]:
        """Returns (total, free, used) in USDT or USDC depending on exchange."""
        if self.config.mode == 'paper':
            bal = self.config.paper_balance
            return bal, bal, 0.0

        async with self._semaphore:
            try:
                stbc    = self.config.stbc or "USDT"
                balance = await self.exchange.fetch_balance()
                coin    = balance.get(stbc, {})
                total   = float(coin.get('total', 0) or 0)
                free    = float(coin.get('free',  0) or 0)
                used    = float(coin.get('used',  0) or 0)
                return total, free, used
            except Exception as e:
                logger.error(f"❌ [Executor] get_balance: {e}")
                return 0.0, 0.0, 0.0

    async def get_ticker(self, symbol: str) -> float:
        try:
            ticker = await self.exchange.fetch_ticker(self._ms(symbol))
            return float(ticker.get('last', 0))
        except Exception as e:
            logger.error(f"❌ [Executor] get_ticker {symbol}: {e}")
            return 0.0

    async def fetch_ohlcv(self, symbol: str, tf: str = '15m', limit: int = 50) -> list:
        """
        Fetch OHLCV candles for chart generation.
        Returns [[timestamp_ms, open, high, low, close, volume], ...]
        """
        try:
            ms    = self._ms(symbol)
            ohlcv = await asyncio.wait_for(
                self.exchange.fetch_ohlcv(ms, tf, limit=limit),
                timeout=15.0,
            )
            return ohlcv or []
        except asyncio.TimeoutError:
            logger.warning(f"[Executor] fetch_ohlcv {symbol} {tf}: timeout")
            return []
        except Exception as e:
            logger.warning(f"[Executor] fetch_ohlcv {symbol} {tf}: {e}")
            return []

    # ─────────────────────────────────────
    # MARKET PARAMS
    # ─────────────────────────────────────

    def get_market_params(self, symbol: str) -> Tuple[int, float, float]:
        """
        Returns (price_precision, amount_step, min_notional).

        amount_step — minimum lot increment (e.g. 0.001 for ETH, 1.0 for TRX).
        Use floor(amount / amount_step) * amount_step to avoid dust.
        """
        market    = self._markets.get(self._ms(symbol), {})
        precision = market.get('precision', {})
        limits    = market.get('limits', {})

        def digits(val, default):
            try:
                return int(abs(round(math.log10(val)))) if val else default
            except Exception:
                return default

        price_prec   = digits(precision.get('price'), 2)
        amount_step  = float(precision.get('amount') or 0.001)
        min_notional = float(limits.get('cost', {}).get('min', 5.0) or 5.0)
        return price_prec, amount_step, min_notional

    def _ms(self, symbol: str) -> str:
        """
        Build ccxt market symbol.
        BTCUSDT → BTC/USDT:USDT  (Binance/Bybit/OKX)
        BTCUSDC → BTC/USDC:USDC  (Hyperliquid)
        """
        stbc = self.config.stbc or "USDT"
        base = symbol.replace('USDT', '').replace('USDC', '').replace('/', '').strip()
        return f"{base}/{stbc}:{stbc}"

    # ─────────────────────────────────────
    # LEVERAGE AND MARGIN
    # ─────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        if self.config.mode == "paper":
            return True  # no-op in paper mode
        async with self._semaphore:
            try:
                ms = self._ms(symbol)
                if self.config.exchange == 'okx':
                    # OKX: pass margin mode + leverage together to avoid "lever" param warning
                    await self.exchange.set_leverage(leverage, ms, {'mgnMode': 'isolated'})
                else:
                    await self.exchange.set_leverage(leverage, ms)
                logger.info(f"✅ [Executor] Leverage x{leverage} set for {symbol}")
                return True
            except Exception as e:
                err = str(e).lower()
                # 110043 = leverage not modified (already at requested value) → OK
                if "110043" in err or "leverage not modified" in err:
                    logger.info(f"✅ [Executor] Leverage x{leverage} already set for {symbol}")
                    return True
                logger.warning(f"⚠️  [Executor] set_leverage {symbol}: {e}")
                return False

    async def set_margin_isolated(self, symbol: str) -> bool:
        """Set isolated margin for a symbol."""
        try:
            ms     = self._ms(symbol)
            ex     = self.config.exchange
            params = {}

            if ex == 'hyperliquid':
                params['leverage'] = self.config.leverage
                ms = self._ms(symbol)

            await self.exchange.set_margin_mode('isolated', ms, params=params)
            logger.info(f"✅ [Executor] Isolated margin set for {symbol}")
            return True

        except Exception as e:
            err = str(e).lower()
            if "already" in err or "no need to change" in err:
                return True
            logger.error(f"❌ [Executor] set_margin_isolated {symbol}: {e}")
            return False

    # ─────────────────────────────────────
    # OPEN POSITION
    # ─────────────────────────────────────

    async def open_position(
        self, params: OrderParams
    ) -> Tuple[Optional[str], Optional[str], list, str]:
        """
        Open position + stop + take(s).
        Returns (position_id, stop_id, take_ids, error).
        take_ids — list of TP order IDs (empty if no TP, 1 item for single, N for ladder).
        """
        if self.config.mode == 'paper':
            n = len(params.take_levels) if params.take_levels else (1 if params.take_price > 0 else 0)
            take_ids = [f"paper_take_{i}" for i in range(n)]
            logger.info(
                f"📋 [PAPER] OPEN {params.symbol} {params.side.value} "
                f"amt:{params.amount} SL:{params.stop_price} "
                f"TP:{[p for p, _ in params.take_levels] if params.take_levels else params.take_price}"
            )
            return "paper_pos", "paper_stop", take_ids, ""

        ms       = self._ms(params.symbol)
        side_str = 'buy' if params.side == PositionSide.LONG else 'sell'
        ex       = self.config.exchange

        async with self._semaphore:

            # 1. Market order
            position_id = None
            try:
                open_params = {}
                if ex == 'okx':
                    open_params = {'tdMode': 'isolated'}

                # Hyperliquid requires price for market orders (used for max slippage calc)
                open_price = params.entry_price if ex == 'hyperliquid' else None

                order = await self._retry(
                    self.exchange.create_order,
                    ms, 'market', side_str, params.amount,
                    price=open_price,
                    params=open_params
                )
                position_id = order.get('id')
                logger.info(f"✅ [Executor] Opened {params.symbol} id:{position_id}")
            except Exception as e:
                return None, None, [], f"open error: {e}"

            # 2. Stop loss (critical)
            stop_id = None
            try:
                stop_id = await self._create_stop(
                    ms, params.side, params.amount, params.stop_price
                )
                logger.info(f"✅ SL {params.symbol} @ {params.stop_price}")
            except Exception as e:
                logger.critical(f"🚨 SL failed {params.symbol}! Closing position! {e}")
                await self.close_position(
                    params.symbol, params.amount,
                    params.side, params.entry_price
                )
                return position_id, None, [], f"stop failed: {e}"

            # 3. Take profit(s) — ladder or single (non-critical)
            take_ids = []
            if params.take_levels:
                # TP ladder: multiple orders with split amounts
                for tp_price, tp_amount in params.take_levels:
                    try:
                        tid = await self._create_take(ms, params.side, tp_amount, tp_price)
                        take_ids.append(tid)
                        logger.info(f"✅ TP {params.symbol} @ {tp_price} | amt: {tp_amount}")
                    except Exception as e:
                        logger.warning(f"⚠️  TP {tp_price} failed {params.symbol}: {e}")
            elif params.take_price > 0:
                # Single TP
                try:
                    tid = await self._create_take(
                        ms, params.side, params.amount, params.take_price
                    )
                    take_ids.append(tid)
                    logger.info(f"✅ TP {params.symbol} @ {params.take_price}")
                except Exception as e:
                    logger.warning(f"⚠️  TP failed {params.symbol}: {e}")

        return position_id, stop_id, take_ids, ""

    # ─────────────────────────────────────
    # CLOSE POSITION
    # ─────────────────────────────────────

    async def close_position(
        self, symbol: str, amount: float,
        side: PositionSide, last_price: float,
    ) -> Tuple[bool, str]:

        if self.config.mode == 'paper':
            logger.info(f"📋 [PAPER] CLOSE {symbol} {side.value}")
            return True, ""

        ms         = self._ms(symbol)
        close_side = 'sell' if side == PositionSide.LONG else 'buy'
        ex         = self.config.exchange

        # Hyperliquid requires price for market close
        price        = last_price if ex == 'hyperliquid' else None
        close_params = {'reduceOnly': True}
        if ex == 'okx':
            close_params['tdMode'] = 'isolated'

        async with self._semaphore:
            try:
                await self.exchange.create_order(
                    ms, 'market', close_side, amount,
                    price=price, params=close_params
                )
            except Exception as e:
                err = str(e).lower()
                if any(s in err for s in SAFE_CLOSE_ERRORS):
                    # Position already closed — not an error, skip retry
                    logger.info(f"✅ [Executor] {symbol} already closed")
                else:
                    logger.error(f"❌ close {symbol}: {e}")
                    return False, str(e)

            await self._cancel_all_orders(ms, symbol)

        return True, ""

    async def _cancel_all_orders(self, ms: str, symbol: str) -> None:
        """Cancel remaining SL/TP orders after closing position."""
        await asyncio.sleep(0.3)
        ex = self.config.exchange
        try:
            if ex == 'binance':
                m_id = self.exchange.market(ms)['id']
                await self.exchange.fapiprivate_delete_algoopenorders({'symbol': m_id})
            elif ex in ('hyperliquid', 'bybit'):
                # SL/TP are position-level (TP_SL algo on HL, setTradingStop on Bybit).
                # Both auto-cancel when the position closes — no action needed.
                pass
            elif ex == 'okx':
                # OKX: cancel_all_orders() not supported for algo orders.
                # Fetch conditional (TP/SL) algo orders and cancel with stop=True flag.
                algo_orders = await self.exchange.fetch_open_orders(ms, params={'stop': True})
                for o in algo_orders:
                    try:
                        await self.exchange.cancel_order(o['id'], ms, {'stop': True})
                    except Exception:
                        pass
            else:
                await self.exchange.cancel_all_orders(ms)
            logger.info(f"🧹 Orders cancelled for {symbol}")
        except Exception as e:
            logger.warning(f"⚠️  cancel_all {symbol}: {e}")

    # ─────────────────────────────────────
    # MODIFY STOP
    # ─────────────────────────────────────

    async def modify_stop(
        self, symbol: str, old_stop_id: str,
        side: PositionSide, amount: float, new_stop: float,
    ) -> Tuple[Optional[str], str]:

        if self.config.mode == 'paper':
            return "paper_stop_new", ""

        ms = self._ms(symbol)

        async with self._semaphore:
            # Create new stop first
            try:
                new_id = await self._create_stop(ms, side, amount, new_stop)
                logger.info(f"✅ SL updated {symbol} → {new_stop}")
            except Exception as e:
                logger.error(f"❌ modify_stop create new: {e}")
                return None, str(e)

            # Then cancel old (non-critical)
            if old_stop_id and 'paper' not in str(old_stop_id):
                try:
                    await self._cancel_stop(ms, old_stop_id)
                except Exception as e:
                    logger.warning(f"⚠️  cancel old stop {symbol}: {e}")

            return new_id, ""

    async def modify_take(
        self, symbol: str, old_take_id: str,
        side: PositionSide, amount: float, new_take: float,
    ) -> Tuple[Optional[str], str]:

        if self.config.mode == 'paper':
            return "paper_take_new", ""

        ms = self._ms(symbol)

        async with self._semaphore:
            try:
                new_id = await self._create_take(ms, side, amount, new_take)
                logger.info(f"✅ TP updated {symbol} → {new_take}")
            except Exception as e:
                logger.error(f"❌ modify_take create new: {e}")
                return None, str(e)

            if old_take_id and 'paper' not in str(old_take_id):
                try:
                    await self._cancel_stop(ms, old_take_id)
                except Exception as e:
                    logger.warning(f"⚠️  cancel old take {symbol}: {e}")

            return new_id, ""

    # ─────────────────────────────────────
    # STOP ORDER
    # ─────────────────────────────────────

    async def _create_stop(
        self, ms: str, side: PositionSide,
        amount: float, stop_price: float,
    ) -> str:
        close_side = 'sell' if side == PositionSide.LONG else 'buy'
        ex         = self.config.exchange

        if ex == 'binance':
            order = await self._retry(
                self.exchange.create_order, ms,
                'STOP_MARKET', close_side, amount,
                params={
                    'stopPrice':     stop_price,
                    'reduceOnly':    True,
                    'closePosition': False,
                }
            )
            # Binance returns algoId for algo orders
            order_id = order.get('info', {}).get('algoId') or order.get('id', '')

        elif ex == 'bybit':
            # Position-level SL via setTradingStop — attaches to the position,
            # auto-cancels when position closes (same as Binance algo / HL TP_SL).
            # Calling this again overwrites the existing SL — no need to cancel first.
            market_id = self.exchange.market(ms)['id']
            await self._retry(
                self.exchange.privatePostV5PositionTradingStop,
                {
                    'category':    'linear',
                    'symbol':      market_id,
                    'stopLoss':    str(stop_price),
                    'tpslMode':    'Full',
                    'slTriggerBy': 'LastPrice',
                    'slOrderType': 'Market',
                    'positionIdx': 0,
                }
            )
            order_id = f"bybit_sl_{market_id}"

        elif ex == 'hyperliquid':
            # HL native TP_SL algo order — attached to position, auto-cancels on close.
            # stopLossPrice makes it a position-linked SL (same as Binance STOP_MARKET algo).
            order = await self._retry(
                self.exchange.create_order, ms,
                'market', close_side, amount, stop_price,
                params={
                    'stopLossPrice': stop_price,
                    'reduceOnly':    True,
                    'p':             '0',      # market execution
                    'algoType':      'TP_SL',
                }
            )
            order_id = order.get('id', '')

        elif ex == 'okx':
            # OKX conditional algo order: slTriggerPx + slOrdPx='-1' (market on trigger)
            # Use raw OKX param names to avoid ccxt stripping/remapping '-1'
            market_id = self.exchange.market(ms)['id']
            order = await self._retry(
                self.exchange.create_order, ms,
                'conditional', close_side, amount, None,
                params={
                    'slTriggerPx': str(stop_price),
                    'slOrdPx':     '-1',
                    'tdMode':      'isolated',
                    'reduceOnly':  True,
                    'instId':      market_id,
                }
            )
            order_id = order.get('id', '') or order.get('info', {}).get('algoId', '')

        else:
            raise ValueError(f"Unknown exchange: {ex}")

        return order_id

    # ─────────────────────────────────────
    # TAKE PROFIT ORDER
    # ─────────────────────────────────────

    async def _create_take(
        self, ms: str, side: PositionSide,
        amount: float, take_price: float,
    ) -> str:
        close_side = 'sell' if side == PositionSide.LONG else 'buy'
        ex         = self.config.exchange

        if ex == 'binance':
            order = await self._retry(
                self.exchange.create_order, ms,
                'TAKE_PROFIT_MARKET', close_side, amount,
                params={
                    'stopPrice':     take_price,
                    'reduceOnly':    True,
                    'closePosition': False,
                }
            )
            order_id = order.get('info', {}).get('algoId') or order.get('id', '')

        elif ex == 'bybit':
            # Position-level TP via setTradingStop — same as SL, attached to position.
            market_id = self.exchange.market(ms)['id']
            await self._retry(
                self.exchange.privatePostV5PositionTradingStop,
                {
                    'category':    'linear',
                    'symbol':      market_id,
                    'takeProfit':  str(take_price),
                    'tpslMode':    'Full',
                    'tpTriggerBy': 'LastPrice',
                    'tpOrderType': 'Market',
                    'positionIdx': 0,
                }
            )
            order_id = f"bybit_tp_{market_id}"

        elif ex == 'hyperliquid':
            # HL native TP_SL order — attached to position, auto-cancels when position closes.
            # Uses takeProfitPrice + algoType=TP_SL (verified in CrCraft project).
            order = await self._retry(
                self.exchange.create_order, ms,
                'market', close_side, amount, take_price,
                params={
                    'takeProfitPrice': take_price,
                    'reduceOnly':      True,
                    'p':               '0',       # market execution (max slippage)
                    'algoType':        'TP_SL',
                }
            )
            order_id = order.get('id', '')

        elif ex == 'okx':
            # OKX conditional algo order: tpTriggerPx + tpOrdPx='-1' (market on trigger)
            market_id = self.exchange.market(ms)['id']
            order = await self._retry(
                self.exchange.create_order, ms,
                'conditional', close_side, amount, None,
                params={
                    'tpTriggerPx': str(take_price),
                    'tpOrdPx':     '-1',
                    'tdMode':      'isolated',
                    'reduceOnly':  True,
                    'instId':      market_id,
                }
            )
            order_id = order.get('id', '') or order.get('info', {}).get('algoId', '')

        else:
            raise ValueError(f"Unknown exchange: {ex}")

        return order_id

    # ─────────────────────────────────────
    # CANCEL STOP
    # ─────────────────────────────────────

    async def _cancel_stop(self, ms: str, stop_id: str) -> None:
        ex = self.config.exchange
        if ex == 'binance':
            # Binance: cancel algo order by algoId
            market_id = self.exchange.market(ms)['id']
            await self.exchange.fapiPrivateDeleteAlgoOrder({
                'symbol':  market_id,
                'algoId':  stop_id,
            })
        elif ex == 'okx':
            # OKX algo orders require stop=True flag to hit the algo cancel endpoint
            await self.exchange.cancel_order(stop_id, ms, {'stop': True})
        elif ex == 'bybit':
            # Position-level SL/TP — setTradingStop overwrites in-place when _create_stop/take
            # is called with a new price, so the "old" value is already gone. No-op here.
            pass
        elif ex == 'hyperliquid':
            # HL auto-cancels the old stop when a new one is created for the same position.
            # Suppress "already canceled or filled" — it's expected, not an error.
            try:
                await self.exchange.cancel_order(stop_id, ms)
            except Exception as e:
                if 'already canceled' in str(e).lower() or 'never placed' in str(e).lower():
                    pass  # expected — HL already cleaned it up
                else:
                    raise
        else:
            await self.exchange.cancel_order(stop_id, ms)

    # ─────────────────────────────────────
    # RETRY
    # ─────────────────────────────────────

    async def _retry(
        self, func, *args,
        max_retries: int = 5, base_delay: float = 0.5,
        **kwargs
    ):
        for attempt in range(1, max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if attempt == max_retries:
                    raise
                delay = min(base_delay * (2 ** (attempt - 1)), 10)
                logger.warning(
                    f"Retry {attempt}/{max_retries} {func.__name__} "
                    f"in {delay}s: {e}"
                )
                await asyncio.sleep(delay)
