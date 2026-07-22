#!/usr/bin/env python3
"""Build a color-coded speed map from scraped RailRat observations.

Draws the official route line (USDOT/NTAD Amtrak Routes geometry), projects
every GPS observation onto it, slices the line into half-mile bins, and colors
each bin by the MAX speed ever observed there (so station dwell and delay
slowdowns don't mask what the track can do). Routes with branches (the
Regional's Virginia legs, the Empire Builder's Portland leg) are drawn as
several sections with mile markers running continuously across them. Output is self-contained HTML:
a Leaflet/OpenStreetMap version that works with zero setup, and a Google Maps
version that activates when you supply an API key.

Usage:
    python build_map.py                          # Acela, both engines
    python build_map.py --google-key AIza...     # bake your key into the Google version
    python build_map.py --route NortheastRegional --engine leaflet
"""

import argparse
import datetime as dt
import json
import math
import statistics
import urllib.parse
import urllib.request
from pathlib import Path

from scrape_railrat import ROUTE_ALIASES  # the one canonicalizer, shared across entry points

HERE = Path(__file__).parent
DATA = HERE / "data"
OUT = HERE / "out"

ARCGIS = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
          "NTAD_Amtrak_Routes/FeatureServer/0/query")

# RailRat route slug -> NTAD feature name, display name, and the endpoint that
# should be mile 0 (so popup mile markers read in timetable direction).
ROUTES = {
    "AcelaExpress": {"ntad": "Acela", "display": "Acela Express",
                     "mile0": (42.35194, -71.05528)},          # Boston South
    "NortheastRegional": {"ntad": "Northeast Regional", "display": "Northeast Regional",
                          "mile0": (42.35194, -71.05528)},
    "PacificSurfliner": {"ntad": "Pacific Surfliner", "display": "Pacific Surfliner",
                         "mile0": (32.71653, -117.16999)},     # San Diego
    "KeystoneService": {"ntad": "Keystone Service", "display": "Keystone Service",
                        "mile0": (40.34467, -76.41135)},       # Harrisburg-ish
    "EthanAllenExpress": {"ntad": "Ethan Allen Express", "display": "Ethan Allen Express",
                          "mile0": (40.75057, -73.99352)},     # NYC Penn
    "EmpireService": {"ntad": "Empire Service", "display": "Empire Service",
                      "mile0": (40.75057, -73.99352)},         # NYC Penn
    "WolverineMichiganService": {"ntad": "Wolverine", "display": "Wolverine (Michigan Service)",
                                 "mile0": (41.87879, -87.63937)},  # Chicago Union
    "Vermonter": {"ntad": "Vermonter", "display": "Vermonter",
                  "mile0": (38.89722, -77.00639)},             # Washington Union
    "AmtrakCascades": {"ntad": "Amtrak Cascades", "display": "Amtrak Cascades",
                       "mile0": (49.27306, -123.09806)},       # Vancouver BC Pacific Central
    "Downeaster": {"ntad": "Downeaster", "display": "Downeaster",
                   "mile0": (42.36583, -71.06167)},            # Boston North
    "EmpireBuilder": {"ntad": "Empire Builder", "display": "Empire Builder",
                      "mile0": (41.87879, -87.63937)},         # Chicago Union
    "SouthwestChief": {"ntad": "Southwest Chief", "display": "Southwest Chief",
                       "mile0": (41.87879, -87.63937)},        # Chicago Union
    # --- Added from the RailRat x NTAD geometry audit: routes RailRat serves
    # that also have NTAD geometry. mile0 is the timetable-origin terminal (the
    # end popup mile markers count up from). Two non-obvious cases: Adirondack
    # counts up from Montreal (Amtrak's own mile column reads 0 at Montreal to
    # 381 at NYP), and TexasEagle's Chicago-San Antonio spine is a separate
    # NTAD section, so mile 0 falls at San Antonio on the longest (SAS-LAX)
    # section. Piedmont rides Carolinian's Raleigh-Charlotte track (one line).
    "Adirondack": {"ntad": "Adirondack", "display": "Adirondack",
                   "mile0": (45.50018, -73.56662)},  # Montreal
    "Borealis": {"ntad": "Borealis", "display": "Borealis",
                 "mile0": (41.87879, -87.63937)},  # Chicago Union
    "CaliforniaZephyr": {"ntad": "California Zephyr", "display": "California Zephyr",
                         "mile0": (41.87879, -87.63937)},  # Chicago Union
    "CapitolCorridor": {"ntad": "Capitol Corridor", "display": "Capitol Corridor",
                        "mile0": (38.90299, -121.08312)},  # Auburn CA
    "Cardinal": {"ntad": "Cardinal", "display": "Cardinal",
                 "mile0": (40.75057, -73.99352)},  # NYC Penn
    "CityofNewOrleans": {"ntad": "City Of New Orleans", "display": "City of New Orleans",
                         "mile0": (41.87879, -87.63937)},  # Chicago Union
    "CoastStarlight": {"ntad": "Coast Starlight", "display": "Coast Starlight",
                       "mile0": (47.59848, -122.32928)},  # Seattle King St
    "Crescent": {"ntad": "Crescent", "display": "Crescent",
                 "mile0": (40.75057, -73.99352)},  # NYC Penn
    "Floridian": {"ntad": "Floridian", "display": "Floridian",
                  "mile0": (41.87879, -87.63937)},  # Chicago Union
    "HeartlandFlyer": {"ntad": "Heartland Flyer", "display": "Heartland Flyer",
                       "mile0": (32.75267, -97.32507)},  # Fort Worth
    "Hiawatha": {"ntad": "Hiawatha Service", "display": "Hiawatha",
                 "mile0": (41.87879, -87.63937)},  # Chicago Union
    "LakeShoreLimited": {"ntad": "Lake Shore Limited", "display": "Lake Shore Limited",
                         "mile0": (40.75057, -73.99352)},  # NYC Penn
    "MapleLeaf": {"ntad": "Maple Leaf", "display": "Maple Leaf",
                  "mile0": (40.75057, -73.99352)},  # NYC Penn
    "MissouriRiverRunner": {"ntad": "Missouri River Runner", "display": "Missouri River Runner",
                            "mile0": (38.62306, -90.20333)},  # St. Louis
    "Palmetto": {"ntad": "Palmetto", "display": "Palmetto",
                 "mile0": (40.75057, -73.99352)},  # NYC Penn
    "Pennsylvanian": {"ntad": "Pennsylvanian", "display": "Pennsylvanian",
                      "mile0": (40.75057, -73.99352)},  # NYC Penn
    "SilverMeteor": {"ntad": "Silver Meteor", "display": "Silver Meteor",
                     "mile0": (40.75057, -73.99352)},  # NYC Penn
    "SunsetLimited": {"ntad": "Sunset Limited", "display": "Sunset Limited",
                      "mile0": (29.96012, -90.09668)},  # New Orleans
    "TexasEagle": {"ntad": "Texas Eagle", "display": "Texas Eagle",
                   "mile0": (29.43517, -98.44361)},  # San Antonio
    "BlueWaterMichiganService": {"ntad": "Blue Water", "display": "Blue Water (Michigan Service)",
                                 "mile0": (41.87879, -87.63937)},  # Chicago Union
    "SalukiIllinoisService": {"ntad": "Saluki", "display": "Saluki (Illinois Service)",
                              "mile0": (41.87879, -87.63937)},  # Chicago Union
    "LincolnServiceIllinoisService": {"ntad": "Lincoln Service", "display": "Lincoln Service",
                                      "mile0": (41.87879, -87.63937)},  # Chicago Union
    "CarolinianPiedmont": {"ntad": "Carolinian", "display": "Carolinian / Piedmont",
                           "mile0": (40.75057, -73.99352)},  # NYC Penn
    "LincolnServiceMissouriRiverRunner": {"ntad": "Lincol Service Missouri River Runner",
                                          "display": "Lincoln Service / Missouri River Runner",
                                          "mile0": (41.87879, -87.63937)},  # Chicago Union
}


