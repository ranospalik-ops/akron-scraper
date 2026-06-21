#!/usr/bin/env python3
"""
Akron Real Estate Deal Scraper
- Pulls active listings from Redfin (Akron, OH) via the gis-csv API
- Scores each as a buy-and-hold using cash-on-cash return
- Flags STEAL / SOLID / MARGINAL / PASS
- Sends an SMS alert (Twilio) for new qualifying deals only
- Deduplicates across runs via seen_deals.csv (persisted by GitHub Actions cache)

Designed to run on GitHub Actions cron (no internal scheduler loop).
"""

import os
import csv
import io
import sys
import logging
import requests
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# CONFIG  — non-secret settings live here; secrets come from env
# ─────────────────────────────────────────────────────────────

# Twilio creds are read from environment variables (GitHub Secrets),
# never hard-coded. See setup guide.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM        = os.environ.get("TWILIO_FROM", "")   # e.g. +13305551234 (your Twilio #)
YOUR_NUMBER        = os.environ.get("YOUR_NUMBER", "")   # e.g. +972501234567 (your mobile)

# Akron, OH Redfin region id (verified). region_type=6 == city.
AKRON_REGION_ID = "30808"

# ── Underwriting assumptions (edit to taste) ──
DOWN_PCT          = 0.25      # 25% down (DSCR-style)
RATE              = 0.075     # mortgage rate
LOAN_TERM_YEARS   = 30
RENT_TO_PRICE     = 0.0135    # duplex two-unit income ~1.35% of price (top-of-funnel; verify each)
VACANCY_PCT       = 0.07
MAINT_PCT         = 0.06      # maintenance reserve
CAPEX_PCT         = 0.06      # capex reserve
PM_PCT            = 0.10      # property management
TAX_INS_MONTHLY   = 230       # combined taxes + insurance estimate (Akron)
CLOSING_PCT       = 0.04      # closing costs as % of price (adds to cash invested)

MIN_PRICE         = 40000     # ignore anything below this (junk / land)
MAX_PRICE         = 200000    # ignore anything above your buy box

# Verdict thresholds (cash-on-cash %)
STEAL    = 15.0
SOLID    = 10.0
MARGINAL = 6.0

SEEN_FILE = "seen_deals.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("akron-scraper")


# ─────────────────────────────────────────────────────────────
# Redfin fetch
# ─────────────────────────────────────────────────────────────
def fetch_akron_listings():
    """Pull active listings for Akron from Redfin's gis-csv endpoint."""
    url = "https://www.redfin.com/stingray/api/gis-csv"
    params = {
        "al": 1,
        "market": "ohio",
        "num_homes": 350,
        "ord": "redfin-recommended-asc",
        "page_number": 1,
        "region_id": AKRON_REGION_ID,
        "region_type": 6,          # 6 = city
        "status": 9,               # active
        "uipt": "1,2,3",           # house, condo, townhouse
        "v": 8,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Redfin request failed: {e}")
        return []

    text = r.text
    # Redfin sometimes prefixes the CSV with a junk line; strip to header row.
    if not text.lstrip().lower().startswith("sale type"):
        idx = text.lower().find("sale type")
        if idx != -1:
            text = text[idx:]

    rows = list(csv.DictReader(io.StringIO(text)))
    log.info(f"Redfin returned {len(rows)} raw Akron rows")
    return rows


# ─────────────────────────────────────────────────────────────
# Underwriting
# ─────────────────────────────────────────────────────────────
def monthly_mortgage(principal):
    r = RATE / 12
    n = LOAN_TERM_YEARS * 12
    if r == 0:
        return principal / n
    return principal * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def underwrite(price):
    """Return (coc_percent, monthly_cashflow) for a buy-and-hold at this price."""
    rent = price * RENT_TO_PRICE
    egi = rent * (1 - VACANCY_PCT)                      # effective gross income
    opex = rent * (MAINT_PCT + CAPEX_PCT + PM_PCT) + TAX_INS_MONTHLY
    noi_monthly = egi - opex

    loan = price * (1 - DOWN_PCT)
    debt = monthly_mortgage(loan)
    cashflow = noi_monthly - debt

    cash_invested = price * DOWN_PCT + price * CLOSING_PCT
    annual_cf = cashflow * 12
    coc = (annual_cf / cash_invested) * 100 if cash_invested else 0
    return round(coc, 1), round(cashflow)


def verdict(coc):
    if coc >= STEAL:    return "STEAL"
    if coc >= SOLID:    return "SOLID"
    if coc >= MARGINAL: return "MARGINAL"
    return "PASS"


# ─────────────────────────────────────────────────────────────
# Dedup
# ─────────────────────────────────────────────────────────────
def load_seen():
    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, newline="") as f:
            for row in csv.reader(f):
                if row:
                    seen.add(row[0])
    return seen


