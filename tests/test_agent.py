"""
test_agent.py — Exchange operations test for Trading Agent.

Tests full cycle:
  1.  Connect to exchange
  2.  Check balance
  3.  Get market params (price precision, amount step, min notional)
  4.  Get current price
  5.  Check position mode (must be One-Way)
  6.  Set leverage
  7.  Calculate position params (amount, SL -2%, TP +4% = 2×SL)
  8.  Open position (LONG or SHORT)
  9.  Verify SL + TP are set on exchange
  10. Pause — manual check in browser/app
  11. Modify SL (+0.25% shift)
  12. Pause — verify updated SL
  13. Close position (market)
  14. Safe-close test (close already-closed position, must not error)

Run:
  python tests/test_agent.py --exchange binance
  python tests/test_agent.py --exchange bybit
  python tests/test_agent.py --exchange hyperliquid
  python tests/test_agent.py --exchange okx
  python tests/test_agent.py --exchange binance --symbol ETH --side SHORT --mode trade

.env must contain API keys for the selected exchange.
"""

import asyncio
import argparse
import logging
import math
import sys
import os
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()

from agent.config import load_config, AgentConfig
from agent.order_executor import OrderExecutor
from agent.state import PositionSide, OrderParams

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────

from agent.logger import setup_logging
from agent.version import VERSION as __version__

setup_logging(level="INFO", log_file="test_agent")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# CANDIDATE SYMBOLS FOR AUTO-SELECTION
# Edit this list to change fallback coins.
# ─────────────────────────────────────────

CANDIDATE_SYMBOLS = ["ETH", "ARB", "TRX", "HYP"]

# ─────────────────────────────────────────
# ARGS
# ─────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Trading Agent Exchange Test")
    parser.add_argument(
        "--exchange", type=str, default=None,
        choices=["binance", "bybit", "hyperliquid", "okx"],
        help="Exchange to test (default: from .env)"
    )
    parser.add_argument(
        "--symbol", type=str, default="ETH",
        help="Symbol to test (default: BTC)"
    )
    parser.add_argument(
        "--side", type=str, default="LONG",
        choices=["LONG", "SHORT"],
        help="Position side (default: LONG)"
    )
    parser.add_argument(
        "--mode", type=str, default="trade",
        choices=["paper", "trade"],
        help="Mode: paper (no orders) or trade (real orders)"
    )
    return parser.parse_args()


# ─────────────────────────────────────────
# SYMBOL AUTO-SELECTION
# ─────────────────────────────────────────

async def pick_free_symbol(
    config:    AgentConfig,
    preferred: str,
    candidates: list,
) -> str:
    """
    Returns the first symbol from [preferred] + candidates that has
    no open futures position on the exchange.

    If all candidates are occupied — returns preferred and logs a warning.
    If exchange is unreachable — returns preferred silently.
    """
    executor = OrderExecutor(config)
    try:
        ok = await executor.connect()
        if not ok:
            logger.warning("⚠️  [pick_free_symbol] Could not connect — using default symbol")
            return preferred

        open_syms = await executor.fetch_open_symbols()

        if open_syms:
            logger.info(f"📊 Open positions on exchange: {', '.join(sorted(open_syms))}")
        else:
            logger.info("📊 No open positions on exchange")

        all_candidates = [preferred] + [s for s in candidates if s != preferred]
        for sym in all_candidates:
            if sym not in open_syms:
                if sym != preferred:
                    logger.info(
                        f"⚠️  {preferred} has an open position — "
                        f"switching test symbol to {sym}"
                    )
                return sym

        logger.warning(
            f"⚠️  All candidate symbols have open positions: {all_candidates}\n"
            f"   Proceeding with {preferred} anyway — close it manually if needed."
        )
        return preferred

    finally:
        try:
            await executor.disconnect()
        except Exception:
            pass


# ─────────────────────────────────────────
# MAIN TEST
# ─────────────────────────────────────────

