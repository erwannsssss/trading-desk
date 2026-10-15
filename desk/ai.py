"""The AI brain: Google Gemini (free tier), with Groq (separate free tier) as the backup provider.
Models are tried in order; a model that keeps failing in a run is skipped for the rest of that run."""
from __future__ import annotations

import json
import re
import time

from desk import config
from desk.http import ApiError, request

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_GAP_SECONDS = 2.0
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
    pool = [m for m in pool if not m.startswith("groq:") or config.groq_key()]
    healthy = [m for m in pool if _strikes.get(m, 0) < MAX_STRIKES] or pool  # skip models that keep failing this run
    for model in healthy:
        if model.startswith("groq:"):
            try:
                attempts += 1
                text = _groq(model[5:], prompt, system, want_json, temperature)
                result = _parse_json(text) if want_json else text
                if check is not None and not check(result):
                    raise ValueError("answer was missing required fields")
                _strikes[model] = 0
                calls_made += 1
                return result
            except Exception as exc:
                errors.append(f"{model}: {exc}")
                print(f"AI model {model} failed: {str(exc)[:160]}")
                _strikes[model] = _strikes.get(model, 0) + 1
                continue
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


_groq_last = 0.0


def _groq(model: str, prompt: str, system: str | None, want_json: bool, temperature: float) -> str:
    """One call to Groq's OpenAI-style API. Waits out short per-minute limits instead of giving up."""
    import requests
    global _groq_last
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": messages, "temperature": temperature}
    if want_json:
        body["response_format"] = {"type": "json_object"}
        if "json" not in (prompt + (system or "")).lower():
            messages[-1]["content"] += "\n\nRespond with JSON."
    for attempt in range(3):
        wait = GROQ_GAP_SECONDS - (time.monotonic() - _groq_last)
        if wait > 0:
            time.sleep(wait)
        _groq_last = time.monotonic()
        r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {config.groq_key()}"}, json=body, timeout=90)
        if r.status_code == 429 and attempt < 2:
            retry = float(r.headers.get("retry-after") or 10)
            if retry > 40:          # daily cap or a long wait: let the next model take it
                raise ValueError(f"Groq rate limit (retry in {retry:.0f}s)")
            time.sleep(retry + 0.5)
            continue
        if r.status_code >= 400:
            raise ValueError(f"Groq error {r.status_code}: {r.text[:200]}")
        text = (r.json()["choices"][0]["message"].get("content") or "").strip()
        if not text:
            raise ValueError("empty answer")
        return text
    raise ValueError("Groq kept rate-limiting")


def _parse_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)  # strip any ```json fences or chatter
        if not m:
            raise ValueError(f"AI did not return JSON: {text[:200]}")
        return json.loads(m.group(0))
