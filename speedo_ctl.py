#!/usr/bin/env python3
"""Manage the speedo pipeline: dataset status, queued updates, map builds.

The manager discovers which routes exist by scanning data/geometry/ (one
cached .geojson per route), reports per-route dataset health, and runs
queued live/wayback scrapes and map builds. Jobs run strictly one at a
time -- the scrapers' politeness throttles are per-process, so a parallel
queue would multiply the request rate.

Usage:
    python speedo_ctl.py                              # status table
    python speedo_ctl.py --live-update RouteA RouteB  # queued live scrapes
    python speedo_ctl.py --full-update RouteA RouteB  # live + wayback backfill
    python speedo_ctl.py --full-update all            # ...for every route on disk
    python speedo_ctl.py --make-map RouteA RouteB     # build maps for routes
    python speedo_ctl.py --make-map all               # build every route with data

The literal token ``all`` stands in for every route discovered under
data/geometry/, and works anywhere a route list is accepted (--live-update,
--full-update, --make-map).
"""

import argparse
import contextlib
import io
import json
from pathlib import Path

import build_map
import scrape_railrat
from build_map import (
    BIN_MILES, MAX_PLAUSIBLE_MPH, MIN_PLAUSIBLE_MPH, OFFROUTE_MILES, ROUTES,
    SIMPLIFY_MILES, SegmentIndex, build_bins, canonical_route, geojson_parts,
    simplify, stitch,
)

HERE = Path(__file__).parent
DATA = HERE / "data"
GEOMETRY = DATA / "geometry"
OBS_FILE = DATA / "observations.jsonl"


def discover_routes(geom_dir=GEOMETRY):
    """The routes we care about = the routes with cached NTAD geometry."""
    return sorted(p.stem for p in geom_dir.glob("*.geojson"))


