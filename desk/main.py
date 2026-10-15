"""Entry point.

    python -m desk.main healthcheck   # test every connection and say hi on Telegram
    python -m desk.main tick          # recurring run: commands, risk checks, saving state
    python -m desk.main research morning|evening   # the research team builds the game plan
"""
from __future__ import annotations

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
    state = load()
    ensure_initialized(state)

    commands.process(state)

    book, _ = snapshot(state)
    roll_day(state, book.equity)

    if not state.get("halted_for_day") and daily_limit_hit(book, state["day_start_equity"]):
        state["halted_for_day"] = True
        loss = daily_loss_pct(book, state["day_start_equity"])
        log_event("daily_limit_hit", loss_pct=round(loss, 2), equity=round(book.equity, 2))
        telegram.send(f"<b>Daily loss limit hit</b> (-{loss:.1f}%). No new trades until tomorrow. "
                      f"Open positions keep their stops and targets.")

    # Phase 3 plugs the research team and trading engine in here.

    state["last_tick"] = now().isoformat(timespec="seconds")
    save(state)
    print(f"tick ok | equity ${book.equity:,.2f} | positions {book.open_positions}")

    listen(state, int(os.environ.get("LISTEN_SECONDS", "0")))
    return 0


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


def research() -> int:
    from desk.research import planner
    session = sys.argv[2] if len(sys.argv) > 2 else "morning"
    planner.run(session)
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    try:
        return {"healthcheck": healthcheck, "tick": tick, "research": research}[cmd]()
    except Exception as exc:
        traceback.print_exc()
        try:
            telegram.send(f"<b>Trading Desk error</b> during {cmd}:\n<code>{telegram.esc(str(exc)[:500])}</code>")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
