#!/usr/bin/env python3
"""
Liputon.fi resale monitor — fast-loop edition
---------------------------------------------
Each run boots the browser ONCE, then re-checks the Ruisrock resale page
every ~15 seconds for a few minutes before exiting. Combined with a
trigger every 5 min (cron-job.org), that gives near-continuous coverage
instead of a single check per cycle.

Reads the seller's asking price (HINTA), not the original (ALKUP. HINTA),
and puts a direct buy link at the top of each Telegram alert.

Environment variables (set in the GitHub workflow):
  TELEGRAM_BOT_TOKEN   (secret)  token from @BotFather
  TELEGRAM_CHAT_ID     (secret)  your numeric Telegram user id
  LIPUTON_URL          the Ruisrock resale page to watch
  MAX_PRICE            only alert when the ASKING price is <= this (euros)
  EVENT_KEYWORD        optional safety filter, e.g. "ruisrock"
  LOOP_SECONDS         how long each run keeps checking (default 180)
  CHECK_EVERY          seconds between checks within a run (default 15)
"""

import os
import re
import json
import time
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
LOOP_SECONDS  = int(os.environ.get("LOOP_SECONDS", "180"))
CHECK_EVERY   = int(os.environ.get("CHECK_EVERY", "15"))

STATE = pathlib.Path("state.json")

EURO = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,4}(?:[.,]\d{1,2})?)")
EURO_NUM = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€")
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
    """ASKING price (HINTA) is the first euro after the '3 PÄIVÄÄ' marker;
    original (ALKUP. HINTA) is the second."""
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


def accept_cookies(page):
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
            return
        except Exception:
            pass


def check_page(page):
    """Reload the page once and return the current 3-day listings."""
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print("nav warning:", e)
    page.wait_for_timeout(3500)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    return parse_listings(text)


def send(message):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": "true"}
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(api, data=payload), timeout=30) as r:
        r.read()


def send_alert(records):
    lines = [f"🎟️ {len(records)} Ruisrock 3-day under €{MAX_PRICE:.0f}!",
             f"👉 BUY: {URL}", ""]
    for r in records[:10]:
        orig = f" (orig €{r['orig']:.0f})" if r["orig"] else ""
        lines.append(f"• €{r['ask']:.2f}{orig} — {r['type']}")
    send("\n".join(lines))


def fingerprint(rec):
    return hashlib.sha1(f"{rec['type']}|{rec['ask']}|{rec['orig']}".encode()).hexdigest()[:16]


def load_state():
    if STATE.exists():
        try:
            return set(json.loads(STATE.read_text()))
        except Exception:
            return set()
    return set()


def main():
    seen = load_state()
    new_seen = set(seen)
    loop_end = time.time() + LOOP_SECONDS
    checks = 0
    total_alerts = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="fi-FI")
        first = True

        while time.time() < loop_end:
            t0 = time.time()
            try:
                listings = check_page(page)
                if first:
                    accept_cookies(page)          # only needed once
                    first = False
                    listings = check_page(page)    # re-read after consent
                checks += 1

                batch = []
                for rec in listings:
                    if rec["ask"] is None or rec["ask"] > MAX_PRICE:
                        continue
                    if EVENT_KEYWORD and EVENT_KEYWORD not in rec["ctx"]:
                        continue
                    fp = fingerprint(rec)
                    if fp in new_seen:
                        continue
                    new_seen.add(fp)
                    batch.append(rec)

                if batch:
                    send_alert(batch)
                    total_alerts += len(batch)
                    print(f"  ALERT: {len(batch)} new (cheapest €{min(r['ask'] for r in batch):.2f})")
            except Exception as e:
                print("check error:", e)

            # pace to CHECK_EVERY, without overshooting the loop window
            to_sleep = CHECK_EVERY - (time.time() - t0)
            if time.time() >= loop_end:
                break
            if to_sleep > 0:
                time.sleep(min(to_sleep, max(0, loop_end - time.time())))

        browser.close()

    STATE.write_text(json.dumps(sorted(new_seen)))
    print(f"Done. {checks} checks in ~{LOOP_SECONDS}s, {total_alerts} alert(s).")


if __name__ == "__main__":
    main()
