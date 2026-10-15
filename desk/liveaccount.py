"""The account right now, from the brokers' own numbers, plus a breakdown that adds up exactly to the balance.

Position values come from Alpaca and OANDA themselves (the same numbers their account balances use), so the
positions always add up to the account. Published to the live branch about once a minute while the Telegram
listener is online (roughly 12 of every 15 minutes), and used by the dashboard build.

The breakdown:  $10,000 start + closed trades + open positions - bank interest - payroll and bonuses
                + other account activity (anything at the brokers that isn't a desk trade, e.g. fees or a manual order)
                = firm equity
"""
from __future__ import annotations

import json
import time

from desk import config
from desk.brokers import alpaca, oanda
from desk.state import now
from desk.trading import ledger

_last = 0.0
EVERY = 30


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def positions_from_brokers(trades: list[dict], alp_pos: list[dict], fx_trades: list[dict]) -> dict[str, dict]:
    """{trade id: {"price", "unrealized_usd", "market_value"}} using each broker's own valuation."""
    by_sym = {p["symbol"]: p for p in alp_pos}
    by_fx = {t["id"]: t for t in fx_trades}
    out = {}
    fx_px: dict[str, float] = {}
    for t in trades:
        if t.get("status") not in ("open", "closing"):
            continue
        if t.get("venue") == "oanda":
            tr = by_fx.get(str(t.get("oanda_trade_id")))
            if not tr:
                continue
            if t["symbol"] not in fx_px:
                try:
                    fx_px[t["symbol"]] = oanda.price(t["symbol"])["mid"]
                except Exception:
                    fx_px[t["symbol"]] = None
            out[t["id"]] = {"price": fx_px[t["symbol"]], "unrealized_usd": round(_f(tr.get("unrealizedPL")), 2),
                            "market_value": None}
        elif t.get("asset") == "option":
            legs = [t.get("option_symbol"), t.get("short_option_symbol") or t.get("hedge_option_symbol")]
            ps = [by_sym[s] for s in legs if s and s in by_sym]
            if not ps:
                continue
            out[t["id"]] = {"price": None, "unrealized_usd": round(sum(_f(p.get("unrealized_pl")) for p in ps), 2),
                            "market_value": round(sum(_f(p.get("market_value")) for p in ps), 2)}
        else:
            p = by_sym.get(t["symbol"].replace("/", "")) or by_sym.get(t["symbol"])
            if not p:
                continue
            out[t["id"]] = {"price": _f(p.get("current_price")), "unrealized_usd": round(_f(p.get("unrealized_pl")), 2),
                            "market_value": round(_f(p.get("market_value")), 2)}
    return out


def breakdown(equity: float, alpaca_pnl: float, oanda_pnl: float, trades: list[dict], live: dict[str, dict],
              financing: float, opex: float, corrections: float = 0.0) -> dict:
    realized = round(sum(t.get("pnl_usd") or 0 for t in trades if t.get("status") == "closed"), 2)
    partials = round(sum(t.get("partial_pnl") or 0 for t in trades if t.get("status") in ("open", "closing")), 2)
    unrealized = round(sum(v["unrealized_usd"] for v in live.values()), 2)
    other = round(alpaca_pnl + oanda_pnl - realized - partials - unrealized, 2)
    return {"start": config.STARTING_CAPITAL, "closed_trades": realized, "partial_profits_taken": partials,
            "open_positions": unrealized, "bank_interest": round(-financing, 2), "payroll_and_bonuses": round(-opex, 2),
            "other_account_activity": other, "corrections": round(corrections, 2),
            "correction_notes": _notes(), "equity": round(equity, 2)}


def _notes() -> list[str]:
    try:
        from desk import corrections
        return [f"{c['date']}: {c['reason']} ({c['amount']:+,.2f})" for c in corrections.load()]
    except Exception:
        return []


def snapshot(state: dict) -> dict:
    from desk.portfolio import snapshot as book_snapshot
    book, d = book_snapshot(state)
    trades = ledger.load()
    live = positions_from_brokers(trades, d["alpaca_positions"], d["oanda_trades"])
    day_start = state.get("day_start_equity") or book.equity
    return {
        "t": now().isoformat(timespec="seconds"),
        "equity": round(book.equity, 2),
        "today_usd": round(book.equity - day_start, 2),
        "today_pct": round((book.equity / day_start - 1) * 100, 2) if day_start else 0.0,
        "total_pct": round((book.equity / config.STARTING_CAPITAL - 1) * 100, 2),
        "alpaca_equity": round(book.alpaca_equity, 2), "oanda_nav": round(book.oanda_nav, 2),
        "positions": live,
        "breakdown": breakdown(book.equity, book.alpaca_equity - book.baseline_alpaca, book.oanda_nav - book.baseline_oanda,
                               trades, live, book.financing_cost, book.opex, book.corrections),
    }


def publish(state: dict, force: bool = False) -> None:
    """Push the snapshot to the live branch (at most about once a minute)."""
    global _last
    if not force and time.monotonic() - _last < EVERY:
        return
    _last = time.monotonic()
    try:
        from desk import activity
        body = json.dumps(snapshot(state), separators=(",", ":"), default=str)
        try:
            activity.LIVE_DIR.mkdir(parents=True, exist_ok=True)
            (activity.LIVE_DIR / "account.json").write_text(body)
        except Exception:
            pass
        activity._push_remote("live/account.json", body)
    except Exception as exc:
        print(f"live account failed: {exc}")
