#!/usr/bin/env python3
"""Scrape RailRat.net train pages for GPS position/speed reports.

RailRat embeds every position report of a train's current (or most recently
completed) run directly in the train page HTML, as Leaflet marker calls:

    L.circleMarker([40.324250,-74.613710],blueCircle).addTo(mymap)
      .bindPopup("<b>Acela Express 2151</b><small><br>09:44 39 mi NE of PHL, 158&nbsp;mph&nbsp;SW</small>");

This script fetches every train page for a route and accumulates the parsed
points into data/observations.jsonl. RailRat only keeps the latest run per
train, so run this whenever you like -- each run merges in whatever is new
and skips points already recorded. With --wayback it also harvests whatever
historical snapshots of the train pages the Internet Archive happens to have.

Usage:
    python scrape_railrat.py                        # Acela, live pages
    python scrape_railrat.py --route NortheastRegional
    python scrape_railrat.py --trains 2100,2102     # seed extra train numbers
    python scrape_railrat.py --wayback              # backfill from archive.org
"""

import argparse
import datetime as dt
import html as htmllib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://railrat.net"
UA = "speedo/0.1 (personal hobby project mapping train speeds; polite: 1 req/sec)"
THROTTLE_LIVE = 1.0      # seconds between requests to railrat.net
THROTTLE_WAYBACK = 10.0  # archive.org rate-limits hard; be extra gentle
MAX_PLAUSIBLE_MPH = 170  # anything above this is a GPS glitch

DATA = Path(__file__).parent / "data"
OBS_FILE = DATA / "observations.jsonl"

# --- fetching ---------------------------------------------------------------

_last_fetch = 0.0


