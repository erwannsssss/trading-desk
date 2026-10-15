"""The trade journal: every trade the desk has planned, placed, held or closed (data/trades.json)."""
from __future__ import annotations

import json

from desk import config

FILE = config.DATA_DIR / "trades.json"
ACTIVE = ("pending", "open", "closing")


def load() -> list[dict]:
    return json.loads(FILE.read_text()) if FILE.exists() else []


def save(trades: list[dict]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(trades, indent=1, default=str) + "\n")


def find(trades: list[dict], trade_id: str) -> dict | None:
    return next((t for t in trades if t["id"] == trade_id), None)


def active_symbols(trades: list[dict]) -> set[str]:
    return {t["symbol"] for t in trades if t["status"] in ACTIVE}


def stop_moved(trade: dict, stop: float, when: str) -> None:
    """Keep every stop level with its time, for the trade replay chart."""
    hist = trade.setdefault("stop_history", [])
    if not hist and trade.get("initial_stop") is not None:
        hist.append({"t": trade.get("opened_at") or when, "stop": trade["initial_stop"]})
    hist.append({"t": when, "stop": stop})


def note(trade: dict, text: str, when: str) -> None:
    trade.setdefault("notes", []).append(f"{when[:16]} {text}")
