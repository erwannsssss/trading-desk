"""Entry point.

    python -m desk.main healthcheck   # test every connection and say hi on Telegram
    python -m desk.main tick          # recurring run: commands, risk checks, saving state
    python -m desk.main research morning|evening   # the research team builds the game plan
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

from desk import ai, commands, config, datasources, telegram
from desk.brokers import alpaca, oanda
from desk.portfolio import snapshot
from desk.risk import daily_limit_hit, daily_loss_pct
from desk.state import load, log_event, now, save


def ensure_initialized(state: dict) -> None:
    """First run ever: remember each account's starting value so P&L is measured from here."""
    if state.get("baseline"):
        return
    state["baseline"] = {"alpaca": alpaca.equity(), "oanda": oanda.nav()}
    state["created_at"] = now().isoformat(timespec="seconds")
    log_event("born", baseline=state["baseline"], budget=config.STARTING_CAPITAL)
    try:
        telegram.set_command_menu()
    except Exception:
        pass


def detect_deposit(state: dict, details: dict) -> bool:
    """If the Alpaca paper balance is raised (e.g. so the bank's loan money sits in the account), don't count
    it as profit: move the baseline. Only when nothing is open, so it can't be confused with a trade."""
    acct = details["alpaca_account"]
    eq = float(acct["equity"])
    quiet = not details["alpaca_positions"] and not details["alpaca_orders"]
    last, was_quiet = state.get("last_alpaca_equity"), state.get("last_alpaca_quiet")
    state["last_alpaca_equity"], state["last_alpaca_quiet"] = eq, quiet
    if last is None or not (quiet and was_quiet) or abs(eq - last) < 500:
        return False
    state["baseline"]["alpaca"] = float(state["baseline"].get("alpaca", last)) + (eq - last)
    log_event("deposit", amount=round(eq - last, 2))
    from desk import activity as floor
    floor.event("treasury", f"Noticed the Alpaca paper balance change by ${eq - last:+,.0f}; treated it as a deposit, not profit")
    telegram.send(f"Treasury: the Alpaca paper balance changed by ${eq - last:+,.0f} with nothing open, so I counted it as a "
                  f"deposit, not profit. The firm's own equity is unchanged.")
    return True


def roll_day(state: dict, equity: float) -> None:
    today = now().date().isoformat()
    if state.get("day") != today:
        state.update(day=today, day_start_equity=equity, trades_today=0, halted_for_day=False)
        log_event("new_day", equity=round(equity, 2))


def healthcheck() -> int:
    results, ok = [], True

    def check(name: str, fn):
        nonlocal ok
        try:
            results.append(f"OK  {name}: {fn()}")
        except Exception as exc:
            ok = False
            results.append(f"FAIL {name}: {str(exc)[:200]}")

    check("Alpaca", lambda: (lambda a: f"${float(a['equity']):,.2f}, shorting={a['shorting_enabled']}, "
                                       f"options level {a.get('options_trading_level')}, crypto {a.get('crypto_status')}")(alpaca.account()))
    check("OANDA", lambda: (lambda s: f"${float(s['NAV']):,.2f} {s['currency']}, max leverage {1 / float(s['marginRate']):.0f}:1")(oanda.summary()))
    check("Market clock", lambda: "open" if alpaca.clock()["is_open"] else "closed")
    check("EUR/USD price", lambda: f"{oanda.price('EUR_USD')['mid']:.5f}")
    check("Finnhub", lambda: f"SPY ${datasources.quote('SPY')['c']}")
    check("FRED", lambda: f"Fed funds {datasources.fred_latest('FEDFUNDS').get('value')}%")
    check("Gemini AI", lambda: ai.ask("Reply with exactly: ready", temperature=0)[:40])
    try:
        from desk.research import extra
        for name, line in extra.health().items():
            if not name.startswith("_"):
                results.append(("OK  " if line != "no data" else "FAIL ") + f"{name}: {line}")
    except Exception as exc:
        results.append(f"FAIL Extra data sources: {str(exc)[:150]}")
    check("Groq AI (backup)", lambda: ai.ask("Reply with exactly: ready", temperature=0, models=config.GROQ_MODELS)[:40]
          if config.groq_key() else "not set up (add the GROQ_API_KEY secret)")

    try:
        telegram.set_command_menu()
    except Exception:
        pass
    report = "\n".join(results)
    print(report)

    state = load()
    try:
        ensure_initialized(state)
        book, _ = snapshot(state)
        roll_day(state, book.equity)
        save(state)
    except Exception:
        traceback.print_exc()
        ok = False

    header = "Trading Desk is online" if ok else "Trading Desk health check found problems"
    telegram.send(f"<b>{header}</b>\n\n<code>{telegram.esc(report)}</code>\n\n"
                  f"Budget: ${config.STARTING_CAPITAL:,.0f} | Mode: {'dry run' if state.get('dry_run') else 'live paper'}\n"
                  f"Send /help to see what I can do.")
    return 0 if ok else 1