def collect_route_stats(obs_path, routes):
    """One pass over observations.jsonl -> the status table's numbers.

    `points` counts every stored observation (the dataset is lossless);
    `plausible` keeps only the (lat, lon) of points inside the build-time
    plausibility band, because coverage should describe what the map will
    actually paint. Routes with no observations still get a zeroed entry.
    """
    stats = {r: {"points": 0, "trains": 0, "latest": None, "wayback": False,
                 "plausible": []} for r in routes}
    trains = {r: set() for r in routes}
    if obs_path.exists():
        with obs_path.open(encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                s = stats.get(o["route"])
                if s is None:
                    continue
                s["points"] += 1
                trains[o["route"]].add(o["train"])
                if s["latest"] is None or o["ts"] > s["latest"]:
                    s["latest"] = o["ts"]
                if o["src"].startswith("wayback:"):
                    s["wayback"] = True
                if MIN_PLAUSIBLE_MPH <= o["mph"] <= MAX_PLAUSIBLE_MPH:
                    s["plausible"].append((o["lat"], o["lon"]))
    for r in routes:
        stats[r]["trains"] = len(trains[r])
    return stats


def coverage(parts, mile0, points):
    """(bins with >=1 on-route point, total bins) for one route's line.

    Builds the same spine and half-mile bins as build_map.build and
    projects observations the same way, so the status percentage predicts
    the map: covered bins draw colored, the rest draw the gray no-data
    dash.
    """
    sections = [simplify(c, SIMPLIFY_MILES) for c in stitch(parts, mile0)]
    bins_pts, _labels, segs, _total = build_bins(sections)
    index = SegmentIndex(segs, OFFROUTE_MILES)
    hit = set()
    for p in points:
        d, si, t = index.nearest(p)
        if si is None or d > OFFROUTE_MILES:
            continue
        _a, _b, seg_mi, bin_base, mile_at_a, last_bin = segs[si]
        hit.add(min(bin_base + int((mile_at_a + t * seg_mi) / BIN_MILES), last_bin))
    return len(hit), len(bins_pts)


def format_status_table(rows):
    """Render status rows as an aligned text table ('-' = not applicable)."""
    header = ["Route", "Points", "Trains", "Coverage", "Latest point", "Wayback"]
    table = [header]
    for r in rows:
        cov = r["coverage"]
        table.append([
            r["name"],
            f"{r['points']:,}",
            f"{r['trains']:,}",
            f"{100 * cov[0] / cov[1]:.1f}%" if cov and cov[1] else "-",
            r["latest"][:16].replace("T", " ") if r["latest"] else "-",
            "yes" if r["wayback"] else "no",
        ])
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    right = {0, 1, 2, 3}  # route names + numeric columns; names read best flush
    return "\n".join(
        "  ".join(c.rjust(widths[i]) if i in right else c.ljust(widths[i])
                  for i, c in enumerate(row)).rstrip()
        for row in table)


def normalize_routes(names, geom_dir=GEOMETRY):
    """Canonicalize CLI route args and drop repeats, preserving order.

    The literal token ``all`` (case-insensitive) expands to every route
    discovered under the geometry folder, so ``--full-update all`` fans the
    command out across the whole cache without naming each slug. ``all``
    must be intercepted here: ``canonical_route`` would reject it as an
    unknown route. It wins over any slugs listed beside it.
    """
    if any(n.lower() == "all" for n in names):
        return discover_routes(geom_dir)
    out = []
    for n in names:
        slug = canonical_route(n)
        if slug not in out:
            out.append(slug)
    return out


def plan_jobs(routes, wayback):
    """Queue order for an update: every live scrape first (fast, freshest
    data), then the slow wayback passes."""
    jobs = [("live", r) for r in routes]
    if wayback:
        jobs += [("wayback", r) for r in routes]
    return jobs


# --- commands (thin orchestration over the pieces above) --------------------

def cmd_status():
    routes = discover_routes()
    if not routes:
        raise SystemExit(f"no route geometry found under {GEOMETRY}")
    stats = collect_route_stats(OBS_FILE, routes)
    rows = []
    for r in routes:
        s = stats[r]
        cov = None
        if s["plausible"]:
            parts = geojson_parts(json.loads(
                (GEOMETRY / f"{r}.geojson").read_text(encoding="utf-8")))
            with contextlib.redirect_stdout(io.StringIO()):  # mute stitch notes
                cov = coverage(parts, ROUTES.get(r, {}).get("mile0"), s["plausible"])
        # RailRat slug, not the display name: rows copy-paste straight into
        # --full-update / --make-map arguments.
        rows.append({"name": r, "points": s["points"], "trains": s["trains"],
                     "coverage": cov, "latest": s["latest"], "wayback": s["wayback"]})
    print(format_status_table(rows))


def cmd_update(routes, wayback):
    jobs = plan_jobs(routes, wayback)
    seen = scrape_railrat.load_seen()
    seen_stn = scrape_railrat.load_seen_stations()
    print(f"{len(seen)} observations, {len(seen_stn)} station events already on file")
    skip_wayback = False
    for i, (kind, route) in enumerate(jobs, 1):
        print(f"\n=== [{i}/{len(jobs)}] {kind} update: {route} ===")
        roster = scrape_railrat.load_roster(route)
        if kind == "live":
            roster = scrape_railrat.update_roster_from_route_page(route, roster)
            if not roster:
                print("  empty roster (route page gave no train links); skipping")
                continue
            scrape_railrat.save_roster(route, roster)
            added, stn = scrape_railrat.scrape_live(route, roster, seen, seen_stn)
            print(f"  live: {added} new observations, {stn} new station events")
        else:
            if skip_wayback:
                print("  skipped: archive.org aborted an earlier wayback pass; "
                      "re-run later to resume")
                continue
            if not roster:
                print("  no roster on file; run a live update first")
                continue
            added, stn, aborted = scrape_railrat.scrape_wayback(
                route, roster, seen, seen_stn)
            print(f"  wayback: {added} new observations, {stn} new station events")
            if aborted:
                skip_wayback = True


def cmd_make_maps(routes):
    stats = collect_route_stats(OBS_FILE, routes)
    for route in routes:
        print(f"\n=== map: {route} ===")
        if route not in ROUTES:
            print("  no ROUTES entry in build_map.py; add one to build this map")
            continue
        if not stats[route]["plausible"]:
            print("  no usable observations; skipping (run an update first)")
            continue
        build_map.build(route)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--full-update", nargs="+", metavar="ROUTE",
                   help="queue a live scrape, then a wayback backfill, for each "
                        "route ('all' = every route under data/geometry/)")
    g.add_argument("--live-update", nargs="+", metavar="ROUTE",
                   help="queue a live scrape for each route "
                        "('all' = every route under data/geometry/)")
    g.add_argument("--make-map", nargs="+", metavar="ROUTE",
                   help="build the speed map(s) for the given routes "
                        "('all' = every route under data/geometry/)")
    args = ap.parse_args()

    if args.full_update:
        cmd_update(normalize_routes(args.full_update), wayback=True)
    elif args.live_update:
        cmd_update(normalize_routes(args.live_update), wayback=False)
    elif args.make_map:
        cmd_make_maps(normalize_routes(args.make_map))
    else:
        cmd_status()


if __name__ == "__main__":
    main()
