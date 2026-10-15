"""The intern program.

Three interns, each with a personality and $150 of pretend money (tracked here, separate from the real paper
accounts). After each morning and evening research session, every intern looks at the same finalists the desk
saw and makes up to two picks of their own. Positions are marked to live prices on every tick and close at the
intern's stop, target, or after a week.

  Turn $150 into $500  -> hired: a permanent seat on the research desk, and their best pick each session gets
                          debated by the full team.
  Drop to $30 or less  -> let go by HR, and a new intern starts with a fresh $150.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from desk import firm
from desk import ai, config, marketdata
from desk.brokers import oanda
from desk.state import now

FILE = config.DATA_DIR / "interns.json"
QUEUE = config.DATA_DIR / "intern_queue"   # research sessions drop picks here; the 15-minute check applies them
START = 150.0
HIRE_AT = 500.0
LET_GO_AT = 30.0
SLOTS = 3
MAX_LEVERAGE = 2.0
MAX_HOLD_DAYS = 7

PERSONAS = [
    ("Tyler Brooks", "Momentum chaser. Loves breakouts, meme stocks and anything trending on Reddit. Buys strength."),
    ("Zoe Kim", "Contrarian. Buys what everyone hates and shorts what everyone loves. Lives for oversold bounces."),
    ("Dev Patel", "Crypto degen. Believes crypto is the future, trades the most volatile coins, sizes big."),
    ("Lucia Romero", "Forex macro nerd. Trades currencies off interest rates and central banks; patient, tight stops."),
    ("Hannah Cole", "Earnings hunter. Trades stocks right around earnings and big news, in and out fast."),
    ("Marco Bianchi", "Value guy in a hurry. Buys cheap, profitable companies that just got beaten up."),
    ("Kai Nakamura", "Chart purist. Only trades clean technical setups: trend pullbacks and breakouts with volume."),
    ("Sienna Clarke", "Short seller. Hunts overhyped, overvalued names and bets against them."),
    ("Jordan Ellis", "Swing-for-the-fences gambler. All-in on one idea at a time with wide targets."),
]


def _load() -> dict:
    data = json.loads(FILE.read_text()) if FILE.exists() else {"interns": [], "next_id": 1, "graduates": []}
    return data


def _save(data: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=1, default=str) + "\n")


def find(intern_id: str) -> dict | None:
    return next((i for i in _load()["interns"] if i["id"] == intern_id), None)


def all_interns() -> list[dict]:
    return _load()["interns"]


def _hire(data: dict) -> dict:
    used = {i["name"] for i in data["interns"]}
    name, persona = random.choice([p for p in PERSONAS if p[0] not in used] or PERSONAS)
    intern = {"id": f"i{data['next_id']}", "name": name, "persona": persona, "status": "intern", "cash": START,
              "positions": [], "closed": [], "started": now().isoformat(timespec="seconds"), "equity": START, "peak": START}
    data["next_id"] += 1
    data["interns"].append(intern)
    _floor("hr", f"Hired a new intern: {name}. {persona} Starting with ${START:.0f}.")
    return intern


def ensure_staffed() -> None:
    data = _load()

    while sum(i["status"] == "intern" for i in data["interns"]) < firm.intern_slots():
        _hire(data)
    _save(data)


def _floor(seat: str, text: str, symbol: str | None = None, kind: str = "info") -> None:
    try:
        from desk import activity
        activity.event(seat, text, symbol, kind=kind)
    except Exception:
        pass


# ---- prices ------------------------------------------------------------------
def _prices(positions: list[dict]) -> dict[str, float]:
    out = {}
    stocks = [p["symbol"] for p in positions if p["asset"] in ("stock", "etf")]
    cryptos = [p["symbol"] for p in positions if p["asset"] == "crypto"]
    if stocks:
        for s, v in marketdata.stock_snapshots(stocks).items():
            if v.get("price"):
                out[s] = v["price"]
    if cryptos:
        out.update(marketdata.crypto_prices(cryptos))
    for p in positions:
        if p["asset"] == "forex" and p["symbol"] not in out:
            out[p["symbol"]] = oanda.price(p["symbol"])["mid"]
    return out


def _value(p: dict, price: float) -> float:
    """Profit/loss on an open position."""
    sign = 1 if p["direction"] == "long" else -1
    return (price - p["entry"]) * p["units"] * sign * p.get("quote_to_usd", 1.0)


# ---- picks -------------------------------------------------------------------
def make_picks(finalists: list[dict], macro: dict, session: str) -> list[dict]:
    """Every active intern (and hired junior) picks up to two trades from the session's finalists.
    Returns the hired juniors' best picks so the planner can add them to the full team's debate."""
    data = _load()
    menu = [{"symbol": c["symbol"], "asset": c["asset"], "price": c["technicals"]["price"],
             "atr_pct": c["technicals"].get("atr_pct"), "trend": c["technicals"].get("trend"),
             "rsi14": round(c["technicals"].get("rsi14") or 0, 1), "ret_5d": c["technicals"].get("ret_5d"),
             "setups": c["technicals"].get("setups"), "why": c.get("why_scanned")} for c in finalists]
    if not menu:
        return []
    junior_picks, queued = [], []
    for intern in [i for i in data["interns"] if i["status"] in ("intern", "junior")]:
        prices = _prices(intern["positions"]) if intern["positions"] else {}
        equity = intern["cash"] + sum(_value(p, prices.get(p["symbol"], p["entry"])) for p in intern["positions"])
        prompt = f"""You are {intern['name']}, a {'junior analyst (hired from the intern program)' if intern['status'] == 'junior' else 'trading intern'}
