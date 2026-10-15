"""Pair trades: within one industry, bet on the strongest stock against the weakest.

Long the leader (shares) and bearish on the laggard (a put spread, since this account can't short shares). If the
whole market falls, the two legs roughly offset; the trade makes money when the gap between them keeps widening.
Both legs are opened together, and when one leg closes the other closes too.
"""
from __future__ import annotations

import json

from desk import ai
from desk.research import dossier as dossiers
from desk.research import extra, team

MIN_GAP_PTS = 15.0      # the leader must have beaten the laggard by 15+ percentage points over 20 days
MIN_PEERS = 3           # the industry needs at least 3 scanned stocks to pick a real leader and laggard


def candidates(analyzed: list[dict], skip: set[str], top: int = 2) -> list[tuple[dict, dict, str, float]]:
    """(leader, laggard, industry, gap) for the industries with the widest split."""
    groups: dict[str, list[dict]] = {}
    for a in analyzed:
        if a["symbol"] in skip:
            continue
        ind = (extra.sector(a["symbol"]) or {}).get("industry")
        r = (a.get("technicals") or {}).get("ret_20d")
        if ind and isinstance(r, (int, float)):
            groups.setdefault(ind, []).append(a)
    out = []
    for ind, members in groups.items():
        if len(members) < MIN_PEERS:
            continue
        members.sort(key=lambda a: a["technicals"]["ret_20d"])
        lag, lead = members[0], members[-1]
        gap = lead["technicals"]["ret_20d"] - lag["technicals"]["ret_20d"]
        if gap >= MIN_GAP_PTS:
            out.append((lead, lag, ind, round(gap, 1)))
    return sorted(out, key=lambda x: -x[3])[:top]


def research(lead: dict, lag: dict, industry: str, gap: float, macro: dict, extra_ctx: dict) -> dict:
    """One AI call: the portfolio manager decides the pair and sets levels for both legs."""
    dl = dossiers.build(lead, {}, light=True)
    dg = dossiers.build(lag, {}, light=True)
    leg = ('{"entry": number, "stop": number, "target": number, "conviction": int, "confidence_pct": int, '
           '"horizon": "1-5 days"|"1-4 weeks", "thesis": str, "evidence": [{"fact": str, "value": number}]}')
    prompt = f"""PAIR TRADE in {industry}. Over 20 days {lead['symbol']} beat {lag['symbol']} by {gap} percentage points.
The idea: buy {lead['symbol']} and bet against {lag['symbol']} (with puts) so a market-wide move roughly cancels out,
and the trade pays if the leader keeps outperforming. Decide whether the gap is likely to keep widening (momentum)
or is stretched and due to snap back (then "pass").
MARKET: {macro.get('regime')} | {macro.get('summary')}

LEADER ({lead['symbol']}) DOSSIER: {json.dumps(dl, default=str)[:7000]}

LAGGARD ({lag['symbol']}) DOSSIER: {json.dumps(dg, default=str)[:7000]}

{team.DATA_GUIDE}
For each leg give a stop where that leg's thesis is wrong (1-2.5 ATR away), a target at least {1.2}x the risk, and
2-5 evidence numbers from THAT leg's dossier as "section.field" paths (e.g. "technicals.ret_20d").

Return JSON: {{"decision": "pair"|"pass", "reason": str, "long": {leg}, "short": {leg}}}"""
    out = ai.ask(prompt, system=team.DESK, want_json=True, temperature=0.2,
                 check=lambda o: isinstance(o, dict) and o.get("decision") in ("pair", "pass"))
    return {"decision": out, "lead_dossier": dl, "lag_dossier": dg}


def ideas_from(res: dict, lead: dict, lag: dict, industry: str, gap: float, date: str) -> list[dict]:
    """Two linked ideas (approved only if both legs pass the fact-checker)."""
    d = res["decision"]
    if d.get("decision") != "pair":
        return [{"symbol": f"{lead['symbol']}/{lag['symbol']} pair", "asset": "stock", "venue": "alpaca", "status": "pass",
                 "price_at_research": None, "decision": {"decision": "pass", "reason": d.get("reason"), "style": "pair trade"},
                 "pair_scan": {"industry": industry, "gap": gap}}]
    pair_id = f"pair-{date}-{lead['symbol']}-{lag['symbol']}"
    out = []
    for side, cand, dz, other in (("long", lead, res["lead_dossier"], lag), ("short", lag, res["lag_dossier"], lead)):
        leg = dict(d.get(side) or {})
        leg.update(decision=side, style="pair trade", bucket="tactical", order_type="limit",
                   expression="shares" if side == "long" else "put_options",
                   reason=d.get("reason"), premortem={"failure_more_likely": False})
        check = team.fact_check(leg, dossiers.facts(dz), dz)
        out.append({"symbol": cand["symbol"], "venue": "alpaca", "asset": "stock", "price_at_research": dz["technicals"]["price"],
                    "atr_pct": dz["technicals"].get("atr_pct"), "why_scanned": [f"pair trade in {industry}: {gap} pt 20-day gap"],
                    "setups": dz["technicals"].get("setups"), "decision": leg, "fact_check": check,
                    "pair_id": pair_id, "pair_with": other["symbol"], "status": "approved" if check["passed"] else "blocked_by_fact_check"})
    if not all(i["status"] == "approved" for i in out):   # both legs or neither
        for i in out:
            if i["status"] == "approved":
                i["status"] = "blocked_by_fact_check"
                i["fact_check"]["problems"] = (i["fact_check"].get("problems") or []) + ["the other leg of the pair failed its fact-check"]
    return out
