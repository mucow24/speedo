"""Build-side payload for the speed-profile graph.

The graph plots one branch (the longest) from end to end and lets you pick a
sub-stretch by clicking two stations, so the build must ship two new things in
CFG: the bin-index range of each section (so the front-end can isolate the
longest branch) and each station's route-mile along that branch (so a station
click maps to a position on the graph). These pin that payload; the front-end
math that consumes it is tested separately under V8 in test_profile.py.
"""
import json

import build_map as bm


def one_section_segs(a, b):
    """build_bins output for a single straight section a->b (two vertices)."""
    _bins, _labels, segs, total = bm.build_bins([[a, b]])
    return segs, total


def test_project_stations_returns_route_mile_on_longest_section():
    # Purpose: a station near the longest branch resolves to its arc-length
    # mile along that branch -- the x-position the graph and the click-to-select
    # both key on. A vertical 69.05-mi leg (1 deg of latitude) puts a station
    # 40% along at mile ~27.62; pin that projection.
    segs, total = one_section_segs((40.0, -75.0), (41.0, -75.0))
    assert abs(total - 69.05) < 0.01  # sanity: 1 deg lat = MI_PER_DEG_LAT mi
    miles = bm.project_stations([(40.4, -75.0, "Midpoint")], segs)
    assert abs(miles[0] - 27.62) < 0.05


def test_project_stations_off_branch_station_is_none():
    # Purpose: a station far from the longest branch (here ~5 deg lon away,
    # hundreds of miles off) has no meaningful position on the graph, so it
    # must resolve to None rather than being snapped to a bogus mile -- clicking
    # it can't form a selection.
    segs, _ = one_section_segs((40.0, -75.0), (41.0, -75.0))
    miles = bm.project_stations([(40.5, -70.0, "FarAway")], segs)
    assert miles[0] is None


def test_project_stations_uses_only_the_longest_section():
    # Purpose: on a branched route the graph is the longest branch alone, so a
    # station sitting on a *shorter* branch must not project onto it. Build two
    # sections; a station on the short branch is off the long one and resolves
    # to None (only bin_base==0 segments -- the longest, emitted first -- count).
    long_a, long_b = (40.0, -75.0), (41.0, -75.0)          # 69 mi vertical
    short_a, short_b = (40.0, -75.0), (40.0, -74.9)         # ~5 mi horizontal
    _bins, _labels, segs, _total = bm.build_bins(
        [[long_a, long_b], [short_a, short_b]])
    # A point out on the short branch, well off the long vertical line.
    miles = bm.project_stations([(40.0, -74.9, "OnShortBranch")], segs)
    assert miles[0] is None


def _build_html_cfg(tmp_path, monkeypatch):
    """Run a minimal real build() and return the embedded CFG dict."""
    monkeypatch.setattr(bm, "DATA", tmp_path)
    monkeypatch.setattr(bm, "OUT", tmp_path / "out")
    (tmp_path / "geometry").mkdir()
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "X"},
         "geometry": {"type": "LineString",
                      "coordinates": [[-71.0, 42.0], [-71.0, 42.2]]}},
        {"type": "Feature",
         "properties": {"kind": "station", "code": "BOS", "name": "Boston, MA"},
         "geometry": {"type": "Point", "coordinates": [-71.0, 42.05]}}]}
    (tmp_path / "geometry" / "AcelaExpress.geojson").write_text(
        json.dumps(gj), encoding="utf-8")
    (tmp_path / "observations.jsonl").write_text(json.dumps({
        "route": "AcelaExpress", "train": 2151, "run_date": "2026-07-21",
        "ts": "2026-07-21T10:00:00", "lat": 42.05, "lon": -71.0, "mph": 90,
        "heading": "N", "desc": "x", "src": "live"}) + "\n", encoding="utf-8")
    bm.build("AcelaExpress")
    html = (tmp_path / "out" / "speed_map_AcelaExpress.html").read_text(
        encoding="utf-8")
    blob = html.split("const CFG = ", 1)[1].split(";\ndocument.title", 1)[0]
    return json.loads(blob)


def test_build_emits_section_ranges(tmp_path, monkeypatch):
    # Purpose: the front-end isolates the longest branch by bin index, so the
    # build must publish each section as an inclusive [firstBin, lastBin] pair.
    # This single-line route is one section spanning every bin.
    cfg = _build_html_cfg(tmp_path, monkeypatch)
    nbins = len(cfg["bins"])
    assert cfg["sections"] == [[0, nbins - 1]]


def test_build_station_dot_carries_route_mile(tmp_path, monkeypatch):
    # Purpose: end-to-end, each station dot in CFG now carries a 4th field --
    # its route-mile along the longest branch -- which the click-to-select uses.
    # BOS sits at lat 42.05 on a 42.0->42.2 line (~13.8 mi); AcelaExpress's
    # mile0 is at the northern (42.2) end, so the route is oriented from there
    # and BOS -- a quarter up from the south end -- reads as ~10.36 mi (13.8 -
    # 3.45). The [lat, lon, name] prefix is unchanged for the existing dots.
    cfg = _build_html_cfg(tmp_path, monkeypatch)
    assert len(cfg["stations"]) == 1
    la, lo, nm, mile = cfg["stations"][0]
    assert [la, lo, nm] == [42.05, -71.0, "Boston, MA"]
    assert abs(mile - 10.36) < 0.05