def fetch(url, throttle=THROTTLE_LIVE, retries=4):
    """Polite GET with throttling and backoff on 429/5xx. Returns text or None."""
    global _last_fetch
    backoff = 20.0
    for attempt in range(retries):
        wait = _last_fetch + throttle - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_fetch = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503) and attempt < retries - 1:
                print(f"    HTTP {e.code}, backing off {backoff:.0f}s...")
                time.sleep(backoff)
                backoff *= 2
                continue
            print(f"    HTTP {e.code} for {url}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            print(f"    failed: {url} ({e})")
            return None
    return None


# --- parsing ----------------------------------------------------------------

H1_RE = re.compile(
    r'<h1><a href="/routes/([A-Za-z]+)/">([^<]+)</a>\s*Train\s*(\d+)</h1>')
UPDATED_RE = re.compile(r"updated\s+(\d{1,2}):(\d{2})&nbsp;on&nbsp;(\d{1,2})/(\d{1,2})")
MARKER_RE = re.compile(
    r"L\.(?:circleMarker|marker)\(\[(-?\d+\.\d+),\s*(-?\d+\.\d+)\][^)]*?\)"
    r"\.addTo\(mymap\)\.bindPopup\(\"(.*?)\"\)",
    re.S)
POPUP_RE = re.compile(
    r"<small><br>(\d{1,2}):(\d{2})\s+(.*?),\s*(\d+)\s*mph(?:\s+([NSEW]{1,3}))?\s*</small>")


def clean_popup(s):
    s = s.replace("&nbsp;", " ")
    return htmllib.unescape(s)


def infer_year(month, day, now):
    """Most recent year that doesn't put (month, day) in the future.

    RailRat pages only ever describe the past (stale train numbers keep their
    last run for months), so never resolve forward. One day of slack covers
    timezone skew between the page clock (ET) and the local clock.
    """
    limit = now.date() + dt.timedelta(days=1)
    for year in (now.year + 1, now.year, now.year - 1, now.year - 2):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            continue
        if d <= limit:
            return d
    return None


def parse_train_page(text, now):
    """Parse one train page. Returns dict or None if page has no usable data.

    Points come out newest-first (document order) with full timestamps,
    dated by walking backward from the page's "updated HH:MM on MM/DD" stamp:
    whenever clock time increases as we walk into the past, we crossed
    midnight, so decrement the date.
    """
    m = H1_RE.search(text)
    if not m:
        return None
    route_slug, route_name, train = m.group(1), m.group(2).strip(), int(m.group(3))

    u = UPDATED_RE.search(text)
    if not u:
        return None
    upd_date = infer_year(int(u.group(3)), int(u.group(4)), now)
    if upd_date is None:
        return None

    raw_pts = []
    for mm in MARKER_RE.finditer(text):
        popup = clean_popup(mm.group(3))
        pm = POPUP_RE.search(popup)
        if not pm:
            continue  # station marker or other non-position popup
        raw_pts.append({
            "lat": float(mm.group(1)),
            "lon": float(mm.group(2)),
            "hh": int(pm.group(1)),
            "mm": int(pm.group(2)),
            "desc": pm.group(3).strip(),
            "mph": int(pm.group(4)),
            "heading": pm.group(5) or "",
        })
    if not raw_pts:
        return None

    # Walk backward through time assigning dates. Position reports can arrive
    # slightly out of order, so a small clock increase toward the past is just
    # jitter; only a large jump (>12h) means we crossed midnight.
    date = upd_date
    prev_minutes = raw_pts[0]["hh"] * 60 + raw_pts[0]["mm"]
    points = []
    for p in raw_pts:
        minutes = p["hh"] * 60 + p["mm"]
        if minutes > prev_minutes + 12 * 60:  # older point, much later clock
            date = date - dt.timedelta(days=1)
            prev_minutes = minutes
        elif minutes < prev_minutes:
            prev_minutes = minutes
        ts = f"{date.isoformat()}T{p['hh']:02d}:{p['mm']:02d}:00"
        points.append({
            "ts": ts, "lat": p["lat"], "lon": p["lon"],
            "mph": p["mph"], "heading": p["heading"], "desc": p["desc"],
        })

    run_date = min(p["ts"] for p in points)[:10]
    return {
        "route": route_slug, "route_name": route_name, "train": train,
        "run_date": run_date, "points": points,
    }


# --- storage ----------------------------------------------------------------

def obs_key(train, ts, lat, lon):
    return f"{train}|{ts}|{lat:.4f}|{lon:.4f}"


def load_seen():
    seen = set()
    if OBS_FILE.exists():
        with OBS_FILE.open(encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seen.add(obs_key(o["train"], o["ts"], o["lat"], o["lon"]))
    return seen


def append_parsed(parsed, seen, src, out_f):
    """Write points not seen before. Returns number added."""
    added = 0
    for p in parsed["points"]:
        key = obs_key(parsed["train"], p["ts"], p["lat"], p["lon"])
        if key in seen or p["mph"] > MAX_PLAUSIBLE_MPH:
            continue
        seen.add(key)
        rec = {
            "route": parsed["route"], "train": parsed["train"],
            "run_date": parsed["run_date"], **p, "src": src,
        }
        out_f.write(json.dumps(rec) + "\n")
        added += 1
    return added


# --- roster -----------------------------------------------------------------

TRAIN_LINK_RE = re.compile(r'href="/trains/(\d+)/"')


def load_roster(route):
    f = DATA / f"roster_{route}.json"
    if f.exists():
        return set(json.loads(f.read_text(encoding="utf-8")))
    return set()


def save_roster(route, roster):
    f = DATA / f"roster_{route}.json"
    f.write_text(json.dumps(sorted(roster)), encoding="utf-8")


def update_roster_from_route_page(route, roster):
    print(f"Fetching route page /routes/{route}/ ...")
    text = fetch(f"{BASE}/routes/{route}/")
    if text is None:
        print("  route page fetch failed; using existing roster")
        return roster
    found = {int(n) for n in TRAIN_LINK_RE.findall(text)}
    new = found - roster
    if new:
        print(f"  roster: {len(found)} trains on page, {len(new)} new: {sorted(new)}")
    else:
        print(f"  roster: {len(found)} trains on page, none new")
    return roster | found


# --- live scrape ------------------------------------------------------------

def scrape_live(route, roster, seen):
    total_added = 0
    scrape_day = dt.date.today().isoformat()
    rawdir = DATA / "raw" / scrape_day
    rawdir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()

    with OBS_FILE.open("a", encoding="utf-8") as out_f:
        for i, train in enumerate(sorted(roster), 1):
            text = fetch(f"{BASE}/trains/{train}/")
            if text is None:
                print(f"  [{i}/{len(roster)}] train {train}: fetch failed")
                continue
            parsed = parse_train_page(text, now)
            if parsed is None:
                print(f"  [{i}/{len(roster)}] train {train}: no position data")
                continue
            if parsed["route"] != route:
                print(f"  [{i}/{len(roster)}] train {train}: belongs to {parsed['route']}, skipping")
                continue
            u = UPDATED_RE.search(text)
            stamp = f"{int(u.group(3)):02d}{int(u.group(4)):02d}-{int(u.group(1)):02d}{u.group(2)}"
            rawfile = rawdir / f"{train}-{stamp}.html"
            if not rawfile.exists():
                rawfile.write_text(text, encoding="utf-8")
            added = append_parsed(parsed, seen, "live", out_f)
            total_added += added
            print(f"  [{i}/{len(roster)}] train {train}: {len(parsed['points'])} points "
                  f"({parsed['run_date']}), {added} new")
    return total_added


# --- wayback backfill -------------------------------------------------------

def wayback_snapshots(train):
    """List archived snapshot timestamps for a train page (cached on disk).

    Returns None on a transient failure (rate limit, network) -- and crucially
    does NOT cache that, so the train is retried on the next run. Only real
    answers (including a genuine empty result) are cached.
    """
    cache = DATA / "raw" / "wayback" / f"cdx-{train}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    url = ("http://web.archive.org/cdx/search/cdx"
           f"?url=railrat.net/trains/{train}/&output=json&fl=timestamp"
           "&filter=statuscode:200&collapse=digest")
    text = fetch(url, throttle=THROTTLE_WAYBACK)
    if text is None:
        return None
    if text.strip() == "":
        stamps = []  # CDX returns an empty body (not "[]") for zero matches
    else:
        try:
            rows = json.loads(text)
            stamps = [r[0] for r in rows[1:]]  # first row is the header
        except json.JSONDecodeError:
            return None  # e.g. an HTML error page; treat as transient
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(stamps), encoding="utf-8")
    return stamps


