"""The trading engine. Runs every tick (about every 15 minutes):

1. MANAGE what's already working: record fills, catch stop/target exits, move stops to
   breakeven and trail them, take partial profits, close trades that outstay their horizon.
2. ENTER new trades from today's game plan: re-check the price, re-size with current equity,
   run the risk manager, then place orders. Stocks/ETFs go in as bracket orders so the broker
   enforces the stop and target instantly; forex gets stop/target attached on fill; crypto gets
   a protective stop order and the engine watches the target.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from desk import config, telegram
from desk.brokers import alpaca, oanda
from desk.research import team
from desk.research.planner import PLANS_DIR, quote_to_usd
from desk.risk import Book, OrderIdea, check_order, in_blackout
from desk.state import log_event, now
from desk.telegram import esc
from desk.trading import ledger

NY = ZoneInfo(config.TIMEZONE)
IDEA_LIFETIME_HOURS = {"crypto": 12, "forex": 12}
HORIZON_DAYS = {"intraday": 0, "1-5 days": 7, "1-4 weeks": 30}
STOCK_LAST_ENTRY = (15, 45)    # no new stock entries after 3:45pm New York
INTRADAY_EXIT = (15, 50)       # intraday trades are closed by 3:50pm


# ---- Pure decision helpers (unit-tested) -------------------------------------
def r_multiple(direction: str, entry: float, initial_stop: float, price: float) -> float:
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return 0.0
    move = (price - entry) if direction == "long" else (entry - price)
    return move / risk


def trailed_stop(direction: str, entry: float, initial_stop: float, current_stop: float, price: float) -> float | None:
    """Breakeven once the trade is up 1R, then trail 1R behind the price once it's up 2R.
    Returns a better stop, or None if the stop should stay where it is. Stops only ever tighten."""
    r = r_multiple(direction, entry, initial_stop, price)
    risk = abs(entry - initial_stop)
    candidate = None
    if r >= 2:
        candidate = price - risk if direction == "long" else price + risk
    elif r >= 1:
        candidate = entry
    if candidate is None:
        return None
    better = candidate > current_stop if direction == "long" else candidate < current_stop
    if not better:
        return None
    # ignore tiny adjustments (less than 0.1R) to avoid spamming order changes
    return candidate if abs(candidate - current_stop) >= 0.1 * risk else None


def time_stop_due(horizon: str | None, opened_at: str, at: datetime) -> bool:
    opened = datetime.fromisoformat(opened_at)
    days = HORIZON_DAYS.get((horizon or "").strip(), 14)
    if days == 0:
        return at.date() > opened.date() or (at.hour, at.minute) >= INTRADAY_EXIT
    return at - opened >= timedelta(days=days)


def plan_ideas(at: datetime) -> list[tuple[str, dict, dict]]:
    """(plan file, plan, idea) for every approved idea in the last day's plans."""
    out = []
    for day in (at.date(), at.date() - timedelta(days=1)):
        for session in ("morning", "evening"):
            path = PLANS_DIR / f"{day.isoformat()}-{session}.json"
            if not path.exists():
                continue
            plan = json.loads(path.read_text())
            for idea in plan.get("ideas", []):
                if idea.get("status") == "approved":
                    out.append((path.name, plan, idea))
    return out


def idea_expiry(plan: dict, idea: dict) -> datetime:
    created = datetime.fromisoformat(plan["created_at"])
    if idea["asset"] in ("stock", "etf"):
        return created.replace(hour=STOCK_LAST_ENTRY[0], minute=STOCK_LAST_ENTRY[1], second=0, microsecond=0)
    return created + timedelta(hours=IDEA_LIFETIME_HOURS[idea["asset"]])


# ---- Prices ------------------------------------------------------------------
def current_price(t: dict) -> float:
    if t["venue"] == "oanda":
        return oanda.price(t["symbol"])["mid"]
    return alpaca.last_price(t["symbol"])