at a small trading desk. Your style: {intern['persona']}
You have ${equity:,.2f} of pretend money (started with ${START:.0f}); interns who reach ${HIRE_AT:.0f} get hired.
Market read from the desk: {macro.get('regime')}. {macro.get('summary', '')}
You already hold: {[p['symbol'] for p in intern['positions']] or 'nothing'}

Today's options (pick only from these):
{json.dumps(menu, default=str)}

Make 0-2 trades in YOUR style. For each: direction (long or short; crypto long only), stop and target prices
(stop on the losing side, target on the winning side, target at least 1.5x as far as the stop), and size_pct =
how much of your money to put in (10-200; above 100 means leverage). Write the reason in your own voice, one
sentence, with personality.

Return JSON: {{"picks": [{{"symbol": str, "direction": "long"|"short", "stop": number, "target": number,
"size_pct": number, "reason": str}}], "mood": str (a few words on how you're feeling)}}"""
        try:
            out = ai.ask(prompt, want_json=True, temperature=0.8)
        except Exception as exc:
            print(f"intern {intern['name']} failed: {exc}")
            continue
        picks = [p for p in (out.get("picks") or [])[:2] if isinstance(p, dict) and any(m["symbol"] == p.get("symbol") for m in menu)]
        for p in picks:
            item = next(m for m in menu if m["symbol"] == p["symbol"])
            p.update(asset=item["asset"], price_at_pick=item["price"])
            _floor(f"intern:{intern['id']}", f"Wants to {'buy' if p.get('direction') == 'long' else 'short'} {p['symbol']}: {(p.get('reason') or '')[:200]}",
                   p["symbol"], kind="plan")
        queued.append({"intern": intern["id"], "mood": (out.get("mood") or "")[:80], "picks": picks})
        if intern["status"] == "junior" and picks:
            junior_picks.append({"intern": intern["name"], "id": intern["id"], "symbol": picks[0]["symbol"],
                                 "direction": picks[0].get("direction"), "reason": picks[0].get("reason", "")})
        if not picks:
            _floor(f"intern:{intern['id']}", f"Passed this {session} session. Mood: {(out.get('mood') or 'thinking')[:80]}")
    if queued:
        QUEUE.mkdir(parents=True, exist_ok=True)
        (QUEUE / f"{now().strftime('%Y%m%d-%H%M%S')}-{session}.json").write_text(json.dumps(queued, default=str))
    return junior_picks


def _open(intern: dict, pick: dict, price_now: float, equity: float) -> dict | None:
    item = {"symbol": pick.get("symbol"), "asset": pick.get("asset"), "price": price_now}
    if not item["symbol"] or any(p["symbol"] == item["symbol"] for p in intern["positions"]):
        return None
    direction = pick.get("direction")
    if direction not in ("long", "short") or (item["asset"] == "crypto" and direction == "short"):
        return None
    try:
        price, stop, target = float(item["price"]), float(pick["stop"]), float(pick["target"])
        size_pct = max(10.0, min(float(pick.get("size_pct") or 50), MAX_LEVERAGE * 100))
    except (TypeError, ValueError, KeyError):
        return None
    long = direction == "long"
    if (long and not stop < price < target) or (not long and not target < price < stop):
        return None
    exposure = sum(p["entry"] * p["units"] * p.get("quote_to_usd", 1.0) for p in intern["positions"])
    budget = min(equity * size_pct / 100, equity * MAX_LEVERAGE - exposure)
    if budget <= 1:
        return None
    q2u = 1.0
    if item["asset"] == "forex":
        from desk.research.planner import quote_to_usd
        q2u = quote_to_usd(item["symbol"])
    units = budget / (price * q2u)
    pos = {"symbol": item["symbol"], "asset": item["asset"], "direction": direction, "entry": price, "units": units,
           "stop": stop, "target": target, "quote_to_usd": q2u, "opened": now().isoformat(timespec="seconds"),
           "reason": (pick.get("reason") or "")[:240], "size_usd": round(budget, 2)}
    intern["positions"].append(pos)
    _floor(f"intern:{intern['id']}", f"{'Bought' if long else 'Shorted'} {item['symbol']} with ${budget:,.0f}: {pos['reason']}", item["symbol"], kind="trade")
    return pos


# ---- marking to market, hiring and firing ------------------------------------
def _apply_queue(data: dict) -> None:
    """Open the positions interns picked in the latest research sessions, at today's live prices."""
    if not QUEUE.exists():
        return
    files = sorted(QUEUE.glob("*.json"))
    picks = [(entry, p) for f in files for entry in json.loads(f.read_text()) for p in entry.get("picks", [])]
    if picks:
        try:
            prices = _prices([{"symbol": p["symbol"], "asset": p.get("asset")} for _, p in picks])
        except Exception as exc:
            print(f"intern queue prices failed: {exc}")
            return
        for entry, p in picks:
            intern = next((i for i in data["interns"] if i["id"] == entry["intern"]), None)
            if not intern or intern["status"] not in ("intern", "junior") or p["symbol"] not in prices:
                continue
            intern["mood"] = entry.get("mood") or intern.get("mood")
            held = _prices(intern["positions"]) if intern["positions"] else {}
            equity = intern["cash"] + sum(_value(x, held.get(x["symbol"], x["entry"])) for x in intern["positions"])
            _open(intern, p, prices[p["symbol"]], equity)
    for f in files:
        f.unlink()


def update() -> list[str]:
    """Mark every intern to market, close finished trades, and hire or let go. Returns announcements."""
    data = _load()
    news = []
    while sum(i["status"] == "intern" for i in data["interns"]) < firm.intern_slots():
        new = _hire(data)
        news.append(f"New intern: {new['name']}. {new['persona']}")
    _apply_queue(data)
    all_pos = [p for i in data["interns"] for p in i["positions"]]
    try:
        prices = _prices(all_pos) if all_pos else {}
    except Exception as exc:
        print(f"intern prices failed: {exc}")
        return []
    for intern in data["interns"]:
        if intern["status"] not in ("intern", "junior"):
            continue
        keep = []
        for p in intern["positions"]:
            px = prices.get(p["symbol"])
            if px is None:
                keep.append(p)
                continue
            long = p["direction"] == "long"
            hit_stop = px <= p["stop"] if long else px >= p["stop"]
            hit_target = px >= p["target"] if long else px <= p["target"]
            too_old = now() - datetime.fromisoformat(p["opened"]) > timedelta(days=MAX_HOLD_DAYS)
            if hit_stop or hit_target or too_old:
                exit_px = p["stop"] if hit_stop else p["target"] if hit_target else px
                pnl = _value(p, exit_px)
                intern["cash"] += pnl
                intern["closed"].append({**p, "exit": exit_px, "pnl": round(pnl, 2), "closed": now().isoformat(timespec="seconds"),
                                         "why": "stop" if hit_stop else "target" if hit_target else "time"})
                intern["closed"] = intern["closed"][-40:]
                _floor(f"intern:{intern['id']}", f"Closed {p['symbol']} ({'stopped out' if hit_stop else 'hit target' if hit_target else 'time up'}): "
                       f"${pnl:+,.2f}", p["symbol"], kind="trade")
            else:
                keep.append(p)
        intern["positions"] = keep
        intern["equity"] = round(intern["cash"] + sum(_value(p, prices.get(p["symbol"], p["entry"])) for p in keep), 2)
        intern["peak"] = max(intern.get("peak", START), intern["equity"])
        if intern["status"] == "intern" and intern["equity"] >= HIRE_AT:
            intern["status"] = "junior"
            intern["hired"] = now().date().isoformat()
            data["graduates"].append({"name": intern["name"], "hired": intern["hired"], "equity": intern["equity"]})
            msg = f"{intern['name']} turned ${START:.0f} into ${intern['equity']:,.0f} and has been hired as a junior analyst!"
            news.append(msg)
            _floor("hr", msg, kind="hire")
        elif intern["status"] == "intern" and intern["equity"] <= LET_GO_AT:
            intern["status"] = "let_go"
            intern["left"] = now().date().isoformat()
            msg = f"{intern['name']} took ${START:.0f} down to ${intern['equity']:,.2f}. HR has let them go."
            news.append(msg)
            _floor("hr", msg, kind="fire")
    while sum(i["status"] == "intern" for i in data["interns"]) < firm.intern_slots():
        new = _hire(data)
        news.append(f"New intern: {new['name']}. {new['persona']}")
    data["interns"] = [i for i in data["interns"] if i["status"] in ("intern", "junior")] + \
                      [i for i in data["interns"] if i["status"] == "let_go"][-6:]
    _save(data)
    return news
