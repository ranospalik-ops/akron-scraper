#!/usr/bin/env python3
"""
Ohio Metro Real Estate Deal Scraper
- Pulls active listings from Redfin for 6 Ohio metros
- Scores each as a duplex buy-and-hold (cash-on-cash)
- Alerts (Telegram) only NEW deals scoring SOLID+ (10%+ CoC), grouped by city
- Deduplicates across runs via seen_deals.csv (committed back by the workflow)
- Fails loudly (exit 1) if ALL cities return zero rows or the send fails

Top-of-funnel filter, NOT an underwriter. Redfin's feed has no unit count, so
it assumes a duplex on every property. Verify each hit with your RE Analyzer.
"""

import os
import csv
import io
import sys
import logging
import requests
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# TARGET CITIES — region_type=6 means "city".
# VERIFY EACH region_id ONCE (2 min): open Redfin, search the city, look at the
# URL: redfin.com/city/<REGION_ID>/OH/<CityName>. The number after /city/ is the
# region_id. If a city's alerts look like another city's listings, its region_id
# is wrong - fix it here. The run log prints the city name with each batch so a
# mismatch is easy to spot.
# ─────────────────────────────────────────────────────────────
CITIES = {
    "Akron":      "30808",
    "Cleveland":  "4407",
    "Columbus":   "4870",
    "Cincinnati": "4488",
    "Dayton":     "5037",
    "Toledo":     "18774",
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

DOWN_PCT          = 0.25
RATE              = 0.075
LOAN_TERM_YEARS   = 30
RENT_TO_PRICE     = 0.0135
VACANCY_PCT       = 0.07
MAINT_PCT         = 0.06
CAPEX_PCT         = 0.06
PM_PCT            = 0.10
TAX_INS_MONTHLY   = 230
CLOSING_PCT       = 0.04

MIN_PRICE         = 40000
MAX_PRICE         = 200000

STEAL    = 15.0
SOLID    = 10.0
MARGINAL = 6.0
ALERT_MIN_COC = SOLID

SEEN_FILE = "seen_deals.csv"

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("ohio-scraper")


def fetch_city(city, region_id):
    url = "https://www.redfin.com/stingray/api/gis-csv"
    params = {"al":1,"market":"ohio","num_homes":350,"ord":"redfin-recommended-asc",
              "page_number":1,"region_id":region_id,"region_type":6,"status":9,
              "uipt":"1,2,3","v":8}
    headers = {"User-Agent":("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
               "Accept":"text/csv,application/csv,*/*","Accept-Language":"en-US,en;q=0.9"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"[{city}] Redfin request failed: {e}")
        return None
    text = r.text
    if not text.lstrip().lower().startswith("sale type"):
        idx = text.lower().find("sale type")
        if idx != -1:
            text = text[idx:]
    rows = list(csv.DictReader(io.StringIO(text)))
    log.info(f"[{city}] Redfin returned {len(rows)} raw rows")
    return rows


def monthly_mortgage(principal):
    r = RATE/12; n = LOAN_TERM_YEARS*12
    if r == 0: return principal/n
    return principal*(r*(1+r)**n)/((1+r)**n-1)


def underwrite(price):
    rent = price*RENT_TO_PRICE
    egi = rent*(1-VACANCY_PCT)
    opex = rent*(MAINT_PCT+CAPEX_PCT+PM_PCT)+TAX_INS_MONTHLY
    noi = egi-opex
    cashflow = noi - monthly_mortgage(price*(1-DOWN_PCT))
    cash_in = price*DOWN_PCT + price*CLOSING_PCT
    coc = (cashflow*12/cash_in)*100 if cash_in else 0
    return round(coc,1), round(cashflow)


def verdict(coc):
    if coc>=STEAL: return "STEAL"
    if coc>=SOLID: return "SOLID"
    if coc>=MARGINAL: return "MARGINAL"
    return "PASS"


def load_seen():
    seen=set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, newline="") as f:
            for row in csv.reader(f):
                if row: seen.add(row[0])
    return seen


def save_seen(seen):
    with open(SEEN_FILE,"w",newline="") as f:
        w=csv.writer(f)
        for k in sorted(seen): w.writerow([k])


def send_telegram(body):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log.error("Telegram env vars missing — cannot send. (Printing instead.)")
        log.info("MESSAGE WOULD BE:\n"+body)
        return False
    if len(body)>4000: body=body[:3990]+"…"
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r=requests.post(url,json={"chat_id":TELEGRAM_CHAT_ID,"text":body,
            "disable_web_page_preview":True},timeout=30)
        r.raise_for_status()
        log.info("Telegram message sent.")
        return True
    except requests.RequestException as e:
        log.error(f"Telegram send failed: {e}")
        return False


def run():
    seen=load_seen()
    deals_by_city={}
    hard_failures=[]

    for city,rid in CITIES.items():
        rows=fetch_city(city,rid)
        if rows is None:
            hard_failures.append(city); continue
        if len(rows)==0:
            hard_failures.append(f"{city}(0 rows)"); continue

        for row in rows:
            try:
                price=float(row.get("PRICE") or 0)
            except ValueError:
                continue
            if not (MIN_PRICE<=price<=MAX_PRICE): continue
            addr=(row.get("ADDRESS") or "").strip()
            url=(row.get("URL (SEE https://www.redfin.com/buy-a-home/comparative-market-analysis FOR INFO ON PRICING)")
                 or row.get("URL") or "").strip()
            if url and url.startswith("/"): url="https://www.redfin.com"+url
            key=url or f"{city}:{addr}"
            if not key or key in seen: continue
            coc,cf=underwrite(price)
            seen.add(key)
            if coc<ALERT_MIN_COC: continue
            deals_by_city.setdefault(city,[]).append(
                {"addr":addr,"price":int(price),"coc":coc,"cf":cf,
                 "verdict":verdict(coc),"url":url})

    save_seen(seen)

    total=sum(len(v) for v in deals_by_city.values())
    if total:
        today=datetime.now().strftime("%b %d")
        lines=[f"🏠 Ohio Deals — {today} ({total} new, SOLID+)",""]
        for city in sorted(deals_by_city,
                key=lambda c:max(d["coc"] for d in deals_by_city[c]),reverse=True):
            ds=sorted(deals_by_city[city],key=lambda d:d["coc"],reverse=True)
            lines.append(f"📍 {city} ({len(ds)})")
            for d in ds[:4]:
                lines.append(f"  • {d['addr']} — ${d['price']:,}")
                lines.append(f"    {d['coc']}% CoC | ${d['cf']}/mo | {d['verdict']}")
                if d["url"]: lines.append(f"    {d['url']}")
            if len(ds)>4: lines.append(f"  + {len(ds)-4} more in {city}")
            lines.append("")
        body="\n".join(lines).rstrip()
        sent_ok=send_telegram(body)
    else:
        log.info("No new SOLID+ deals across all metros this run.")
        sent_ok=True

    if len(hard_failures)==len(CITIES):
        log.error(f"All cities failed to fetch: {hard_failures}. Failing loudly.")
        return False
    if hard_failures:
        log.warning(f"Some cities failed to fetch (continuing): {hard_failures}")
    return sent_ok


if __name__=="__main__":
    ok=run()
    sys.exit(0 if ok else 1)