async def test_exchange(
    config: AgentConfig,
    symbol: str,
    side:   PositionSide,
) -> tuple[bool, str]:

    executor = OrderExecutor(config)
    errors   = []

    logger.info("=" * 55)
    logger.info(
        f"🚀 EXCHANGE TEST: {config.exchange.upper()} | "
        f"{symbol} {side.value} | mode: {config.mode.upper()}"
    )
    logger.info("=" * 55)

    try:
        # ── STEP 1: Connect ───────────────────────────────
        logger.info("⏳ STEP 1: Connecting to exchange...")
        ok = await executor.connect()
        if not ok:
            return False, "Connection failed"
        logger.info(f"✅ Connected to {config.exchange.upper()}")

        # ── STEP 2: Balance ───────────────────────────────
        logger.info("⏳ STEP 2: Getting balance...")
        total, free, used = await executor.get_balance()
        logger.info(f"✅ Balance: total={total:.2f}  free={free:.2f}  used={used:.2f} USDT")

        if free < 5 and config.mode == 'trade':
            return False, f"Insufficient balance: {free:.2f} USDT"

        # ── STEP 3: Market params ─────────────────────────
        logger.info(f"⏳ STEP 3: Market params for {symbol}...")
        price_prec, amount_step, min_notional = executor.get_market_params(symbol)
        logger.info(
            f"✅ price_prec={price_prec}  amount_step={amount_step}  "
            f"min_notional={min_notional}"
        )

        # ── STEP 4: Current price ─────────────────────────
        logger.info(f"⏳ STEP 4: Current price for {symbol}...")
        price = await executor.get_ticker(symbol)
        if price <= 0:
            return False, "Could not get price"
        logger.info(f"✅ {symbol} price: {price}")

        # ── STEP 5: Position mode — check and ensure One-Way ─
        logger.info("⏳ STEP 5: Checking position mode...")
        if config.mode == 'paper':
            logger.info("📋 [PAPER] Position mode check skipped")
        else:
            # connect() already called _ensure_one_way_mode()
            # Here we verify the result and report clearly
            pos_mode = await executor.get_position_mode()

            if pos_mode == 'one-way':
                logger.info(f"✅ Position mode: ONE-WAY ✓")

            elif pos_mode == 'hedge':
                # connect() tried to switch but failed
                errors.append("position_mode: Still in Hedge mode after connect()")
                logger.error(
                    "❌ Exchange is still in HEDGE mode!\n"
                    "   connect() attempted to switch but failed.\n"
                    "   Switch manually: exchange settings → Position Mode → One-Way\n"
                    "   Then restart the test."
                )
                return False, "\n".join(errors)

            else:
                # unknown — log and continue cautiously
                logger.warning(
                    f"⚠️  Position mode: {pos_mode} (could not confirm One-Way)\n"
                    f"   Proceeding, but orders may fail if exchange is in Hedge mode."
                )

            logger.info(
                f"   is_one_way flag: {executor.is_one_way} | "
                f"exchange reported: {pos_mode}"
            )

        # ── STEP 6: Set leverage ──────────────────────────
        logger.info(f"⏳ STEP 6: Setting leverage x{config.leverage}...")
        await executor.set_leverage(symbol, config.leverage)
        logger.info(f"✅ Leverage x{config.leverage} set")

        # ── STEP 7: Calculate position params ─────────────
        # Ensure volume is large enough to buy at least one amount_step
        min_vol_for_step = amount_step * price * 1.1
        volume      = max(min_notional * 1.1, min_vol_for_step, 10.0)
        amount      = math.floor(volume / price / amount_step) * amount_step
        sl_pct      = 0.02                  # 2% stop loss
        tp_pct      = sl_pct * 2            # 4% take profit — always 2× SL distance

        if side == PositionSide.LONG:
            stop_price = round(price * (1 - sl_pct), price_prec)
            take_price = round(price * (1 + tp_pct), price_prec)
        else:
            stop_price = round(price * (1 + sl_pct), price_prec)
            take_price = round(price * (1 - tp_pct), price_prec)

        params = OrderParams(
            symbol      = f"{symbol}USDT",
            side        = side,
            amount      = amount,
            volume      = volume,
            margin      = volume / config.leverage,
            leverage    = config.leverage,
            stop_price  = stop_price,
            take_price  = take_price,
            sl_pct      = sl_pct,
            tp_pct      = tp_pct,
            entry_price = price,
        )
        logger.info(
            f"📋 Params: amount={amount}  volume={volume:.2f}$  "
            f"entry≈{price}  SL={stop_price} (-{sl_pct*100:.0f}%)  "
            f"TP={take_price} (+{tp_pct*100:.0f}% = 2×SL)"
            if side == PositionSide.LONG else
            f"📋 Params: amount={amount}  volume={volume:.2f}$  "
            f"entry≈{price}  SL={stop_price} (+{sl_pct*100:.0f}%)  "
            f"TP={take_price} (-{tp_pct*100:.0f}% = 2×SL)"
        )

        # ── STEP 8: Open position ─────────────────────────
        logger.info(f"⏳ STEP 8: Opening {side.value} {symbol}...")
        pos_id, stop_id, take_ids, err = await executor.open_position(params)

        if err and not pos_id:
            errors.append(f"open_position: {err}")
            logger.error(f"❌ Open failed: {err}")
        else:
            logger.info(
                f"✅ Position opened | pos:{pos_id}  stop:{stop_id}  takes:{take_ids}"
            )
            if err:
                logger.warning(f"⚠️  Partial error: {err}")

        # ── STEP 9: Verify SL + TP orders on exchange ─────
        logger.info("⏳ STEP 9: Verifying SL/TP orders on exchange...")
        if config.mode == 'paper':
            logger.info("📋 [PAPER] Order verification skipped")
        elif pos_id:
            try:
                ms_sym = executor._ms(symbol)

                # Bybit and Hyperliquid use position-level SL/TP (setTradingStop / TP_SL algo).
                # These do NOT appear in fetch_open_orders — they are stored on the position itself.
                position_level_sl = config.exchange in ('bybit', 'hyperliquid')

                if position_level_sl:
                    # Verify via fetch_positions: check stopLossPrice / takeProfitPrice
                    positions = await executor.exchange.fetch_positions([ms_sym])
                    pos = next((p for p in positions if p.get('contracts', 0) != 0), None)
                    sl_price_actual = float(pos.get('stopLossPrice') or 0) if pos else 0
                    tp_price_actual = float(pos.get('takeProfitPrice') or 0) if pos else 0

                    sl_ok       = bool(stop_id) and sl_price_actual > 0
                    tp_ok       = bool(take_ids) and tp_price_actual > 0
                    direction_ok = True
                    if sl_price_actual and side == PositionSide.LONG and sl_price_actual >= price:
                        logger.error(f"❌ SL {sl_price_actual} is ABOVE entry {price} for LONG!")
                        direction_ok = False
                    if sl_price_actual and side == PositionSide.SHORT and sl_price_actual <= price:
                        logger.error(f"❌ SL {sl_price_actual} is BELOW entry {price} for SHORT!")
                        direction_ok = False
                    if tp_price_actual and side == PositionSide.LONG and tp_price_actual <= price:
                        logger.error(f"❌ TP {tp_price_actual} is BELOW entry {price} for LONG!")
                        direction_ok = False
                    if tp_price_actual and side == PositionSide.SHORT and tp_price_actual >= price:
                        logger.error(f"❌ TP {tp_price_actual} is ABOVE entry {price} for SHORT!")
                        direction_ok = False

                    logger.info(
                        f"✅ Position SL/TP: "
                        f"SL={sl_price_actual if sl_ok else '❌ not set'} | "
                        f"TP={tp_price_actual if tp_ok else '❌ not set'} | "
                        f"Direction={'✅' if direction_ok else '❌ wrong'}"
                    )

                else:
                    # OKX: conditional algo orders don't appear in fetch_open_orders —
                    # verify each order individually by ID using fetch_order + stop=True flag.
                    # Other exchanges: use fetch_open_orders and match by ID.
                    if config.exchange == 'okx':
                        sl_ok = False
                        tp_ok = False
                        direction_ok = True
                        if stop_id:
                            try:
                                o = await executor.exchange.fetch_order(stop_id, ms_sym, {'stop': True})
                                sl_ok = o.get('status') in ('open', 'live', 'untriggered')
                                o_price = float(o.get('stopPrice') or o.get('triggerPrice') or 0)
                                if o_price and side == PositionSide.LONG and o_price >= price:
                                    logger.error(f"❌ SL {o_price} is ABOVE entry {price} for LONG!")
                                    direction_ok = False
                                if o_price and side == PositionSide.SHORT and o_price <= price:
                                    logger.error(f"❌ SL {o_price} is BELOW entry {price} for SHORT!")
                                    direction_ok = False
                            except Exception as e:
                                logger.warning(f"⚠️  fetch SL order: {e}")
                        if take_ids:
                            try:
                                o = await executor.exchange.fetch_order(take_ids[0], ms_sym, {'stop': True})
                                tp_ok = o.get('status') in ('open', 'live', 'untriggered')
                                o_price = float(o.get('stopPrice') or o.get('triggerPrice') or 0)
                                if o_price and side == PositionSide.LONG and o_price <= price:
                                    logger.error(f"❌ TP {o_price} is BELOW entry {price} for LONG!")
                                    direction_ok = False
                                if o_price and side == PositionSide.SHORT and o_price >= price:
                                    logger.error(f"❌ TP {o_price} is ABOVE entry {price} for SHORT!")
                                    direction_ok = False
                            except Exception as e:
                                logger.warning(f"⚠️  fetch TP order: {e}")
                        logger.info(
                            f"✅ OKX algo orders: "
                            f"SL={'✅' if sl_ok else '❌ not found'} | "
                            f"TP={'✅' if tp_ok else '❌ not found'} | "
                            f"Direction={'✅' if direction_ok else '❌ wrong'}"
                        )
                    else:
                        open_orders = await executor.exchange.fetch_open_orders(ms_sym)

                        sl_ok = stop_id and any(
                            str(o.get('id')) == str(stop_id) for o in open_orders
                        )
                        tp_ok = take_ids and any(
                            str(o.get('id')) == str(take_ids[0]) for o in open_orders
                        )

                        direction_ok = True
                        for o in open_orders:
                            o_price = o.get('stopPrice') or o.get('price') or 0
                            o_id    = str(o.get('id', ''))
                            if o_price and stop_id and o_id == str(stop_id):
                                if side == PositionSide.LONG and o_price >= price:
                                    logger.error(f"❌ SL price {o_price} is ABOVE entry {price} for LONG!")
                                    direction_ok = False
                                elif side == PositionSide.SHORT and o_price <= price:
                                    logger.error(f"❌ SL price {o_price} is BELOW entry {price} for SHORT!")
                                    direction_ok = False
                            if o_price and take_ids and o_id == str(take_ids[0]):
                                if side == PositionSide.LONG and o_price <= price:
                                    logger.error(f"❌ TP price {o_price} is BELOW entry {price} for LONG!")
                                    direction_ok = False
                                elif side == PositionSide.SHORT and o_price >= price:
                                    logger.error(f"❌ TP price {o_price} is ABOVE entry {price} for SHORT!")
                                    direction_ok = False

                        logger.info(
                            f"✅ Orders on exchange: {len(open_orders)} open | "
                            f"SL={'✅' if sl_ok else '❌ not found'} | "
                            f"TP={'✅' if tp_ok else '❌ not found'} | "
                            f"Direction={'✅' if direction_ok else '❌ wrong'}"
                        )

                if not sl_ok:
                    errors.append("verify_orders: SL order not found on exchange")
                if not tp_ok:
                    errors.append("verify_orders: TP order not found on exchange")
                if not direction_ok:
                    errors.append("verify_orders: SL/TP direction is wrong")

            except Exception as e:
                logger.warning(f"⚠️  Order verification failed: {e}")

        # ── STEP 10: Pause — manual check ─────────────────
        wait = 10 if config.mode == 'trade' else 0
        if wait:
            logger.info(
                f"⏳ STEP 10: Pause {wait}s — verify position and orders on "
                f"{config.exchange.upper()} in browser or app"
            )
            await asyncio.sleep(wait)

        # ── STEP 11: Modify SL ────────────────────────────
        if stop_id:
            logger.info("⏳ STEP 11: Modifying SL...")
            # Move SL 0.25% closer to entry (min meaningful shift for HL/Bybit algo orders)
            new_stop = (
                round(stop_price * 1.0025, price_prec)
                if side == PositionSide.LONG
                else round(stop_price * 0.9975, price_prec)
            )
            new_stop_id, err = await executor.modify_stop(
                symbol, stop_id, side, amount, new_stop
            )
            if new_stop_id:
                logger.info(f"✅ SL updated → {new_stop}  new_id:{new_stop_id}")
                stop_id = new_stop_id
            else:
                errors.append(f"modify_stop: {err}")
                logger.error(f"❌ modify_stop failed: {err}")

        # ── STEP 12: Pause after modify ───────────────────
        if wait:
            logger.info(f"⏳ STEP 12: Pause {wait}s — verify updated SL")
            await asyncio.sleep(wait)

        # ── STEP 13: Close position ───────────────────────
        logger.info(f"⏳ STEP 13: Closing position {symbol}...")
        current_price = await executor.get_ticker(symbol)
        ok, err = await executor.close_position(symbol, amount, side, current_price)
        if ok:
            logger.info(f"✅ Position closed @ {current_price}")
        else:
            errors.append(f"close_position: {err}")
            logger.error(f"❌ Close failed: {err}")

        # ── STEP 14: Safe-close test ──────────────────────
        logger.info("⏳ STEP 14: Safe-close test (closing already-closed position)...")
        ok2, err2 = await executor.close_position(symbol, amount, side, current_price)
        if ok2:
            logger.info("✅ Safe-close works correctly")
        else:
            errors.append(f"safe_close: {err2}")
            logger.error(f"❌ Safe-close error: {err2}")

    except Exception as e:
        errors.append(f"Critical: {e}")
        logger.error(f"💥 Critical error: {e}", exc_info=True)
    finally:
        await executor.disconnect()

    # ── RESULT ────────────────────────────────────────────
    logger.info("=" * 55)
    if errors:
        logger.error(f"❌ TEST FAILED ({len(errors)} error(s)):")
        for e in errors:
            logger.error(f"   • {e}")
        return False, "\n".join(errors)
    else:
        logger.info("✅✅✅ ALL STEPS PASSED")
        return True, ""


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────


