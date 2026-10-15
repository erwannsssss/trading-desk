"""The screener: scans the whole market and picks what deserves deep research today.

Covers big-cap stocks, ETFs (sectors, international, bonds, commodities, inverse),
whatever is most active or moving hardest today, Reddit's hottest tickers, upcoming
earnings, 30+ cryptos and 16 currency pairs. Plain code; the AI only sees the finalists.
"""
from __future__ import annotations

from datetime import datetime, timezone

from desk import config, datasources, marketdata
from desk.research import technicals
from desk.state import now

ETFS = [
    # broad market
    "SPY", "QQQ", "IWM", "DIA", "RSP",
    # sectors and themes
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "XBI", "KRE", "ITB", "ARKK", "TAN", "URA",
    # international
    "EFA", "EEM", "FXI", "KWEB", "EWJ", "EWZ", "INDA", "EWG", "EWW",
    # rates, credit, commodities, dollar
    "TLT", "IEF", "HYG", "GLD", "SLV", "USO", "UNG", "CPER", "UUP",
    # inverse (profit when markets fall)
    "SH", "PSQ",
]

STOCKS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "ORCL", "AMD", "NFLX", "CRM", "ADBE", "INTC", "MU",
    "QCOM", "TSM", "ASML", "ARM", "PLTR", "SNOW", "CRWD", "PANW", "NET", "SHOP", "UBER", "ABNB", "COIN", "HOOD", "XYZ",
    "PYPL", "JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA", "AXP", "BRK.B", "UNH", "LLY", "NVO", "JNJ", "PFE", "MRK",
    "ABBV", "XOM", "CVX", "OXY", "COP", "SLB", "CAT", "DE", "BA", "GE", "LMT", "RTX", "HD", "LOW", "WMT", "COST", "TGT",
    "NKE", "SBUX", "MCD", "DIS", "KO", "PEP", "F", "GM", "RIVN", "SMCI", "DELL", "MSTR", "SOFI", "RKLB", "IONQ", "CVNA",
]

CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "AVAX/USD", "LINK/USD", "LTC/USD", "ADA/USD",
          "DOT/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "SHIB/USD", "PEPE/USD", "HYPE/USD", "RENDER/USD", "ONDO/USD",
          "PAXG/USD"]

FOREX = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD", "EUR_JPY", "GBP_JPY",
         "EUR_GBP", "AUD_JPY", "EUR_CHF", "CAD_JPY", "USD_MXN", "EUR_AUD", "GBP_AUD"]

MIN_PRICE = 10.0           # cheap stocks have wide spreads and jumpy prices
MIN_DOLLAR_VOLUME = 50_000_000  # avg $ traded per day, so we can get in and out cheaply


def _clean(sym: str) -> bool:
    return sym.replace(".", "").isalpha() and len(sym) <= 6