# ---- The engine --------------------------------------------------------------
class Engine:
    def __init__(self, state: dict, book: Book):
        self.state = state
        self.book = book
        self.at = now()
        self.iso = self.at.isoformat(timespec="seconds")
        self.trades = ledger.load()
        self.alerts: list[str] = []
        self._clock = None

    # -- helpers --
    def clock(self) -> dict:
        if self._clock is None:
            self._clock = alpaca.clock()
        return self._clock

    def stock_market_open(self) -> bool:
        return bool(self.clock().get("is_open"))

    def blackout(self) -> bool:
        c = self.clock()
        if not c.get("is_open"):
            return False
        nxt_close = datetime.fromisoformat(c["next_close"]).astimezone(NY)
        today_open = nxt_close.replace(hour=9, minute=30)
        return in_blackout(today_open, nxt_close, self.at)

    def alert(self, text: str) -> None:
        self.alerts.append(text)

    def run(self) -> dict:
        summary = {"managed": 0, "entered": 0, "skipped": []}
        for t in [t for t in self.trades if t["status"] in ledger.ACTIVE]:
            try:
                self.manage(t)
                summary["managed"] += 1
            except Exception as exc:
                ledger.note(t, f"manage error: {str(exc)[:150]}", self.iso)
        for plan_file, plan, idea in plan_ideas(self.at):
            try:
                result = self.enter(plan_file, plan, idea)
                if result == "entered":
                    summary["entered"] += 1
                elif result:
                    summary["skipped"].append(f"{idea['symbol']}: {result}")
            except Exception as exc:
                summary["skipped"].append(f"{idea['symbol']}: error {str(exc)[:120]}")
        ledger.save(self.trades)
        for a in self.alerts:
            telegram.send(a)
        return summary

    # ---------------------------------------------------------------- ENTRIES
    def enter(self, plan_file: str, plan: dict, idea: dict) -> str | None:
        d = idea["decision"]
        trade_id = f"{plan_file.removesuffix('.json')}-{idea['symbol'].replace('/', '')}"
        existing = ledger.find(self.trades, trade_id)
        if existing and existing["status"] == "would_trade" and not self.state.get("dry_run"):
            self.trades.remove(existing)  # went live since the dry run: place it for real
        elif existing:
            return None  # already handled this idea
        if idea["symbol"] in ledger.active_symbols(self.trades):
            return self._record_skip(trade_id, plan_file, idea, "already have a trade on this symbol")
        if self.at >= idea_expiry(plan, idea):
            return self._record_skip(trade_id, plan_file, idea, "idea expired before an entry was possible")

        asset, direction = idea["asset"], d["decision"]
        # Is the market open for this asset right now?
        if asset in ("stock", "etf"):
            if not self.stock_market_open() or self.blackout():
                return "waiting for the stock market"
        elif asset == "forex" and not oanda.is_open_now():
            return "forex market closed"
        if asset == "crypto" and direction == "short":
            return self._record_skip(trade_id, plan_file, idea, "crypto can't be shorted on Alpaca")

        price = current_price({"venue": idea["venue"], "symbol": idea["symbol"]})
        entry, stop, target = float(d["entry"]), float(d["stop"]), float(d["target"])
        long = direction == "long"
        if (long and (price <= stop or price >= target)) or (not long and (price >= stop or price <= target)):
            return self._record_skip(trade_id, plan_file, idea, f"price {price} already past the stop or target")
        if d.get("order_type") == "market":
            entry = price
        # If the market already moved through our limit, chasing is only allowed within a quarter of the risk.
        risk = abs(entry - stop)
        if (long and price > entry + 0.25 * risk) or (not long and price < entry - 0.25 * risk):
            return "price moved away from the entry; waiting for a pullback"

        kw = {}
        if asset == "forex":
            kw = {"quote_to_usd": quote_to_usd(idea["symbol"]), "unit_usd": oanda.usd_value_per_unit(idea["symbol"])}
        sizing = team.size({**d, "entry": entry}, self.book.equity, asset, **kw)
        units = sizing["units"]
        if asset in ("stock", "etf"):
            units = math.floor(units)
            if units < 1:
                return self._record_skip(trade_id, plan_file, idea, "position too small for one whole share")
            if not long:
                a = alpaca.asset(idea["symbol"])
                if not (a.get("shortable") and a.get("easy_to_borrow")):
                    return self._record_skip(trade_id, plan_file, idea, "not available to short")
            notional = units * entry
        elif asset == "forex":
            units = int(units)
            notional = units * kw["unit_usd"]
        else:
            notional = units * entry

        verdict = check_order(OrderIdea(idea["symbol"], idea["venue"], "buy" if long else "sell", notional, entry, stop, target),
                              self.book, self.state, stock_blackout=self.blackout() if asset in ("stock", "etf") else False)
        if not verdict.ok:
            if any("paused" in r or "daily loss" in r or "trades today" in r or "open/close" in r for r in verdict.reasons):
                return str(verdict)  # temporary block; try again later
            return self._record_skip(trade_id, plan_file, idea, str(verdict))

        t = {
            "id": trade_id, "plan_file": plan_file, "symbol": idea["symbol"], "venue": idea["venue"], "asset": asset,
            "direction": direction, "bucket": d.get("bucket"), "style": d.get("style"), "conviction": d.get("conviction"),
            "confidence_pct": d.get("confidence_pct"), "horizon": d.get("horizon"), "thesis": d.get("thesis"),
            "entry_plan": entry, "initial_stop": stop, "stop": stop, "target": target, "qty": units,
            "notional_usd": round(notional, 2), "risk_usd": sizing["risk_usd"], "risk_pct": sizing["risk_pct"],
            "quote_to_usd": kw.get("quote_to_usd", 1.0), "created_at": self.iso,
            "expires_at": idea_expiry(plan, idea).isoformat(timespec="seconds"),
        }

        if self.state.get("dry_run"):
            t["status"] = "would_trade"
            self.trades.append(t)
            self.alert(f"<b>[DRY RUN] Would {'BUY' if long else 'SHORT'} {esc(idea['symbol'])}</b>\n"
                       f"{_qty(t)} @ {_p(entry)} | stop {_p(stop)} | target {_p(target)}\n"
                       f"Size ${notional:,.0f}, risking ${sizing['risk_usd']:,.0f}")
            return "entered"

        self._submit(t, long)
        self.trades.append(t)
        self.state["trades_today"] = self.state.get("trades_today", 0) + 1
        if asset in ("stock", "etf"):
            self.book.stock_gross_exposure += notional
        elif asset == "forex":
            self.book.forex_gross_notional += notional
        else:
            self.book.stock_gross_exposure += notional
        self.book.open_positions += 1
        log_event("order_placed", trade=trade_id, symbol=t["symbol"], side=direction, qty=t["qty"], entry=entry)
        self.alert(f"<b>ORDER PLACED: {'BUY' if long else 'SHORT'} {esc(t['symbol'])}</b> ({esc(t['bucket'])}, {esc(t['style'])}, "
                   f"conviction {t['conviction']}/10)\n"
                   f"{_qty(t)} @ {_p(entry)} | stop {_p(stop)} | target {_p(target)}\n"
                   f"Size ${notional:,.0f}, risking ${t['risk_usd']:,.0f} ({t['risk_pct']}%)\n<i>{esc(t['thesis'])}</i>")
        return "entered"

    def _submit(self, t: dict, long: bool) -> None:
        order_type = "limit"
        if t["asset"] in ("stock", "etf"):
            o = alpaca.submit_order(t["symbol"], "buy" if long else "sell", t["qty"], order_type, t["entry_plan"],
                                    take_profit=t["target"], stop_loss=t["stop"], time_in_force="gtc",
                                    client_order_id=t["id"])
            t["entry_order_id"] = o["id"]
        elif t["asset"] == "crypto":
            o = alpaca.submit_order(t["symbol"], "buy", t["qty"], order_type, t["entry_plan"], client_order_id=t["id"])
            t["entry_order_id"] = o["id"]
        else:
            units = t["qty"] if long else -t["qty"]
            o = oanda.submit_order(t["symbol"], units, "LIMIT", t["entry_plan"], take_profit=t["target"],
                                   stop_loss=t["stop"], client_tag=t["id"])
            created = o.get("orderCreateTransaction", {})
            t["entry_order_id"] = created.get("id")
            if o.get("orderCancelTransaction"):
                raise RuntimeError(f"OANDA rejected the order: {o['orderCancelTransaction'].get('reason')}")
        t["status"] = "pending"
        ledger.note(t, "entry order placed", self.iso)

    def _record_skip(self, trade_id: str, plan_file: str, idea: dict, reason: str) -> str:
        d = idea["decision"]
        self.trades.append({"id": trade_id, "plan_file": plan_file, "symbol": idea["symbol"], "venue": idea["venue"],
                            "asset": idea["asset"], "direction": d.get("decision"), "status": "skipped",
                            "skip_reason": reason, "entry_plan": d.get("entry"), "initial_stop": d.get("stop"),
                            "stop": d.get("stop"), "target": d.get("target"), "bucket": d.get("bucket"),
                            "style": d.get("style"), "conviction": d.get("conviction"), "thesis": d.get("thesis"),
                            "created_at": self.iso})
        return reason

    # ------------------------------------------------------------- MANAGEMENT
    def manage(self, t: dict) -> None:
        if t["status"] == "pending":
            self._check_entry(t)
        elif t["status"] == "closing":
            self._check_closing(t)
        if t["status"] == "open":
            if self._check_exit(t):
                return
            self._adjust(t)

    def _check_entry(self, t: dict) -> None:
        expired = self.at >= datetime.fromisoformat(t["expires_at"])
        if t["venue"] == "alpaca":
            o = alpaca.get_order(t["entry_order_id"])
            filled = float(o.get("filled_qty") or 0)
            if o["status"] == "filled" or (expired and filled > 0):
                if o["status"] != "filled":
                    alpaca.cancel_order(o["id"])
                self._opened(t, float(o["filled_avg_price"]), filled, o)
            elif o["status"] in ("canceled", "expired", "rejected"):
                self._cancelled(t, f"entry order {o['status']}")
            elif expired:
                alpaca.cancel_order(o["id"])
                self._cancelled(t, "entry never filled before the idea expired")
        else:
            o = oanda.get_order(t["entry_order_id"])
            if o["state"] == "FILLED":
                trade = oanda.get_trade(o["tradeOpenedID"])
                t["oanda_trade_id"] = trade["id"]
                self._opened(t, float(trade["price"]), abs(float(trade["initialUnits"])), None)
            elif o["state"] == "CANCELLED":
                self._cancelled(t, f"entry order cancelled ({o.get('cancellingTransactionID')})")
            elif expired:
                oanda.cancel_order(o["id"])
                self._cancelled(t, "entry never filled before the idea expired")

    def _opened(self, t: dict, price: float, qty: float, order: dict | None) -> None:
        if t["asset"] == "crypto":  # Alpaca takes crypto fees out of the coins received
            pos = alpaca.position(t["symbol"])
            if pos:
                qty = float(pos.get("qty_available") or pos["qty"])
        t.update(status="open", entry_price=price, filled_qty=qty, filled_qty_initial=qty, opened_at=self.iso)
        if order and t["asset"] in ("stock", "etf"):
            for leg in order.get("legs") or []:
                if leg["type"] in ("stop", "stop_limit", "trailing_stop"):
                    t["stop_order_id"] = leg["id"]
                elif leg["type"] == "limit":
                    t["target_order_id"] = leg["id"]
        if t["asset"] == "crypto":
            self._place_crypto_stop(t, t["stop"], qty)
        ledger.note(t, f"filled {qty:g} @ {price}", self.iso)
        log_event("filled", trade=t["id"], price=price, qty=qty)
        self.alert(f"<b>FILLED: {'BOUGHT' if t['direction'] == 'long' else 'SHORTED'} {esc(t['symbol'])}</b> "
                   f"{qty:g} @ {_p(price)}\nStop {_p(t['stop'])} | target {_p(t['target'])}")

    def _cancelled(self, t: dict, why: str) -> None:
        t.update(status="cancelled", closed_at=self.iso, exit_reason=why)
        ledger.note(t, why, self.iso)

    def _place_crypto_stop(self, t: dict, stop: float, qty: float) -> None:
        o = alpaca.submit_raw({"symbol": t["symbol"], "side": "sell", "type": "stop_limit", "qty": str(qty),
                               "stop_price": alpaca.px(stop), "limit_price": alpaca.px(stop * 0.99), "time_in_force": "gtc"})
        t["stop_order_id"] = o["id"]

    def _check_exit(self, t: dict) -> bool:
        """Has the stop or target already closed the trade? Returns True if it's closed."""
        if t["venue"] == "oanda":
            tr = oanda.get_trade(t["oanda_trade_id"])
            if tr["state"] == "CLOSED":
                price = float(tr.get("averageClosePrice") or 0)
                # realizedPL already includes any partial profit taken earlier
                self._closed(t, price, _exit_reason(t, price), pnl=float(tr.get("realizedPL") or 0), includes_partials=True)
                return True
            return False

        for key, reason in (("target_order_id", "hit target"), ("stop_order_id", "stopped out")):
            if t.get(key):
                o = alpaca.get_order(t[key])
                if o["status"] == "filled":
                    self._closed(t, float(o["filled_avg_price"]), reason)
                    return True
        if alpaca.position(t["symbol"]) is None:
            price = current_price(t)
            self._closed(t, price, "position closed outside the engine (e.g. /flatten)")
            return True
        return False

    def _adjust(self, t: dict) -> None:
        price = current_price(t)
        long = t["direction"] == "long"
        entry = t["entry_price"]
        t["last_price"] = price
        r = r_multiple(t["direction"], entry, t["initial_stop"], price)
        t["max_r"] = round(max(t.get("max_r", 0), r), 2)

        # Crypto has no resting take-profit order, so the engine takes it.
        if t["asset"] == "crypto" and price >= t["target"]:
            self._exit_now(t, "hit target")
            return
        if time_stop_due(t.get("horizon"), t["opened_at"], self.at):
            if t["asset"] in ("stock", "etf") and not self.stock_market_open():
                return
            self._exit_now(t, f"time stop ({t.get('horizon')})")
            return

        # Partial profit on forex/crypto at +1.5R (stock brackets keep the full size and trail instead).
        if t["asset"] in ("forex", "crypto") and r >= 1.5 and not t.get("partial_taken"):
            self._take_partial(t, price)

        new_stop = trailed_stop(t["direction"], entry, t["initial_stop"], t["stop"], price)
        if new_stop is not None:
            self._move_stop(t, new_stop)

    def _move_stop(self, t: dict, new_stop: float) -> None:
        if t["venue"] == "oanda":
            oanda.set_stop(t["oanda_trade_id"], new_stop, t["symbol"])
        elif t["asset"] in ("stock", "etf"):
            o = alpaca.replace_order(t["stop_order_id"], stop_price=alpaca.px(new_stop))
            t["stop_order_id"] = o.get("id", t["stop_order_id"])  # a replaced order gets a new id
        else:
            alpaca.cancel_order(t["stop_order_id"])
            self._place_crypto_stop(t, new_stop, t["filled_qty"])
        what = "to breakeven" if abs(new_stop - t["entry_price"]) < 1e-9 else f"to {_p(new_stop)} (trailing)"
        ledger.note(t, f"stop moved {what}", self.iso)
        t["stop"] = new_stop
        self.alert(f"Stop on {esc(t['symbol'])} moved {what}. Locking in gains.")

    def _take_partial(self, t: dict, price: float) -> None:
        if t["venue"] == "oanda":
            half = int(t["filled_qty"] // 2)
            if half < 1:
                return
            res = oanda.close_trade(t["oanda_trade_id"], half)
            pnl = float(res.get("orderFillTransaction", {}).get("pl", 0) or 0)
        else:
            half = round(t["filled_qty"] / 2, 6)
            if half * price < 12 or (t["filled_qty"] - half) * t["stop"] < 12:
                return  # Alpaca's crypto minimum is $10 per order; too small to split
            alpaca.cancel_order(t["stop_order_id"])
            alpaca.submit_raw({"symbol": t["symbol"], "side": "sell", "type": "market", "qty": str(half), "time_in_force": "gtc"})
            pnl = (price - t["entry_price"]) * half
            self._place_crypto_stop(t, t["stop"], round(t["filled_qty"] - half, 6))
        t["filled_qty"] = round(t["filled_qty"] - half, 6)
        t["partial_pnl"] = round(t.get("partial_pnl", 0) + pnl, 2)
        t["partial_taken"] = True
        ledger.note(t, f"took half off at {price} (+${pnl:,.2f})", self.iso)
        self.alert(f"Took partial profit on {esc(t['symbol'])}: closed half at {_p(price)} for ${pnl:+,.2f}.")

    def _exit_now(self, t: dict, reason: str) -> None:
        """Close the whole position at market."""
        if t["venue"] == "oanda":
            res = oanda.close_trade(t["oanda_trade_id"])
            fill = res.get("orderFillTransaction", {})
            self._closed(t, float(fill.get("price") or current_price(t)), reason, pnl=float(fill.get("pl") or 0))
            return
        for key in ("stop_order_id", "target_order_id"):
            if t.get(key):
                try:
                    alpaca.cancel_order(t[key])
                except Exception:
                    pass
        o = alpaca.close_position(t["symbol"])
        t.update(status="closing", exit_order_id=o.get("id"), exit_reason=reason)
        ledger.note(t, f"closing: {reason}", self.iso)

    def _check_closing(self, t: dict) -> None:
        if not t.get("exit_order_id"):
            self._closed(t, current_price(t), t.get("exit_reason", "closed"))
            return
        o = alpaca.get_order(t["exit_order_id"])
        if o["status"] == "filled":
            self._closed(t, float(o["filled_avg_price"]), t.get("exit_reason", "closed"))

    def _closed(self, t: dict, price: float, reason: str, pnl: float | None = None, includes_partials: bool = False) -> None:
        long = t["direction"] == "long"
        qty = t["filled_qty"]
        if pnl is None:
            pnl = (price - t["entry_price"]) * qty * (1 if long else -1) * t.get("quote_to_usd", 1.0)
        if not includes_partials:
            pnl += t.get("partial_pnl", 0)
        initial_risk = abs(t["entry_price"] - t["initial_stop"]) * (t.get("filled_qty_initial") or t["qty"]) * t.get("quote_to_usd", 1.0)
        t.update(status="closed", exit_price=price, exit_reason=reason, closed_at=self.iso, pnl_usd=round(pnl, 2),
                 r_result=round(pnl / initial_risk, 2) if initial_risk else None)
        ledger.note(t, f"closed at {price}: {reason}, P&L ${pnl:+,.2f}", self.iso)
        log_event("closed", trade=t["id"], pnl=round(pnl, 2), reason=reason)
        verdict = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
        self.alert(f"<b>CLOSED {esc(t['symbol'])}: {verdict} ${pnl:+,.2f}</b> ({t['r_result']}R)\n"
                   f"{'Long' if long else 'Short'} {_p(t['entry_price'])} -> {_p(price)} | {esc(reason)}")


def _exit_reason(t: dict, price: float) -> str:
    if not price:
        return "closed"
    long = t["direction"] == "long"
    if (long and price >= t["target"] * 0.999) or (not long and price <= t["target"] * 1.001):
        return "hit target"
    return "stopped out"


def _p(x) -> str:
    x = float(x)
    return f"{x:,.2f}" if x >= 10 else f"{x:.5f}" if x >= 0.01 else f"{x:.8f}"


def _qty(t: dict) -> str:
    if t["asset"] == "forex":
        return f"{int(t['qty']):,} units"
    if t["asset"] == "crypto":
        return f"{t['qty']:g}"
    return f"{int(t['qty'])} shares"
