"""Trade guards that keep the desk from quietly doubling up or walking into avoidable risk.

  same_bet        at most 2 open positions leaning the same way on one currency, 3 crypto longs (they all move
                  with Bitcoin), and 3 stock positions with the same direction and style (e.g. "long mean reversion")
  earnings        no new stock trade that would be held through the company's earnings report, unless the trade
                  IS an earnings play; positions about to hit a report get closed the afternoon before
  risk-off rule   when the macro read is risk-off or panic, buying stocks needs conviction 6+
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from desk.trading import ledger

MAX_SAME_CURRENCY = 2
MAX_CRYPTO_LONGS = 3
MAX_SAME_STYLE = 3
EARNINGS_STYLE = "earnings play"
HOLD_DAYS = {"intraday": 0, "1-5 days": 7, "1-4 weeks": 30}
RISK_OFF = ("risk-off/defensive", "panic/high volatility")
RISK_OFF_MIN_CONVICTION = 6


def _legs(symbol: str, direction: str) -> list[tuple[str, int]]:
    base, quote = symbol.split("_")
    s = 1 if direction == "long" else -1
    return [(base, s), (quote, -s)]


def _group(asset: str) -> str:
    return "stocks" if asset in ("stock", "etf", "option") else asset


def same_bet(trades: list[dict], asset: str, symbol: str, direction: str, style: str | None) -> str | None:
    """Why this would be one bet too many, or None if it's fine."""
    active = [t for t in trades if t.get("status") in ledger.ACTIVE]
    if asset == "forex":
        for cur, sign in _legs(symbol, direction):
            same = [t for t in active if t.get("asset") == "forex" and (cur, sign) in _legs(t["symbol"], t["direction"])]
            if len(same) >= MAX_SAME_CURRENCY:
                way = "up" if sign > 0 else "down"
                return (f"already {len(same)} positions betting the {cur} goes {way} "
                        f"({', '.join(t['symbol'] for t in same)}); one more would just be the same bet again")
        return None
    if asset == "crypto":
        longs = [t for t in active if t.get("asset") == "crypto"]
        if direction == "long" and len(longs) >= MAX_CRYPTO_LONGS:
            return f"already {len(longs)} crypto longs ({', '.join(t['symbol'] for t in longs)}); they all move with Bitcoin"
        return None
    style = (style or "").lower()
    same = [t for t in active if _group(t.get("asset")) == "stocks" and t.get("direction") == direction
            and (t.get("style") or "").lower() == style]
    if style and len(same) >= MAX_SAME_STYLE:
        return (f"already {len(same)} {direction} {style} stock positions ({', '.join(t['symbol'] for t in same)}); "
                f"they would all win or lose together")
    return None


def earnings_conflict(earnings_date: str | None, horizon: str | None, style: str | None, today: date) -> str | None:
    if not earnings_date or (style or "").lower() == EARNINGS_STYLE:
        return None
    try:
        d = date.fromisoformat(earnings_date[:10])
    except ValueError:
        return None
    days = HOLD_DAYS.get(horizon or "", 7)
    if today <= d <= today + timedelta(days=days):
        return f"reports earnings on {d.isoformat()}, inside the planned holding time; not an earnings play, so skipping the gap risk"
    return None


def must_exit_before_earnings(t: dict, earnings: dict, now: datetime) -> str | None:
    """Close a non-earnings stock/option position on the afternoon before its report."""
    if t.get("asset") not in ("stock", "etf", "option") or (t.get("style") or "").lower() == EARNINGS_STYLE:
        return None
    e = earnings.get(t["symbol"])
    if not e or not e.get("date"):
        return None
    d = date.fromisoformat(e["date"][:10])
    today = now.date()
    nxt = today + timedelta(days=3 if today.weekday() == 4 else 1)
    hour = (e.get("hour") or "").lower()
    if (d == today and hour in ("amc", "")) or (d == nxt and hour in ("bmo", "")) or (today < d < nxt):
        return f"earnings report {d.isoformat()} {hour or '(time not given)'}; closing before it so a surprise gap can't jump the stop"
    return None


def risk_off_block(regime: str | None, asset: str, direction: str, conviction) -> str | None:
    if regime in RISK_OFF and asset in ("stock", "etf") and direction == "long" and int(conviction or 0) < RISK_OFF_MIN_CONVICTION:
        return f"the market read is {regime}; buying stocks needs conviction {RISK_OFF_MIN_CONVICTION}+"
    return None


_cal: dict | None = None


def earnings_calendar() -> dict:
    """{symbol: {"date", "hour"}} for the next 5 weeks, fetched once per run."""
    global _cal
    if _cal is None:
        try:
            from desk import datasources
            _cal = {}
            for e in datasources.earnings_calendar(days_ahead=35):
                if e.get("symbol") and e.get("date"):
                    _cal.setdefault(e["symbol"], e)
        except Exception as exc:
            print(f"earnings calendar failed: {exc}")
            _cal = {}
    return _cal
