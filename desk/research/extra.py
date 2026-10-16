"""Extra free data for the analysts. Every function returns plain numbers (or None on failure) and caches,
so one source being down never breaks a research session.

  sector()           FinanceDatabase (JerBouma): sector, industry group, industry, size for ~19,000 US stocks
  yahoo_stats()      yfinance: short interest, analyst price targets and ratings, institutional ownership, beta
  short_volume()     FINRA daily short-sale volume: share of each day's trading that was short selling
  sec_snapshot()     edgartools: latest quarter and year from SEC XBRL filings, plus insider Form 4 trades
  cot()              CFTC Commitments of Traders: how speculators are positioned in currency, gold, oil,
                     stock index, Treasury and bitcoin futures
  crypto_sentiment() alternative.me Crypto Fear & Greed index, plus DefiLlama stablecoin supply and DeFi value
"""
from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from datetime import date, datetime, timedelta

import requests

from desk import config

CACHE = config.DATA_DIR / "cache"
UA = {"User-Agent": "TradingDesk paper-trading research erwann.sadki@icloud.com"}
SEC_IDENTITY = "TradingDesk paper-trading research erwann.sadki@icloud.com"
_mem: dict = {}


def _load(name: str):
    p = CACHE / name
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def _save(name: str, data) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / name).write_text(json.dumps(data, separators=(",", ":"), default=str))


def _r(x, n=2):
    try:
        return None if x is None else round(float(x), n)
    except (TypeError, ValueError):
        return None


# ---- Sector and industry (FinanceDatabase) -------------------------------------------
def sector_map(max_age_days: int = 30) -> dict:
    if "sectors" in _mem:
        return _mem["sectors"]
    cached = _load("sectors.json")
    fresh = cached and cached.get("built") and (date.today() - date.fromisoformat(cached["built"])).days < max_age_days
    if not fresh:
        try:
            import financedatabase as fd
            df = fd.Equities().select(country="United States")
            m = {}
            for sym, row in df.iterrows():
                if isinstance(sym, str) and row.get("sector") and str(row.get("sector")) != "nan":
                    m[sym] = [row.get("sector"), row.get("industry_group"), row.get("industry"), row.get("market_cap")]
            cached = {"built": date.today().isoformat(), "map": {k: [str(x) if x is not None and str(x) != "nan" else None for x in v]
                                                                 for k, v in m.items()}}
            _save("sectors.json", cached)
        except Exception as exc:
            print(f"sector database failed: {exc}")
            cached = cached or {"map": {}}
    _mem["sectors"] = cached.get("map", {})
    return _mem["sectors"]


def sector(symbol: str) -> dict | None:
    row = sector_map().get(symbol)
    if not row:
        return None
    return {"sector": row[0], "industry_group": row[1], "industry": row[2], "size": row[3]}


# ---- Short interest, analyst targets, ownership (yfinance) ---------------------------
def yahoo_stats(symbol: str, price: float | None = None) -> dict | None:
    key = f"yf:{symbol}"
    if key in _mem:
        return _mem[key]
    out = None
    try:
        import yfinance as yf
        i = yf.Ticker(symbol).info or {}
        tgt = i.get("targetMeanPrice")
        px = price or i.get("currentPrice") or i.get("regularMarketPrice")
        out = {
            "short_pct_of_float": _r((i.get("shortPercentOfFloat") or 0) * 100, 1) if i.get("shortPercentOfFloat") is not None else None,
            "days_to_cover": _r(i.get("shortRatio"), 1),
            "analyst_target_mean": _r(tgt),
            "analyst_target_upside_pct": _r((tgt / px - 1) * 100, 1) if tgt and px else None,
            "analyst_rating_1_buy_5_sell": _r(i.get("recommendationMean"), 2),
            "analyst_count": i.get("numberOfAnalystOpinions"),
            "institutions_own_pct": _r((i.get("heldPercentInstitutions") or 0) * 100, 1) if i.get("heldPercentInstitutions") is not None else None,
            "insiders_own_pct": _r((i.get("heldPercentInsiders") or 0) * 100, 1) if i.get("heldPercentInsiders") is not None else None,
            "beta": _r(i.get("beta"), 2),
        }
        out = {k: v for k, v in out.items() if v is not None} or None
    except Exception as exc:
        print(f"yahoo stats failed for {symbol}: {str(exc)[:120]}")
    _mem[key] = out
    return out


