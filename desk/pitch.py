"""/pitch TICKER: Erwann pitches an idea and the full research team debates it.

Works for stocks, ETFs, crypto (BTC, ETH/USD...) and forex (EURUSD, EUR_USD...). If the portfolio manager
approves it and it passes the fact-checker, it's saved as a plan and the trading engine treats it like any
other idea (all the same risk limits apply).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from desk import activity as floor
from desk import config, marketdata
from desk.brokers import oanda
from desk.portfolio import snapshot
from desk.research import dossier as dossiers
from desk.research import planner, scanner, smartmoney, team, technicals
from desk.state import load, now
from desk.telegram import esc

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "MXN", "ZAR", "SEK", "NOK", "SGD", "HKD", "TRY", "PLN", "CNH"}


def classify(raw: str) -> tuple[str, str]:
    s = raw.strip().upper().replace("-", "/")
    crypto_bases = {c.split("/")[0] for c in scanner.CRYPTO} | {"SUI", "ARB", "WIF", "BONK", "TRUMP", "POL"}
    if "_" in s:
        return s, "forex"
    if len(s) == 6 and s[:3] in CURRENCIES and s[3:] in CURRENCIES:
        return f"{s[:3]}_{s[3:]}", "forex"
    if "/" in s:
        return s, "crypto"
    if s.endswith("USD") and s[:-3] in crypto_bases:
        return f"{s[:-3]}/USD", "crypto"
    if s in crypto_bases and s not in scanner.STOCKS:
        return f"{s}/USD", "crypto"
    return s, "etf" if s in scanner.ETFS else "stock"


def candidate(symbol: str, asset: str) -> dict:
    if asset == "forex":
        t = technicals.analyze(marketdata.forex_daily_bars(symbol))
        venue = "oanda"
    elif asset == "crypto":
        bars = marketdata.crypto_daily_bars([symbol]).get(symbol, [])
        live = marketdata.crypto_prices([symbol]).get(symbol)
        t = technicals.analyze(bars, live, live_replaces_last=bool(bars) and bars[-1]["t"][:10] == datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        venue = "alpaca"
    else:
        if not marketdata.tradable_stock(symbol):
            raise ValueError(f"{symbol} isn't a stock or ETF Alpaca can trade")
        bars = marketdata.stock_daily_bars([symbol]).get(symbol, [])
        snap = marketdata.stock_snapshots([symbol]).get(symbol, {})
        live = snap.get("price") if snap.get("trade_time") and scanner._ny_date(snap["trade_time"]) == now().date().isoformat() else None
        t = technicals.analyze(bars, live, live_replaces_last=bool(bars) and scanner._ny_date(bars[-1]["t"]) == now().date().isoformat())
        venue = "alpaca"
    if not t:
        raise ValueError(f"not enough price history for {symbol}")
    return {"symbol": symbol, "venue": venue, "asset": asset, "why_scanned": ["pitched by Erwann (managing partner)"], "technicals": t}


def run(raw_symbol: str) -> str:
    symbol, asset = classify(raw_symbol)
    floor.event("partner", f"Pitched {symbol} to the research desk", symbol, kind="plan")
    floor.working("associate", f"Pulling the dossier on {symbol} for Erwann's pitch", symbol)
    cand = candidate(symbol, asset)
    funds = {}
    try:
        funds = smartmoney.fund_holdings()
    except Exception:
        pass
    d = dossiers.build(cand, funds)
    floor.done("associate", f"Dossier ready on {symbol}", symbol)
    macro = planner.latest_macro(72) or {"regime": "unknown", "summary": "No fresh market read today."}
    state = load()
    book, details = snapshot(state)
    holdings = [p["symbol"] for p in details["alpaca_positions"]] + [t["instrument"] for t in details["oanda_trades"]]
    extra = planner.extra_context("morning")
    extra["session_note"] = "The managing partner pitched this idea personally. Judge it on the merits; passing is fine."

    for a in floor.SPECIALISTS:
        floor.working(a, f"Analyzing Erwann's pitch: {symbol}", symbol)
    notes = team.specialists(d, macro)
    planner._report_specialists(symbol, notes)
    floor.working("bull", f"Building the case for {symbol}", symbol)
    bull = team.bull(d, notes)
    floor.done("bull", f"{symbol}: argues {bull.get('direction', 'long')}. {bull.get('case', '')}", symbol)
    floor.working("bear", f"Attacking the case on {symbol}", symbol)
    bear = team.bear(d, notes, bull)
    floor.done("bear", f"{symbol}: {bear.get('case', '')}", symbol)
    floor.working("pm", f"Final call on Erwann's pitch: {symbol}", symbol)
    pm = team.portfolio_manager(d, notes, bull, bear, macro, [], holdings, planner._rulebook(), extra)
    check = team.fact_check(pm, dossiers.facts(d), d)
    idea = {"symbol": symbol, "venue": cand["venue"], "asset": asset, "price_at_research": d["technicals"]["price"],
            "atr_pct": d["technicals"].get("atr_pct"), "why_scanned": cand["why_scanned"], "setups": d["technicals"].get("setups"),
            "specialists": notes, "bull": bull, "bear": bear, "decision": pm, "fact_check": check}
    approved = pm.get("decision") in ("long", "short") and check["passed"]
    if approved:
        kw = {}
        if asset == "forex":
            kw = {"quote_to_usd": planner.quote_to_usd(symbol), "unit_usd": oanda.usd_value_per_unit(symbol)}
        idea["sizing"] = team.size(pm, book.equity, asset, **kw)
        idea["status"] = "approved"
    else:
        idea["status"] = "pass" if pm.get("decision") == "pass" else "blocked_by_fact_check"
    floor.done("pm", f"{symbol} (Erwann's pitch): {pm.get('decision', '?').upper()}. {pm.get('reason') or pm.get('thesis') or ''}", symbol,
               kind="decision" if approved else "pass")

    stamp = now()
    plan = {"date": stamp.date().isoformat(), "session": "pitch", "created_at": stamp.isoformat(timespec="seconds"),
            "dry_run": state.get("dry_run", True), "equity": round(book.equity, 2), "macro": None, "ideas": [idea],
            "pitched_by": "Erwann"}
    planner.PLANS_DIR.mkdir(parents=True, exist_ok=True)
    (planner.PLANS_DIR / f"{plan['date']}-pitch-{stamp.strftime('%H%M%S')}.json").write_text(json.dumps(plan, indent=1, default=str))

    lines = [f"<b>Your pitch: {esc(symbol)}</b>", "",
             f"<b>Jake (bull):</b> argues {esc(bull.get('direction', 'long'))}. {esc((bull.get('case') or '')[:400])}", "",
             f"<b>Nadia (bear):</b> {esc((bear.get('case') or '')[:400])}", ""]
    if approved:
        s = idea["sizing"]
        expr = {"call_options": " with calls", "put_options": " with puts"}.get(pm.get("expression"), "")
        lines += [f"<b>Elena's call: {pm['decision'].upper()}{expr}</b>, conviction {pm.get('conviction')}/10",
                  f"Entry {pm['entry']}, stop {pm['stop']}, target {pm['target']} (reward {pm.get('reward_to_risk')}x the risk)",
                  f"Size ${s['notional_usd']:,.0f}, risking ${s['risk_usd']:,.0f}", "",
                  esc(pm.get("plain_english") or pm.get("thesis") or ""), "",
                  "It's in the plan now. Kenji places it on the next check once the market's open." if not state.get("dry_run")
                  else "It's in the plan. The desk is in dry run, so it'll be logged, not placed."]
    elif idea["status"] == "pass":
        lines += [f"<b>Elena's call: PASS</b>", esc(pm.get("reason") or ""), "", esc(pm.get("plain_english") or "")]
    else:
        lines += [f"<b>Elena wanted to {esc(pm.get('decision'))}, but Sam's fact-check blocked it:</b>",
                  esc(check["problems"][0] if check["problems"] else "")]
    return "\n".join(lines)
