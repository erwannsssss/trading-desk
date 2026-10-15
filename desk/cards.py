"""Trade of the week: a shareable 1080x1350 image card (Instagram portrait size) in the dashboard's style."""
from __future__ import annotations

import textwrap
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from desk import config

FONTS = config.ROOT / "assets" / "fonts"
PAPER, INK, INK2, INK3, RULE = "#EEF2F5", "#16233F", "#4A5772", "#7D889E", "#D5DCE4"
GAIN, LOSS, SIGNAL = "#0E7A5C", "#B3263E", "#E8B425"


def _font(kind: str, size: int, weight: int = 400, width: int = 100):
    try:
        if kind == "serif":
            f = ImageFont.truetype(str(FONTS / "SourceSerif4.ttf"), size)
            f.set_variation_by_axes([weight, min(60, max(8, size // 2))])
        else:
            f = ImageFont.truetype(str(FONTS / "Archivo.ttf"), size)
            f.set_variation_by_axes([weight, width])
        return f
    except Exception:
        return ImageFont.load_default(size)


def _p(x: float) -> str:
    return f"{x:,.2f}" if abs(x) >= 10 else f"{x:.4f}" if abs(x) >= 0.01 else f"{x:.8f}"


def trade_card(trade: dict, week_label: str, called_by: str, argued_by: str, out_path) -> None:
    W, H, M = 1080, 1350, 84
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    pnl = trade.get("pnl_usd") or 0
    tone = GAIN if pnl >= 0 else LOSS

    d.text((M, 78), "Trade of the week", font=_font("sans", 40, 700, 110), fill=INK)
    brand = "Erwann's Trading Desk"
    bf = _font("sans", 28, 700, 112)
    d.text((W - M - d.textlength(brand, font=bf), 88), brand, font=bf, fill=INK)
    note = "Paper trading"
    nf = _font("sans", 24, 400)
    d.text((W - M - d.textlength(note, font=nf), 136), note, font=nf, fill=INK3)
    d.text((M, 132), week_label, font=_font("sans", 30, 500), fill=INK3)
    d.rectangle([M, 196, W - M, 199], fill=INK)

    sym = trade["symbol"]
    size = 190 if len(sym) <= 5 else 150 if len(sym) <= 7 else 120
    d.text((M - 6, 236), sym, font=_font("sans", size, 800, 125), fill=INK)
    side = "Calls" if trade.get("option_type") == "call" else "Puts" if trade.get("option_type") == "put" else \
        ("Long" if trade.get("direction") == "long" else "Short")
    pill_font = _font("sans", 32, 700, 110)
    pw = d.textlength(side, font=pill_font) + 40
    y_pill = 236 + size + 24
    side_color = GAIN if side in ("Long", "Calls") else LOSS
    d.rounded_rectangle([M, y_pill, M + pw, y_pill + 54], radius=10, outline=side_color, width=3)
    d.text((M + 20, y_pill + 9), side, font=pill_font, fill=side_color)
    meta = f"{(trade.get('asset') or '').title()}, {trade.get('style') or ''}"
    d.text((M + pw + 22, y_pill + 11), meta, font=_font("sans", 30, 500), fill=INK2)

    y = y_pill + 110
    sign = "+" if pnl >= 0 else "-"
    d.text((M - 4, y), f"{sign}${abs(pnl):,.2f}", font=_font("sans", 128, 800, 118), fill=tone)
    r = trade.get("r_result")
    d.text((M, y + 150), (f"{r:+.1f}R, " if r is not None else "") + f"{_p(trade['entry_price'])} to {_p(trade['exit_price'])}",
           font=_font("sans", 38, 600), fill=INK)
    held = ""
    try:
        days = (datetime.fromisoformat(trade["closed_at"]) - datetime.fromisoformat(trade["opened_at"])).days
        held = f"Held {days} day{'s' if days != 1 else ''}, " if days else "Closed the same day, "
    except Exception:
        pass
    d.text((M, y + 202), f"{held}{trade.get('exit_reason') or ''}", font=_font("sans", 30, 400), fill=INK2)

    y += 290
    d.rectangle([M, y, W - M, y + 1], fill=RULE)
    thesis = (trade.get("thesis") or "").strip()
    serif = _font("serif", 36, 400)
    fy = H - 150
    max_lines = max(1, (fy - (y + 34) - 20) // 52)
    wrapped = textwrap.wrap(thesis, width=48)
    lines = wrapped[:max_lines]
    if len(wrapped) > max_lines:
        lines[-1] = lines[-1].rstrip(".,;") + "..."
    for i, line in enumerate(lines):
        d.text((M, y + 34 + i * 52), line, font=serif, fill=INK)

    d.rectangle([M, fy, W - M, fy + 1], fill=RULE)
    d.text((M, fy + 26), f"Called by {called_by}", font=_font("sans", 30, 600), fill=INK)
    d.text((M, fy + 66), f"Argued by {argued_by}", font=_font("sans", 30, 400), fill=INK2)
    d.rectangle([0, H - 14, W, H], fill=SIGNAL)
    img.save(out_path, "PNG", optimize=True)