# ---- FINRA daily short-sale volume ---------------------------------------------------
def _finra_days(n: int = 5) -> list[dict]:
    if "finra" in _mem:
        return _mem["finra"]
    days, d = [], date.today()
    tries = 0
    while len(days) < n and tries < 12:
        tries += 1
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        try:
            r = requests.get(f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt", headers=UA, timeout=30)
            if r.status_code != 200:
                continue
            m = {}
            for line in r.text.splitlines()[1:]:
                p = line.split("|")
                if len(p) >= 5 and p[1]:
                    try:
                        m[p[1]] = (float(p[2]), float(p[4]))
                    except ValueError:
                        pass
            if m:
                days.append({"date": d.isoformat(), "m": m})
        except Exception as exc:
            print(f"FINRA {d} failed: {exc}")
    _mem["finra"] = days
    return days


def short_volume(symbol: str) -> dict | None:
    days = _finra_days()
    rows = [(x["date"], x["m"][symbol]) for x in days if symbol in x["m"]]
    if not rows:
        return None
    ratios = [s / t * 100 for _, (s, t) in rows if t]
    if not ratios:
        return None
    return {"short_volume_pct_latest": _r(ratios[0], 1), "short_volume_pct_5d_avg": _r(sum(ratios) / len(ratios), 1),
            "as_of": rows[0][0]}


# ---- SEC filings: financial statements and insider Form 4 trades (edgartools) ---------
def sec_snapshot(symbol: str, days: int = 45) -> dict | None:
    key = f"sec:{symbol}"
    if key in _mem:
        return _mem[key]
    out: dict = {}
    try:
        from edgar import Company, set_identity
        set_identity(SEC_IDENTITY)
        c = Company(symbol)
        try:
            q = c.get_quarterly_financials()
            out.update({"q_revenue_musd": _r((q.get_revenue() or 0) / 1e6, 1), "q_net_income_musd": _r((q.get_net_income() or 0) / 1e6, 1),
                        "q_operating_income_musd": _r((q.get_operating_income() or 0) / 1e6, 1),
                        "q_free_cash_flow_musd": _r((q.get_free_cash_flow() or 0) / 1e6, 1)})
            if out.get("q_revenue_musd"):
                out["q_operating_margin_pct"] = _r(out["q_operating_income_musd"] / out["q_revenue_musd"] * 100, 1)
        except Exception as exc:
            print(f"SEC quarterly for {symbol}: {str(exc)[:100]}")
        try:
            a = c.get_financials()
            out.update({"fy_revenue_musd": _r((a.get_revenue() or 0) / 1e6, 1), "fy_net_income_musd": _r((a.get_net_income() or 0) / 1e6, 1),
                        "total_assets_musd": _r((a.get_total_assets() or 0) / 1e6, 1),
                        "total_liabilities_musd": _r((a.get_total_liabilities() or 0) / 1e6, 1)})
        except Exception as exc:
            print(f"SEC annual for {symbol}: {str(exc)[:100]}")
        try:
            since = (date.today() - timedelta(days=days)).isoformat()
            buys = sells = planned = 0
            buy_usd = sell_usd = 0.0
            for f in c.get_filings(form="4").head(12):
                if str(f.filing_date) < since:
                    break
                o = f.obj()
                for df, sign in ((o.common_stock_purchases, 1), (o.common_stock_sales, -1)):
                    if df is None or len(df) == 0:
                        continue
                    usd = float((df["Shares"].astype(float) * df["Price"].astype(float)).sum())
                    if sign > 0:
                        buys += len(df); buy_usd += usd
                    else:
                        sells += len(df); sell_usd += usd
                try:
                    if o.get_ownership_summary().has_10b5_1_plan:
                        planned += 1
                except Exception:
                    pass
            out.update({f"insider_buys_{days}d": buys, f"insider_buy_usd_{days}d": _r(buy_usd, 0),
                        f"insider_sells_{days}d": sells, f"insider_sell_usd_{days}d": _r(sell_usd, 0),
                        f"insider_filings_on_preset_plans_{days}d": planned})
        except Exception as exc:
            print(f"SEC Form 4 for {symbol}: {str(exc)[:100]}")
    except Exception as exc:
        print(f"SEC snapshot failed for {symbol}: {str(exc)[:120]}")
    out = {k: v for k, v in out.items() if v is not None} or None
    _mem[key] = out
    return out


# ---- CFTC Commitments of Traders ----------------------------------------------------
COT_MARKETS = {
    "EUR": ["EURO FX - CHICAGO MERCANTILE EXCHANGE"], "JPY": ["JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE"],
    "GBP": ["BRITISH POUND - CHICAGO MERCANTILE EXCHANGE"], "CAD": ["CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
    "CHF": ["SWISS FRANC - CHICAGO MERCANTILE EXCHANGE"], "AUD": ["AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
    "NZD": ["NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE"], "MXN": ["MEXICAN PESO - CHICAGO MERCANTILE EXCHANGE"],
    "USD": ["USD INDEX - ICE FUTURES U.S."], "GOLD": ["GOLD - COMMODITY EXCHANGE INC."],
    "OIL": ["WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE", "CRUDE OIL, LIGHT SWEET-WTI - ICE FUTURES EUROPE"],
    "SP500": ["E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE"], "NASDAQ100": ["NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE"],
    "TREASURY_10Y": ["UST 10Y NOTE - CHICAGO BOARD OF TRADE"], "BITCOIN": ["BITCOIN - CHICAGO MERCANTILE EXCHANGE"],
    "ETHER": ["ETHER CASH SETTLED - CHICAGO MERCANTILE EXCHANGE"],
}


def _cot_rows(year: int) -> list[dict]:
    r = requests.get(f"https://www.cftc.gov/files/dea/history/deacot{year}.zip", headers=UA, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    wanted = {n for names in COT_MARKETS.values() for n in names}
    out = []
    for row in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="latin-1")):
        if row["Market and Exchange Names"].strip() in wanted:
            out.append({"m": row["Market and Exchange Names"].strip(), "d": row["As of Date in Form YYYY-MM-DD"],
                        "oi": float(row["Open Interest (All)"] or 0), "l": float(row["Noncommercial Positions-Long (All)"] or 0),
                        "s": float(row["Noncommercial Positions-Short (All)"] or 0)})
    return out


def cot(max_age_days: int = 3) -> dict:
    """Per market: speculators' net position (as % of open interest), its weekly change, and where it sits in its
    3-year range (0 = most bearish in 3 years, 100 = most bullish). Extremes often mean a crowded trade."""
    if "cot" in _mem:
        return _mem["cot"]
    cached = _load("cot.json")
    if cached and cached.get("fetched") and (datetime.now() - datetime.fromisoformat(cached["fetched"])).days < max_age_days:
        _mem["cot"] = cached["markets"]
        return _mem["cot"]
    try:
        y = date.today().year
        rows = []
        for year in (y - 2, y - 1, y):
            try:
                rows += _cot_rows(year)
            except Exception as exc:
                print(f"COT {year} failed: {exc}")
        markets = {}
        for key, names in COT_MARKETS.items():
            hist = sorted([r for r in rows if r["m"] in names], key=lambda r: r["d"])
            if len(hist) < 10:
                continue
            nets = [(r["l"] - r["s"]) / r["oi"] * 100 if r["oi"] else 0 for r in hist]
            cur, prev = nets[-1], nets[-2]
            pct_rank = sum(n <= cur for n in nets) / len(nets) * 100
            markets[key] = {"as_of": hist[-1]["d"], "spec_net_pct_of_open_interest": _r(cur, 1),
                            "weekly_change_pts": _r(cur - prev, 1), "three_year_percentile": _r(pct_rank, 0)}
        if markets:
            _save("cot.json", {"fetched": datetime.now().isoformat(timespec="minutes"), "markets": markets})
        _mem["cot"] = markets or (cached or {}).get("markets", {})
    except Exception as exc:
        print(f"COT failed: {exc}")
        _mem["cot"] = (cached or {}).get("markets", {})
    return _mem["cot"]


def cot_for_pair(instrument: str) -> dict | None:
    base, quote = instrument.split("_")
    c = cot()
    out = {cur: c[cur] for cur in (base, quote) if cur in c}
    if "USD" in (base, quote) and "USD" not in out and "USD" in c:
        out["USD"] = c["USD"]
    return out or None


# ---- Crypto sentiment and flows -------------------------------------------------------
def crypto_sentiment() -> dict | None:
    if "crypto" in _mem:
        return _mem["crypto"]
    out: dict = {}
    try:
        f = requests.get("https://api.alternative.me/fng/?limit=30", timeout=20).json()["data"]
        vals = [int(x["value"]) for x in f]
        out.update({"fear_greed_now_0_100": vals[0], "fear_greed_label": f[0]["value_classification"],
                    "fear_greed_7d_avg": _r(sum(vals[:7]) / 7, 0), "fear_greed_30d_avg": _r(sum(vals) / len(vals), 0)})
    except Exception as exc:
        print(f"fear and greed failed: {exc}")
    try:
        s = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=30).json()
        now, ago = s[-1]["totalCirculatingUSD"]["peggedUSD"], s[-31]["totalCirculatingUSD"]["peggedUSD"]
        out.update({"stablecoin_supply_busd": _r(now / 1e9, 1), "stablecoin_supply_change_30d_pct": _r((now / ago - 1) * 100, 2)})
    except Exception as exc:
        print(f"stablecoin supply failed: {exc}")
    try:
        t = requests.get("https://api.llama.fi/v2/historicalChainTvl", timeout=30).json()
        out.update({"defi_value_locked_busd": _r(t[-1]["tvl"] / 1e9, 1), "defi_value_locked_change_30d_pct": _r((t[-1]["tvl"] / t[-31]["tvl"] - 1) * 100, 2)})
    except Exception as exc:
        print(f"DeFi value locked failed: {exc}")
    _mem["crypto"] = out or None
    return _mem["crypto"]


# ---- Volatility indexes and the options market ---------------------------------------
def vol_indexes() -> dict | None:
    """VIX (30-day fear gauge), VIX3M (3-month), VVIX (volatility of the VIX) and SKEW (crash-protection demand)."""
    if "vol" in _mem:
        return _mem["vol"]
    out = None
    try:
        import yfinance as yf
        d = yf.download(["^VIX", "^VIX3M", "^VVIX", "^SKEW"], period="1mo", progress=False, auto_adjust=False)["Close"]
        last = {c: float(d[c].dropna().iloc[-1]) for c in d.columns if len(d[c].dropna())}
        vix, v3 = last.get("^VIX"), last.get("^VIX3M")
        out = {"vix": _r(vix), "vix_3m": _r(v3), "vvix": _r(last.get("^VVIX")), "skew": _r(last.get("^SKEW"), 1),
               "vix_term_ratio": _r(vix / v3, 3) if vix and v3 else None,
               "vix_20d_avg": _r(float(d["^VIX"].dropna().tail(20).mean())) if "^VIX" in d else None}
        out["term_structure"] = ("inverted (near-term fear above long-term: stress)" if (out["vix_term_ratio"] or 0) > 1
                                 else "normal (calm)")
    except Exception as exc:
        print(f"vol indexes failed: {exc}")
    _mem["vol"] = out
    return out


def implied_move(symbol: str, price: float | None = None) -> dict | None:
    """How big a move the options market is pricing by the nearest weekly expiry (at-the-money straddle / price)."""
    try:
        import yfinance as yf
        from datetime import date as _d
        t = yf.Ticker(symbol)
        exps = [e for e in t.options if (_d.fromisoformat(e) - _d.today()).days >= 2][:1]
        if not exps:
            return None
        ch = t.option_chain(exps[0])
        px = price or float(t.fast_info["last_price"])

        def val(df):
            row = df.iloc[(df["strike"] - px).abs().argsort()[:1]]
            bid, ask, last = (float(row[k].iloc[0]) for k in ("bid", "ask", "lastPrice"))
            return ((bid + ask) / 2 if bid > 0 and ask > 0 else last), float(row["strike"].iloc[0]), float(row["impliedVolatility"].iloc[0])
        c, k, iv_c = val(ch.calls)
        p, _, iv_p = val(ch.puts)
        if not c or not p:
            return None
        return {"expiry": exps[0], "days": (_d.fromisoformat(exps[0]) - _d.today()).days, "atm_strike": k,
                "implied_move_pct": _r((c + p) / px * 100), "implied_vol_pct": _r((iv_c + iv_p) / 2 * 100, 1),
                "put_call_volume_ratio": _r(float(ch.puts["volume"].fillna(0).sum()) / max(1.0, float(ch.calls["volume"].fillna(0).sum())))}
    except Exception as exc:
        print(f"implied move {symbol} failed: {str(exc)[:80]}")
        return None


def market_internals(stocks: list[dict], etfs: dict) -> dict | None:
    """Breadth and leadership from the scanner's own numbers (no extra downloads)."""
    ts = [s["technicals"] for s in stocks if s.get("technicals")]
    if len(ts) < 20:
        return None
    pct = lambda cond: _r(sum(1 for t in ts if cond(t)) / len(ts) * 100, 0)
    spy = (etfs.get("SPY") or {}).get("ret_20d")
    sectors = {k: v.get("ret_20d") for k, v in etfs.items() if k.startswith("XL") or k in ("SMH", "XBI", "KRE", "ITB")}
    rank = sorted((k for k in sectors if isinstance(sectors[k], (int, float))), key=lambda k: -sectors[k])
    return {
        "stocks_measured": len(ts),
        "pct_above_50d_avg": pct(lambda t: t.get("sma50") and t["price"] > t["sma50"]),
        "pct_above_200d_avg": pct(lambda t: t.get("sma200") and t["price"] > t["sma200"]),
        "pct_up_today": pct(lambda t: (t.get("ret_1d") or 0) > 0),
        "pct_rsi_over_70": pct(lambda t: (t.get("rsi14") or 50) > 70),
        "pct_rsi_under_30": pct(lambda t: (t.get("rsi14") or 50) < 30),
        "near_52w_high": sum(1 for t in ts if t.get("high_52w") and t["price"] >= 0.97 * t["high_52w"]),
        "near_52w_low": sum(1 for t in ts if t.get("low_52w") and t["price"] <= 1.03 * t["low_52w"]),
        "spy_ret_20d": spy,
        "leading_sectors_20d": [f"{k} {sectors[k]:+.1f}%" for k in rank[:3]],
        "lagging_sectors_20d": [f"{k} {sectors[k]:+.1f}%" for k in rank[-3:][::-1]],
    }


def health() -> dict[str, str]:
    """One line per source, for the health check."""
    res = {}
    t = time.monotonic()
    s = sector("NVDA")
    res["FinanceDatabase"] = f"NVDA: {s['industry']}" if s else "no data"
    y = yahoo_stats("AAPL")
    res["Yahoo (yfinance)"] = f"AAPL short {y.get('short_pct_of_float')}% of float, target ${y.get('analyst_target_mean')}" if y else "no data"
    v = short_volume("AAPL")
    res["FINRA short volume"] = f"AAPL {v['short_volume_pct_latest']}% short ({v['as_of']})" if v else "no data"
    e = sec_snapshot("AAPL")
    res["SEC filings (edgartools)"] = f"AAPL quarter revenue ${e.get('q_revenue_musd')}M" if e else "no data"
    c = cot()
    res["CFTC positioning"] = f"{len(c)} markets, EUR speculators at {c['EUR']['three_year_percentile']:.0f} on a 0-100 three-year scale" if c.get("EUR") else "no data"
    k = crypto_sentiment()
    res["Crypto sentiment"] = f"fear & greed {k.get('fear_greed_now_0_100')} ({k.get('fear_greed_label')})" if k else "no data"
    vi = vol_indexes()
    res["Volatility indexes"] = f"VIX {vi.get('vix')}, VIX3M {vi.get('vix_3m')}, {vi.get('term_structure')}" if vi else "no data"
    im = implied_move("SPY")
    res["Options implied move"] = f"SPY {im['implied_move_pct']}% by {im['expiry']}" if im else "no data"
    res["_seconds"] = f"{time.monotonic() - t:.0f}"
    return res
