"""The trading floor: who's on the desk, what each person is doing right now, and what they did recently.

Every agent in the system is a named person. Code calls `working()` when someone starts a task and `done()`
when they finish. The state is published as a small JSON file to the repo's `live` branch about every
40 seconds (via the GitHub API), so the dashboard's Trading floor page stays close to live without waiting
for the slower GitHub Pages rebuild. A copy also goes to docs/live/ as a fallback.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timedelta

import requests

from desk import config
from desk.state import now

# id: (name, role, desk)
ROSTER = {
    "maya":   ("Maya Chen", "Market scanner", "research"),
    "darius": ("Darius Webb", "Chief macro strategist", "research"),
    "ben":    ("Ben Carter", "Research associate", "research"),
    "priya":  ("Priya Natarajan", "Technical analyst", "research"),
    "marcus": ("Marcus Hale", "Fundamental analyst", "research"),
    "sofia":  ("Sofia Reyes", "News and sentiment analyst", "research"),
    "theo":   ("Theo Okafor", "Smart money tracker", "research"),
    "jake":   ("Jake Morrison", "The bull", "research"),
    "nadia":  ("Nadia Volkov", "The bear", "research"),
    "elena":  ("Elena Castillo", "Portfolio manager", "research"),
    "sam":    ("Sam Brooks", "Fact-checker", "research"),
    "iris":   ("Iris Lindqvist", "Position sizer", "research"),
    "kenji":  ("Kenji Watanabe", "Execution trader", "trading"),
    "grace":  ("Grace Adeyemi", "Risk manager", "trading"),
    "leo":    ("Leo Moretti", "Position manager", "trading"),
    "ava":    ("Ava Sinclair", "Options trader", "trading"),
    "ruth":   ("Ruth Klein", "Trade reviewer", "learning"),
    "omar":   ("Omar Haddad", "Performance analyst", "learning"),
    "nina":   ("Nina Petrova", "Quant researcher", "learning"),
    "victor": ("Victor Ashford", "Chief investment officer", "learning"),
    "riley":  ("Riley Park", "Client desk (your Telegram)", "learning"),
}
SPECIALISTS = ("priya", "marcus", "sofia", "theo")
DESK_TEAM = SPECIALISTS + ("jake", "nadia", "elena")

LIVE_DIR = config.ROOT / "docs" / "live"
HISTORY_HOURS = 3
MAX_EVENTS = 600
PUBLISH_EVERY = 40  # seconds

_job: str | None = None
_state: dict = {}
_last_publish = 0.0
_shas: dict[str, str] = {}


def _iso() -> str:
    return now().isoformat(timespec="seconds")


def start_job(job: str, label: str) -> None:
    """Begin a job (research, engine, learning). Picks up recent history so the last-hour view survives restarts."""
    global _job, _state
    _job = job
    prior = _fetch_remote(job) or _read_local(job) or {}
    cutoff = (now() - timedelta(hours=HISTORY_HOURS)).isoformat()
    _state = {"job": job, "label": label, "running": True, "started": _iso(), "updated": _iso(),
              "agents": prior.get("agents", {}),
              "events": [e for e in prior.get("events", []) if e.get("t", "") >= cutoff]}
    for a in _state["agents"].values():
        if a.get("status") == "working":
            a["status"] = "idle"  # a previous run that died mid-task
    publish(force=True)


def working(agent: str, task: str, symbol: str | None = None) -> None:
    if not _state:
        return
    _state["agents"][agent] = {"status": "working", "task": task, "symbol": symbol, "since": _iso(),
                               "last": _state["agents"].get(agent, {}).get("last")}
    publish()


def done(agent: str, result: str, symbol: str | None = None, kind: str = "done") -> None:
    """Record what someone finished; they go back to idle with this as their latest result."""
    if not _state:
        return
    _state["agents"][agent] = {"status": "idle", "task": None, "symbol": None, "since": _iso(), "last": result}
    _event(agent, result, symbol, kind)
    publish()


def event(agent: str, text: str, symbol: str | None = None, kind: str = "info") -> None:
    """Something someone did without changing what they're working on."""
    if not _state:
        return
    a = _state["agents"].setdefault(agent, {"status": "idle", "since": _iso()})
    a["last"] = text
    _event(agent, text, symbol, kind)
    publish()


def _event(agent, text, symbol, kind) -> None:
    _state["events"].append({"t": _iso(), "agent": agent, "text": text[:400], "symbol": symbol, "kind": kind, "job": _job})
    _state["events"] = _state["events"][-MAX_EVENTS:]


def finish(summary: str | None = None) -> None:
    if not _state:
        return
    for a in _state["agents"].values():
        if a.get("status") == "working":
            a["status"] = "idle"
    _state["running"] = False
    _state["finished"] = _iso()
    if summary:
        _state["summary"] = summary
    publish(force=True)


# ---- publishing --------------------------------------------------------------
def publish(force: bool = False) -> None:
    global _last_publish
    if not _state or (not force and time.monotonic() - _last_publish < PUBLISH_EVERY):
        return
    _last_publish = time.monotonic()
    _state["updated"] = _iso()
    body = json.dumps(_state, separators=(",", ":"), default=str)
    try:
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        (LIVE_DIR / f"{_job}.json").write_text(body)
    except Exception:
        pass
    _push_remote(f"live/{_job}.json", body)


def _gh():
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None, None
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, repo


def _ensure_branch(headers, repo) -> None:
    r = requests.get(f"https://api.github.com/repos/{repo}/git/ref/heads/live", headers=headers, timeout=15)
    if r.status_code == 200:
        return
    main = requests.get(f"https://api.github.com/repos/{repo}/git/ref/heads/main", headers=headers, timeout=15).json()
    requests.post(f"https://api.github.com/repos/{repo}/git/refs", headers=headers, timeout=15,
                  json={"ref": "refs/heads/live", "sha": main["object"]["sha"]})


def _push_remote(path: str, body: str) -> None:
    headers, repo = _gh()
    if not headers:
        return
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    for attempt in range(3):
        try:
            sha = _shas.get(path)
            if sha is None:
                r = requests.get(url, headers=headers, params={"ref": "live"}, timeout=15)
                if r.status_code == 404 and "Branch" in r.text:
                    _ensure_branch(headers, repo)
                    continue
                sha = r.json().get("sha") if r.status_code == 200 else ""
            payload = {"message": f"live {path}", "branch": "live",
                       "content": base64.b64encode(body.encode()).decode()}
            if sha:
                payload["sha"] = sha
            r = requests.put(url, headers=headers, json=payload, timeout=20)
            if r.status_code in (200, 201):
                _shas[path] = r.json()["content"]["sha"]
                return
            if r.status_code in (404,) and "Branch" in r.text:
                _ensure_branch(headers, repo)
            _shas.pop(path, None)  # stale sha (409/422): refetch and retry
        except Exception as exc:
            print(f"live publish failed: {exc}")
            return


def _fetch_remote(job: str) -> dict | None:
    headers, repo = _gh()
    if not headers:
        return None
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/live/{job}.json",
                         headers={**headers, "Accept": "application/vnd.github.raw+json"}, params={"ref": "live"}, timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _read_local(job: str) -> dict | None:
    p = LIVE_DIR / f"{job}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def roster() -> list[dict]:
    return [{"id": k, "name": n, "role": r, "desk": d} for k, (n, r, d) in ROSTER.items()]
