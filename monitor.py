#!/usr/bin/env python3
"""
Liputon.fi resale monitor
--------------------------
Pings a Telegram chat when a matching ticket (e.g. a 3-day Ruisrock pass)
appears at or below a price you set.

It opens the page in a headless browser and reads it like a real visitor,
so there is NO hidden API URL to grab. You just point it at the page.

Config comes from environment variables (set in the GitHub workflow):
  TELEGRAM_BOT_TOKEN   (secret)  token from @BotFather
  TELEGRAM_CHAT_ID     (secret)  your numeric Telegram user id
  LIPUTON_URL          the resale page to watch (Ruisrock 3-day event page)
  MAX_PRICE            only alert on matches at or below this many euros
  EVENT_KEYWORD        optional: a word that must appear (e.g. "ruisrock").
                       Leave empty if LIPUTON_URL is already event-specific.

State (which listings you've already been told about) is kept in state.json,
which the workflow commits back so you don't get re-alerted about the same
listing every run.
"""

import os
import re
import json
import hashlib
import pathlib

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- config ----
def _req(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required environment variable: {name}")
    return v

BOT_TOKEN     = _req("TELEGRAM_BOT_TOKEN")
CHAT_ID       = _req("TELEGRAM_CHAT_ID")
URL           = os.environ.get("LIPUTON_URL", "https://www.liputon.fi/bargains").strip()
MAX_PRICE     = float(os.environ.get("MAX_PRICE", "150").replace(",", "."))
EVENT_KEYWORD = os.environ.get("EVENT_KEYWORD", "").strip().lower()

STATE = pathlib.Path("state.json")

# ------------------------------------------------------------- matching -----
# Counts as a 3-day / weekend pass.
THREE_DAY = re.compile(
    r"(3\s*-?\s*(?:pv|paiv|päiv|day|vrk)|kolmen\s+paiv|kolmen\s+päiv|3\s*-?\s*day|viikonlopp|weekend)",
    re.I,
)
# Obvious single-/two-day labels we do NOT want. The "3" anchor above already
# filters most of these, this is just belt-and-suspenders.
NOT_THREE = re.compile(
    r"(paivalippu|päivälippu|1\s*-?\s*(?:pv|paiv|päiv|day)|yhden\s+paiv|yhden\s+päiv|"
    r"2\s*-?\s*(?:pv|paiv|päiv|day)|kahden\s+paiv|kahden\s+päiv)",
    re.I,
)
# A euro amount anywhere in a blob of text.
EURO = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,4}(?:[.,]\d{1,2})?)")


def to_price(value):
    """Parse '120', '120,00', '120.00 €', 120 -> 120.0  (or None)."""
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


def is_match(text, price):
    """True if the text looks like a 3-day pass for the wanted event."""
    if price is None or price > MAX_PRICE:
        return False
    if not THREE_DAY.search(text):
        return False
    if NOT_THREE.search(text):
        return False
    if EVENT_KEYWORD and EVENT_KEYWORD not in text.lower():
        return False
    return True


def fingerprint(text, price):
    norm = re.sub(r"\s+", " ", text.lower()).strip()[:160]
    return hashlib.sha1(f"{round(price, 2)}|{norm}".encode()).hexdigest()[:16]


# ------------------------------------------------- pull data from the page --
def walk_json(obj, out):
    """Recursively find record-like dicts that carry a price + text."""
    if isinstance(obj, dict):
        blob = " ".join(str(v) for v in obj.values() if isinstance(v, (str, int, float)))
        price = None
        for k, v in obj.items():
            if re.search(r"price|hinta|amount|eur", str(k), re.I):
                price = to_price(v) or price
        if price is None:
            price = to_price(blob)
        if price is not None and THREE_DAY.search(blob):
            out.append((blob[:200], price))
        for v in obj.values():
            walk_json(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_json(v, out)


def scan_text(text, out):
    """Fallback: slide a small window over the rendered page text."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    window = 6
    for i, line in enumerate(lines):
        if not EURO.search(line):
            continue
        blob = " ".join(lines[max(0, i - window): i + window])
        price = to_price(line) or to_price(blob)
        if price is not None and THREE_DAY.search(blob):
            out.append((blob[:200], price))


def collect():
    matches = []
    captured_json = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="fi-FI")

        def on_response(resp):
            try:
                if "application/json" in resp.headers.get("content-type", ""):
                    captured_json.append(resp.json())
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
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

        page.wait_for_timeout(6000)  # let late XHR / rendering settle
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""

        browser.close()

    for data in captured_json:
        walk_json(data, matches)
    scan_text(body_text, matches)
    return matches


# --------------------------------------------------------------- telegram ---
def send(message):
    import urllib.parse
    import urllib.request

    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": "false"}
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(api, data=payload), timeout=30) as r:
        r.read()


# ------------------------------------------------------------------- main ---
def main():
    matches = collect()

    seen = set()
    if STATE.exists():
        try:
            seen = set(json.loads(STATE.read_text()))
        except Exception:
            seen = set()

    new_seen = set(seen)
    alerts = []
    for text, price in matches:
        if not is_match(text, price):
            continue
        fp = fingerprint(text, price)
        if fp in seen:
            continue
        new_seen.add(fp)
        alerts.append((price, text))

    # de-dup identical alerts within this run
    alerts = sorted(set(alerts))

    if alerts:
        lines = [f"🎟️ Liputon: {len(alerts)} new match under €{MAX_PRICE:.0f}", ""]
        for price, text in alerts[:10]:
            snippet = re.sub(r"\s+", " ", text).strip()[:120]
            lines.append(f"• €{price:.2f} — {snippet}")
        lines += ["", URL]
        send("\n".join(lines))

    STATE.write_text(json.dumps(sorted(new_seen)))
    print(f"Checked. {len(matches)} candidate(s), {len(alerts)} new alert(s).")


if __name__ == "__main__":
    main()