def scan_stocks(n: int, max_etfs: int = 5, core_slots: int = 5) -> tuple[list[dict], dict]:
    """Returns (ranked finalists, context) where context has ETF readings for the macro analyst."""
    dynamic: dict[str, list[str]] = {}

    def tag(sym: str, why: str):
        if _clean(sym):
            dynamic.setdefault(sym, []).append(why)

    try:
        for a in marketdata.most_actives(40):
            tag(a["symbol"], "one of today's most-traded stocks")
        mv = marketdata.movers(30)
        for g in mv["gainers"]:
            if (g.get("price") or 0) >= MIN_PRICE:
                tag(g["symbol"], f"top gainer ({g['percent_change']:+.1f}%)")
        for g in mv["losers"]:
            if (g.get("price") or 0) >= MIN_PRICE:
                tag(g["symbol"], f"top loser ({g['percent_change']:+.1f}%)")
    except Exception as exc:
        print(f"screener endpoints failed: {exc}")

    reddit = {}
    try:
        reddit = datasources.reddit_trending(30)
        for sym in reddit:
            tag(sym, f"trending on Reddit (#{reddit[sym]['rank']})")
    except Exception as exc:
        print(f"reddit failed: {exc}")

    earnings = {}
    try:
        for e in datasources.earnings_calendar(days_ahead=3):
            earnings[e["symbol"]] = e
        for sym in earnings:
            if sym in STOCKS:
                tag(sym, f"earnings on {earnings[sym]['date']} ({earnings[sym].get('hour') or 'time n/a'})")
    except Exception as exc:
        print(f"earnings calendar failed: {exc}")

    # Earnings season: when lots of big names report in the next few days, lean into earnings plays.
    big_reporting = [s for s in earnings if s in STOCKS]
    season = len(big_reporting) >= 6
    universe = sorted(set(ETFS) | set(STOCKS) | set(dynamic))
    bars = marketdata.stock_daily_bars(universe)
    snaps = marketdata.stock_snapshots(universe)
    today_ny = now().date().isoformat()

    analyzed, etf_readings = [], {}
    for sym in universe:
        b = bars.get(sym) or []
        if not b:
            continue
        snap = snaps.get(sym, {})
        live, gap = snap.get("price"), None
        if not live or not snap.get("trade_time") or _ny_date(snap["trade_time"]) != today_ny:
            live = None  # no trades yet today; use the last close
        last_bar_today = _ny_date(b[-1]["t"]) == today_ny
        if live and not last_bar_today:
            gap = round((live / b[-1]["c"] - 1) * 100, 2)  # pre-market move vs yesterday's close
        t = technicals.analyze(b, live, live_replaces_last=last_bar_today)
        if not t:
            continue
        if gap is not None and abs(gap) >= 3:
            t["setups"].append(f"pre-market gap {gap:+.1f}%")
            t["interest"] += min(abs(gap) / 2, 4)
        t["gap_pct"] = gap
        if sym in ETFS:
            etf_readings[sym] = {k: t[k] for k in ("price", "ret_1d", "ret_5d", "ret_20d", "rsi14", "trend", "pct_from_52w_high")}
            etf_readings[sym]["gap_pct"] = gap
        if t["price"] < MIN_PRICE or (t["avg_dollar_vol_20d"] or 0) < MIN_DOLLAR_VOLUME:
            continue
        reasons = list(dynamic.get(sym, []))
        if sym in earnings:
            t["interest"] += 6 if season else 3
            reasons.append(f"earnings {earnings[sym]['date']}")
        if sym in reddit:
            t["interest"] += 1
        analyzed.append({"symbol": sym, "venue": "alpaca", "asset": "etf" if sym in ETFS else "stock",
                         "why_scanned": reasons, "technicals": t,
                         "earnings": earnings.get(sym), "reddit": reddit.get(sym)})

    analyzed.sort(key=lambda x: x["technicals"]["interest"], reverse=True)
    # Reserve a few slots for big, high-quality names so it's not all small-cap action.
    core = [a for a in analyzed if a["symbol"] in STOCKS][:max(0, min(core_slots, n))]
    picks, etfs = list(core), 0
    for a in analyzed:
        if a in picks:
            continue
        if a["asset"] == "etf":
            if etfs >= max_etfs:
                continue
            etfs += 1
        picks.append(a)
        if len(picks) >= n:
            break
    if season:  # make sure at least 3 earnings names get researched
        have = sum(1 for p in picks if p.get("earnings"))
        extra = [a for a in analyzed if a.get("earnings") and a not in picks][:max(0, 3 - have)]
        picks = picks[:max(0, n - len(extra))] + extra
    return picks, {"etf_readings": etf_readings, "scanned": len(analyzed), "earnings_soon": list(earnings)[:40],
                   "earnings_season": season, "big_reporting": big_reporting,
                   "analyzed": [a for a in analyzed if a.get("asset") == "stock"]}


def scan_crypto(n: int) -> tuple[list[dict], dict]:
    bars = marketdata.crypto_daily_bars(CRYPTO)
    prices = marketdata.crypto_prices(CRYPTO)
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out, readings = [], {}
    for sym in CRYPTO:
        b = bars.get(sym) or []
        t = technicals.analyze(b, prices.get(sym), live_replaces_last=bool(b) and b[-1]["t"][:10] == today_utc)
        if not t:
            continue
        readings[sym] = {k: t[k] for k in ("price", "ret_1d", "ret_5d", "ret_20d", "rsi14", "trend")}
        out.append({"symbol": sym, "venue": "alpaca", "asset": "crypto", "why_scanned": [], "technicals": t})
    out.sort(key=lambda x: x["technicals"]["interest"], reverse=True)
    return out[:n], {"crypto_readings": readings}


def scan_forex(n: int) -> tuple[list[dict], dict]:
    out, readings = [], {}
    for inst in FOREX:
        try:
            t = technicals.analyze(marketdata.forex_daily_bars(inst))
        except Exception as exc:
            print(f"{inst} failed: {exc}")
            continue
        if not t:
            continue
        readings[inst] = {k: t[k] for k in ("price", "ret_1d", "ret_5d", "ret_20d", "rsi14", "trend")}
        out.append({"symbol": inst, "venue": "oanda", "asset": "forex", "why_scanned": [], "technicals": t})
    out.sort(key=lambda x: x["technicals"]["interest"], reverse=True)
    return out[:n], {"forex_readings": readings}


def _ny_date(ts: str) -> str:
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo(config.TIMEZONE)).date().isoformat()
