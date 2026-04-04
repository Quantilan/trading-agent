# agent/pnl_image.py
"""
Graphical PnL report image for /positions command.
Uses matplotlib (already a dependency) — no extra packages needed.
"""

import io
from datetime import datetime, timezone
from typing import List, Optional

from .state import Position, PositionSide


def generate_pnl_image(
    exchange:      str,
    mode:          str,
    leverage:      int,
    balance:       float,
    start_balance: float,
    equity_history: List[float],
    positions:     List[Position],   # unrealized_pnl must be set before calling
    stbc:          str = "USDT",
) -> Optional[bytes]:
    """
    Render positions PnL report as PNG bytes.
    Returns None if matplotlib is unavailable.

    Color logic:
      - Overall PnL positive → accent green, negative → accent red.
      - Per-position uPnL positive → green, negative → red.
      - LONG side → green label, SHORT → red label.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return None

    # ── Overall PnL ──────────────────────────────────────────────
    rpnl     = balance - start_balance
    rpnl_pct = (rpnl / start_balance * 100) if start_balance > 0 else 0.0

    # ── Max drawdown from equity history ─────────────────────────
    max_dd = 0.0
    if len(equity_history) >= 2:
        peak = equity_history[0]
        for v in equity_history:
            peak   = max(peak, v)
            dd     = (peak - v) / peak * 100 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

    # ── Per-position rows ─────────────────────────────────────────
    rows = []
    total_upnl  = 0.0
    total_margin = 0.0
    for p in positions:
        upnl = p.unrealized_pnl
        total_upnl   += upnl
        total_margin += p.margin
        roe_pct = (upnl / p.margin * 100) if p.margin > 0 else 0.0
        rows.append({
            "symbol":   p.symbol,
            "side":     p.side.value,
            "margin":   p.margin,
            "upnl":     upnl,
            "roe":      roe_pct,
        })

    total_upnl_pct = (total_upnl / balance * 100) if balance > 0 else 0.0

    # ── Palette ───────────────────────────────────────────────────
    is_positive = rpnl >= 0
    ACCENT  = "#00ffad" if is_positive else "#ff4b4b"
    BG      = "#0f0f0f"
    ROW_ALT = "#161616"
    COL_HDR = "#1a1a1a"
    WHITE   = "#FFFFFF"
    GRAY    = "#888888"
    DKGRAY  = "#444444"
    GREEN   = "#00ffad"
    RED     = "#ff4b4b"
    LINE    = "#333333"

    # ── Layout ───────────────────────────────────────────────────
    DPI        = 100
    IMG_W      = 900      # px
    ROW_H      = 48       # px per position row
    HEADER_H   = 210
    COL_HDR_H  = 35
    FOOTER_H   = 80
    MIN_ROWS   = 1        # keep image tall enough even with no positions

    n_rows   = max(MIN_ROWS, len(rows))
    img_h    = HEADER_H + COL_HDR_H + n_rows * ROW_H + FOOTER_H

    fig = plt.figure(figsize=(IMG_W / DPI, img_h / DPI), dpi=DPI)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, IMG_W)
    ax.set_ylim(img_h, 0)   # y=0 at top
    ax.axis("off")
    ax.set_facecolor(BG)

    # ── Header ───────────────────────────────────────────────────
    ax.text(40, 35,  exchange.upper(),
            color=ACCENT, fontsize=22, fontweight="bold", va="top")
    ax.text(40, 72,
            f"PnL: {rpnl:+.2f}{stbc} ({rpnl_pct:+.2f}%)",
            color=ACCENT, fontsize=14, va="top", fontweight="bold")
    ax.text(40, 107,
            f"Balance: {balance:.2f}{stbc}",
            color=WHITE, fontsize=14, va="top")
    ax.text(40, 138,
            f"Mode: {mode}  |  Leverage: x{leverage}",
            color=GRAY, fontsize=12, va="top")
    ax.text(40, 165,
            f"Start: {start_balance:.2f}$ "
            f" |  Max Drawdown: -{max_dd:.2f}%"
            f"  |  Margin used: {total_margin:.1f}$",
            color=GRAY, fontsize=11, va="top")

    # ── Column header row ─────────────────────────────────────────
    y_col = HEADER_H
    ax.add_patch(mpatches.Rectangle((0, y_col), IMG_W, COL_HDR_H, color=COL_HDR, zorder=1))

    COLS = {"sym": 40, "side": 170, "margin": 320, "upnl": 490, "roe": 680}
    for x, label in [
        (COLS["sym"],    "ASSET"),
        (COLS["side"],   "SIDE"),
        (COLS["margin"], "MARGIN"),
        (COLS["upnl"],   "uPNL"),
        (COLS["roe"],    "(ROE%)"),
    ]:
        ax.text(x, y_col + 8, label, color=GRAY, fontsize=11, va="top", zorder=2)

    # ── Position rows ─────────────────────────────────────────────
    y = HEADER_H + COL_HDR_H
    if not rows:
        ax.text(40, y + 14, "No open positions", color=GRAY, fontsize=13, va="top")
    else:
        for i, row in enumerate(rows):
            if i % 2 == 0:
                ax.add_patch(mpatches.Rectangle((0, y), IMG_W, ROW_H, color=ROW_ALT, zorder=1))

            upnl_color = GREEN if row["upnl"] >= 0 else RED
            side_color = GREEN if row["side"] == "LONG" else RED

            ax.text(COLS["sym"],    y + 11, row["symbol"],
                    color=WHITE,      fontsize=14, va="top", fontweight="bold",    zorder=2)
            ax.text(COLS["side"],   y + 11, row["side"],
                    color=side_color, fontsize=14, va="top", fontweight="bold",    zorder=2)
            ax.text(COLS["margin"], y + 11, f"{row['margin']:.1f}$",
                    color=WHITE,      fontsize=14, va="top",                       zorder=2)
            ax.text(COLS["upnl"],   y + 11, f"{row['upnl']:+.2f}$",
                    color=upnl_color, fontsize=14, va="top", fontweight="bold",    zorder=2)
            ax.text(COLS["roe"],    y + 14, f"({row['roe']:+.1f}%)",
                    color=GRAY,       fontsize=11, va="top",                       zorder=2)
            y += ROW_H

    # ── Footer ────────────────────────────────────────────────────
    y += 15
    ax.add_patch(mpatches.Rectangle((0, y), IMG_W, 1, color=LINE, zorder=1))
    y += 18
    upnl_color = GREEN if total_upnl >= 0 else RED
    ax.text(40, y,
            f"TOTAL uPnL: {total_upnl:+.1f}$  ({total_upnl_pct:+.2f}%)",
            color=upnl_color, fontsize=13, va="top", fontweight="bold", zorder=2)
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    ax.text(IMG_W - 40, y + 4, now_str,
            color=DKGRAY, fontsize=10, va="top", ha="right", zorder=2)

    # ── Export ───────────────────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
