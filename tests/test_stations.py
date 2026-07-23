"""Station coordinates: resolve observed stops to external NTAD coords and
embed them as Point features in each route's geometry file, then render them.

The rules these pin, straight from the request:
- *Which* stations a route has = the codes actually seen in station_events
  (real observed stops), scoped to that route -- never nearest-line guessing.
- *Where* each station is = coordinates from the external NTAD station index,
  never inferred from our own GPS pings; a code with no external coord is
  reported as missing, not fabricated.
- Adding station Point features must not disturb the line-spine parsing that
  the whole map build (and speedo_ctl coverage) rests on.
"""
import json
import re

import build_map as bm


def fc(*features):
    # A FeatureCollection carrying some metadata (crs) we must preserve.
    return {"type": "FeatureCollection", "crs": {"type": "name"},
            "features": list(features)}


def line_feature(coords):  # coords: list of [lon, lat]
    return {"type": "Feature", "properties": {"name": "X"},
            "geometry": {"type": "LineString", "coordinates": coords}}


def stn_feat(code, name, lat, lon):
    return {"type": "Feature",
            "properties": {"kind": "station", "code": code, "name": name},
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def write_events(path, rows):  # rows: (route, code, name)
    path.write_text("".join(json.dumps({
        "route": r, "train": 1, "run_date": "2026-07-21", "station": c,
        "name": n, "arr": None, "arr_delay": None,
        "dep": "2026-07-21T07:00:00", "dep_delay": 0, "src": "live"}) + "\n"
        for r, c, n in rows), encoding="utf-8")


def test_station_features_uses_external_coords_and_reports_missing(tmp_path):
    # Purpose: membership is route-scoped and comes from observed events;
    # coordinates come from the external index in GeoJSON [lon,lat] order; a
    # code absent from the index (here the CBN border checkpoint) is reported
    # as missing rather than invented. This is the "don't infer" contract.
    ev = tmp_path / "se.jsonl"
    write_events(ev, [("AcelaExpress", "WAS", "Washington, DC"),
                      ("AcelaExpress", "BOS", "Boston, MA"),
                      ("AcelaExpress", "CBN", "Canadian Border NY"),
                      ("NortheastRegional", "PHL", "Philadelphia, PA")])
    index = {"WAS": (38.89696, -77.00643), "BOS": (42.36531, -71.06228),
             "PHL": (39.9, -75.1)}
    feats, missing = bm.station_features("AcelaExpress", index, path=ev)
    got = {f["properties"]["code"]: f for f in feats}
    assert set(got) == {"WAS", "BOS"}          # route-scoped, PHL is another route
    assert missing == ["CBN"]                  # reported, not fabricated
    assert got["WAS"]["geometry"]["coordinates"] == [-77.00643, 38.89696]
    assert got["WAS"]["properties"]["name"] == "Washington, DC"
    assert got["WAS"]["properties"]["kind"] == "station"


def test_route_station_names_dedupes_route_scoped(tmp_path):
    # Purpose: a station recurs across many runs; membership must collapse to
    # one deterministic (code, name) per station and stay scoped to the route.
    ev = tmp_path / "se.jsonl"
    write_events(ev, [("AcelaExpress", "WAS", "Washington, DC"),
                      ("AcelaExpress", "WAS", "Washington, DC"),
                      ("AcelaExpress", "BOS", "Boston, MA"),
                      ("NortheastRegional", "PHL", "Philadelphia, PA")])
    assert bm.route_station_names("AcelaExpress", path=ev) == [
        ("BOS", "Boston, MA"), ("WAS", "Washington, DC")]


def test_geojson_parts_ignores_station_points():
    # Purpose: the load-bearing invariant -- adding Point features to a
    # geometry file must leave the line-spine extraction (used by build_map
    # and speedo_ctl coverage) seeing exactly the lines, no phantom part.
    gj = fc(line_feature([[0.0, 0.0], [1.0, 0.0]]),
            stn_feat("WAS", "Washington, DC", 38.9, -77.0))
    assert bm.geojson_parts(gj) == [[(0.0, 0.0), (0.0, 1.0)]]


def test_merge_station_features_idempotent_and_preserves_lines():
    # Purpose: refreshing stations replaces the prior station set (never
    # accumulates duplicates) while every non-station feature and the
    # collection metadata survive untouched -- so re-running is safe.
    line = line_feature([[0, 0], [0, 1]])
    gj = fc(line)
    merged = bm.merge_station_features(
        gj, [stn_feat("WAS", "Washington, DC", 38.9, -77.0)])
    assert line in merged["features"]
    assert merged["crs"] == gj["crs"]
    merged2 = bm.merge_station_features(
        merged, [stn_feat("BOS", "Boston, MA", 42.3, -71.0)])
    codes = {f["properties"]["code"] for f in merged2["features"]
             if f["properties"].get("kind") == "station"}
    assert codes == {"BOS"}          # old station replaced, not duplicated
    assert line in merged2["features"]


def test_geojson_station_points_round_trips(tmp_path):
    # Purpose: what station_features writes is exactly what the renderer reads
    # back -- (lat, lon, name), the shape the map dots consume.
    ev = tmp_path / "se.jsonl"
    write_events(ev, [("AcelaExpress", "WAS", "Washington, DC")])
    feats, _ = bm.station_features("AcelaExpress", {"WAS": (38.89696, -77.00643)},
                                   path=ev)
    gj = bm.merge_station_features(fc(line_feature([[0, 0], [0, 1]])), feats)
    assert bm.geojson_station_points(gj) == [(38.89696, -77.00643, "Washington, DC")]


def test_refresh_route_stations_writes_and_is_idempotent(tmp_path, monkeypatch):
    # Purpose: the populate op reads the route's geometry file, injects the
    # resolved station points, and writing twice yields one copy -- the
    # mechanism that keeps the committed geometry files current.
    monkeypatch.setattr(bm, "DATA", tmp_path)
    (tmp_path / "geometry").mkdir()
    geo = tmp_path / "geometry" / "AcelaExpress.geojson"
    geo.write_text(json.dumps(fc(line_feature([[0, 0], [0, 1]]))), encoding="utf-8")
    write_events(tmp_path / "station_events.jsonl",
                 [("AcelaExpress", "WAS", "Washington, DC")])
    added, missing = bm.refresh_route_stations("AcelaExpress", {"WAS": (38.9, -77.0)})
    assert (added, missing) == (1, [])
    gj = json.loads(geo.read_text(encoding="utf-8"))
    assert bm.geojson_station_points(gj) == [(38.9, -77.0, "Washington, DC")]
    bm.refresh_route_stations("AcelaExpress", {"WAS": (38.9, -77.0)})
    gj = json.loads(geo.read_text(encoding="utf-8"))
    assert len(bm.geojson_station_points(gj)) == 1


def test_build_emits_station_dots(tmp_path, monkeypatch):
    # Purpose: end-to-end, a station Point in the geometry file surfaces in the
    # rendered map's CFG as a [lat, lon, name, mile] dot the front-end can draw
    # -- the payload behind the on-map station markers. The 4th field (route
    # mile on the longest branch) is pinned separately in test_profile_data.py;
    # here only the [lat, lon, name] prefix the dots consume is asserted.
    monkeypatch.setattr(bm, "DATA", tmp_path)
    monkeypatch.setattr(bm, "OUT", tmp_path / "out")
    (tmp_path / "geometry").mkdir()
    gj = fc(line_feature([[-71.0, 42.0], [-71.0, 42.2]]),
            stn_feat("BOS", "Boston, MA", 42.05, -71.0))
    (tmp_path / "geometry" / "AcelaExpress.geojson").write_text(
        json.dumps(gj), encoding="utf-8")
    (tmp_path / "observations.jsonl").write_text(json.dumps({
        "route": "AcelaExpress", "train": 2151, "run_date": "2026-07-21",
        "ts": "2026-07-21T10:00:00", "lat": 42.05, "lon": -71.0, "mph": 90,
        "heading": "N", "desc": "x", "src": "live"}) + "\n", encoding="utf-8")
    bm.build("AcelaExpress")
    html = (tmp_path / "out" / "speed_map_AcelaExpress.html").read_text(encoding="utf-8")
    blob = html.split("const CFG = ", 1)[1].split(";\ndocument.title", 1)[0]
    (la, lo, nm, _mile), = json.loads(blob)["stations"]
    assert [la, lo, nm] == [42.05, -71.0, "Boston, MA"]


def test_load_station_index_parses_ntad_cache(tmp_path, monkeypatch):
    # Purpose: pin the contract with the external source's field names -- codes
    # come from "Code", coords from the Point geometry (rounded to 5dp), and a
    # blank code row is skipped. If NTAD renamed a field the index would go
    # silently empty and every station would read as "missing"; this catches
    # that without touching the network.
    cache = tmp_path / "amtrak_stations.geojson"
    cache.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"properties": {"Code": "WAS", "StationName": "Washington, DC"},
         "geometry": {"type": "Point", "coordinates": [-77.006432, 38.896969]}},
        {"properties": {"Code": " ", "StationName": " "},
         "geometry": {"type": "Point", "coordinates": [0, 0]}},
    ]}), encoding="utf-8")
    monkeypatch.setattr(bm, "STATIONS_CACHE", cache)
    assert bm.load_station_index() == {"WAS": (38.89697, -77.00643)}


def test_template_renders_station_layer_with_hover():
    # Purpose: pin that the shipped template actually draws a station layer
    # (fed by CFG.stations), binds a hover tooltip, and exposes it as a
    # toggleable overlay -- the visible half of the feature.
    assert "CFG.stations" in bm.LEAFLET_TMPL
    assert "'Stations'" in bm.LEAFLET_TMPL
    assert re.search(r"stationMarks[\s\S]*bindTooltip", bm.LEAFLET_TMPL)
