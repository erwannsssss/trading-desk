"""SEC data: what big hedge funds hold (13F) and what companies said in earnings releases (8-K)."""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from functools import lru_cache

from desk import config
from desk.http import request

UA = {"User-Agent": "TradingDesk paper-trading research erwann.sadki@icloud.com"}
CACHE = config.DATA_DIR / "cache" / "13f.json"

FUNDS = {
    1067983: "Berkshire Hathaway (Buffett)",
    1336528: "Pershing Square (Ackman)",
    1656456: "Appaloosa (Tepper)",
    1536411: "Duquesne (Druckenmiller)",
    1350694: "Bridgewater (Dalio's firm)",
    1167483: "Tiger Global",
    1135730: "Coatue",
    1061165: "Lone Pine",
    1103804: "Viking Global",
    1040273: "Third Point (Loeb)",
    1061768: "Baupost (Klarman)",
    1029160: "Soros Fund Management",
    1603466: "Point72 (Cohen)",
    1009207: "D. E. Shaw",
}

_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|company|ltd|plc|holdings?|group|class [a-c]|cl [a-c]|com|new|del|the|n\.?v\.?|s\.?a\.?|ag|se|lp|llc)\b\.?")


def _sec(url: str):
    return request("SEC", "GET", url, headers=UA, timeout=60)


@lru_cache(maxsize=1)
def ticker_map() -> dict:
    """SEC's official ticker list: ticker -> (cik, name) and normalized name -> ticker."""
    d = _sec("https://www.sec.gov/files/company_tickers.json")
    by_ticker, by_name = {}, {}
    for row in d.values():
        by_ticker[row["ticker"]] = (row["cik_str"], row["title"])
        by_name.setdefault(_norm(row["title"]), row["ticker"])
    return {"ticker": by_ticker, "name": by_name}


def _norm(name: str) -> str:
    n = re.sub(r"/[a-z]{2,3}/?", " ", name.lower())  # "/DE/" state tags
    n = re.sub(r"\bamer\b", "america", n.replace("&", " and ").replace(",", " "))
    n = re.sub(r"\bbk\b", "bank", n)
    n = re.sub(r"\b(of)\b", " ", n)
    n = _SUFFIX.sub(" ", n)
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", n)).strip()


# ---- 13F hedge fund holdings -------------------------------------------------
def fund_holdings(refresh_days: int = 7) -> dict:
    """{ticker: [{"fund", "pct_of_fund", "value_usd"}...]}, cached on disk and refreshed weekly."""
    if CACHE.exists():
        cached = json.loads(CACHE.read_text())
        if date.fromisoformat(cached["fetched"]) > date.today() - timedelta(days=refresh_days):
            return cached["holdings"]
    holdings: dict[str, list] = {}
    names = ticker_map()["name"]
    for cik, fund in FUNDS.items():
        try:
            rows, filed = _latest_13f(cik)
        except Exception as exc:
            print(f"13F {fund} failed: {exc}")
            continue
        total = sum(v for _, v in rows) or 1
        for issuer, value in sorted(rows, key=lambda r: -r[1])[:25]:
            ticker = names.get(_norm(issuer))
            if ticker:
                holdings.setdefault(ticker, []).append({"fund": fund, "pct_of_fund": round(value / total * 100, 1),
                                                        "value_usd": value, "as_of_filing": filed})
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"fetched": date.today().isoformat(), "holdings": holdings}, indent=1))
    return holdings


def _latest_13f(cik: int) -> tuple[list[tuple[str, float]], str]:
    sub = _sec(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")["filings"]["recent"]
    acc, filed = next((a, d) for f, a, d in zip(sub["form"], sub["accessionNumber"], sub["filingDate"]) if f == "13F-HR")
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
    files = _sec(f"{folder}/index.json")["directory"]["item"]
    xml_name = next(f["name"] for f in files if f["name"].endswith(".xml") and "primary_doc" not in f["name"])
    raw = request("SEC", "GET", f"{folder}/{xml_name}", headers=UA, timeout=60)["raw"]
    root = ET.fromstring(raw)
    agg: dict[str, float] = {}
    for el in root.iter():
        if el.tag.endswith("infoTable"):
            issuer = value = None
            for child in el:
                if child.tag.endswith("nameOfIssuer"):
                    issuer = child.text or ""
                elif child.tag.endswith("value"):
                    value = float(child.text or 0)
            if issuer and value:
                agg[issuer] = agg.get(issuer, 0) + value
    return list(agg.items()), filed


# ---- Earnings press releases (management's own words) ------------------------
def latest_earnings_release(ticker: str, within_days: int = 21, max_chars: int = 5000) -> dict | None:
    """Text of the most recent earnings press release (8-K item 2.02, exhibit 99.1), if recent."""
    entry = ticker_map()["ticker"].get(ticker.upper())
    if not entry:
        return None
    cik = entry[0]
    sub = _sec(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")["filings"]["recent"]
    cutoff = date.today() - timedelta(days=within_days)
    for form, acc, filed, items in zip(sub["form"], sub["accessionNumber"], sub["filingDate"], sub.get("items", [""] * len(sub["form"]))):
        if date.fromisoformat(filed) < cutoff:
            break
        if form != "8-K" or "2.02" not in (items or ""):
            continue
        folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
        files = [f["name"] for f in _sec(f"{folder}/index.json")["directory"]["item"]]
        exhibits = [f for f in files if re.search(r"ex(hibit)?[-_]?99", f.lower()) and f.lower().endswith((".htm", ".html"))]
        exhibits.sort(key=lambda f: 0 if re.search(r"99[-_.]?0?1(?!\d)", f.lower()) else 1)  # 99.1 = the press release
        ex = exhibits[0] if exhibits else None
        if not ex:
            continue
        raw = request("SEC", "GET", f"{folder}/{ex}", headers=UA, timeout=60)["raw"]
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        return {"filed": filed, "text": text[:max_chars]}
    return None


def days_since(iso: str) -> int:
    return (date.today() - datetime.fromisoformat(iso).date()).days
