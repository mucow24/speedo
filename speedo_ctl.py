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
    python speedo_ctl.py --make-index                 # write out/index.html landing page

The literal token ``all`` stands in for every route discovered under
data/geometry/, and works anywhere a route list is accepted (--live-update,
--full-update, --make-map).
"""

import argparse
import contextlib
import html
import io
import json
import re
from pathlib import Path

import build_map
import scrape_railrat
from build_map import (
    BIN_MILES, COLOR_ANCHORS, MAX_MPH, MAX_PLAUSIBLE_MPH, MIN_PLAUSIBLE_MPH,
    OFFROUTE_MILES, ROUTES, SIMPLIFY_MILES, SegmentIndex, build_bins,
    canonical_route, geojson_parts, simplify, speed_color, stitch,
)

HERE = Path(__file__).parent
DATA = HERE / "data"
GEOMETRY = DATA / "geometry"
OBS_FILE = DATA / "observations.jsonl"
OUT = HERE / "out"


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


# --- maps landing page ------------------------------------------------------
# --make-index reads back the CFG blob each generated map already embeds and
# renders a static out/index.html linking to them. The maps are the source of
# truth for their own stats; the index recomputes nothing.

_CFG_RE = re.compile(r"const CFG = (.+);")


def discover_maps(out_dir=OUT):
    """Route slug -> Path for each `speed_map_<slug>.html` in out/.

    One entry per route. Anything else in out/ (a prior index.html, strays)
    is ignored. Ordered by slug, matching the status table.
    """
    prefix = "speed_map_"
    return {p.stem[len(prefix):]: p
            for p in sorted(out_dir.glob(f"{prefix}*.html"))}


def extract_config(html_text):
    """Pull the CFG JSON blob a generated map embeds (`const CFG = ...;`).

    Each map carries all its own numbers in that one blob, so the index
    reads it back rather than recomputing anything. Returns the parsed dict,
    or None if the file has no recognizable CFG line (not one of our maps).
    """
    m = _CFG_RE.search(html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def map_summary(cfg):
    """Headline stats for one map's card, from its embedded CFG.

    Top speed is the fastest bin max -- the maps color by max-per-bin, so the
    landing page headlines the same number; avg is the mean of the covered
    bins' maxes; coverage is covered bins / total bins. A map with no covered
    bins yields top/avg = None (the renderer shows a dash).
    """
    bins = cfg.get("bins", [])
    maxes = [b["mx"] for b in bins if "mx" in b]
    st = cfg.get("stats", {})
    return {
        "display": cfg.get("display") or cfg.get("title", ""),
        "miles": cfg.get("totalMiles"),
        "top": max(maxes) if maxes else None,
        "avg": round(sum(maxes) / len(maxes)) if maxes else None,
        "covered": len(maxes),
        "bins": len(bins),
        "obs": st.get("obs"),
        "runs": st.get("runs"),
        "from": st.get("from"),
        "to": st.get("to"),
        "built": st.get("built"),
    }


# Coverage tiers for the landing page, highest first. Each (label, low) claims
# the routes whose coverage percent is >= `low` and below the previous tier's
# floor, so the bands are [90, 100], [75, 90), [50, 75), [0, 50).
COVERAGE_SECTIONS = [
    ("Fully covered", 90.0),
    ("Covered", 75.0),
    ("Poorly covered", 50.0),
    ("Very poorly covered", 0.0),
]


def coverage_pct(summary):
    """A map's mapped-bin percentage, or 0.0 when it has no bins.

    Same ratio the card shows (covered bins / total bins); a binless map has
    no defined coverage, so it reads as 0% and sinks to the lowest tier
    rather than dividing by zero.
    """
    return 100.0 * summary["covered"] / summary["bins"] if summary["bins"] else 0.0


def group_by_coverage(summaries):
    """Bucket summaries into the labelled coverage tiers for the index.

    Returns ``[(label, [summary, ...]), ...]`` in descending-coverage order,
    each tier's members sorted alphabetically by display name (case-insensitive),
    and empty tiers omitted (no divider without cards under it).
    """
    buckets = {label: [] for label, _low in COVERAGE_SECTIONS}
    for s in summaries:
        pct = coverage_pct(s)
        for label, low in COVERAGE_SECTIONS:  # highest floor first: first match wins
            if pct >= low:
                buckets[label].append(s)
                break
    groups = []
    for label, _low in COVERAGE_SECTIONS:
        members = sorted(buckets[label], key=lambda s: s["display"].lower())
        if members:
            groups.append((label, members))
    return groups


def _hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _gradient_css():
    """The legend's speed gradient as a CSS linear-gradient, stops placed at
    each color anchor's fraction of the scale -- identical to the map legend."""
    stops = ", ".join(f"{_hex(c)} {v / MAX_MPH * 100:.0f}%" for v, c in COLOR_ANCHORS)
    return f"linear-gradient(to right, {stops})"


