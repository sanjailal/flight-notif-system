#!/usr/bin/env python3
"""Alert when an Alliance Air (9I) flight Agatti -> Kochi is bookable on Dec 16-19.

Sources:
  A. bookme.allianceair.in (airline's own booking engine)  - every run
  B. Google Flights via SerpApi, filtered to 9I             - every SERPAPI_EVERY_HOURS hours
Alerts go to ntfy.sh. State in state.json prevents duplicate alerts.

Env: NTFY_TOPIC (required unless --dry-run), SERPAPI_KEY (optional),
     SERPAPI_EVERY_HOURS (default 24), ORIGIN/DEST/DATES (optional overrides)
Flags: --dry-run (print, don't notify or save state), --force-serpapi,
       --test-notify (send a test push and exit)
"""
import json
import os
import re
import sys
import datetime as dt
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

ORIGIN = os.environ.get("ORIGIN", "AGX")
DEST = os.environ.get("DEST", "COK")
DATES = os.environ.get("DATES", "2026-12-16,2026-12-17,2026-12-18,2026-12-19").split(",")
BOOK_URL = "https://bookme.allianceair.in"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
FAIL_ALERT_AFTER = 6  # consecutive failed runs before a "checker broken" alert
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------- Source A: Alliance Air ----------
def alliance_check(dates):
    """Return {date: [ {flight, dep, fare} ]}. Raises on any failure."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    opener.addheaders = [("User-Agent", UA)]
    home = opener.open(BOOK_URL + "/", timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'name="csrf-token" content="([^"]+)"', home)
    if not m:
        raise RuntimeError("csrf token not found (site layout changed?)")
    csrf = m.group(1)
    out = {}
    for d in dates:
        y, mo, da = d.split("-")
        form = urllib.parse.urlencode({
            "_csrf": csrf, "org": ORIGIN, "des": DEST, "dep_date": f"{da}/{mo}/{y}",
            "ret_date": "", "pax": "1 pax", "adult": 1, "child": 0, "infant": 0,
            "ccy": "INR", "promo_code": "", "multi_route": "", "multi_date": "",
        }).encode()
        html = opener.open(urllib.request.Request(BOOK_URL + "/search-schedule", data=form),
                           timeout=60).read().decode("utf-8", "replace")
        sched = extract_schedule(html)
        out[d] = sched
    return out


def extract_schedule(html):
    """Pull departure_schedule out of the embedded dataSchedule JSON."""
    m = re.search(r'this\.dataSchedule\s*=\s*(\[.*?\]);\s*\n', html, re.S)
    if not m:
        raise RuntimeError("dataSchedule not found in response (site layout changed?)")
    data = json.loads(m.group(1))
    block = next((x for x in data if isinstance(x, dict) and "departure_schedule" in x), None)
    if block is None:
        raise RuntimeError("departure_schedule missing in response")
    best = {}  # one entry per flight, cheapest fare class
    for item in block["departure_schedule"]:
        legs = item.get("connecting_flight_routes", [])
        nums = "+".join(l.get("flight_number", "?") for l in legs)
        d0 = legs[0]["departure_date"] if legs else {}
        dep = f'{d0.get("hour", "??")}:{d0.get("minute", "??")}'
        fare = item.get("fare_info", {}).get("total_search_fare", {}).get("amount")
        key = (nums, dep)
        if key not in best or (fare is not None and (best[key]["fare"] is None or fare < best[key]["fare"])):
            best[key] = {"flight": nums, "dep": dep, "fare": fare}
    return list(best.values())


# ---------- Source B: SerpApi Google Flights ----------
def serpapi_check(dates, key):
    out = {}
    for d in dates:
        q = urllib.parse.urlencode({
            "engine": "google_flights", "departure_id": ORIGIN, "arrival_id": DEST,
            "outbound_date": d, "type": 2, "currency": "INR", "hl": "en",
            "include_airlines": "9I", "api_key": key,
        })
        with urllib.request.urlopen("https://serpapi.com/search.json?" + q, timeout=60) as r:
            j = json.loads(r.read())
        if j.get("error") and "hasn't returned any results" not in j["error"]:
            raise RuntimeError("serpapi: " + j["error"])
        flights = []
        for grp in (j.get("best_flights") or []) + (j.get("other_flights") or []):
            legs = grp.get("flights", [])
            if not any("Alliance" in l.get("airline", "") or
                       str(l.get("flight_number", "")).startswith("9I") for l in legs):
                continue
            flights.append({
                "flight": "+".join(l.get("flight_number", "?") for l in legs),
                "dep": (legs[0].get("departure_airport", {}).get("time", "") or "")[-5:],
                "fare": grp.get("price"),
            })
        out[d] = flights
    return out


# ---------- state / notify ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"seen": {}, "fails": {}, "fail_alerted": {}}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, sort_keys=True)


def notify(title, body, priority="high", dry=False):
    log(f"[notify] {title}\n{body}")
    topic = os.environ.get("NTFY_TOPIC")
    if dry or not topic:
        if not dry:
            log("NTFY_TOPIC not set; skipping push")
        return
    req = urllib.request.Request(
        "https://ntfy.sh/" + urllib.parse.quote(topic), data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": "airplane",
                 "Click": BOOK_URL, "User-Agent": "flight-alert"})
    urllib.request.urlopen(req, timeout=30).read()


def fmt(d, flights):
    parts = [f'{f["flight"]} {f["dep"]} Rs{f["fare"]}' for f in flights]
    return f"{d}: " + "; ".join(parts)


def main():
    dry = "--dry-run" in sys.argv
    if "--test-notify" in sys.argv:
        notify("Flight alert test", "If you see this, push works.", dry=False)
        return 0

    state = load_state()
    found = {}  # date -> {source: flights}
    errors = {}

    try:
        for d, fl in alliance_check(DATES).items():
            found.setdefault(d, {})["Alliance Air site"] = fl
        state["fails"]["alliance"] = 0
        state["fail_alerted"]["alliance"] = False
    except Exception as e:  # noqa: BLE001
        errors["alliance"] = str(e)

    key = os.environ.get("SERPAPI_KEY")
    every = int(os.environ.get("SERPAPI_EVERY_HOURS", "24"))
    now = dt.datetime.utcnow()
    due = now.hour % every == 0 and now.minute < 15
    if key and (due or "--force-serpapi" in sys.argv):
        try:
            for d, fl in serpapi_check(DATES, key).items():
                found.setdefault(d, {})["Google Flights"] = fl
            state["fails"]["serpapi"] = 0
            state["fail_alerted"]["serpapi"] = False
        except Exception as e:  # noqa: BLE001
            errors["serpapi"] = str(e)

    # failure tracking
    for src, msg in errors.items():
        log(f"[error] {src}: {msg}")
        state["fails"][src] = state["fails"].get(src, 0) + 1
        if state["fails"][src] >= FAIL_ALERT_AFTER and not state["fail_alerted"].get(src):
            notify(f"Flight checker broken: {src}",
                   f"{src} failed {state['fails'][src]} runs in a row: {msg[:200]}",
                   priority="default", dry=dry)
            state["fail_alerted"][src] = True

    # availability alerts (only for sources that succeeded this run)
    alerts = []
    for d in DATES:
        srcs = found.get(d, {})
        flights = [f for fl in srcs.values() for f in fl]
        log(f"{d}: " + (fmt(d, flights) if flights else "no flights") + f"  (checked: {', '.join(srcs) or 'none'})")
        if not srcs:
            continue  # nothing checked, keep previous state
        sig = json.dumps(sorted((f["flight"], f["fare"]) for f in flights))
        if not flights:
            # only clear if the primary source confirmed empty
            if "Alliance Air site" in srcs:
                state["seen"].pop(d, None)
            continue
        if state["seen"].get(d) != sig:
            alerts.append(fmt(d, flights) + f" [{', '.join(k for k, v in srcs.items() if v)}]")
            state["seen"][d] = sig

    if alerts:
        notify(f"Alliance Air {ORIGIN}->{DEST} available!", "\n".join(alerts), dry=dry)

    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
