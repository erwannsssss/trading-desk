"""Telegram commands Erwann can send the bot."""
from __future__ import annotations

from desk import config, telegram
from desk.brokers import alpaca, oanda
from desk.portfolio import snapshot
from desk.risk import daily_loss_pct
from desk.state import log_event
from desk.telegram import esc

HELP = (
    "<b>Commands</b>\n"
    "/status - account value, P&amp;L, risk status\n"
    "/positions - everything it's holding\n"
    "/pause - kill switch: no new trades\n"
    "/resume - turn trading back on\n"
    "/golive - leave dry-run and place paper trades\n"
    "/dryrun - back to analyze-only mode\n"
    "/flatten confirm - emergency: close everything\n"
    "/letter - the latest weekly fund letter\n"
    "/rules - the desk's rulebook\n"
    "/pitch NVDA - the full team researches your idea\n"
    "/interns - how the interns are doing\n"
    "/ask Nadia why ONDS? - ask anyone at the firm directly\n"
    "\nOr just ask a question, like \"why did you short IONQ?\"\n"
    "\nReplies usually take a few seconds (occasionally a few minutes between runs)."
)


def status_text(state: dict) -> str:
    book, d = snapshot(state)
    eq = book.equity
    total = (eq / config.STARTING_CAPITAL - 1) * 100
    day = -daily_loss_pct(book, state.get("day_start_equity") or eq) or 0.0
    mode = "PAUSED" if state.get("paused") else ("DRY RUN (analyze only)" if state.get("dry_run") else "LIVE paper trading")
    if state.get("halted_for_day"):
        mode += " | daily loss limit hit"
    return (
        f"<b>Trading Desk status</b>\n"
        f"Mode: {mode}\n"
        f"Value: <b>${eq:,.2f}</b> ({total:+.2f}% since start)\n"
        f"Today: {day:+.2f}%\n"
        f"Open positions: {book.open_positions}\n"
        f"Trades today: {state.get('trades_today', 0)}/{config.MAX_TRADES_PER_DAY}\n"
        f"Stock/crypto exposure: ${book.stock_gross_exposure:,.0f}\n"
        f"Forex exposure: ${book.forex_gross_notional:,.0f}\n"
        f"Borrowed from the bank: ${book.borrowed:,.0f} of $100,000 (interest so far ${book.financing_cost:,.2f})\n"
        f"At risk if every stop hit: ${book.open_risk:,.0f} (limit {config.MAX_TOTAL_OPEN_RISK_PCT:.0f}% of equity)"
    )


def positions_text(state: dict) -> str:
    _, d = snapshot(state)
    lines = ["<b>Positions</b>"]
    for p in d["alpaca_positions"]:
        pl = float(p["unrealized_pl"])
        lines.append(f"{esc(p['symbol'])} {esc(p['side'])} {float(p['qty']):g} @ ${float(p['avg_entry_price']):,.2f} | P&amp;L ${pl:+,.2f}")
    for t in d["oanda_trades"]:
        units = float(t["currentUnits"])
        lines.append(f"{esc(t['instrument'])} {'long' if units > 0 else 'short'} {abs(units):,.0f} @ {t['price']} | P&amp;L ${float(t['unrealizedPL']):+,.2f}")
    if len(lines) == 1:
        lines.append("No open positions.")
    return "\n".join(lines)


def handle(text: str, state: dict) -> str | None:
    cmd = text.split()[0].lower().split("@")[0]
    args = text.split()[1:]

    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return status_text(state)
    if cmd == "/positions":
        return positions_text(state)
    if cmd == "/pause":
        state["paused"] = True
        log_event("kill_switch", on=True)
        return "Paused. No new trades will be opened. Existing stops and targets stay in place. Send /resume to restart."
    if cmd == "/resume":
        state["paused"] = False
        log_event("kill_switch", on=False)
        return "Resumed. The bot can open new trades again."
    if cmd == "/golive":
        state["dry_run"] = False
        log_event("mode", dry_run=False)
        return "Live paper trading is ON. The bot will now place real orders in your paper accounts (fake money)."
    if cmd == "/dryrun":
        state["dry_run"] = True
        log_event("mode", dry_run=True)
        return "Dry-run mode ON. The bot will analyze and log trades but not place orders."
    if cmd == "/ask":
        from desk import ask
        if not args:
            return "Ask anyone at the firm: /ask Nadia why are you bearish on ONDS? or /ask Kenji how did the FMC fill go?"
        return ask.answer(" ".join(args))
    if cmd == "/pitch":
        if not args:
            return "Send a ticker after it, like /pitch NVDA, /pitch BTC or /pitch EURUSD."
        telegram.send(f"On it. The research desk is huddling on {esc(args[0].upper())}; give them a few minutes.")
        from desk import pitch
        try:
            return pitch.run(args[0])
        except Exception as exc:
            return f"Couldn't research {esc(args[0])}: {esc(str(exc)[:200])}"
    if cmd == "/interns":
        from desk import interns
        rows = [i for i in interns.all_interns() if i["status"] in ("intern", "junior")]
        if not rows:
            return "No interns yet. The first class starts with the next research session."
        return "<b>Intern program</b> (hire at $500, let go at $30)\n" + "\n".join(
            f"{esc(i['name'])} ({'junior analyst' if i['status'] == 'junior' else 'intern'}): ${i['equity']:,.2f}, "
            f"{len(i['positions'])} open. {esc(i.get('mood') or '')}" for i in rows)
    if cmd == "/letter":
        from desk import learning
        letters = learning._load(learning.LETTERS, [])
        if not letters:
            return "No weekly letter yet. The first one comes Sunday evening."
        l = letters[-1]
        return f"<b>{esc(l['title'])}</b>\n\n{esc(l['body'])}"
    if cmd == "/rules":
        from desk import learning
        rules = learning.rulebook()
        return "<b>Rulebook</b>\n" + "\n".join(f"- {esc(r['rule'])}" for r in rules) if rules else "No rules yet. The weekly review writes them from real results."
    if cmd == "/flatten":
        if args[:1] != ["confirm"]:
            return "This closes EVERY position and cancels every order. To do it, send: /flatten confirm"
        alpaca.close_all_positions()
        oanda.close_all()
        state["paused"] = True
        log_event("flatten")
        return "Closed all positions, cancelled all orders, and paused trading. Send /resume when you're ready."
    if text.startswith("/"):
        return f"Unknown command {esc(cmd)}.\n\n{HELP}"
    from desk import chat
    return telegram.esc(chat.answer(text, state))


def telegram_plain(text) -> str:
    import html, re
    return html.unescape(re.sub(r"<[^>]+>", "", str(text or ""))).replace("\n", " ")


def process(state: dict, wait: int = 0) -> int:
    texts, state["telegram_offset"] = telegram.get_commands(state.get("telegram_offset", 0), wait)
    for text in texts:
        try:
            reply = handle(text, state)
        except Exception as exc:
            reply = f"Something went wrong running that: {esc(exc)}"
        if reply:
            telegram.send(reply)
        try:
            from desk import activity as floor
            floor.event("client", f"You sent \"{text[:120]}\". Replied: {telegram_plain(reply)[:220]}", kind="chat")
            floor.working("client", "On the phone: waiting for your Telegram messages")
        except Exception:
            pass
    return len(texts)
