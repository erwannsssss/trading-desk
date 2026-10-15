"""Telegram: alerts out, commands in."""
from __future__ import annotations

import html

from desk import config
from desk.http import request

MAX_LEN = 4000  # Telegram limit is 4096 characters


def _url(method: str) -> str:
    return f"https://api.telegram.org/bot{config.telegram_token()}/{method}"


def send(text: str, *, silent: bool = False) -> None:
    """Send a message to Erwann. Supports Telegram HTML (<b>, <i>, <code>)."""
    for start in range(0, len(text), MAX_LEN):
        request("Telegram", "POST", _url("sendMessage"), json={
            "chat_id": config.telegram_chat(),
            "text": text[start:start + MAX_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        })


def esc(text) -> str:
    return html.escape(str(text))


def get_commands(offset: int) -> tuple[list[str], int]:
    """New messages Erwann sent the bot since the last run. Ignores anyone else."""
    d = request("Telegram", "GET", _url("getUpdates"), params={"offset": offset, "timeout": 0})
    texts, new_offset = [], offset
    for upd in d.get("result", []):
        new_offset = max(new_offset, upd["update_id"] + 1)
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == str(config.telegram_chat()) and msg.get("text"):
            texts.append(msg["text"].strip())
    return texts, new_offset


def set_command_menu() -> None:
    request("Telegram", "POST", _url("setMyCommands"), json={"commands": [
        {"command": "status", "description": "Account value, P&L and risk status"},
        {"command": "positions", "description": "Everything the bot is holding"},
        {"command": "pause", "description": "Kill switch: stop opening new trades"},
        {"command": "resume", "description": "Turn trading back on"},
        {"command": "golive", "description": "Leave dry-run and place paper trades"},
        {"command": "dryrun", "description": "Go back to analyze-only mode"},
        {"command": "flatten", "description": "Emergency: close every position"},
        {"command": "help", "description": "List commands"},
    ]})
