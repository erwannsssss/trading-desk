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
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from desk import activity as floor
from desk import bank, config, telegram
from desk.brokers import alpaca, oanda
from desk.research import team
from desk.research.planner import PLANS_DIR, quote_to_usd
from desk.risk import Book, OrderIdea, check_order, in_blackout
from desk.state import log_event, now
from desk.telegram import esc
from desk.trading import guards, ledger, options

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
    if not PLANS_DIR.exists():
        return out
    days = {at.date().isoformat(), (at.date() - timedelta(days=1)).isoformat()}
    for path in sorted(PLANS_DIR.glob("*.json")):
        if path.name[:10] not in days:
            continue
        plan = json.loads(path.read_text())
        for idea in plan.get("ideas", []):
            if idea.get("status") == "approved":
                out.append((path.name, plan, idea))
    return out


def idea_expiry(plan: dict, idea: dict) -> datetime:
    created = datetime.fromisoformat(plan["created_at"])
    if idea["asset"] in ("stock", "etf"):
        last = created.replace(hour=STOCK_LAST_ENTRY[0], minute=STOCK_LAST_ENTRY[1], second=0, microsecond=0)
        if plan.get("session") in ("intraday", "alert"):
            return min(created + timedelta(hours=2), last)  # intraday ideas go stale fast
        if plan.get("session") == "pitch":
            return created + timedelta(hours=26)  # a pitch made after hours can still be placed the next day
        return last
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

    def can_short_stocks(self) -> bool:
        """Alpaca turns shorting off for some paper accounts; bearish stock ideas then go through put options."""
        if not hasattr(self, "_can_short"):
            try:
                self._can_short = bool(alpaca.account().get("shorting_enabled"))
            except Exception:
                self._can_short = False
        return self._can_short

    def stock_market_open(self) -> bool:
        return bool(self.clock().get("is_open"))

    def blackout(self) -> bool:
        c = self.clock()
        if not c.get("is_open"):
            return False
        nxt_close = datetime.fromisoformat(c["next_close"]).astimezone(NY)
        today_open = nxt_close.replace(hour=9, minute=30)
        return in_blackout(today_open, nxt_close, self.at)

    def alert(self, text: str, agent: str = "execution", symbol: str | None = None, telegram_too: bool = True) -> None:
        if telegram_too:
            self.alerts.append(text)
        plain = re.sub(r"<[^>]+>", "", text).replace("\n", " | ")
        floor.event(agent, plain, symbol, kind="trade")

    def run(self) -> dict:
        summary = {"managed": 0, "entered": 0, "skipped": []}
        active = [t for t in self.trades if t["status"] in ledger.ACTIVE]
        ideas = plan_ideas(self.at)
        floor.working("positions", f"Watching {len(active)} open trade{'s' if len(active) != 1 else ''}") if active else None
        floor.working("execution", f"Working through {len(ideas)} idea{'s' if len(ideas) != 1 else ''} from the latest plans")
        for t in [t for t in self.trades if t["status"] in ledger.ACTIVE]:
            if t.get("asset") in ("stock", "option") and "industry" not in t:
                t["industry"] = _industry(t["symbol"])   # older trades, for the same-industry limit
            try:
                self.manage(t)
                summary["managed"] += 1
            except Exception as exc:
                ledger.note(t, f"manage error: {str(exc)[:150]}", self.iso)
        self._earnings_exits()
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
        waiting = [s for s in summary["skipped"] if "waiting" in s or "closed" in s]
        floor.done("execution", f"Check done: {summary['entered']} new order{'s' if summary['entered'] != 1 else ''}"
                   + (f", {len(waiting)} waiting for their market" if waiting else ""), kind="check")
        if active:
            open_now = [t for t in self.trades if t["status"] == "open"]
            floor.done("positions", f"Managing {len(open_now)} open position{'s' if len(open_now) != 1 else ''}: "
                       + (", ".join(f"{t['symbol']} {t['direction']}" for t in open_now[:8]) or "none"), kind="check")
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
        why = guards.risk_off_block((plan.get("macro") or {}).get("regime"), asset, direction, d.get("conviction"))
        if why:
            return self._record_skip(trade_id, plan_file, idea, why)
        industry = _industry(idea["symbol"]) if asset == "stock" else None
        why = guards.same_bet(self.trades, asset, idea["symbol"], direction, d.get("style"), industry)
        if why:
            floor.event("risk", f"Blocked {idea['symbol']}: {why}", idea["symbol"], kind="blocked")
            return self._record_skip(trade_id, plan_file, idea, f"same bet: {why}")
        if asset in ("stock", "etf"):
            e = guards.earnings_calendar().get(idea["symbol"]) or {}
            why = guards.earnings_conflict(e.get("date"), d.get("horizon"), d.get("style"), self.at.date())
            if why:
                floor.event("risk", f"Skipped {idea['symbol']}: {why}", idea["symbol"], kind="blocked")
                return self._record_skip(trade_id, plan_file, idea, why)

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

        short_blocked = asset in ("stock", "etf") and not long and not self.can_short_stocks()
        if asset in ("stock", "etf") and (d.get("expression") in ("call_options", "put_options") or short_blocked):
            result = self._enter_option(trade_id, plan_file, plan, idea, price, entry, stop, target, long)
            if result is not None:
                if short_blocked and d.get("expression") != "put_options":
                    floor.event("options", f"{idea['symbol']}: the account can't short stocks, so the bearish view goes on as puts", idea["symbol"])
                return result  # otherwise no affordable contract: trade the shares instead
            if short_blocked:
                return self._record_skip(trade_id, plan_file, idea, "this Alpaca account can't short stocks, and no affordable put contract was found")

        from desk import learning
        kw = {"multiplier": learning.risk_multiplier(d.get("style"))}
        if asset == "forex":
            kw.update(quote_to_usd=quote_to_usd(idea["symbol"]), unit_usd=oanda.usd_value_per_unit(idea["symbol"]))
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

        # The bank: forex only ties up its margin; other trades borrow only at conviction 7+.
        cash_needed = notional * oanda.margin_rate(idea["symbol"]) if asset == "forex" else notional
        loan = bank.loan_needed(cash_needed, self.book.equity, self.book.cash_used)
        if loan > 0 and asset != "forex" and int(d.get("conviction") or 0) < bank.BORROW_MIN_CONVICTION:
            free = bank.free_cash(self.book.equity, self.book.cash_used) * 0.98
            new_units = math.floor(free / entry) if asset in ("stock", "etf") else round(free / entry, 6)
            if new_units <= 0 or new_units * entry < 50:
                return self._record_skip(trade_id, plan_file, idea, "the firm's own cash is fully used, and only conviction "
                                         f"{bank.BORROW_MIN_CONVICTION}+ trades can borrow from the bank")
            ratio = new_units / units
            floor.event("treasury", f"{idea['symbol']}: conviction {d.get('conviction')} is too low to borrow, so it's sized to the "
                        f"firm's free cash (${free:,.0f}) instead of ${notional:,.0f}", idea["symbol"])
            units, notional, cash_needed, loan = new_units, new_units * entry, new_units * entry, 0.0
            sizing = {**sizing, "units": units, "risk_usd": round(sizing["risk_usd"] * ratio, 2),
                      "risk_pct": round(sizing["risk_pct"] * ratio, 2)}

        verdict = check_order(OrderIdea(idea["symbol"], idea["venue"], "buy" if long else "sell", notional, entry, stop, target),
                              self.book, self.state, stock_blackout=self.blackout() if asset in ("stock", "etf") else False)
        if not verdict.ok:
            floor.event("risk", f"Blocked {idea['symbol']}: {'; '.join(verdict.reasons)}", idea["symbol"], kind="blocked")
            if any("paused" in r or "daily loss" in r or "trades today" in r or "open/close" in r for r in verdict.reasons):
                return str(verdict)  # temporary block; try again later
            return self._record_skip(trade_id, plan_file, idea, str(verdict))
        floor.event("risk", f"Approved {idea['symbol']}: ${notional:,.0f} position, risking ${sizing['risk_usd']:,.0f}; all limits OK", idea["symbol"])

        t = {
            "id": trade_id, "plan_file": plan_file, "symbol": idea["symbol"], "venue": idea["venue"], "asset": asset,
            "direction": direction, "bucket": d.get("bucket"), "style": d.get("style"), "conviction": d.get("conviction"),
            "confidence_pct": d.get("confidence_pct"), "horizon": d.get("horizon"), "thesis": d.get("thesis"),
            "entry_plan": entry, "initial_stop": stop, "stop": stop, "target": target, "qty": units,
            "notional_usd": round(notional, 2), "risk_usd": sizing["risk_usd"], "risk_pct": sizing["risk_pct"],
            "quote_to_usd": kw.get("quote_to_usd", 1.0), "created_at": self.iso, "industry": industry,
            "expires_at": idea_expiry(plan, idea).isoformat(timespec="seconds"),
        }

        t["loan_usd"] = loan
        t["loan_apr"] = bank.current_apr()
        t["loan_basis"] = "margin" if asset == "forex" else "cash"
        if self.state.get("dry_run"):
            t["status"] = "would_trade"
            self.trades.append(t)
            self.alert(f"<b>[DRY RUN] Would {'BUY' if long else 'SHORT'} {esc(idea['symbol'])}</b>\n"
                       f"{_qty(t)} @ {_p(entry)} | stop {_p(stop)} | target {_p(target)}\n"
                       f"Size ${notional:,.0f}, risking ${sizing['risk_usd']:,.0f}", symbol=idea["symbol"])
            return "entered"

        if asset in ("stock", "etf", "crypto") and not self._fits_broker(t, asset):
            return "not enough broker buying power right now"
        self._submit(t, long)
        self.trades.append(t)
        self._borrow(t)
        self.state["trades_today"] = self.state.get("trades_today", 0) + 1
        if asset in ("stock", "etf"):
            self.book.stock_gross_exposure += notional
        elif asset == "forex":
            self.book.forex_gross_notional += notional
            self.book.forex_margin += cash_needed
        else:
            self.book.stock_gross_exposure += notional
        self.book.open_positions += 1
        log_event("order_placed", trade=trade_id, symbol=t["symbol"], side=direction, qty=t["qty"], entry=entry)
        self.alert(f"<b>ORDER PLACED: {'BUY' if long else 'SHORT'} {esc(t['symbol'])}</b> ({esc(t['bucket'])}, {esc(t['style'])}, "
                   f"conviction {t['conviction']}/10)\n"
                   f"{_qty(t)} @ {_p(entry)} | stop {_p(stop)} | target {_p(target)}\n"
                   f"Size ${notional:,.0f}, risking ${t['risk_usd']:,.0f} ({t['risk_pct']}%)\n<i>{esc(t['thesis'])}</i>", symbol=t["symbol"])
        return "entered"

    def _enter_option(self, trade_id, plan_file, plan, idea, price, entry, stop, target, long) -> str | None:
        d = idea["decision"]
        kind = "call" if long else "put"
        conv = int(d.get("conviction") or 5)
        budget_pct = min(config.MAX_OPTIONS_PREMIUM_PCT, 6.0 if conv >= 8 else 4.0 if conv >= 6 else 2.5)
        budget = self.book.equity * budget_pct / 100
        open_premium = sum(t.get("notional_usd", 0) for t in self.trades if t.get("asset") == "option" and t["status"] in ledger.ACTIVE)
        if open_premium + budget > self.book.equity * config.MAX_OPTIONS_TOTAL_PCT / 100:
            return None
        c = None
        try:
            c = options.pick_spread(idea["symbol"], kind, price, target, d.get("horizon"), max_cost=budget)
        except Exception as exc:
            print(f"spread pick failed for {idea['symbol']}: {exc}")
        if c:
            limit = c["limit"]
        else:
            c = options.pick_contract(idea["symbol"], kind, price, d.get("horizon"), max_cost=budget)
            if not c:
                return None
            limit = options.entry_limit(c)
        qty = options.contracts_for(budget, limit)
        if qty < 1:
            return None
        premium_total = round(qty * limit * 100, 2)
        loan = bank.loan_needed(premium_total, self.book.equity, self.book.cash_used)
        if loan > 0 and conv < bank.BORROW_MIN_CONVICTION:
            return None   # options below conviction 7 must be paid from the firm's own cash
        verdict = check_order(OrderIdea(c["symbol"], "alpaca", "buy", premium_total, limit, limit * 0.5, limit * 2),
                              self.book, self.state, stock_blackout=self.blackout())
        if not verdict.ok:
            floor.event("risk", f"Blocked {idea['symbol']} options: {'; '.join(verdict.reasons)}", idea["symbol"], kind="blocked")
            return str(verdict)
        floor.event("risk", f"Approved {idea['symbol']} options: ${premium_total:,.0f} premium is the most it can lose", idea["symbol"])
        t = {
            "id": trade_id, "plan_file": plan_file, "symbol": idea["symbol"], "venue": "alpaca", "asset": "option",
            "option_symbol": c["symbol"], "option_type": kind, "strike": c["strike"], "expiry": c["expiry"],
            **({"short_option_symbol": c["short_symbol"], "short_strike": c["short_strike"], "spread_width": c["width"],
                "single_option_cost": c.get("single_cost")} if c.get("short_symbol") else {}),
            "direction": "long" if long else "short", "bucket": d.get("bucket") or "high_risk", "style": d.get("style"),
            "industry": _industry(idea["symbol"]),
            "conviction": d.get("conviction"), "confidence_pct": d.get("confidence_pct"), "horizon": d.get("horizon"),
            "thesis": d.get("thesis"), "entry_plan": entry, "initial_stop": stop, "stop": stop, "target": target,
            "qty": qty, "premium_limit": limit, "notional_usd": premium_total, "risk_usd": premium_total,
            "risk_pct": round(premium_total / self.book.equity * 100, 2), "quote_to_usd": 100.0,
            "created_at": self.iso, "expires_at": idea_expiry(plan, idea).isoformat(timespec="seconds"),
        }
        if c.get("short_symbol"):
            label = (f"{qty} {esc(idea['symbol'])} {c['expiry']} ${c['strike']:g}/${c['short_strike']:g} {kind} spread{'s' if qty > 1 else ''}")
        else:
            label = f"{qty} {esc(idea['symbol'])} {c['expiry']} ${c['strike']:g} {kind}{'s' if qty > 1 else ''}"
        if self.state.get("dry_run"):
            t["status"] = "would_trade"
            self.trades.append(t)
            self.alert(agent="options", symbol=idea["symbol"], text=f"<b>[DRY RUN] Would buy {label}</b> at ${limit:.2f} (${premium_total:,.0f} total, the most it can lose)\n"
                       f"Exit if {esc(idea['symbol'])} hits {_p(stop)}; target {_p(target)}")
            return "entered"
        t["loan_usd"] = loan
        t["loan_apr"] = bank.current_apr()
        if c.get("short_symbol"):
            o = alpaca.submit_raw({"order_class": "mleg", "qty": str(qty), "type": "limit", "limit_price": f"{limit:.2f}",
                                   "time_in_force": "day", "client_order_id": trade_id[:48],
                                   "legs": [{"symbol": c["symbol"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
                                            {"symbol": c["short_symbol"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}]})
            floor.event("options", f"{idea['symbol']}: a ${c['strike']:g}/${c['short_strike']:g} {kind} spread costs ${limit:.2f} instead of "
                        f"${c.get('single_cost') or 0:.2f} for the single {kind}; the profit is capped at the ${c['width']:g} gap", idea["symbol"])
        else:
            o = alpaca.submit_raw({"symbol": c["symbol"], "side": "buy", "type": "limit", "qty": str(qty),
                                   "limit_price": f"{limit:.2f}", "time_in_force": "day", "client_order_id": trade_id[:48]})
        t.update(status="pending", entry_order_id=o["id"])
        self._borrow(t)
        ledger.note(t, f"bought to open {c['symbol']} limit {limit}", self.iso)
        self.trades.append(t)
        self.state["trades_today"] = self.state.get("trades_today", 0) + 1
        self.book.stock_gross_exposure += premium_total
        self.book.open_positions += 1
        log_event("order_placed", trade=trade_id, symbol=c["symbol"], side="buy", qty=qty, entry=limit)
        self.alert(agent="options", symbol=idea["symbol"], text=f"<b>ORDER PLACED: buy {label}</b> ({esc(t['style'])}, conviction {t['conviction']}/10)\n"
                   f"${limit:.2f} each, ${premium_total:,.0f} total (the most it can lose)\n"
                   f"Exit if {esc(idea['symbol'])} hits {_p(stop)}; target {_p(target)}\n<i>{esc(t['thesis'])}</i>")
        return "entered"

    def _borrow(self, t: dict) -> None:
        if t.get("loan_usd"):
            self.book.borrowed += t["loan_usd"]
            floor.event("treasury", f"Borrowed ${t['loan_usd']:,.0f} from {bank.BANK_NAME} for {t['symbol']} at {(t.get("loan_apr") or bank.LOAN_APR) * 100:g}% a year "
                        f"(${self.book.borrowed:,.0f} of ${bank.CREDIT_LINE:,.0f} now drawn)", t["symbol"], kind="trade")

    def _fits_broker(self, t: dict, asset: str) -> bool:
        """Alpaca has its own limits: crypto is cash-only and stocks held overnight get 2x margin. Shrink to fit."""
        try:
            acct = alpaca.account()
        except Exception:
            return True
        if asset == "crypto":
            bp = float(acct.get("non_marginable_buying_power") or acct.get("cash") or 0)
        elif (t.get("horizon") or "") == "intraday":
            bp = float(acct.get("daytrading_buying_power") or acct.get("buying_power") or 0)
        else:
            bp = float(acct.get("regt_buying_power") or acct.get("buying_power") or 0)
        need = t["qty"] * t["entry_plan"]
        if need <= bp * 0.97:
            return True
        scale = bp * 0.97 / need
        qty = math.floor(t["qty"] * scale) if asset in ("stock", "etf") else round(t["qty"] * scale, 6)
        if qty <= 0 or qty * t["entry_plan"] < 10:
            floor.event("execution", f"Skipped {t['symbol']}: Alpaca only has ${bp:,.0f} of buying power for it", t["symbol"], kind="skip")
            return False
        floor.event("execution", f"Trimmed {t['symbol']} from {t['qty']:g} to {qty:g} to fit Alpaca's ${bp:,.0f} buying power", t["symbol"])
        ratio = qty / t["qty"]
        t["qty"] = qty
        for k in ("notional_usd", "risk_usd", "loan_usd"):
            if t.get(k):
                t[k] = round(t[k] * ratio, 2)
        return True

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
        floor.event("execution", f"Skipped {idea['symbol']}: {reason}", idea["symbol"], kind="skip")
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
                price = options.mleg_net_price(o) if t.get("short_option_symbol") else float(o["filled_avg_price"])
                self._opened(t, price if price is not None else t.get("premium_limit") or 0.0, filled, o)
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
        if t["asset"] == "option":
            t["premium_paid"] = price
            t["underlying_entry"] = current_price(t)
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
                   f"{qty:g} @ {_p(price)}\nStop {_p(t['stop'])} | target {_p(t['target'])}",
                   agent="options" if t["asset"] == "option" else "execution", symbol=t["symbol"])

    def _cancelled(self, t: dict, why: str) -> None:
        if t.get("loan_usd"):
            floor.event("treasury", f"Returned the unused ${t['loan_usd']:,.0f} loan for {t['symbol']} (order never filled)", t["symbol"])
            t["loan_usd"] = 0.0
        floor.event("execution", f"Cancelled the {t['symbol']} entry: {why}", t["symbol"], kind="skip")
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

        if t["asset"] == "option":
            if alpaca.position(t["option_symbol"]) is None:
                if t.get("short_option_symbol"):
                    q = options.spread_mark(t["option_symbol"], t["short_option_symbol"]) or {}
                else:
                    q = options.quote(t["option_symbol"]) or {}
                self._closed(t, max(0.0, float(q.get("bid") or 0)), "option position closed outside the engine or expired")
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
        if t["asset"] == "option":
            self._adjust_option(t)
            return
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

    def _adjust_option(self, t: dict) -> None:
        under = current_price(t)
        spread = bool(t.get("short_option_symbol"))
        q = (options.spread_mark(t["option_symbol"], t["short_option_symbol"]) if spread else options.quote(t["option_symbol"])) or {}
        mark = q.get("mid")
        t["last_price"], t["option_mark"] = under, mark
        long = t["direction"] == "long"   # long = calls (bullish), short = puts (bearish)
        r = r_multiple(t["direction"], t["underlying_entry"], t["initial_stop"], under)
        t["max_r"] = round(max(t.get("max_r", 0), r), 2)
        reason = None
        if (long and under <= t["stop"]) or (not long and under >= t["stop"]):
            reason = "underlying hit the stop"
        elif (long and under >= t["target"]) or (not long and under <= t["target"]):
            reason = "underlying hit the target"
        elif spread and mark and t.get("spread_width") and mark >= 0.8 * t["spread_width"]:
            reason = "spread is worth 80%+ of its maximum; taking the profit"
        elif mark and mark >= 2 * t["premium_paid"]:
            reason = "option doubled; taking the profit"
        elif options.days_to_expiry(t["option_symbol"], self.at.date()) <= 3:
            reason = "close to expiry"
        elif time_stop_due(t.get("horizon"), t["opened_at"], self.at):
            reason = f"time stop ({t.get('horizon')})"
        if reason:
            if self.stock_market_open():
                self._exit_now(t, reason)
            return
        new_stop = trailed_stop(t["direction"], t["underlying_entry"], t["initial_stop"], t["stop"], under)
        if new_stop is not None:
            t["stop"] = new_stop
            ledger.stop_moved(t, new_stop, self.iso)
            ledger.note(t, f"exit level on the underlying moved to {_p(new_stop)}", self.iso)
            floor.event("options", f"Raised the exit level on {t['symbol']} calls/puts to {_p(new_stop)} to lock in gains", t["symbol"])

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
        ledger.stop_moved(t, new_stop, self.iso)
        t["stop"] = new_stop
        self.alert(f"Stop on {esc(t['symbol'])} moved {what}. Locking in gains.", agent="positions", symbol=t["symbol"])

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
        self.alert(f"Took partial profit on {esc(t['symbol'])}: closed half at {_p(price)} for ${pnl:+,.2f}.", agent="positions", symbol=t["symbol"])

    def _earnings_exits(self) -> None:
        """In the last stretch of the session, close positions that would otherwise be held through earnings."""
        if self.at.weekday() >= 5 or not ((15, 15) <= (self.at.hour, self.at.minute) < (15, 50)):
            return
        held = [t for t in self.trades if t["status"] == "open" and t.get("asset") in ("stock", "etf", "option")]
        if not held or not self.stock_market_open():
            return
        cal = guards.earnings_calendar()
        for t in held:
            why = guards.must_exit_before_earnings(t, cal, self.at)
            if why:
                try:
                    self._exit_now(t, why)
                    self.alert(f"<b>Closing {esc(t['symbol'])} before earnings</b>\n<i>{esc(why)}</i>", agent="positions", symbol=t["symbol"])
                except Exception as exc:
                    ledger.note(t, f"earnings exit failed: {str(exc)[:120]}", self.iso)

    def _exit_now(self, t: dict, reason: str) -> None:
        """Close the whole position at market."""
        if t["venue"] == "oanda":
            res = oanda.close_trade(t["oanda_trade_id"])
            fill = res.get("orderFillTransaction", {})
            self._closed(t, float(fill.get("price") or current_price(t)), reason, pnl=float(fill.get("pl") or 0))
            return
        if t["asset"] == "option" and t.get("short_option_symbol"):
            legs = [{"symbol": t["option_symbol"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
                    {"symbol": t["short_option_symbol"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"}]
            try:
                o = alpaca.submit_raw({"order_class": "mleg", "qty": str(int(t["filled_qty"])), "type": "market",
                                       "time_in_force": "day", "legs": legs})
            except Exception:   # if market orders aren't accepted for spreads, sell at the current bid instead
                bid = (options.spread_mark(t["option_symbol"], t["short_option_symbol"]) or {}).get("bid") or 0.01
                o = alpaca.submit_raw({"order_class": "mleg", "qty": str(int(t["filled_qty"])), "type": "limit",
                                       "limit_price": f"{max(0.01, bid):.2f}", "time_in_force": "day", "legs": legs})
            t.update(status="closing", exit_order_id=o.get("id"), exit_reason=reason)
            ledger.note(t, f"closing the spread: {reason}", self.iso)
            floor.event("options", f"Closing the {t['symbol']} spread: {reason}", t["symbol"])
            return
        if t["asset"] == "option":
            o = alpaca.submit_raw({"symbol": t["option_symbol"], "side": "sell", "type": "market",
                                   "qty": str(int(t["filled_qty"])), "time_in_force": "day"})
            t.update(status="closing", exit_order_id=o.get("id"), exit_reason=reason)
            ledger.note(t, f"selling to close: {reason}", self.iso)
            floor.event("options", f"Selling {t['symbol']} options to close: {reason}", t["symbol"])
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
        floor.event("positions", f"Closing {t['symbol']}: {reason}", t["symbol"])

    def _check_closing(self, t: dict) -> None:
        if not t.get("exit_order_id"):
            self._closed(t, current_price(t), t.get("exit_reason", "closed"))
            return
        o = alpaca.get_order(t["exit_order_id"])
        if o["status"] == "filled":
            price = options.mleg_net_price(o) if t.get("short_option_symbol") else float(o["filled_avg_price"])
            self._closed(t, price or 0.0, t.get("exit_reason", "closed"))

    def _closed(self, t: dict, price: float, reason: str, pnl: float | None = None, includes_partials: bool = False) -> None:
        long = t["direction"] == "long"
        qty = t["filled_qty"]
        if pnl is None:
            if t["asset"] == "option":
                # options are always bought (debit) or sold (credit) as a package, whatever the view on the stock:
                # a debit position makes money when its price rises, a credit position when it falls
                pnl = (t["entry_price"] - price if t.get("credit") else price - t["entry_price"]) * qty * 100
            else:
                pnl = (price - t["entry_price"]) * qty * (1 if long else -1) * t.get("quote_to_usd", 1.0)
        if not includes_partials:
            pnl += t.get("partial_pnl", 0)
        if t["asset"] == "option":
            per = (t.get("spread_width", 0) - t["entry_price"]) if t.get("credit") else t["entry_price"]
            initial_risk = per * (t.get("filled_qty_initial") or t["qty"]) * 100
        else:
            initial_risk = abs(t["entry_price"] - t["initial_stop"]) * (t.get("filled_qty_initial") or t["qty"]) * t.get("quote_to_usd", 1.0)
        t.update(status="closed", exit_price=price, exit_reason=reason, closed_at=self.iso, pnl_usd=round(pnl, 2),
                 r_result=round(pnl / initial_risk, 2) if initial_risk else None)
        if t.get("loan_usd"):
            t["interest_usd"] = bank.interest(t["loan_usd"], t.get("opened_at"), self.at, t.get("loan_apr"))
            t["pnl_after_interest"] = round(pnl - t["interest_usd"], 2)
            floor.event("treasury", f"Repaid {bank.BANK_NAME} ${t['loan_usd']:,.0f} + ${t['interest_usd']:,.2f} interest for {t['symbol']}",
                        t["symbol"], kind="trade")
        ledger.note(t, f"closed at {price}: {reason}, P&L ${pnl:+,.2f}", self.iso)
        log_event("closed", trade=t["id"], pnl=round(pnl, 2), reason=reason)
        verdict = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
        self.alert(f"<b>CLOSED {esc(t['symbol'])}: {verdict} ${pnl:+,.2f}</b> ({t['r_result']}R)\n"
                   f"{'Long' if long else 'Short'} {_p(t['entry_price'])} -> {_p(price)} | {esc(reason)}",
                   agent="options" if t["asset"] == "option" else "positions", symbol=t["symbol"])


def _industry(symbol: str) -> str | None:
    """The company's industry (FinanceDatabase), for the same-industry limit."""
    try:
        from desk.research import extra
        return (extra.sector(symbol) or {}).get("industry")
    except Exception:
        return None


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
    if t["asset"] == "option":
        return f"{int(t['qty'])} contracts"
    return f"{int(t['qty'])} shares"