def _scale_ticks():
    return "".join(f"<span>{v}</span>" for v, _c in COLOR_ANCHORS)


def _num(v, fmt="{}"):
    """Format a stat, or an em dash when it's missing (e.g. an empty map)."""
    return fmt.format(v) if v is not None else "&mdash;"


def _card_html(s):
    # The corner stacks both speeds, each tinted by its own value: the max on
    # top (and driving the card's left-border accent), the average below.
    accent = speed_color(s["top"]) if s["top"] is not None else "#7a7f87"
    avg_color = speed_color(s["avg"]) if s["avg"] is not None else "#7a7f87"
    pct = f"{100 * s['covered'] / s['bins']:.0f}%" if s["bins"] else "&mdash;"
    span = (f"{s['from']} &ndash; {s['to']}"
            if s.get("from") and s.get("to") else "")
    meta_bits = []
    if s.get("obs") is not None:
        meta_bits.append(f"{s['obs']:,} obs")
    if s.get("runs") is not None:
        meta_bits.append(f"{s['runs']} runs")
    if span:
        meta_bits.append(span)
    return f"""      <li class="card" style="--accent:{accent}">
        <a class="card-link" href="{html.escape(s["leaflet"])}">
          <div class="card-head">
            <span class="route">{html.escape(s["display"])}</span>
            <span class="speeds">
              <span class="top" style="color:{accent}">{_num(s["top"])}<span class="unit">mph max</span></span>
              <span class="avg" style="color:{avg_color}">{_num(s["avg"])}<span class="unit">mph avg</span></span>
            </span>
          </div>
          <div class="stats">
            <span><b>{_num(s["miles"], "{:.1f}")}</b> mi</span>
            <span><b>{pct}</b> mapped</span>
          </div>
          <div class="meta">{" &middot; ".join(meta_bits)}</div>
        </a>
      </li>"""


def _section_html(label, members):
    """One coverage tier: a labelled horizontal divider above its card grid.

    `label` is a fixed COVERAGE_SECTIONS constant (never user data), so it
    goes into the markup unescaped.
    """
    cards = "\n".join(_card_html(s) for s in members)
    return (f'    <section class="section">\n'
            f'      <div class="section-head"><span class="section-label">{label}</span></div>\n'
            f'      <ul class="maps">\n{cards}\n      </ul>\n'
            f'    </section>')


def render_index(summaries):
    """Render the full out/index.html landing page for the given map summaries.

    Self-contained HTML (no external assets), themed to match the maps: the
    CARTO-dark background, the #232323 popup cards, the shared speed gradient,
    and the gold accent from the popups.
    """
    if summaries:
        body = "\n".join(_section_html(label, members)
                         for label, members in group_by_coverage(summaries))
    else:
        body = ('    <div class="empty">No maps built yet.<br>Run '
                '<code>python speedo_ctl.py --make-map all</code> to build some, '
                'then re-run <code>--make-index</code>.</div>')

    n = len(summaries)
    total_mi = sum(s["miles"] for s in summaries
                   if isinstance(s.get("miles"), (int, float)))
    total_obs = sum(s["obs"] for s in summaries if isinstance(s.get("obs"), int))
    built = next((s["built"] for s in summaries if s.get("built")), None)
    foot = [f"{n} route" + ("" if n == 1 else "s")]
    if total_mi:
        foot.append(f"{total_mi:,.0f} route miles")
    if total_obs:
        foot.append(f"{total_obs:,} observations")
    if built:
        foot.append(f"built {built}")

    # Footer holds only program-generated counts/dates -- no escaping needed.
    return (PAGE_TMPL
            .replace("__GRADIENT__", _gradient_css())
            .replace("__TICKS__", _scale_ticks())
            .replace("__BODY__", body)
            .replace("__FOOTER__", " &middot; ".join(foot)))