def confirm_mode(mode: str, symbol: str, exchange: str) -> str:
    """
    Ask user to confirm test mode.
    Returns confirmed mode: 'trade' or 'paper'.
    """
    print()
    print("=" * 55)
    print("  TRADING AGENT — EXCHANGE TEST")
    print("=" * 55)
    print(f"  Exchange : {exchange.upper()}")
    print(f"  Symbol   : {symbol}")
    print()
    print("  Select test mode:")
    print()
    print("  [1] TRADE  — real orders, minimum size  (recommended)")
    print("  [2] PAPER  — no real orders, logic only")
    print()

    if mode == 'trade':
        default_choice = '1'
        default_label  = 'TRADE'
    else:
        default_choice = '2'
        default_label  = 'PAPER'

    try:
        answer = input(
            f"  Enter 1 or 2  [default: {default_choice} — {default_label}]: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ''

    if answer == '2':
        selected = 'paper'
    elif answer == '1':
        selected = 'trade'
    elif answer == '':
        selected = mode   # use default
    else:
        print(f"  Invalid input '{answer}' — using default: {default_label}")
        selected = mode

    print()

    if selected == 'trade':
        print("  ⚠️  TRADE MODE selected.")
        print()
        print("  The test will place a REAL position on the exchange")
        print(f"  for the minimum allowed size on {symbol}.")
        print()
        print("  You will pay commission for:")
        print("    • opening  the position  (~0.04–0.05% of volume)")
        print("    • closing  the position  (~0.04–0.05% of volume)")
        print()
        print("  This is required to fully verify exchange connectivity,")
        print("  order placement, SL/TP, and position mode.")
        print()
        try:
            confirm = input("  Proceed with TRADE mode? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = 'n'

        if confirm not in ('y', 'yes'):
            print("  Switched to PAPER mode.")
            selected = 'paper'

    print()
    print(f"  ✅ Running in {selected.upper()} mode")
    print("=" * 55)
    print()

    return selected


async def main():
    args   = parse_args()
    config = load_config()

    if args.exchange:
        config.exchange = args.exchange
    if args.mode:
        config.mode = args.mode

    side = PositionSide.LONG if args.side == 'LONG' else PositionSide.SHORT

    # Interactive mode confirmation (skip if --mode explicitly passed via CI/pipe)
    import sys as _sys
    if _sys.stdin.isatty():
        config.mode = confirm_mode(config.mode, args.symbol, config.exchange)
    else:
        logger.info(f"[non-interactive] using mode={config.mode}")

    # ── Symbol auto-selection ─────────────────────────────
    symbol = await pick_free_symbol(config, args.symbol, CANDIDATE_SYMBOLS)

    logger.info(f"🚀 Trading Agent Exchange Test v{__version__}")
    logger.info(f"   Exchange: {config.exchange.upper()}")
    logger.info(f"   Symbol:   {symbol}")
    logger.info(f"   Side:     {side.value}")
    logger.info(f"   Mode:     {config.mode.upper()}")
    logger.info("")

    ok, errors = await test_exchange(config, symbol, side)

    if ok:
        logger.info("🎉 All operations completed successfully!")
    else:
        logger.error("💔 Test failed. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⛔ Test stopped by user")
    except Exception as e:
        logger.error(f"💥 {e}")
        sys.exit(1)
    finally:
        logger.info("👋 Test finished")