def canonical_route(route):
    """Resolve a --route argument to its canonical RailRat slug.

    Route identity is the RailRat slug. Normalize spacing (so the display
    name "Empire Builder" collapses to the slug "EmpireBuilder") and apply
    ROUTE_ALIASES (so "Keystone" -> "KeystoneService"), then require the
    result to be a known ROUTES key. Without this, a spaced or aliased name
    fell through ROUTES.get()'s default and fetched NTAD geometry into a
    parallel, non-canonical cache file -- a silent byte-for-byte duplicate --
    while matching zero observations, which are stored under the slug.
    """
    slug = "".join(route.split())
    slug = ROUTE_ALIASES.get(slug, slug)
    if slug not in ROUTES:
        known = ", ".join(sorted(ROUTES))
        raise SystemExit(f"unknown route {route!r} (resolved to {slug!r}); "
                         f"known routes: {known}")
    return slug


BIN_MILES = 0.5
OUTLIER_RATIO = 1.7      # a lone point is an outlier when both neighbors beat it by this
OFFROUTE_MILES = 2.0     # drop observations farther than this from the line
MAX_MPH = 160            # top of the color scale
MAX_PLAUSIBLE_MPH = 170  # above this is a GPS glitch; filtered here at build
                         # time -- ingest (scrape_railrat) stores everything
MIN_PLAUSIBLE_MPH = 10   # below this is a stopped/stuck train (station dwell,
                         # held signal), not a track speed limit; filtered here
                         # at build time too. Overridable via --min-mph.
SIMPLIFY_MILES = 0.015   # ~25 m Douglas-Peucker tolerance
MIN_SECTION_MILES = 5.0  # stitched leftovers shorter than this are scraps, not track
DUP_TOL_MILES = 0.15     # a part everywhere this close to a longer one is a duplicate

COLOR_ANCHORS = [(0, (220, 30, 30)), (40, (255, 140, 0)), (80, (255, 215, 0)),
                 (120, (40, 180, 70)), (160, (30, 60, 255))]

MI_PER_DEG_LAT = 69.05


# --- geometry helpers (lat/lon in degrees, distances in miles) --------------

def dist_mi(a, b):
    ky = MI_PER_DEG_LAT
    kx = MI_PER_DEG_LAT * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot((a[0] - b[0]) * ky, (a[1] - b[1]) * kx)


