#!/usr/bin/env python3
"""Scrape RailRat.net train pages for GPS position/speed reports.

RailRat embeds every position report of a train's current (or most recently
completed) run directly in the train page HTML, as Leaflet marker calls:

    L.circleMarker([40.324250,-74.613710],blueCircle).addTo(mymap)
      .bindPopup("<b>Acela Express 2151</b><small><br>09:44 39 mi NE of PHL, 158&nbsp;mph&nbsp;SW</small>");

Each page also carries a "Progress Tracker": per-station actual arrival and
departure times with delay-vs-schedule.

This script fetches every train page for a route and accumulates the parsed
position reports into data/observations.jsonl and the station timings into
data/station_events.jsonl. Ingest is lossless -- every parsed value is
stored; plausibility filtering (GPS glitches etc.) happens at build time.
RailRat only keeps the latest run per train, so run this whenever you like --
each run merges in whatever is new and skips records already on file. With
--wayback it also harvests whatever historical snapshots of the train pages
the Internet Archive happens to have.

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

DATA = Path(__file__).parent / "data"
OBS_FILE = DATA / "observations.jsonl"
STN_FILE = DATA / "station_events.jsonl"

# RailRat's train pages sometimes self-declare a route slug that differs from
# the one its route index uses (Keystone train pages link /routes/Keystone/,
# which 404s; the route page lives at /routes/KeystoneService/; 2020-era
# Michigan pages say /routes/MichiganServices/). Canonicalize to the
# route-index slug so the mismatch check doesn't reject real trains and
# historical runs don't get filed under a slug nothing else recognizes.
ROUTE_ALIASES = {
    "Keystone": "KeystoneService",
    "MichiganServices": "WolverineMichiganService",
}

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

# Progress Tracker: one <li> per station. Current pages state a delay for the
# departure and tuck the bare arrival time into a viewport span; 2020-era
# pages have one bold verb per row ("departed"/"arrived"/"completed") with an
# "ET" suffix. Delays may be color-wrapped (<span class="yellow">...) and
# "est." rows are predictions, not events -- none of the actual-event
# patterns below can match them ("arrival"/"departure" vs "arrived"/
# "departed", no bold verb).
TRACKER_RE = re.compile(r'<div id="train_progress">.*?</ol>', re.S)
TRACKER_LI_RE = re.compile(
    r'<li><a href="/stations/([A-Z0-9]+)/" title="([^"]*)">[^<]*</a>,\s*([^\n]*)')
_DELAY = r"(?:<span[^>]*>)?(on time|\d+\s*min\.\s*(?:late|early))"
DEP_RE = re.compile(r"<b>departed</b>\s*(\d{1,2}):(\d{2})(?:\s*ET)?,\s*" + _DELAY)
ARR_BOLD_RE = re.compile(r"<b>arrived</b>\s*(\d{1,2}):(\d{2})(?:\s*ET)?,\s*" + _DELAY)
ARR_PLAIN_RE = re.compile(r",\s*arrived\s*(\d{1,2}):(\d{2})")
COMPLETED_RE = re.compile(r"<b>completed</b>,?\s*(\d{1,2}):(\d{2})")


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


def parse_delay(s):
    """'on time' -> 0, 'N min. late' -> +N, 'N min. early' -> -N."""
    if s == "on time":
        return 0
    n = int(re.match(r"\d+", s).group(0))
    return -n if "early" in s else n


def parse_station_entries(text):
    """Extract per-station actual events (clock times only, dates come later).

    Returns tracker-order dicts: station, name, arr_hm/dep_hm ((hh, mm) or
    None), arr_delay/dep_delay (minutes or None). Rows with only "est."
    predictions are skipped; a "completed" row is the destination arrival.
    """
    m = TRACKER_RE.search(text)
    if not m:
        return []
    entries = []
    for code, name, body in TRACKER_LI_RE.findall(m.group(0)):
        e = {"station": code, "name": name.strip(),
             "arr_hm": None, "arr_delay": None, "dep_hm": None, "dep_delay": None}
        d = DEP_RE.search(body)
        if d:
            e["dep_hm"] = (int(d.group(1)), int(d.group(2)))
            e["dep_delay"] = parse_delay(d.group(3))
        a = ARR_BOLD_RE.search(body)
        if a:
            e["arr_hm"] = (int(a.group(1)), int(a.group(2)))
            e["arr_delay"] = parse_delay(a.group(3))
        else:
            a = ARR_PLAIN_RE.search(body) or COMPLETED_RE.search(body)
            if a:
                e["arr_hm"] = (int(a.group(1)), int(a.group(2)))
        if e["arr_hm"] or e["dep_hm"]:
            entries.append(e)
    return entries


def walk_dates_backward(items, anchor_date, anchor_minutes):
    """Assign a date to each (hh, mm) item, walking newest -> oldest.

    Times on the page are clock-only; walking into the past from the anchor,
    a small clock increase is out-of-order jitter but a large jump (>12h)
    means we crossed midnight, so decrement the date. Yields ISO timestamps
    in input order. Assumes consecutive items are less than ~12h apart.
    """
    date, prev_minutes = anchor_date, anchor_minutes
    out = []
    for hh, mm in items:
        minutes = hh * 60 + mm
        if minutes > prev_minutes + 12 * 60:
            date = date - dt.timedelta(days=1)
            prev_minutes = minutes
        elif minutes < prev_minutes:
            prev_minutes = minutes
        out.append(f"{date.isoformat()}T{hh:02d}:{mm:02d}:00")
    return out


def parse_train_page(text, now):
    """Parse one train page. Returns dict or None if page has no usable data.

    Position points come out newest-first (document order); station events
    oldest-first (tracker order). Both carry full timestamps, dated by
    walking backward from the page's "updated HH:MM on MM/DD" stamp.
    """
    m = H1_RE.search(text)
    if not m:
        return None
    route_slug, route_name, train = m.group(1), m.group(2).strip(), int(m.group(3))
    route_slug = ROUTE_ALIASES.get(route_slug, route_slug)

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

    # Points are newest-first already; anchor at the newest point itself (the
    # page updates when a report arrives, so it coincides with the stamp).
    points = []
    if raw_pts:
        stamps = walk_dates_backward(
            [(p["hh"], p["mm"]) for p in raw_pts],
            upd_date, raw_pts[0]["hh"] * 60 + raw_pts[0]["mm"])
        points = [{
            "ts": ts, "lat": p["lat"], "lon": p["lon"],
            "mph": p["mph"], "heading": p["heading"], "desc": p["desc"],
        } for p, ts in zip(raw_pts, stamps)]

    # Station events are oldest-first and can trail the newest position by
    # hours, so anchor at the updated stamp and walk them reversed.
    entries = parse_station_entries(text)
    flat = [(e, kind) for e in entries
            for kind in ("arr", "dep") if e[kind + "_hm"]]
    stamps = walk_dates_backward(
        [e[kind + "_hm"] for e, kind in reversed(flat)],
        upd_date, int(u.group(1)) * 60 + int(u.group(2)))
    for (e, kind), ts in zip(reversed(flat), stamps):
        e[kind] = ts
    station_events = [{
        "station": e["station"], "name": e["name"],
        "arr": e.get("arr"), "arr_delay": e["arr_delay"],
        "dep": e.get("dep"), "dep_delay": e["dep_delay"],
    } for e in entries]

    if not points and not station_events:
        return None
    run_date = min([p["ts"] for p in points] +
                   [t for e in station_events for t in (e["arr"], e["dep"]) if t])[:10]
    return {
        "route": route_slug, "route_name": route_name, "train": train,
        "run_date": run_date, "points": points, "station_events": station_events,
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
    """Write points not seen before. Returns number added.

    Lossless: even implausible speeds are stored -- glitch filtering is
    build-time policy (see build_map.MAX_PLAUSIBLE_MPH), not data loss.
    """
    added = 0
    for p in parsed["points"]:
        key = obs_key(parsed["train"], p["ts"], p["lat"], p["lon"])
        if key in seen:
            continue
        seen.add(key)
        rec = {
            "route": parsed["route"], "train": parsed["train"],
            "run_date": parsed["run_date"], **p, "src": src,
        }
        out_f.write(json.dumps(rec) + "\n")
        added += 1
    return added


def stn_key(train, e):
    return (f'{train}|{e["station"]}|{e["arr"]}|{e["arr_delay"]}'
            f'|{e["dep"]}|{e["dep_delay"]}')


def load_seen_stations():
    seen = set()
    if STN_FILE.exists():
        with STN_FILE.open(encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seen.add(stn_key(o["train"], o))
    return seen


def append_station_events(parsed, seen, src, out_f):
    """Write station events not seen before. Returns number added.

    The key covers the full event content: as a run progresses, a station's
    record gains fields across page fetches (arrival first, departure and
    delay later) and each distinct variant is appended -- consumers merge by
    (train, run_date, station), preferring the most complete record.
    """
    added = 0
    for e in parsed["station_events"]:
        key = stn_key(parsed["train"], e)
        if key in seen:
            continue
        seen.add(key)
        rec = {"route": parsed["route"], "train": parsed["train"],
               "run_date": parsed["run_date"], **e, "src": src}
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

def scrape_live(route, roster, seen, seen_stn):
    total_added = stn_added = 0
    scrape_day = dt.date.today().isoformat()
    rawdir = DATA / "raw" / scrape_day
    rawdir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()

    with OBS_FILE.open("a", encoding="utf-8") as out_f, \
         STN_FILE.open("a", encoding="utf-8") as stn_f:
        for i, train in enumerate(sorted(roster), 1):
            text = fetch(f"{BASE}/trains/{train}/")
            if text is None:
                print(f"  [{i}/{len(roster)}] train {train}: fetch failed")
                continue
            parsed = parse_train_page(text, now)
            if parsed is None:
                print(f"  [{i}/{len(roster)}] train {train}: no usable data")
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
            s_added = append_station_events(parsed, seen_stn, "live", stn_f)
            total_added += added
            stn_added += s_added
            print(f"  [{i}/{len(roster)}] train {train}: {len(parsed['points'])} points "
                  f"({parsed['run_date']}), {added} new, {s_added} new station events")
    return total_added, stn_added


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


def scrape_wayback(route, roster, seen, seen_stn):
    total_added = stn_added = 0
    wbdir = DATA / "raw" / "wayback"
    wbdir.mkdir(parents=True, exist_ok=True)

    failures = 0
    with OBS_FILE.open("a", encoding="utf-8") as out_f, \
         STN_FILE.open("a", encoding="utf-8") as stn_f:
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
                s_added = append_station_events(parsed, seen_stn, f"wayback:{ts}", stn_f)
                total_added += added
                stn_added += s_added
                if added or s_added:
                    print(f"      {ts[:8]}: {added} new points, {s_added} new "
                          f"station events ({parsed['run_date']})")
    return total_added, stn_added


# --- reparse ----------------------------------------------------------------

def reparse_raw():
    """Rebuild both datasets from the saved raw HTML snapshots.

    Lets parser fixes be re-applied to everything already fetched, without
    touching the network. The date anchor comes from the snapshot's own
    provenance: the scrape-day directory for live pages, the archive.org
    timestamp for wayback pages.
    """
    files = sorted((DATA / "raw").rglob("*.html"))
    if not files:
        sys.exit("no raw snapshots under data/raw to reparse")
    tmp = OBS_FILE.with_suffix(".jsonl.tmp")
    stn_tmp = STN_FILE.with_suffix(".jsonl.tmp")
    seen, seen_stn = set(), set()
    total = stn_total = 0
    with tmp.open("w", encoding="utf-8") as out_f, \
         stn_tmp.open("w", encoding="utf-8") as stn_f:
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
            stn_total += append_station_events(parsed, seen_stn, src, stn_f)
    tmp.replace(OBS_FILE)
    stn_tmp.replace(STN_FILE)
    print(f"Reparsed {len(files)} snapshots -> {total} observations, "
          f"{stn_total} station events")


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
    args.route = ROUTE_ALIASES.get(args.route, args.route)
    seen = load_seen()
    seen_stn = load_seen_stations()
    print(f"{len(seen)} observations, {len(seen_stn)} station events already on file")

    roster = load_roster(args.route)
    roster |= {int(t) for t in args.trains.split(",") if t.strip().isdigit()}
    roster = update_roster_from_route_page(args.route, roster)
    if not roster:
        sys.exit("empty roster: route page gave no train links and none were seeded")
    save_roster(args.route, roster)

    print(f"Scraping {len(roster)} live train pages...")
    added, stn_added = scrape_live(args.route, roster, seen, seen_stn)
    print(f"Live scrape: {added} new observations, {stn_added} new station events")

    if args.wayback:
        print("Wayback backfill (slow, archive.org rate limits)...")
        wb_added, wb_stn = scrape_wayback(args.route, roster, seen, seen_stn)
        print(f"Wayback backfill: {wb_added} new observations, {wb_stn} new station events")

    total = sum(1 for _ in OBS_FILE.open(encoding="utf-8")) if OBS_FILE.exists() else 0
    stn_total = sum(1 for _ in STN_FILE.open(encoding="utf-8")) if STN_FILE.exists() else 0
    print(f"Done. {total} observations, {stn_total} station events on file")


if __name__ == "__main__":
    main()