PAGE_TMPL = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>speedo &mdash; Amtrak observed speeds</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body { background: #151515; color: #fff; padding: 0 20px 56px;
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 960px; margin: 0 auto; }
  header { padding: 44px 2px 0; }
  h1 { margin: 0; font-size: 34px; font-weight: 800; letter-spacing: -1px; }
  .tagline { color: #b9bdc4; margin: 6px 0 20px; }
  .gradient { height: 8px; border-radius: 5px; background: __GRADIENT__; }
  .scale { display: flex; justify-content: space-between; color: #7a7f87;
           font-size: 11px; margin: 4px 1px 0; }
  .section { margin-top: 30px; }
  .section-head { display: flex; align-items: center; gap: 14px; margin: 0 2px; }
  .section-head::after { content: ""; flex: 1 1 auto; height: 1px; background: #2f2f2f; }
  .section-label { color: #d7dade; font-size: 12px; font-weight: 700; white-space: nowrap;
                   text-transform: uppercase; letter-spacing: 1px; }
  .maps { list-style: none; padding: 0; margin: 14px 0 0; display: grid; gap: 14px;
          grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
  .card { background: #232323; border: 1px solid #2f2f2f;
          border-left: 4px solid var(--accent); border-radius: 14px; overflow: hidden;
          transition: transform .12s ease, border-color .12s ease; }
  .card:hover { transform: translateY(-2px); border-color: #4a4a4a; }
  .card-link { display: block; padding: 16px 18px 14px; color: inherit; text-decoration: none; }
  .card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
  .route { font-size: 18px; font-weight: 700; padding-top: 2px; }
  .speeds { display: flex; flex-direction: column; align-items: flex-end; gap: 3px;
            text-align: right; }
  .top { font-size: 30px; font-weight: 800; line-height: 1; letter-spacing: -1px;
         white-space: nowrap; }
  .avg { font-size: 19px; font-weight: 800; line-height: 1; letter-spacing: -.5px;
         white-space: nowrap; }
  .unit { font-size: 11px; font-weight: 700; color: #7a7f87; letter-spacing: 0;
          margin-left: 4px; text-transform: uppercase; }
  .stats { display: flex; flex-wrap: wrap; gap: 4px 14px; color: #b9bdc4; font-size: 13px;
           margin: 13px 0 0; }
  .stats b { color: #fff; font-weight: 700; }
  .meta { color: #7a7f87; font-size: 12px; margin-top: 9px; }
  .empty { background: #232323; border: 1px solid #2f2f2f; border-radius: 14px; padding: 44px;
           text-align: center; color: #b9bdc4; margin-top: 26px; line-height: 1.9; }
  .empty code { background: #151515; padding: 2px 6px; border-radius: 4px; color: #fff;
                font-size: 13px; }
  footer { color: #7a7f87; font-size: 12px; margin-top: 34px; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>speedo</h1>
    <p class="tagline">How fast Amtrak trains actually go, mile by mile &mdash;
      from observed position reports.</p>
    <div class="gradient"></div>
    <div class="scale">__TICKS__</div>
  </header>
  <main>
__BODY__
  </main>
  <footer>__FOOTER__</footer>
</div>
</body>
</html>
"""


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


def cmd_make_index(out_dir=OUT):
    maps = discover_maps(out_dir)
    summaries = []
    for slug, path in maps.items():
        cfg = extract_config(path.read_text(encoding="utf-8"))
        if cfg is None:
            print(f"  skip {path.name}: no CFG blob (not a speedo map?)")
            continue
        s = map_summary(cfg)
        s["slug"] = slug
        s["leaflet"] = path.name
        summaries.append(s)
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(render_index(summaries), encoding="utf-8")
    plural = "" if len(summaries) == 1 else "s"
    print(f"Wrote {out_path} ({len(summaries)} map{plural}, "
          f"{out_path.stat().st_size / 1024:.0f} KB)")


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
    g.add_argument("--make-index", action="store_true",
                   help="write out/index.html: a landing page linking the maps "
                        "currently in out/")
    args = ap.parse_args()

    if args.full_update:
        cmd_update(normalize_routes(args.full_update), wayback=True)
    elif args.live_update:
        cmd_update(normalize_routes(args.live_update), wayback=False)
    elif args.make_map:
        cmd_make_maps(normalize_routes(args.make_map))
    elif args.make_index:
        cmd_make_index()
    else:
        cmd_status()


if __name__ == "__main__":
    main()