def project_to_segment(p, a, b):
    """Distance (mi) from p to segment ab and fraction t along it."""
    ky = MI_PER_DEG_LAT
    kx = MI_PER_DEG_LAT * math.cos(math.radians(a[0]))
    px, py = (p[1] - a[1]) * kx, (p[0] - a[0]) * ky
    bx, by = (b[1] - a[1]) * kx, (b[0] - a[0]) * ky
    seg2 = bx * bx + by * by
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / seg2))
    dx, dy = px - t * bx, py - t * by
    return math.hypot(dx, dy), t


def fetch_route_geometry(route):
    """Download (and cache) the NTAD line for the route as a list of parts.

    `route` must already be a canonical slug (see `canonical_route`); callers
    funnel through `build`, which canonicalizes before anything touches the
    per-route cache filename.
    """
    cfg = ROUTES[route]
    gdir = DATA / "geometry"
    gdir.mkdir(parents=True, exist_ok=True)
    cache = gdir / f"{route}.geojson"
    if cache.exists():
        gj = json.loads(cache.read_text(encoding="utf-8"))
    else:
        params = urllib.parse.urlencode({
            "where": f"name='{cfg['ntad']}'", "outFields": "name",
            "returnGeometry": "true", "outSR": "4326",
            "geometryPrecision": "6", "f": "geojson",
        })
        print(f"Fetching NTAD geometry for '{cfg['ntad']}' ...")
        req = urllib.request.Request(f"{ARCGIS}?{params}",
                                     headers={"User-Agent": "speedo/0.1 hobby project"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
        gj = json.loads(body)
        if gj.get("features"):
            cache.write_bytes(body)  # don't cache an empty result (bad name)
    parts = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") == "LineString":
            coords = [geom["coordinates"]]
        elif geom.get("type") == "MultiLineString":
            coords = geom["coordinates"]
        else:
            continue
        for line in coords:
            parts.append([(lat, lon) for lon, lat in line])
    if not parts:
        raise SystemExit(f"NTAD returned no geometry for name='{cfg['ntad']}'")
    return parts, cfg


def part_miles(part):
    return sum(dist_mi(a, b) for a, b in zip(part, part[1:]))


def dedupe_parts(parts):
    """Drop parts that only re-trace a longer part.

    NTAD features are littered with duplicate scraps (second track, twice-
    digitized stubs) around junctions and stations; left in, they dead-end the
    stitcher by doubling the chain back on itself.
    """
    parts = sorted(parts, key=part_miles, reverse=True)
    pad = DUP_TOL_MILES / 30  # degrees; generous at any US latitude
    kept, boxes = [], []
    for p in parts:
        lats, lons = [v[0] for v in p], [v[1] for v in p]
        box = (min(lats), min(lons), max(lats), max(lons))
        step = max(1, len(p) // 20)
        probe = list(p[::step]) + [p[-1]]
        dup = False
        for q, qb in zip(kept, boxes):
            if (box[0] < qb[0] - pad or box[1] < qb[1] - pad or
                    box[2] > qb[2] + pad or box[3] > qb[3] + pad):
                continue
            if all(any(project_to_segment(v, a, b)[0] <= DUP_TOL_MILES
                       for a, b in zip(q, q[1:])) for v in probe):
                dup = True
                break
        if not dup:
            kept.append(p)
            boxes.append(box)
    return kept


def stitch(parts, mile0):
    """Join line parts into continuous chains; orient each from mile0.

    Returns sections, longest first. A plain route is one chain; a branched
    route (the Regional's Virginia legs, the Empire Builder's Portland leg)
    yields one section per branch, because a branch meets the main line
    mid-chain where endpoint-stitching can't absorb it.
    """
    parts = dedupe_parts([p for p in parts if len(p) >= 2])
    tol = 0.5  # miles between endpoints that count as "connected"
    sections, scrap_mi = [], 0.0
    while parts:
        chain = list(parts.pop(0))  # longest remaining; dedupe pre-sorted
        changed = True
        while changed and parts:
            changed = False
            for i, p in enumerate(parts):
                if dist_mi(chain[-1], p[0]) < tol:
                    chain += p[1:]
                elif dist_mi(chain[-1], p[-1]) < tol:
                    chain += p[::-1][1:]
                elif dist_mi(chain[0], p[-1]) < tol:
                    chain = p[:-1] + chain
                elif dist_mi(chain[0], p[0]) < tol:
                    chain = p[::-1][:-1] + chain
                else:
                    continue
                parts.pop(i)
                changed = True
                break
        if part_miles(chain) >= MIN_SECTION_MILES:
            sections.append(chain)
        else:
            scrap_mi += part_miles(chain)
    if scrap_mi:
        print(f"  note: dropped {scrap_mi:.1f} mi of duplicate/stub scraps")
    sections.sort(key=part_miles, reverse=True)
    if mile0:
        for c in sections:
            if dist_mi(c[-1], mile0) < dist_mi(c[0], mile0):
                c.reverse()
    if len(sections) > 1:
        print("  sections: " + ", ".join(f"{part_miles(c):.0f} mi" for c in sections))
    return sections


def simplify(pts, tol):
    """Iterative Douglas-Peucker."""
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        worst, wd = None, tol
        for i in range(i0 + 1, i1):
            d, _ = project_to_segment(pts[i], pts[i0], pts[i1])
            if d >= wd:
                worst, wd = i, d
        if worst is not None:
            keep[worst] = True
            stack.append((i0, worst))
            stack.append((worst, i1))
    return [p for p, k in zip(pts, keep) if k]


# --- binning ----------------------------------------------------------------

def build_bins(sections):
    """Slice each section into BIN_MILES arc-length bins of vertex runs.

    Mile numbering runs continuously across sections; bins never span a
    section boundary. Returns the bins, each bin's start-mile label, a flat
    segment list for projection -- (a, b, seg_mi, bin_base, mile_at_a,
    last_bin_of_section) -- and total mileage.
    """
    bins, labels, segs = [], [], []
    offset = 0.0
    for spine in sections:
        seglen = [dist_mi(a, b) for a, b in zip(spine, spine[1:])]
        cum = [0.0]
        for s in seglen:
            cum.append(cum[-1] + s)
        base = len(bins)
        cur = [spine[0]]
        next_cut = BIN_MILES
        for i, s in enumerate(seglen):
            a, b = spine[i], spine[i + 1]
            start = cum[i]
            while next_cut < start + s - 1e-9:
                t = (next_cut - start) / s
                cutpt = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                cur.append(cutpt)
                bins.append(cur)
                cur = [cutpt]
                next_cut += BIN_MILES
            cur.append(b)
        if len(cur) > 1:
            bins.append(cur)
        labels += [offset + j * BIN_MILES for j in range(len(bins) - base)]
        last = len(bins) - 1
        for i in range(len(spine) - 1):
            segs.append((spine[i], spine[i + 1], seglen[i], base, cum[i], last))
        offset += cum[-1]
    return bins, labels, segs, offset


class SegmentIndex:
    """Spatial hash of route segments for fast nearest-segment lookup."""

    CELL = 0.05  # degrees

    def __init__(self, segs, tol_mi):
        self.segs = segs
        self.grid = {}
        pad = tol_mi / MI_PER_DEG_LAT + self.CELL
        for i, seg in enumerate(segs):
            (la1, lo1), (la2, lo2) = seg[0], seg[1]
            for cy in range(int((min(la1, la2) - pad) / self.CELL),
                            int((max(la1, la2) + pad) / self.CELL) + 1):
                for cx in range(int((min(lo1, lo2) - pad) / self.CELL),
                                int((max(lo1, lo2) + pad) / self.CELL) + 1):
                    self.grid.setdefault((cy, cx), []).append(i)

    def nearest(self, p):
        cell = (int(p[0] / self.CELL), int(p[1] / self.CELL))
        best = (float("inf"), None, 0.0)
        for i in self.grid.get(cell, ()):
            d, t = project_to_segment(p, self.segs[i][0], self.segs[i][1])
            if d < best[0]:
                best = (d, i, t)
        return best


# --- post-processing --------------------------------------------------------
# Sparse data leaves artifacts: a lone slow reading amid fast track (the one
# train that happened to be braking there) and stretches with no data at all.
# All decisions are made here at build time; the HTML checkboxes only pick
# which precomputed annotation to display.

def find_outliers(maxes, counts, ratio=OUTLIER_RATIO):
    """Indices of single-point bins both of whose neighbors are >ratio x faster.

    maxes/counts are one section's per-bin max mph (None = no data) and point
    counts. A real speed restriction slows every train, so it shows in the
    neighbors too; a lone slow point between fast bins is sampling noise.
    Edge bins and bins with an empty neighbor are never flagged -- "both
    neighbors faster" can't be established.
    """
    out = []
    for i in range(1, len(maxes) - 1):
        m = maxes[i]
        if m is None or counts[i] != 1:
            continue
        left, right = maxes[i - 1], maxes[i + 1]
        if left is not None and right is not None and left > ratio * m and right > ratio * m:
            out.append(i)
    return out


def interpolate_gaps(maxes):
    """Linear speed estimates for interior gaps (runs of None) in one section.

    Returns {bin index: (mph, gap length in bins)}; the length lets the
    front-end threshold how big a gap it is willing to fill. Gaps touching a
    section end have only one bookend and are left empty.
    """
    filled = {}
    i, n = 0, len(maxes)
    while i < n:
        if maxes[i] is not None:
            i += 1
            continue
        j = i
        while j < n and maxes[j] is None:
            j += 1
        if 0 < i and j < n:
            left, right, gap = maxes[i - 1], maxes[j], j - i
            for k in range(i, j):
                f = (k - i + 1) / (gap + 1)
                filled[k] = (round(left + (right - left) * f), gap)
        i = j
    return filled


def annotate_bins(maxes, counts, ranges, ratio=OUTLIER_RATIO):
    """Per-bin post-processing annotations for the front-end toggles.

    ranges is [(first bin, last bin)] per section; outliers and gaps never
    cross a section boundary. Each annotation carries "out" (single-point
    outlier), "ia" ([mph, gap] interpolation with outliers left in) and "ib"
    (interpolation with outliers hidden, only where it differs from "ia").
    Both variants exist because outlier removal runs before interpolation:
    hiding an outlier turns its bin into a fillable gap.
    """
    ann = {}
    for start, last in ranges:
        sm = maxes[start:last + 1]
        outs = find_outliers(sm, counts[start:last + 1], ratio)
        for i in outs:
            ann.setdefault(start + i, {})["out"] = 1
        ia = interpolate_gaps(sm)
        masked = list(sm)
        for i in outs:
            masked[i] = None
        for i, (v, g) in ia.items():
            ann.setdefault(start + i, {})["ia"] = [v, g]
        for i, (v, g) in interpolate_gaps(masked).items():
            if ia.get(i) != (v, g):
                ann.setdefault(start + i, {})["ib"] = [v, g]
    return ann


# --- color ------------------------------------------------------------------

def speed_color(mph):
    v = max(0, min(MAX_MPH, mph))
    for (v0, c0), (v1, c1) in zip(COLOR_ANCHORS, COLOR_ANCHORS[1:]):
        if v <= v1:
            f = (v - v0) / (v1 - v0)
            rgb = [round(a + f * (b - a)) for a, b in zip(c0, c1)]
            return "#{:02x}{:02x}{:02x}".format(*rgb)
    return "#1e3cff"


# --- main build -------------------------------------------------------------

def load_observations(path, route, min_mph=MIN_PLAUSIBLE_MPH):
    """Load one route's observations, dropping implausible speeds.

    Ingest is lossless, so the plausibility band lives here: a GPS-glitch
    ceiling (MAX_PLAUSIBLE_MPH) above and a stopped-train floor (`min_mph`)
    below. A point slower than the floor is nearly always a train halted at a
    station or held at a signal, not a legitimate speed restriction, so
    counting it would paint fake slow track. Either threshold is a rebuild
    away from being fixed, not permanent data loss.
    """
    obs, glitches, slow = [], 0, 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            if o["route"] != route:
                continue
            if o["mph"] > MAX_PLAUSIBLE_MPH:
                glitches += 1
                continue
            if o["mph"] < min_mph:
                slow += 1
                continue
            obs.append(o)
    if glitches:
        print(f"  ignored {glitches} glitch points (>{MAX_PLAUSIBLE_MPH} mph)")
    if slow:
        print(f"  ignored {slow} stopped-train points (<{min_mph} mph)")
    return obs


def short_ts(ts):
    return f"{ts[5:7]}/{ts[8:10]} {ts[11:16]}"


def build(route, engines, google_key, min_mph=MIN_PLAUSIBLE_MPH):
    route = canonical_route(route)  # every entry point resolves to the one canonical slug
    parts, cfg = fetch_route_geometry(route)
    sections = stitch(parts, cfg.get("mile0"))
    raw_verts = sum(len(c) for c in sections)
    sections = [simplify(c, SIMPLIFY_MILES) for c in sections]
    bins_pts, bin_mile, segs, total = build_bins(sections)
    print(f"Spine: {raw_verts} -> {sum(len(c) for c in sections)} vertices "
          f"after simplify, {total:.1f} miles in {len(sections)} section(s), "
          f"{len(bins_pts)} bins of {BIN_MILES} mi")

    obs = load_observations(DATA / "observations.jsonl", route, min_mph)
    if not obs:
        raise SystemExit(f"no observations for route {route} - run scrape_railrat.py first")

    index = SegmentIndex(segs, OFFROUTE_MILES)
    binstats = [{"speeds": [], "max": -1, "top": None} for _ in bins_pts]
    used, offroute = 0, 0
    for o in obs:
        d, si, t = index.nearest((o["lat"], o["lon"]))
        if si is None or d > OFFROUTE_MILES:
            offroute += 1
            continue
        _a, _b, seg_mi, bin_base, mile_at_a, last_bin = segs[si]
        b = min(bin_base + int((mile_at_a + t * seg_mi) / BIN_MILES), last_bin)
        st = binstats[b]
        st["speeds"].append(o["mph"])
        if o["mph"] > st["max"]:
            st["max"] = o["mph"]
            st["top"] = (o["train"], short_ts(o["ts"]))
        used += 1
    print(f"Observations: {used} used, {offroute} dropped as off-route "
          f"(>{OFFROUTE_MILES} mi from line)")

    ranges = sorted({(s[3], s[5]) for s in segs})  # (first bin, last bin) per section
    ann = annotate_bins([st["max"] if st["speeds"] else None for st in binstats],
                        [len(st["speeds"]) for st in binstats], ranges)

    bins_out = []
    for i, (pts, mile, st) in enumerate(zip(bins_pts, bin_mile, binstats)):
        rec = {"m": round(mile, 1),
               "pts": [[round(la, 5), round(lo, 5)] for la, lo in pts]}
        if st["speeds"]:
            rec.update(mx=st["max"], n=len(st["speeds"]),
                       med=round(statistics.median(st["speeds"])),
                       top=list(st["top"]))
        rec.update(ann.get(i, {}))
        bins_out.append(rec)
    empty = sum(1 for b in bins_out if "mx" not in b)
    print(f"Bins with data: {len(bins_out) - empty}/{len(bins_out)}")
    print(f"Post-process: {sum(1 for a in ann.values() if 'out' in a)} single-point "
          f"outliers flagged, "
          f"{sum(1 for a in ann.values() if 'ia' in a or 'ib' in a)} bins interpolable")

    runs = {(o["train"], o["run_date"]) for o in obs}
    dates = sorted(o["run_date"] for o in obs)
    config = {
        "title": f"{cfg['display']} - observed speeds",
        "display": cfg["display"],
        "totalMiles": round(total, 1),
        "binMiles": BIN_MILES,
        "maxMph": MAX_MPH,
        "anchors": [[v, "#{:02x}{:02x}{:02x}".format(*c)] for v, c in COLOR_ANCHORS],
        "stats": {"obs": used, "runs": len(runs), "from": dates[0], "to": dates[-1],
                  "built": dt.date.today().isoformat()},
        "bins": bins_out,
        "obsPts": [[round(o["lat"], 5), round(o["lon"], 5), o["mph"],
                    o["train"], short_ts(o["ts"])] for o in obs],
    }
    blob = json.dumps(config, separators=(",", ":"))

    OUT.mkdir(exist_ok=True)
    written = []
    if "leaflet" in engines:
        path = OUT / f"speed_map_{route}.html"
        path.write_text(LEAFLET_TMPL.replace("__CONFIG__", blob), encoding="utf-8")
        written.append(path)
    if "google" in engines:
        key = google_key or "YOUR_GOOGLE_MAPS_API_KEY"
        html = GOOGLE_TMPL.replace("__CONFIG__", blob).replace("__KEY__", key)
        path = OUT / f"speed_map_{route}_google.html"
        path.write_text(html, encoding="utf-8")
        written.append(path)
        if not google_key:
            print("  note: no --google-key given; edit YOUR_GOOGLE_MAPS_API_KEY "
                  "in the google file to activate it")
    for p in written:
        print(f"Wrote {p} ({p.stat().st_size / 1024:.0f} KB)")


# --- shared front-end pieces ------------------------------------------------

COMMON_JS = r"""
function speedColor(v){
  const A = CFG.anchors.map(([s,h]) => [s, [parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16)]]);
  v = Math.max(A[0][0], Math.min(CFG.maxMph, v));
  for (let i = 1; i < A.length; i++){
    if (v <= A[i][0]){
      const f = (v - A[i-1][0]) / (A[i][0] - A[i-1][0]);
      const c = A[i-1][1].map((a,j) => Math.round(a + f*(A[i][1][j]-a)));
      return `rgb(${c[0]},${c[1]},${c[2]})`;
    }
  }
  return CFG.anchors[CFG.anchors.length-1][1];
}
// Post-processing toggles. Outlier removal runs before interpolation: hiding
// an outlier turns its bin into a gap, so the build ships two interpolation
// variants ("ia" outliers-in, "ib" outliers-hidden where it differs).
const S = {hideOut: true, interp: true, maxGap: 10};

function binState(b){
  const hidOut = b.out === 1 && S.hideOut;
  if (b.mx !== undefined && !hidOut) return {kind: 'data', mph: b.mx};
  const ip = S.hideOut ? (b.ib || b.ia) : b.ia;
  if (S.interp && ip && ip[1] <= S.maxGap)
    return {kind: 'interp', mph: ip[0], gap: ip[1], hidOut};
  return {kind: 'none', hidOut};
}
function binStyle(b){
  const st = binState(b);
  if (st.kind === 'data')
    return {color: speedColor(st.mph), weight: 5, dash: null, opacity: .95};
  if (st.kind === 'interp')
    return {color: speedColor(st.mph), weight: 4, dash: '6 6', opacity: .85};
  return {color: '#9aa0a6', weight: 3, dash: '3 7', opacity: .95};
}
function binHtml(b){
  const st = binState(b);
  const hidNote = st.hidOut ?
    `<div class="pop-meta">outlier hidden: ${b.mx} mph (1 pt)</div>` : '';
  if (st.kind === 'none')
    return `<div class="pop">
      <div class="pop-mph pop-nodata">&ndash;</div>
      <div class="pop-label">no data</div>
      <div class="pop-meta">mile ${b.m.toFixed(1)} of ${CFG.totalMiles}</div>
      ${hidNote}
    </div>`;
  if (st.kind === 'interp')
    return `<div class="pop">
      <div class="pop-mph" style="color:${speedColor(st.mph)}">~${st.mph}</div>
      <div class="pop-label pop-est">interpolated</div>
      <div class="pop-meta">no data here &ndash; estimated across a ${(st.gap * CFG.binMiles).toFixed(1)} mi gap</div>
      <div class="pop-meta">mile ${b.m.toFixed(1)} of ${CFG.totalMiles}</div>
      ${hidNote}
    </div>`;
  const outNote = (b.out === 1 && !S.hideOut) ?
    `<div class="pop-meta pop-est">flagged single-point outlier</div>` : '';
  return `<div class="pop">
    <div class="pop-mph" style="color:${speedColor(b.mx)}">${b.mx}</div>
    <div class="pop-label">max mph</div>
    <div class="pop-meta">#${b.top[0]}, ${b.top[1]}</div>
    <div class="pop-meta">${b.n} pts, median ${b.med} mph</div>
    ${outNote}
  </div>`;
}
function controlsHtml(){
  return `<div class="lg-controls">
    <label><input type="checkbox" id="cb-out" checked> hide single-point outliers</label>
    <label><input type="checkbox" id="cb-interp" checked> interpolate gaps</label>
    <div class="lg-gap" id="gap-row">
      max gap <input type="range" id="gap-range" min="1" max="100" value="${S.maxGap}">
      <input type="number" id="gap-num" min="1" max="100" value="${S.maxGap}"> bins
    </div>
  </div>`;
}
function wireControls(root, restyle){
  const out = root.querySelector('#cb-out'), itp = root.querySelector('#cb-interp');
  const rng = root.querySelector('#gap-range'), num = root.querySelector('#gap-num');
  const apply = () => {
    S.hideOut = out.checked;
    S.interp = itp.checked;
    rng.disabled = num.disabled = !itp.checked;
    root.querySelector('#gap-row').classList.toggle('lg-off', !itp.checked);
    restyle();
  };
  const setGap = v => {
    v = Math.min(100, Math.max(1, Math.round(+v) || 1));
    rng.value = num.value = v;
    if (v !== S.maxGap){ S.maxGap = v; restyle(); }
  };
  out.addEventListener('change', apply);
  itp.addEventListener('change', apply);
  rng.addEventListener('input', () => setGap(rng.value));
  num.addEventListener('change', () => setGap(num.value));
  apply();
}
function legendHtml(){
  const stops = CFG.anchors.map(([s,c]) => `${c} ${s/CFG.maxMph*100}%`).join(", ");
  const ticks = CFG.anchors.map(([s]) => `<span>${s}</span>`).join("");
  return `<div class="lg-title">${CFG.display}</div>
    <div class="lg-bar" style="background:linear-gradient(to right, ${stops})"></div>
    <div class="lg-ticks">${ticks}</div>
    <div class="lg-sub">max observed mph per half-mile bin - click the line</div>
    <div class="lg-sub">${CFG.stats.obs.toLocaleString()} obs / ${CFG.stats.runs} runs / ${CFG.stats.from} to ${CFG.stats.to}</div>`;
}
"""

COMMON_CSS = r"""
  html, body, #map { height: 100%; margin: 0; }
  .legend { background: rgba(255,255,255,.95); border-radius: 8px; padding: 10px 12px;
            box-shadow: 0 1px 6px rgba(0,0,0,.35); font: 12px/1.45 system-ui, sans-serif; }
  .lg-title { font-weight: 700; font-size: 14px; margin-bottom: 6px; }
  .lg-bar { height: 10px; border-radius: 5px; }
  .lg-ticks { display: flex; justify-content: space-between; color: #444; margin: 2px 0 4px; }
  .lg-sub { color: #666; }
  .lg-controls { border-top: 1px solid #ddd; margin-top: 6px; padding-top: 6px; color: #444; }
  .lg-controls label { display: block; margin: 2px 0; }
  .lg-gap { display: flex; align-items: center; gap: 5px; margin: 2px 0 0 18px; }
  .lg-gap input[type=range] { width: 80px; }
  .lg-gap input[type=number] { width: 46px; }
  .lg-off { opacity: .45; }

  /* speed popup: dark rounded card, big speed number in the segment's color */
  .pop { text-align: center; font: 13px/1.35 system-ui, sans-serif; min-width: 132px; }
  .pop-mph { font-size: 62px; font-weight: 800; line-height: 1; letter-spacing: -2px; }
  .pop-nodata { color: #7a7f87; }
  .pop-label { font-size: 20px; font-weight: 800; color: #fff; letter-spacing: 1px;
               text-transform: uppercase; margin: 2px 0 9px; }
  .pop-meta { font-size: 13px; color: #b9bdc4; margin-top: 3px; }
  .pop-est { color: #e8b93c; }

  /* Leaflet popup chrome -> dark card */
  .leaflet-popup-content-wrapper { background: #232323; color: #fff; border: 3px solid #fff;
      border-radius: 22px; box-shadow: 0 3px 16px rgba(0,0,0,.45); padding: 4px; }
  .leaflet-popup-content { margin: 14px 22px; }
  .leaflet-popup-tip { background: #232323; box-shadow: none; }
  .leaflet-container a.leaflet-popup-close-button { color: #9aa0a6; top: 6px; right: 8px; }
  .leaflet-container a.leaflet-popup-close-button:hover { color: #fff; }

  /* Google InfoWindow chrome -> dark card (best-effort; classes are Google's) */
  .gm-style .gm-style-iw-c { background: #232323; border: 3px solid #fff; border-radius: 22px;
      box-shadow: 0 3px 16px rgba(0,0,0,.45); padding: 0; }
  .gm-style .gm-style-iw-d { overflow: hidden !important; padding: 14px 22px; }
  .gm-style .gm-style-iw-t::after { background: #232323; box-shadow: none; }
  .gm-style .gm-style-iw-c button.gm-ui-hover-effect > span { background: #9aa0a6; }
"""

LEAFLET_TMPL = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>speedo</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
""" + COMMON_CSS + r"""
</style>
</head>
<body>
<div id="map"></div>
<script>
const CFG = __CONFIG__;
document.title = CFG.title;
""" + COMMON_JS + r"""
const map = L.map('map');
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {maxZoom: 20, subdomains: 'abcd',
   attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'}).addTo(map);

function toLeaflet(s){
  return {color: s.color, weight: s.weight, opacity: s.opacity, dashArray: s.dash};
}
let bounds = [];
const binLines = [];
for (const b of CFG.bins){
  const line = L.polyline(b.pts, toLeaflet(binStyle(b))).addTo(map);
  line.on('click', e => L.popup().setLatLng(e.latlng).setContent(binHtml(b)).openOn(map));
  line.on('mouseover', () => line.setStyle({weight: 9}));
  line.on('mouseout',  () => line.setStyle({weight: binStyle(b).weight}));
  binLines.push([line, b]);
  bounds.push(b.pts[0], b.pts[b.pts.length-1]);
}
map.fitBounds(bounds, {padding: [20, 20]});
const restyle = () => binLines.forEach(([line, b]) => line.setStyle(toLeaflet(binStyle(b))));

const dots = L.layerGroup(CFG.obsPts.map(([la, lo, mph, train, ts]) =>
  L.circleMarker([la, lo], {radius: 3, weight: 1, color: '#fff',
                            fillColor: speedColor(mph), fillOpacity: .9})
   .bindTooltip(`${mph} mph - train ${train}, ${ts}`)));
L.control.layers(null, {'Raw observations': dots}, {collapsed: false}).addTo(map);

const legend = L.control({position: 'bottomleft'});
legend.onAdd = () => {
  const d = L.DomUtil.create('div', 'legend');
  d.innerHTML = legendHtml() + controlsHtml();
  L.DomEvent.disableClickPropagation(d);
  L.DomEvent.disableScrollPropagation(d);
  wireControls(d, restyle);
  return d;
};
legend.addTo(map);
</script>
</body>
</html>
"""

GOOGLE_TMPL = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>speedo</title>
<style>
""" + COMMON_CSS + r"""
  .legend { margin: 0 0 24px 10px; }
</style>
</head>
<body>
<div id="map"></div>
<script>
const CFG = __CONFIG__;
document.title = CFG.title;
""" + COMMON_JS + r"""
function initMap(){
  const map = new google.maps.Map(document.getElementById('map'),
    {mapTypeControl: true, streetViewControl: false});
  const bounds = new google.maps.LatLngBounds();
  const info = new google.maps.InfoWindow();

  // Google polylines have no dashArray; dashed styles are drawn as repeated
  // line symbols over an invisible stroke.
  function toGoogle(s){
    if (!s.dash)
      return {strokeColor: s.color, strokeWeight: s.weight,
              strokeOpacity: s.opacity, icons: null};
    return {strokeColor: s.color, strokeWeight: s.weight, strokeOpacity: 0,
            icons: [{icon: {path: 'M 0,-1 0,1', strokeOpacity: s.opacity,
                            strokeColor: s.color, strokeWeight: s.weight, scale: 2},
                     offset: '0', repeat: '12px'}]};
  }
  const binLines = [];
  for (const b of CFG.bins){
    const path = b.pts.map(([lat, lng]) => ({lat, lng}));
    const line = new google.maps.Polyline({path, map, ...toGoogle(binStyle(b))});
    line.addListener('click', e => {
      info.setContent(binHtml(b)); info.setPosition(e.latLng); info.open(map);
    });
    line.addListener('mouseover', () => line.setOptions(toGoogle({...binStyle(b), weight: 9})));
    line.addListener('mouseout',  () => line.setOptions(toGoogle(binStyle(b))));
    binLines.push([line, b]);
    path.forEach(p => bounds.extend(p));
  }
  map.fitBounds(bounds);
  const restyle = () => binLines.forEach(([line, b]) => line.setOptions(toGoogle(binStyle(b))));

  const dots = CFG.obsPts.map(([la, lo, mph, train, ts]) => {
    const m = new google.maps.Marker({
      position: {lat: la, lng: lo},
      icon: {path: google.maps.SymbolPath.CIRCLE, scale: 3.5,
             fillColor: speedColor(mph), fillOpacity: .9,
             strokeColor: '#fff', strokeWeight: 1},
      title: `${mph} mph - train ${train}, ${ts}`,
    });
    m.addListener('click', e => {
      info.setContent(`<b>${mph} mph</b> - train ${train}, ${ts}`);
      info.setPosition(e.latLng); info.open(map);
    });
    return m;
  });

  const wrap = document.createElement('div');
  wrap.className = 'legend';
  wrap.innerHTML = legendHtml() + controlsHtml() +
    `<label class="lg-sub" style="display:block;margin-top:4px">
       <input type="checkbox" id="dots-cb"> show raw observations</label>`;
  map.controls[google.maps.ControlPosition.LEFT_BOTTOM].push(wrap);
  wireControls(wrap, restyle);
  wrap.querySelector('#dots-cb').addEventListener('change', ev =>
    dots.forEach(m => m.setMap(ev.target.checked ? map : null)));
}
</script>
<script async defer
  src="https://maps.googleapis.com/maps/api/js?key=__KEY__&callback=initMap"></script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--route", default="AcelaExpress",
                    help="RailRat route slug (default AcelaExpress)")
    ap.add_argument("--engine", default="both", choices=["both", "leaflet", "google"])
    ap.add_argument("--google-key", default="",
                    help="Google Maps JS API key to bake into the google output")
    ap.add_argument("--min-mph", type=int, default=MIN_PLAUSIBLE_MPH,
                    help="ignore observations slower than this -- stopped or "
                         "stuck trains, not track speed limits "
                         f"(default {MIN_PLAUSIBLE_MPH})")
    args = ap.parse_args()
    engines = ["leaflet", "google"] if args.engine == "both" else [args.engine]
    build(args.route, engines, args.google_key, args.min_mph)


if __name__ == "__main__":
    main()
