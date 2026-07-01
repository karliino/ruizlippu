#!/usr/bin/env python3
"""
Liputon.fi resale monitor
--------------------------
Pings a Telegram chat when a 3-day Ruisrock ticket is being SOLD (the seller's
asking price / HINTA) at or below a price you set.

It deliberately reads HINTA (asking price), NOT ALKUP. HINTA (original price).
It opens the page in a headless browser, so there is no hidden API to grab.

Environment variables (set in the GitHub workflow):
  TELEGRAM_BOT_TOKEN   (secret)  token from @BotFather
  TELEGRAM_CHAT_ID     (secret)  your numeric Telegram user id
  LIPUTON_URL          the Ruisrock resale page to watch
  MAX_PRICE            only alert when the ASKING price is <= this (euros)
  EVENT_KEYWORD        optional safety filter, e.g. "ruisrock" (usually not needed)
"""

import os
import re
import json
import hashlib
import pathlib
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright


def _req(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required environment variable: {name}")
    return v


BOT_TOKEN     = _req("TELEGRAM_BOT_TOKEN")
CHAT_ID       = _req("TELEGRAM_CHAT_ID")
URL           = os.environ.get("LIPUTON_URL", "https://www.liputon.fi/events/110487").strip()
MAX_PRICE     = float(os.environ.get("MAX_PRICE", "170").replace(",", "."))
EVENT_KEYWORD = os.environ.get("EVENT_KEYWORD", "").strip().lower()

STATE = pathlib.Path("state.json")

# A euro amount, either "236,00 €" or "€ 236,00".
EURO = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,4}(?:[.,]\d{1,2})?)")
# The listing format puts the number before the € sign.
EURO_NUM = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€")
# Marks a 3-day / weekend pass.
MARKER = re.compile(
    r"3\s*-?\s*(?:PÄIVÄÄ|PAIVAA|PÄIVÄN|PAIVAN|päivää|päivän|paivaa|paivan|pv|vrk)"
    r"|kolmen\s+päiv|kolmen\s+paiv|3-day\b|3 day\b",
    re.I,
)


def to_price(value):
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    m = EURO.search(s)
    if m:
        num = m.group(1) or m.group(2)
    else:
        m2 = re.search(r"\d{1,4}(?:[.,]\d{1,2})?", s)
        if not m2:
            return None
        num = m2.group(0)
    try:
        return float(num.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def parse_listings(text):
    """
    For each 3-day pass, the ASKING price (HINTA) is the first euro amount
    after the '3 PÄIVÄÄ' marker; the original price (ALKUP. HINTA) is the
    second. Returns one record per listing.
    """
    out = []
    for m in MARKER.finditer(text):
        after = text[m.end(): m.end() + 150]
        euros = EURO_NUM.findall(after)
        if not euros:
            continue
        ask = to_price(euros[0])
        orig = to_price(euros[1]) if len(euros) > 1 else None
        head = re.split(r"(?i)paikka", text[max(0, m.start() - 60): m.start()])[-1]
        tname = head.strip() or "3-day"
        out.append({
            "type": tname[:40],
            "ask": ask,
            "orig": orig,
            "ctx": (tname + " " + after).lower(),
        })
    return out


def render_text(url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="fi-FI")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("Navigation warning:", e)
        # Best-effort cookie accept so listings render.
        for sel in (
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "#CybotCookiebotDialogBodyButtonAccept",
            "text=Salli kaikki",
            "text=Hyväksy kaikki",
            "text=Hyväksy",
            "text=Accept all",
        ):
            try:
                page.click(sel, timeout=1500)
                break
            except Exception:
                pass
        page.wait_for_timeout(6000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        browser.close()
    return text


def send(message):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": "true"}
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(api, data=payload), timeout=30) as r:
        r.read()


def fingerprint(rec):
    key = f"{rec['type']}|{rec['ask']}|{rec['orig']}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def main():
    text = render_text(URL)
    listings = parse_listings(text)

    seen = set()
    if STATE.exists():
        try:
            seen = set(json.loads(STATE.read_text()))
        except Exception:
            seen = set()
    new_seen = set(seen)

    alerts = []
    for rec in listings:
        if rec["ask"] is None or rec["ask"] > MAX_PRICE:
            continue
        if EVENT_KEYWORD and EVENT_KEYWORD not in rec["ctx"]:
            continue
        fp = fingerprint(rec)
        if fp in seen:
            continue
        new_seen.add(fp)
        alerts.append(rec)

    alerts.sort(key=lambda r: r["ask"])

    if alerts:
        lines = [f"🎟️ Liputon: {len(alerts)} new 3-day under €{MAX_PRICE:.0f}", ""]
        for r in alerts[:10]:
            orig = f" (orig €{r['orig']:.0f})" if r["orig"] else ""
            lines.append(f"• €{r['ask']:.2f}{orig} — {r['type']}")
        lines += ["", URL]
        send("\n".join(lines))

    STATE.write_text(json.dumps(sorted(new_seen)))
    under = sum(1 for r in listings if r["ask"] is not None and r["ask"] <= MAX_PRICE)
    print(f"Checked. {len(listings)} 3-day listing(s), {under} under EUR {MAX_PRICE:.0f}, {len(alerts)} new alert(s).")


if __name__ == "__main__":
    main()