def scrape_wayback(route, roster, seen):
    total_added = 0
    wbdir = DATA / "raw" / "wayback"
    wbdir.mkdir(parents=True, exist_ok=True)

    failures = 0
    with OBS_FILE.open("a", encoding="utf-8") as out_f:
        for i, train in enumerate(sorted(roster), 1):
            stamps = wayback_snapshots(train)
            if stamps is None:
                failures += 1
                print(f"  [{i}/{len(roster)}] train {train}: CDX lookup failed "
                      "(rate-limited?), will retry on a future run")
                if failures >= 3:
                    print("  3 consecutive failures -- archive.org is unhappy; "
                          "aborting the wayback pass. Re-run --wayback later; "
                          "it resumes from cache.")
                    break
                continue
            failures = 0
            print(f"  [{i}/{len(roster)}] train {train}: {len(stamps)} snapshots")
            for ts in stamps:
                snapfile = wbdir / f"{train}-{ts}.html"
                if snapfile.exists():
                    text = snapfile.read_text(encoding="utf-8")
                else:
                    # id_ serves the original page bytes without archive toolbar
                    text = fetch(f"https://web.archive.org/web/{ts}id_/{BASE}/trains/{train}/",
                                 throttle=THROTTLE_WAYBACK)
                    if text is None:
                        continue
                    snapfile.write_text(text, encoding="utf-8")
                snap_dt = dt.datetime.strptime(ts[:8], "%Y%m%d")
                parsed = parse_train_page(text, snap_dt)
                if parsed is None or parsed["route"] != route:
                    continue
                added = append_parsed(parsed, seen, f"wayback:{ts}", out_f)
                total_added += added
                if added:
                    print(f"      {ts[:8]}: {added} new points ({parsed['run_date']})")
    return total_added


# --- reparse ----------------------------------------------------------------

def reparse_raw():
    """Rebuild observations.jsonl from the saved raw HTML snapshots.

    Lets parser fixes be re-applied to everything already fetched, without
    touching the network. The date anchor comes from the snapshot's own
    provenance: the scrape-day directory for live pages, the archive.org
    timestamp for wayback pages.
    """
    files = sorted((DATA / "raw").rglob("*.html"))
    if not files:
        sys.exit("no raw snapshots under data/raw to reparse")
    tmp = OBS_FILE.with_suffix(".jsonl.tmp")
    seen = set()
    total = 0
    with tmp.open("w", encoding="utf-8") as out_f:
        for f in files:
            rel = f.relative_to(DATA / "raw")
            if rel.parts[0] == "wayback":
                if "-" not in f.stem:
                    continue
                ts = f.stem.split("-", 1)[1]
                anchor = dt.datetime.strptime(ts[:8], "%Y%m%d")
                src = f"wayback:{ts}"
            else:
                anchor = dt.datetime.fromisoformat(rel.parts[0])
                src = "live"
            parsed = parse_train_page(f.read_text(encoding="utf-8"), anchor)
            if parsed is None:
                continue
            total += append_parsed(parsed, seen, src, out_f)
    tmp.replace(OBS_FILE)
    print(f"Reparsed {len(files)} snapshots -> {total} observations in {OBS_FILE}")


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--route", default="AcelaExpress",
                    help="RailRat route slug, e.g. AcelaExpress (default), NortheastRegional")
    ap.add_argument("--trains", default="",
                    help="comma-separated train numbers to add to the roster")
    ap.add_argument("--wayback", action="store_true",
                    help="also harvest archive.org snapshots of the roster's train pages")
    ap.add_argument("--reparse", action="store_true",
                    help="rebuild observations.jsonl from saved raw HTML (no network)")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    if args.reparse:
        reparse_raw()
        return
    seen = load_seen()
    print(f"{len(seen)} observations already on file")

    roster = load_roster(args.route)
    roster |= {int(t) for t in args.trains.split(",") if t.strip().isdigit()}
    roster = update_roster_from_route_page(args.route, roster)
    if not roster:
        sys.exit("empty roster: route page gave no train links and none were seeded")
    save_roster(args.route, roster)

    print(f"Scraping {len(roster)} live train pages...")
    added = scrape_live(args.route, roster, seen)
    print(f"Live scrape: {added} new observations")

    if args.wayback:
        print("Wayback backfill (slow, archive.org rate limits)...")
        wb_added = scrape_wayback(args.route, roster, seen)
        print(f"Wayback backfill: {wb_added} new observations")

    total = sum(1 for _ in OBS_FILE.open(encoding="utf-8")) if OBS_FILE.exists() else 0
    print(f"Done. {total} total observations in {OBS_FILE}")


if __name__ == "__main__":
    main()
