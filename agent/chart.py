# agent/chart.py
"""
Candlestick chart generator for Telegram notifications.

Rules:
  - Header (title):  position info if position is open, else "SYMBOL TF"
  - Footer watermark: EXCHANGE | TF: xx | Generated at: dd/mm/yyyy HH:MM:SS UTC
  - Events:
      open       — entry marker (▲/▼) at last candle, OPEN/STOP/TP lines
      modify_sl  — position in header, updated STOP line (pos.stop_price = new stop before call)
      close      — simple header, entry marker + X at close candle, no lines

Returns PNG as bytes (BytesIO).  Returns None on error.
"""

import gc
import io
import logging
from datetime import datetime, timezone
from typing import List, Optional

import matplotlib
matplotlib.use('Agg')

import matplotlib.patches as mpatches
import matplotlib.lines as mlines
from matplotlib.collections import PatchCollection
import matplotlib.pyplot as plt

from .state import Position, PositionSide

logger = logging.getLogger(__name__)

# ─── Candle interval → seconds ────────────────────────────────────────────────
_TF_SECONDS: dict = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400, '1d': 86400,
}


def draw_chart(
    ohlcv: List[list],
    pos: Optional[Position],
    event: str,           # 'open' | 'close' | 'modify_sl'
    exchange_name: str,
    tf: str,
    base_symbol: str,
) -> Optional[bytes]:
    """
    Draw candlestick chart and return PNG bytes.

    ohlcv — ccxt format: [[timestamp_ms, open, high, low, close, volume], ...]
    pos   — current Position (None safe; used for title and markers)
    event — 'open' | 'close' | 'modify_sl'
    """
    if not ohlcv:
        return None

    fig = None
    patches = []
    lines = []
    collection = None

    try:
        # ── 1. Unpack OHLCV ───────────────────────────────────────────────────
        timestamps = [int(row[0]) for row in ohlcv]
        opens  = [float(row[1]) for row in ohlcv]
        highs  = [float(row[2]) for row in ohlcv]
        lows   = [float(row[3]) for row in ohlcv]
        closes = [float(row[4]) for row in ohlcv]
        n = len(ohlcv)

        # ── 2. Figure setup ───────────────────────────────────────────────────
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(12, 8), facecolor='#121212')
        ax.set_facecolor('#121212')

        # ── 3. Candlesticks ───────────────────────────────────────────────────
        width = 0.6
        for i in range(n):
            bull  = closes[i] >= opens[i]
            color = '#26a69a' if bull else '#ef5350'
            y     = opens[i] if bull else closes[i]
            h     = abs(closes[i] - opens[i]) or 1e-9
            rect  = mpatches.Rectangle((i - width / 2, y), width, h)
            rect.set_facecolor(color)
            rect.set_edgecolor(color)
            patches.append(rect)
            lines.append(mlines.Line2D([i, i], [lows[i], highs[i]], lw=1, color=color))

        collection = PatchCollection(patches, match_original=True)
        ax.add_collection(collection)
        for ln in lines:
            ax.add_line(ln)

        ax.set_xlim(-1, n)
        ax.autoscale_view()
        y_min, y_max = ax.get_ylim()

        # ── 4. Position markers and lines ─────────────────────────────────────
        if pos:
            is_long = pos.side == PositionSide.LONG
            pct     = pos.entry_price * 0.003
            last    = n - 1

            if event == 'open':
                # Entry marker at the last (current) candle
                y_m = lows[last] - pct if is_long else highs[last] + pct
                ax.scatter(last, y_m,
                           marker='^' if is_long else 'v',
                           color='#00ff00' if is_long else '#ff0000',
                           s=150, zorder=5, edgecolors='white', lw=1.0)

            elif event == 'close':
                # Try to find entry candle by timestamp
                tf_ms  = _TF_SECONDS.get(tf, 900) * 1000
                e_idx  = None
                for i, ts in enumerate(timestamps):
                    if abs(ts - pos.open_timestamp) < tf_ms:
                        e_idx = i
                        break

                if e_idx is not None:
                    y_e = lows[e_idx] - pct if is_long else highs[e_idx] + pct
                    ax.scatter(e_idx, y_e,
                               marker='^' if is_long else 'v',
                               color='#00ff00' if is_long else '#ff0000',
                               s=150, zorder=5, edgecolors='white', lw=1.0)
                    c_price = pos.close_price or closes[last]
                    ax.plot([e_idx, last], [pos.entry_price, c_price],
                            color='#00ff00' if is_long else '#ff0000',
                            linestyle='--', linewidth=1.0, alpha=0.6)

                # Close marker (X): green if profit, white if loss
                x_color = '#87eb1c' if pos.rpnl >= 0 else '#F1F6F6'
                x_y     = pos.close_price or closes[last]
                ax.scatter(last, x_y, marker='x', color=x_color, s=150, zorder=6, lw=2)

            # modify_sl: no new marker, OPEN/STOP lines drawn below

        # ── 5. Horizontal lines (open/modify_sl only) ─────────────────────────
        if pos and event in ('open', 'modify_sl'):
            # OPEN line
            ax.axhline(pos.entry_price, color='#067105', linestyle='-',
                       linewidth=2, alpha=0.6)
            ax.text(n + 0.2, pos.entry_price, ' OPEN',
                    color='#067105', va='center', fontsize=14, fontweight='bold',
                    bbox=dict(facecolor='#121212', alpha=0.7, edgecolor='none'))

            # STOP line — expand y range if needed
            s = pos.stop_price
            y_min = min(y_min, s * 0.999)
            y_max = max(y_max, s * 1.001)
            ax.set_ylim(y_min, y_max)
            ax.axhline(s, color='#ff4444', linestyle='--', linewidth=1.5, alpha=0.8)
            ax.text(n + 0.2, s, ' STOP',
                    color='#ff4444', va='center', fontsize=12, fontweight='bold')

            # TAKE line (if set and within visible range)
            if pos.take_price > 0:
                t = pos.take_price
                y_min = min(y_min, t * 0.999)
                y_max = max(y_max, t * 1.001)
                ax.set_ylim(y_min, y_max)
                ax.axhline(t, color='#ffd700', linestyle='--', linewidth=1.5, alpha=0.8)
                ax.text(n + 0.2, t, ' TAKE',
                        color='#ffd700', va='center', fontsize=12, fontweight='bold')

        # ── 6. Title ──────────────────────────────────────────────────────────
        if pos and event != 'close':
            upnl_pct = (pos.unrealized_pnl / pos.margin * 100) if pos.margin > 0 else 0.0
            take_str = f"  TP: {pos.take_price}" if pos.take_price > 0 else ""
            title = (
                f"{base_symbol} {tf}  {pos.side.value}  "
                f"size: {pos.amount} {base_symbol}    price: {pos.entry_price}\n"
                f"isol x{pos.leverage}  Margin: {round(pos.margin, 2)}  "
                f"uPnL: {upnl_pct:.2f}%  SL: {pos.stop_price}{take_str}"
            )
        else:
            title = f"{base_symbol} {tf}"

        ax.set_title(title, color='white', fontsize=14)

        # ── 7. X-axis ─────────────────────────────────────────────────────────
        step   = max(1, n // 6)
        ticks  = list(range(0, n, step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([
            datetime.fromtimestamp(timestamps[i] / 1000, tz=timezone.utc).strftime('%H:%M')
            for i in ticks
        ])
        ax.grid(True, color='gray', alpha=0.2, linestyle='--')
        ax.tick_params(axis='both', which='major', labelsize=12, colors='white')

        # ── 8. Footer watermark ───────────────────────────────────────────────
        now = datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M:%S UTC')
        fig.text(0.5, 0.02,
                 f"{exchange_name.upper()} | TF: {tf} | Generated at: {now}",
                 ha='center', va='bottom', color='gray', fontsize=12, fontstyle='italic')

        fig.patch.set_linewidth(4)
        fig.patch.set_edgecolor('white')

        plt.subplots_adjust(left=0.08, right=0.88, top=0.85, bottom=0.1)

        # ── 9. Render to bytes ────────────────────────────────────────────────
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150, pad_inches=0.1,
                    facecolor=fig.get_facecolor(), edgecolor=fig.get_edgecolor())
        buf.seek(0)
        image_bytes = buf.read()
        buf.close()

        return image_bytes

    except Exception as e:
        logger.error(f"[Chart] draw_chart {base_symbol}: {e}")
        return None

    finally:
        try:
            if fig is not None:
                plt.close(fig)
            plt.clf()
            plt.style.use('default')
        except Exception:
            pass
        del patches, lines
        if collection is not None:
            del collection
        gc.collect()
