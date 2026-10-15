"""Small HTTP helper with retries, so one hiccup doesn't kill a run."""
import time

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, service: str, status: int, body: str):
        super().__init__(f"{service} error {status}: {body[:300]}")
        self.service = service
        self.status = status
        self.body = body


def request(service: str, method: str, url: str, *, retries: int = 3, timeout: int = 30, **kwargs):
    last = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:  # network blip
            last = exc
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code in RETRY_STATUS and attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            raise ApiError(service, resp.status_code, resp.text)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}
    raise ApiError(service, 0, f"network failure: {last}")