def save_seen(seen):
    with open(SEEN_FILE, "w", newline="") as f:
        w = csv.writer(f)
        for k in sorted(seen):
            w.writerow([k])


# ─────────────────────────────────────────────────────────────
# SMS
# ─────────────────────────────────────────────────────────────
def send_sms(body):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, YOUR_NUMBER]):
        log.error("Twilio env vars missing — cannot send SMS. (Printing instead.)")
        log.info("MESSAGE WOULD BE:\n" + body)
        return False

    # SMS segments at ~160 chars; keep it tight so it doesn't fragment/fail.
    if len(body) > 600:
        body = body[:590] + "…"

    from twilio.rest import Client
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=body, from_=TWILIO_FROM, to=YOUR_NUMBER)
        log.info(f"SMS sent, sid={msg.sid}")
        return True
    except Exception as e:
        log.error(f"Twilio send failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def run():
    rows = fetch_akron_listings()
    if not rows:
        # Fail LOUDLY: zero rows almost always means Redfin blocked us or
        # changed format, not a genuinely empty market. Returning False exits
        # non-zero -> GitHub marks the run failed -> you get an email.
        # (Rare false alarm on a truly dead-listing day is the accepted tradeoff.)
        log.error("Zero rows fetched from Redfin — likely blocked or format change. Failing loudly.")
        return False

    seen = load_seen()
    deals = []

    for row in rows:
        try:
            price = float(row.get("PRICE") or 0)
        except ValueError:
            continue
        if not (MIN_PRICE <= price <= MAX_PRICE):
            continue

        addr = (row.get("ADDRESS") or "").strip()
        url = (row.get("URL (SEE https://www.redfin.com/buy-a-home/comparative-market-analysis FOR INFO ON PRICING)")
               or row.get("URL") or "").strip()
        if url and url.startswith("/"):
            url = "https://www.redfin.com" + url

        key = url or addr
        if not key or key in seen:
            continue

        coc, cf = underwrite(price)
        v = verdict(coc)
        if v == "PASS":
            seen.add(key)        # mark seen so we don't re-evaluate junk forever
            continue

        deals.append({
            "addr": addr, "price": int(price), "coc": coc,
            "cf": cf, "verdict": v, "url": url, "key": key,
        })
        seen.add(key)

    # Best first
    deals.sort(key=lambda d: d["coc"], reverse=True)
    save_seen(seen)

    if not deals:
        log.info("No new qualifying deals this run.")
        return True

    log.info(f"{len(deals)} new qualifying deal(s).")

    # Build a terse SMS — top 3 only to stay short
    today = datetime.now().strftime("%m/%d")
    lines = [f"Akron Deals {today} — {len(deals)} new"]
    for i, d in enumerate(deals[:3], 1):
        lines.append(f"{i}) {d['addr']} ${d['price']//1000}k | {d['coc']}% {d['verdict']}")
        if d["url"]:
            lines.append(f"   {d['url']}")
    body = "\n".join(lines)
    ok = send_sms(body)
    return ok


if __name__ == "__main__":
    success = run()
    # Exit non-zero on failure so GitHub Actions marks the run failed
    # and emails you automatically. Silence = working; email = something broke.
    sys.exit(0 if success else 1)
