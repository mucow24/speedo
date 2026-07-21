#!/usr/bin/env python3
"""Build a color-coded speed map from scraped RailRat observations.

Draws the official route line (USDOT/NTAD Amtrak Routes geometry), projects
every GPS observation onto it, slices the line into half-mile bins, and colors
each bin by the MAX speed ever observed there (so station dwell and delay
slowdowns don't mask what the track can do). Output is self-contained HTML:
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
    "KeystoneService": {"ntad": "Keystone", "display": "Keystone Service",
                        "mile0": (40.34467, -76.41135)},       # Harrisburg-ish
}

BIN_MILES = 0.5
OFFROUTE_MILES = 2.0     # drop observations farther than this from the line
MAX_MPH = 160            # top of the color scale
SIMPLIFY_MILES = 0.015   # ~25 m Douglas-Peucker tolerance

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
    """Download (and cache) the NTAD line for the route as a list of parts."""
    cfg = ROUTES.get(route, {"ntad": route, "display": route, "mile0": None})
    gdir = DATA / "geometry"
    gdir.mkdir(parents=True, exist_ok=True)
    cache = gdir / f"{route}.geojson"
    if not cache.exists():
        params = urllib.parse.urlencode({
            "where": f"name='{cfg['ntad']}'", "outFields": "name",
            "returnGeometry": "true", "outSR": "4326",
            "geometryPrecision": "6", "f": "geojson",
        })
        print(f"Fetching NTAD geometry for '{cfg['ntad']}' ...")
        req = urllib.request.Request(f"{ARCGIS}?{params}",
                                     headers={"User-Agent": "speedo/0.1 hobby project"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            cache.write_bytes(resp.read())
    gj = json.loads(cache.read_text(encoding="utf-8"))
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
        raise SystemExit(f"NTAD returned no geometry for name='{cfg['ntad']}' "
                         f"(check data/geometry/{route}.geojson)")
    return parts, cfg


def stitch(parts, mile0):
    """Join line parts into one chain by endpoint proximity; orient from mile0."""
    parts = [p for p in parts if len(p) >= 2]
    parts.sort(key=lambda p: -sum(dist_mi(a, b) for a, b in zip(p, p[1:])))
    chain = list(parts.pop(0))
    tol = 0.5  # miles between endpoints that count as "connected"
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
    if parts:
        leftover = sum(dist_mi(a, b) for p in parts for a, b in zip(p, p[1:]))
        print(f"  note: {len(parts)} unconnected part(s) dropped ({leftover:.1f} mi)")
    if mile0 and dist_mi(chain[-1], mile0) < dist_mi(chain[0], mile0):
        chain.reverse()
    return chain


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

def build_bins(spine):
    """Slice the spine into BIN_MILES arc-length bins of vertex runs."""
    seglen = [dist_mi(a, b) for a, b in zip(spine, spine[1:])]
    cum = [0.0]
    for s in seglen:
        cum.append(cum[-1] + s)
    total = cum[-1]

    bins = []
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
    return bins, seglen, cum, total


class SegmentIndex:
    """Spatial hash of spine segments for fast nearest-segment lookup."""

    CELL = 0.05  # degrees

    def __init__(self, spine, tol_mi):
        self.spine = spine
        self.grid = {}
        pad = tol_mi / MI_PER_DEG_LAT + self.CELL
        for i in range(len(spine) - 1):
            (la1, lo1), (la2, lo2) = spine[i], spine[i + 1]
            for cy in range(int((min(la1, la2) - pad) / self.CELL),
                            int((max(la1, la2) + pad) / self.CELL) + 1):
                for cx in range(int((min(lo1, lo2) - pad) / self.CELL),
                                int((max(lo1, lo2) + pad) / self.CELL) + 1):
                    self.grid.setdefault((cy, cx), []).append(i)

    def nearest(self, p):
        cell = (int(p[0] / self.CELL), int(p[1] / self.CELL))
        best = (float("inf"), None, 0.0)
        for i in self.grid.get(cell, ()):
            d, t = project_to_segment(p, self.spine[i], self.spine[i + 1])
            if d < best[0]:
                best = (d, i, t)
        return best


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

def short_ts(ts):
    return f"{ts[5:7]}/{ts[8:10]} {ts[11:16]}"


def build(route, engines, google_key):
    parts, cfg = fetch_route_geometry(route)
    chain = stitch(parts, cfg.get("mile0"))
    spine = simplify(chain, SIMPLIFY_MILES)
    bins_pts, seglen, cum, total = build_bins(spine)
    print(f"Spine: {len(chain)} -> {len(spine)} vertices after simplify, "
          f"{total:.1f} miles, {len(bins_pts)} bins of {BIN_MILES} mi")

    obs = []
    with (DATA / "observations.jsonl").open(encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            if o["route"] == route:
                obs.append(o)
    if not obs:
        raise SystemExit(f"no observations for route {route} - run scrape_railrat.py first")

    index = SegmentIndex(spine, OFFROUTE_MILES)
    binstats = [{"speeds": [], "max": -1, "top": None} for _ in bins_pts]
    used, offroute = 0, 0
    for o in obs:
        d, seg, t = index.nearest((o["lat"], o["lon"]))
        if seg is None or d > OFFROUTE_MILES:
            offroute += 1
            continue
        mile = cum[seg] + t * seglen[seg]
        b = min(int(mile / BIN_MILES), len(binstats) - 1)
        st = binstats[b]
        st["speeds"].append(o["mph"])
        if o["mph"] > st["max"]:
            st["max"] = o["mph"]
            st["top"] = (o["train"], short_ts(o["ts"]))
        used += 1
    print(f"Observations: {used} used, {offroute} dropped as off-route "
          f"(>{OFFROUTE_MILES} mi from line)")

    bins_out = []
    for i, (pts, st) in enumerate(zip(bins_pts, binstats)):
        rec = {"m": round(i * BIN_MILES, 1),
               "pts": [[round(la, 5), round(lo, 5)] for la, lo in pts]}
        if st["speeds"]:
            rec.update(mx=st["max"], n=len(st["speeds"]),
                       med=round(statistics.median(st["speeds"])),
                       top=list(st["top"]))
        bins_out.append(rec)
    empty = sum(1 for b in bins_out if "mx" not in b)
    print(f"Bins with data: {len(bins_out) - empty}/{len(bins_out)}")

    runs = {(o["train"], o["run_date"]) for o in obs}
    dates = sorted(o["run_date"] for o in obs)
    config = {
        "title": f"{cfg['display']} - observed speeds",
        "display": cfg["display"],
        "totalMiles": round(total, 1),
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
function binHtml(b){
  if (b.mx === undefined)
    return `<b>Mile ${b.m.toFixed(1)}</b> of ${CFG.totalMiles}<br>no observations yet`;
  return `<b>Mile ${b.m.toFixed(1)}</b> of ${CFG.totalMiles}<br>` +
    `<span style="font-size:15px"><b>max ${b.mx} mph</b></span> - train ${b.top[0]}, ${b.top[1]}<br>` +
    `${b.n} obs - median ${b.med} mph`;
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
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 18, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);

let bounds = [];
for (const b of CFG.bins){
  const has = b.mx !== undefined;
  const line = L.polyline(b.pts, {
    color: has ? speedColor(b.mx) : '#9aa0a6',
    weight: has ? 5 : 3, opacity: .95, dashArray: has ? null : '3 7',
  }).addTo(map);
  line.on('click', e => L.popup().setLatLng(e.latlng).setContent(binHtml(b)).openOn(map));
  line.on('mouseover', () => line.setStyle({weight: 9}));
  line.on('mouseout',  () => line.setStyle({weight: has ? 5 : 3}));
  bounds.push(b.pts[0], b.pts[b.pts.length-1]);
}
map.fitBounds(bounds, {padding: [20, 20]});

const dots = L.layerGroup(CFG.obsPts.map(([la, lo, mph, train, ts]) =>
  L.circleMarker([la, lo], {radius: 3, weight: 1, color: '#fff',
                            fillColor: speedColor(mph), fillOpacity: .9})
   .bindTooltip(`${mph} mph - train ${train}, ${ts}`)));
L.control.layers(null, {'Raw observations': dots}, {collapsed: false}).addTo(map);

const legend = L.control({position: 'bottomleft'});
legend.onAdd = () => { const d = L.DomUtil.create('div', 'legend'); d.innerHTML = legendHtml(); return d; };
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

  for (const b of CFG.bins){
    const has = b.mx !== undefined;
    const path = b.pts.map(([lat, lng]) => ({lat, lng}));
    const line = new google.maps.Polyline({
      path, map, strokeColor: has ? speedColor(b.mx) : '#9aa0a6',
      strokeWeight: has ? 5 : 3, strokeOpacity: .95,
    });
    line.addListener('click', e => {
      info.setContent(binHtml(b)); info.setPosition(e.latLng); info.open(map);
    });
    line.addListener('mouseover', () => line.setOptions({strokeWeight: 9}));
    line.addListener('mouseout',  () => line.setOptions({strokeWeight: has ? 5 : 3}));
    path.forEach(p => bounds.extend(p));
  }
  map.fitBounds(bounds);

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
  wrap.innerHTML = legendHtml() +
    `<label class="lg-sub" style="display:block;margin-top:4px">
       <input type="checkbox" id="dots-cb"> show raw observations</label>`;
  map.controls[google.maps.ControlPosition.LEFT_BOTTOM].push(wrap);
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
    args = ap.parse_args()
    engines = ["leaflet", "google"] if args.engine == "both" else [args.engine]
    build(args.route, engines, args.google_key)


if __name__ == "__main__":
    main()
