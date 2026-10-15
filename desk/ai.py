"""The AI brain: Google Gemini (free tier), with automatic fallback between models."""
from __future__ import annotations

import json
import re
import time

from desk import config
from desk.http import ApiError, request

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MIN_GAP_SECONDS = 6.0   # stay under the free tier's requests-per-minute limit
_last_call = 0.0
calls_made = 0          # successful answers
attempts = 0            # every request sent, including ones that failed over to another model
_strikes: dict[str, int] = {}   # consecutive failures per model in this run
MAX_STRIKES = 3


def _pace() -> None:
    global _last_call, calls_made
    wait = MIN_GAP_SECONDS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def ask(prompt: str, *, system: str | None = None, want_json: bool = False,
        temperature: float = 0.4, models: list[str] | None = None, check=None) -> str | dict:
    """Ask the AI a question. Tries each free model in turn if one is busy, out of quota, or (with `check`)
    answers in the wrong shape."""
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if want_json:
        body["generationConfig"]["responseMimeType"] = "application/json"

    global calls_made, attempts
    errors = []
    pool = models or config.AI_MODELS
    healthy = [m for m in pool if _strikes.get(m, 0) < MAX_STRIKES] or pool  # skip models that keep failing this run
    for model in healthy:
        payload = body
        if model.startswith("gemma"):
            # Gemma doesn't take system instructions or JSON mode; fold them into the prompt.
            text = (system + "\n\n" if system else "") + prompt + ("\n\nRespond with valid JSON only." if want_json else "")
            payload = {"contents": [{"role": "user", "parts": [{"text": text}]}],
                       "generationConfig": {"temperature": temperature}}
        try:
            _pace()
            attempts += 1
            d = request("Gemini", "POST", f"{BASE}/{model}:generateContent",
                        headers={"x-goog-api-key": config.gemini_key(), "Content-Type": "application/json"},
                        json=payload, retries=2, timeout=120)
            parts = d["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if not text:
                raise ValueError("empty answer")
            result = _parse_json(text) if want_json else text
            if check is not None and not check(result):
                raise ValueError("answer was missing required fields")
            _strikes[model] = 0
            calls_made += 1
            return result
        except (ApiError, KeyError, IndexError, ValueError) as exc:
            errors.append(f"{model}: {exc}")
            print(f"AI model {model} failed: {str(exc)[:160]}")
            _strikes[model] = _strikes.get(model, 0) + 1
            time.sleep(1)
    raise RuntimeError("All AI models failed:\n" + "\n".join(errors))


def _parse_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)  # strip any ```json fences or chatter
        if not m:
            raise ValueError(f"AI did not return JSON: {text[:200]}")
        return json.loads(m.group(0))
