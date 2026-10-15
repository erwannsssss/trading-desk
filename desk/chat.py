"""Answers free-text Telegram messages ("why did you short Tesla?", "what's the plan for tomorrow?")."""
from __future__ import annotations

import json

from desk import ai, config
from desk.portfolio import snapshot
from desk.research.planner import PLANS_DIR
from desk.trading import ledger


def context(state: dict) -> dict:
    book, _ = snapshot(state)
    trades = ledger.load()
    recent = sorted(trades, key=lambda t: t.get("created_at", ""), reverse=True)[:25]
    plans = sorted(PLANS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)[-3:] if PLANS_DIR.exists() else []
    plan_bits = []
    for p in plans:
        plan = json.loads(p.read_text())
        plan_bits.append({"file": p.name, "session": plan.get("session"), "created_at": plan.get("created_at"),
                          "macro": (plan.get("macro") or {}).get("summary"), "regime": (plan.get("macro") or {}).get("regime"),
                          "ideas": [{"symbol": i.get("symbol"), "status": i.get("status"),
                                     "decision": (i.get("decision") or {}).get("decision"),
                                     "thesis": (i.get("decision") or {}).get("thesis"),
                                     "reason": (i.get("decision") or {}).get("reason"),
                                     "bear": (i.get("bear") or {}).get("case")} for i in plan.get("ideas", [])]})
    from desk import learning
    return {
        "mode": "paused" if state.get("paused") else ("dry run" if state.get("dry_run") else "live paper trading"),
        "equity": round(book.equity, 2), "start": config.STARTING_CAPITAL, "open_positions": book.open_positions,
        "trades_today": state.get("trades_today"),
        "recent_trades": [{k: t.get(k) for k in ("symbol", "asset", "direction", "status", "style", "conviction", "entry_price",
                                                 "stop", "target", "exit_price", "pnl_usd", "exit_reason", "skip_reason",
                                                 "thesis", "postmortem", "option_symbol", "created_at")} for t in recent],
        "recent_plans": plan_bits, "rulebook": [r["rule"] for r in learning.rulebook()],
        "tournament": learning.tournament_summary(),
    }


def answer(question: str, state: dict) -> str:
    ctx = json.dumps(context(state), default=str)[:30000]
    prompt = f"""You are the AI trading desk Erwann built (paper money). He asked you on Telegram:

"{question}"

Answer from the desk's real records below. Be direct and conversational, explain finance in plain English
(he's a first-year finance student), keep it under 170 words, and say so if the records don't cover it.
Don't invent trades or numbers. Plain text only, no markdown.

DESK RECORDS:
{ctx}"""
    return ai.ask(prompt, temperature=0.4)