def tick() -> int:
    from desk import activity as floor
    state = load()
    ensure_initialized(state)
    floor.start_job("desk", "Trading desk check")

    commands.process(state)

    book, details = snapshot(state)
    if detect_deposit(state, details):
        book, details = snapshot(state)
    roll_day(state, book.equity)

    if not state.get("halted_for_day") and daily_limit_hit(book, state["day_start_equity"]):
        state["halted_for_day"] = True
        loss = daily_loss_pct(book, state["day_start_equity"])
        log_event("daily_limit_hit", loss_pct=round(loss, 2), equity=round(book.equity, 2))
        telegram.send(f"<b>Daily loss limit hit</b> (-{loss:.1f}%). No new trades until tomorrow. "
                      f"Open positions keep their stops and targets.")

    # The trading engine: manage open trades, then enter new ones from the game plan.
    try:
        from desk.trading.engine import Engine
        summary = Engine(state, book).run()
        print("engine:", summary)
    except Exception as exc:
        traceback.print_exc()
        telegram.send(f"<b>Trading engine error</b>: <code>{telegram.esc(str(exc)[:400])}</code>")

    # Learning: grade closed trades, refresh the strategy tournament daily, send the evening recap.
    try:
        from desk import learning
        from desk.trading import ledger
        if learning.run_postmortems(limit=2) or state.get("tournament_day") != now().date().isoformat():
            floor.working("performance", "Re-scoring the strategy tournament")
            t = learning.update_tournament(ledger.load())
            leader = (t.get("board") or [{}])[0]
            floor.done("performance", f"Tournament updated: {len(t.get('board', []))} styles scored"
                       + (f"; leader is {leader['style']} ({leader['pnl']:+,.2f})" if leader.get("style") else "; waiting for closed trades"))
            state["tournament_day"] = now().date().isoformat()
        learning.maybe_send_recap(state, book)
        from desk import interns
        for msg in interns.update():
            telegram.send(f"<b>Intern program:</b> {telegram.esc(msg)}")
        hour = now().strftime("%Y-%m-%d %H")
        if state.get("scorecard_hour") != hour:
            from desk import scorecard
            from desk.dashboard import _recent_plans
            plans45 = _recent_plans(45)
            scorecard.grade_ideas(plans45)
            from desk import shadow
            shadow.update(plans45)
            state["scorecard_hour"] = hour
        from desk import hr
        hr.maybe_season_recap(state)
    except Exception:
        traceback.print_exc()
    save(state)

    # The business side: payroll, compliance audit, unlockable desks, monthly bonuses.
    try:
        run_firm(state, book)
    except Exception:
        traceback.print_exc()
    save(state)

    # Breaking-news huddles on open positions, and re-research of today's big movers.
    try:
        from desk import alerts
        from desk.trading.engine import Engine
        from desk.trading import ledger as _ledger
        eng = Engine(state, book)
        notes = alerts.review_positions(state, eng)
        _ledger.save(eng.trades)
        for a in eng.alerts + notes:
            telegram.send(a)
        for note in alerts.research_movers(state):
            telegram.send(note)
    except Exception:
        traceback.print_exc()
    save(state)

    record_equity(state)
    try:
        from desk import dashboard
        dashboard.build(state)
    except Exception:
        traceback.print_exc()

    state["last_tick"] = now().isoformat(timespec="seconds")
    save(state)
    print(f"tick ok | equity ${book.equity:,.2f} | positions {book.open_positions}")
    floor.event("risk", f"Account ${book.equity:,.2f}; today {-daily_loss_pct(book, state['day_start_equity']):+.2f}%; "
                f"{book.open_positions} positions; {state.get('trades_today', 0)}/{config.MAX_TRADES_PER_DAY} trades used", kind="check")
    floor.finish()

    listen(state, int(os.environ.get("LISTEN_SECONDS", "0")))
    return 0


