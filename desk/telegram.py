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
    for chunk in _split(text):
        request("Telegram", "POST", _url("sendMessage"), json={
            "chat_id": config.telegram_chat(),
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        })


def _split(text: str) -> list[str]:
    """Split long messages between lines so HTML tags never get cut in half."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > MAX_LEN:  # a single giant line: hard cut
            chunks.append(line[:MAX_LEN])
            line = line[MAX_LEN:]
        if len(cur) + len(line) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def esc(text) -> str:
    return html.escape(str(text))


def get_commands(offset: int, wait: int = 0) -> tuple[list[str], int]:
    """New messages Erwann sent the bot. Ignores anyone else.
    `wait` > 0 holds the connection open up to that many seconds (long polling),
    so a command is picked up the moment it arrives."""
    d = request("Telegram", "GET", _url("getUpdates"), params={"offset": offset, "timeout": wait},
                timeout=wait + 15)
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
