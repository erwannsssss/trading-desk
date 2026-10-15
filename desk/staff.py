"""The firm's people: seats, who sits in them, who reports to whom, plus hiring, firing and alumni.

A seat is a job (e.g. "technical" = technical analyst). The person in a seat can change: if Ashley from HR
fires someone, a new hire takes the seat with a different working approach, which goes into the AI prompts.
"""
from __future__ import annotations

import json

from desk import config
from desk.state import now

FILE = config.DATA_DIR / "staff.json"

# seat: (default name, role, desk, reports_to, fireable)
SEATS = {
    "partner":     ("Erwann Sadki", "Managing partner", "partner", None, False),
    "cio":         ("Victor Ashford", "Chief investment officer", "leadership", "partner", False),
    "hr":          ("Ashley Brennan", "Head of HR", "leadership", "partner", False),
    "client":      ("Riley Park", "Client desk (your Telegram)", "leadership", "partner", False),
    "pm":          ("Elena Castillo", "Portfolio manager, head of research", "research", "cio", True),
    "macro":       ("Darius Webb", "Chief macro strategist", "research", "pm", True),
    "scanner":     ("Maya Chen", "Market scanner", "research", "pm", False),
    "associate":   ("Ben Carter", "Research associate", "research", "pm", False),
    "technical":   ("Priya Natarajan", "Technical analyst", "research", "pm", True),
    "fundamental": ("Marcus Hale", "Fundamental analyst", "research", "pm", True),
    "sentiment":   ("Sofia Reyes", "News and sentiment analyst", "research", "pm", True),
    "smartmoney":  ("Theo Okafor", "Smart money tracker", "research", "pm", True),
    "bull":        ("Jake Morrison", "The bull", "research", "pm", True),
    "bear":        ("Nadia Volkov", "The bear", "research", "pm", True),
    "factcheck":   ("Sam Brooks", "Fact-checker", "research", "pm", False),
    "sizer":       ("Iris Lindqvist", "Position sizer", "research", "pm", False),
    "execution":   ("Kenji Watanabe", "Execution trader, head of trading", "trading", "cio", False),
    "positions":   ("Leo Moretti", "Position manager", "trading", "execution", False),
    "options":     ("Ava Sinclair", "Options trader", "trading", "execution", False),
    "risk":        ("Grace Adeyemi", "Risk manager", "trading", "cio", False),
    "treasury":    ("Hugo Brandt", "Treasurer (manages the bank credit line)", "trading", "cio", False),
    "performance": ("Omar Haddad", "Performance analyst, head of learning", "learning", "cio", False),
    "reviewer":    ("Ruth Klein", "Trade reviewer", "learning", "performance", False),
    "quant":       ("Nina Petrova", "Quant researcher", "learning", "performance", False),
    "compliance":  ("Diane Foster", "Chief compliance officer", "leadership", "partner", False),
}
# Seats that join when the firm unlocks a new desk (see firm.UNLOCKS)
UNLOCK_SEATS = {
    "crypto":      ("Lena Hoffmann", "Crypto strategist", "research", "pm", True),
    "events":      ("Marisol Vega", "Event-driven analyst (earnings and news)", "research", "pm", True),
    "fx":          ("Callum Reid", "FX strategist", "research", "pm", True),
    "quantdesk":   ("Arjun Mehta", "Quant strategist", "learning", "performance", False),
}
ALL_SEATS = {**SEATS, **UNLOCK_SEATS}
SPECIALISTS = ("technical", "fundamental", "sentiment", "smartmoney")
DESK_TEAM = SPECIALISTS + ("bull", "bear", "pm")

# names for future hires (picked in order, skipping any already used)
HIRE_POOL = ["Carmen Diaz", "Owen Fitzgerald", "Hana Sato", "Rafael Mendes", "Imani Brooks", "Lukas Becker",
             "Amira Haddad", "Connor Walsh", "Mei Lin", "Andre Laurent", "Freya Nilsson", "Tariq Aziz",
             "Bianca Rossi", "Ethan Cole", "Yara Saleh", "Felix Wagner", "Noor Khan", "Diego Herrera"]


def load() -> dict:
    data = json.loads(FILE.read_text()) if FILE.exists() else {}
    data.setdefault("seats", {})
    data.setdefault("alumni", [])
    for seat, (name, role, desk, boss, fireable) in SEATS.items():
        data["seats"].setdefault(seat, {"name": name, "since": None, "approach": None})
    return data


def save(data: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=1) + "\n")


def name(seat: str) -> str:
    if seat.startswith("intern:"):
        from desk import interns
        i = interns.find(seat.split(":", 1)[1])
        return i["name"] if i else seat
    return load()["seats"].get(seat, {}).get("name") or ALL_SEATS.get(seat, (seat,))[0]


def approach(seat: str) -> str | None:
    return load()["seats"].get(seat, {}).get("approach")


def approaches() -> dict:
    """Working styles of anyone hired with a specific approach (fed into the AI prompts)."""
    return {s: v["approach"] for s, v in load()["seats"].items() if v.get("approach")}


def fire_and_replace(seat: str, reason: str, new_approach: str) -> tuple[str, str]:
    data = load()
    old = data["seats"][seat]
    used = {v["name"] for v in data["seats"].values()} | {a["name"] for a in data["alumni"]}
    new_name = next((n for n in HIRE_POOL if n not in used), f"New hire {len(data['alumni']) + 1}")
    today = now().date().isoformat()
    data["alumni"].append({"seat": seat, "role": ALL_SEATS[seat][1], "name": old["name"], "from": old.get("since"),
                           "to": today, "reason": reason})
    data["seats"][seat] = {"name": new_name, "since": today, "approach": new_approach}
    save(data)
    try:
        from desk import firm
        firm.set_level(seat, "Associate", f"new hire replacing {old['name']}")
    except Exception as exc:
        print(f"level reset failed: {exc}")
    return old["name"], new_name


def org_chart() -> list[dict]:
    """Everyone in reporting order, for the dashboard."""
    from desk import firm
    data = load()
    fdata = firm.load()
    out = []
    for seat, (default, role, desk, boss, fireable) in ALL_SEATS.items():
        s = data["seats"].get(seat) or {"name": default, "since": None, "approach": None}
        row = {"id": seat, "name": s["name"], "role": role, "desk": desk, "reports_to": boss,
               "fireable": fireable, "since": s.get("since"), "approach": s.get("approach")}
        if seat in UNLOCK_SEATS:
            u = firm.unlock_for_seat(seat)
            row["locked"] = not firm.unlocked(u[0], fdata)
            row["unlock_at"] = u[1]
            row["unlock_title"] = u[2]
        row["level"] = None if row.get("locked") else firm.level(seat, fdata)
        out.append(row)
    return out
