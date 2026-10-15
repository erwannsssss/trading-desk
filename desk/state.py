"""The bot's memory between runs, saved as data/state.json in the repo."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from desk import config

STATE_FILE = config.DATA_DIR / "state.json"
EVENTS_FILE = config.DATA_DIR / "events.jsonl"

DEFAULTS = {
    "created_at": None,
    "paused": False,            # kill switch
    "dry_run": config.DEFAULT_DRY_RUN,
    "baseline": {},             # starting equity of each account when the bot was born
    "day": None,                # trading day (New York date) the counters below belong to
    "day_start_equity": None,
    "trades_today": 0,
    "halted_for_day": False,    # daily loss limit hit
    "telegram_offset": 0,
    "last_tick": None,
}


def now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def load() -> dict:
    data = dict(DEFAULTS)
    if STATE_FILE.exists():
        data.update(json.loads(STATE_FILE.read_text()))
    return data


def save(state: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def log_event(kind: str, **details) -> None:
    """Append-only activity log (also feeds the dashboard later)."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    row = {"time": now().isoformat(timespec="seconds"), "kind": kind, **details}
    with EVENTS_FILE.open("a") as f:
        f.write(json.dumps(row) + "\n")