def run_firm(state: dict, book) -> None:
    from desk import activity as floor
    from desk import compliance, firm, scorecard, staff
    from desk.dashboard import _curve
    from desk.telegram import esc
    from desk.trading import ledger
    firm.accrue_payroll(book.equity)
    from desk import bank
    trades = ledger.load()
    fixes = bank.reprice_forex_loans(trades)
    if fixes:
        ledger.save(trades)
        for n in fixes:
            floor.event("treasury", f"Repriced a bank loan: {n}", kind="trade")

    findings = compliance.audit(ledger.load(), book)
    major = [f for f in findings if f["severity"] == "major"]
    if findings:
        floor.event("compliance", f"Audit done: {len(findings)} new finding{'s' if len(findings) > 1 else ''}", kind="check")
    for f in major:
        telegram.send(f"<b>Compliance alert from {esc(staff.name('compliance'))}</b>\n{esc(f['rule'])}"
                      + (f" on {esc(f['symbol'])}" if f.get("symbol") else "") + f"\nResponsible: {esc(f['who'])}\n<i>{esc(f['detail'])}</i>")

    for key, at, title, effect, seat in firm.check_unlocks(book.equity):
        floor.event("cio", f"Unlocked: {title}. The firm passed ${at:,.0f}. {effect}", kind="unlock")
        if seat:
            floor.event(seat, f"First day on the job as {staff.ALL_SEATS[seat][1]}", kind="hire")
        telegram.send(f"<b>New desk unlocked: {esc(title)}</b>\nThe firm passed ${at:,.0f}.\n{esc(effect)}")

    scores = scorecard.seat_scores(ledger.load(), _curve())
    paid = firm.maybe_pay_bonuses(state, book.equity, scores)
    if paid is not None:
        if paid["pool"] > 0:
            top = sorted(paid["by_seat"].values(), key=lambda v: -v["amount"])[:5]
            telegram.send(f"<b>Bonus day ({esc(paid['month'])})</b>\nThe firm made ${paid['profit_above_hwm']:,.2f} above its previous high, "
                          f"so the bonus pool is ${paid['pool']:,.2f} (20%).\nTop bonuses: "
                          + ", ".join(f"{esc(v['name'])} ${v['amount']:,.2f}" for v in top))
            floor.event("hr", f"Paid {paid['month']} bonuses: ${paid['pool']:,.2f} pool across {len(paid['by_seat'])} people", kind="bonus")
        else:
            telegram.send(f"<b>No bonuses for {esc(paid['month'])}</b>\nThe firm is below its previous high "
                          f"(${paid['hwm_before']:,.2f}), so the bonus pool is $0.")
            floor.event("hr", f"No {paid['month']} bonuses: the firm is below its high-water mark", kind="bonus")


def listen(state: dict, seconds: int) -> None:
    """Stay online and answer Telegram commands within seconds until the next run takes over."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 5:
        try:
            if commands.process(state, wait=int(min(50, left - 3))):
                save(state)
        except Exception as exc:
            print(f"listen error: {exc}")
            time.sleep(10)


def weekly() -> int:
    from desk import activity as floor
    from desk import dashboard, learning
    floor.start_job("learning", "Weekly review")
    try:
        learning.weekly_review()
    finally:
        floor.finish()
    dashboard.build(load())
    return 0


def reviews() -> int:
    from desk import activity as floor
    from desk import dashboard, hr
    floor.start_job("learning", "HR reviews")
    try:
        hr.weekly_reviews()
    finally:
        floor.finish()
    dashboard.build(load())
    return 0


def run_backtest() -> int:
    from desk import backtest
    out = backtest.run()
    print(json.dumps(out["results"], indent=1))
    return 0


def listen_only() -> int:
    """Answer Telegram commands for LISTEN_SECONDS, then exit (runs after the tick's work is saved)."""
    from desk import activity as floor
    floor.start_job("client", "Telegram client desk")
    floor.working("client", "On the phone: waiting for your Telegram messages")
    state = load()
    try:
        listen(state, int(os.environ.get("LISTEN_SECONDS", "780")))
    finally:
        save(state)
        floor.finish()
    return 0


def record_equity(state: dict) -> None:
    """One row per run for the equity curve: bot equity vs. the S&P 500 (SPY)."""
    import json
    try:
        book, _ = snapshot(state)
        spy = alpaca.last_price("SPY")
    except Exception:
        return
    path = config.DATA_DIR / "equity.jsonl"
    with path.open("a") as f:
        f.write(json.dumps({"t": now().isoformat(timespec="minutes"), "equity": round(book.equity, 2), "spy": spy,
                            "fin": round(book.financing_cost, 4), "opex": round(book.opex, 4)}) + "\n")


def research() -> int:
    from desk import activity as floor
    from desk.research import planner
    session = sys.argv[2] if len(sys.argv) > 2 else "morning"
    try:
        planner.run(session)
    finally:
        floor.finish()
    try:
        from desk import dashboard
        dashboard.build(load())
    except Exception:
        traceback.print_exc()
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    try:
        return {"healthcheck": healthcheck, "tick": tick, "research": research, "listen": listen_only,
                "weekly": weekly, "backtest": run_backtest, "reviews": reviews}[cmd]()
    except Exception as exc:
        traceback.print_exc()
        try:
            telegram.send(f"<b>Trading Desk error</b> during {cmd}:\n<code>{telegram.esc(str(exc)[:500])}</code>")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
