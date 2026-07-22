"""speedo_ctl logic: route discovery, dataset stats, coverage, job planning.

The manager's status table and queue decisions are pure functions over the
geometry folder and the observations file; these tests pin them offline.
Command orchestration (argparse wiring, the sequential executor) is thin
sequencing of already-tested pieces and is excluded per TESTING.md.
"""
import json

import speedo_ctl as ctl


def obs(route, train, ts, mph, lat=40.0, lon=-74.0, src="live"):
    return json.dumps({"route": route, "train": train, "run_date": ts[:10],
                       "ts": ts, "lat": lat, "lon": lon, "mph": mph,
                       "heading": "N", "desc": "", "src": src})


def test_discover_routes_scans_geometry_folder(tmp_path):
    # Purpose: the set of routes speedo_ctl manages is defined by the
    # data/geometry cache, not a hardcoded list -- dropping in a .geojson
    # adds a route; other files are ignored.
    (tmp_path / "Vermonter.geojson").write_text("{}", encoding="utf-8")
    (tmp_path / "AcelaExpress.geojson").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    assert ctl.discover_routes(tmp_path) == ["AcelaExpress", "Vermonter"]


def test_collect_route_stats_aggregates(tmp_path):
    # Purpose: the status table's per-route numbers (point count, distinct
    # trains, freshest timestamp, wayback presence) come from one pass over
    # observations.jsonl; pin each aggregation rule, including that a route
    # with geometry but no data still gets a (zeroed) row.
    f = tmp_path / "obs.jsonl"
    f.write_text("\n".join([
        obs("A", 1, "2026-07-01T08:00", 100),
        obs("A", 1, "2026-07-02T09:00", 120),
        obs("A", 2, "2026-06-30T10:00", 90, src="wayback:20200926175945"),
        obs("B", 7, "2026-05-01T12:00", 80),
    ]) + "\n", encoding="utf-8")
    stats = ctl.collect_route_stats(f, ["A", "B", "C"])
    assert stats["A"]["points"] == 3
    assert stats["A"]["trains"] == 2
    assert stats["A"]["latest"] == "2026-07-02T09:00"
    assert stats["A"]["wayback"] is True
    assert stats["B"]["points"] == 1
    assert stats["B"]["wayback"] is False
    assert stats["C"] == {"points": 0, "trains": 0, "latest": None,
                          "wayback": False, "plausible": []}


def test_collect_route_stats_plausibility_band(tmp_path):
    # Purpose: coverage must reflect the same plausibility band the map
    # build applies -- GPS glitches (>170 mph) and stopped trains (<10 mph)
    # don't count as covering track, though they still count as stored
    # points (the DB is lossless).
    f = tmp_path / "obs.jsonl"
    f.write_text("\n".join([
        obs("A", 1, "2026-07-01T08:00", 100, lat=41.0),
        obs("A", 1, "2026-07-01T08:05", 200, lat=42.0),   # GPS glitch
        obs("A", 1, "2026-07-01T08:10", 3, lat=43.0),     # stopped train
    ]) + "\n", encoding="utf-8")
    stats = ctl.collect_route_stats(f, ["A"])
    assert stats["A"]["points"] == 3
    assert stats["A"]["plausible"] == [(41.0, -74.0)]


def test_coverage_counts_bins_with_data():
    # Purpose: coverage is the fraction of half-mile bins containing at
    # least one plausible on-route observation, projected exactly as
    # build_map projects them: two points in one bin cover one bin, and a
    # point >2 mi off-route covers nothing.
    line = [(40.0 + i * (10 / 69.05) / 10, -74.0) for i in range(11)]  # 10 mi due north
    pts = [(40.001, -74.0),                 # bin 0
           (40.002, -74.0),                 # bin 0 again -- same bin, not a new one
           (40.0 + 9.75 / 69.05, -74.0),    # last bin
           (40.05, -73.0)]                  # ~50 mi east: off-route, ignored
    covered, total = ctl.coverage([line], None, pts)
    assert total == 20
    assert covered == 2


def test_format_status_table_rows():
    # Purpose: the table is the entire no-args UI; pin the column rendering
    # (thousands separators, one-decimal coverage, '-' for not-applicable,
    # yes/no wayback) so a formatting regression is visible in tests.
    rows = [
        {"name": "AcelaExpress", "points": 41203, "trains": 38,
         "coverage": (157, 200), "latest": "2026-07-21T14:32:00", "wayback": True},
        {"name": "Vermonter", "points": 0, "trains": 0,
         "coverage": None, "latest": None, "wayback": False},
    ]
    text = ctl.format_status_table(rows)
    lines = text.splitlines()
    assert "Route" in lines[0] and "Coverage" in lines[0] and "Wayback" in lines[0]
    # Route names are right-justified (header too), so every name ends flush
    # two spaces left of the Points column instead of trailing ragged gaps.
    assert lines[0].startswith("       Route  ")     # "Route" padded to "AcelaExpress"
    assert lines[2].startswith("   Vermonter  ")
    acela = next(ln for ln in lines if "AcelaExpress" in ln)
    assert "41,203" in acela and "78.5%" in acela
    assert "2026-07-21 14:32" in acela and "yes" in acela
    verm = next(ln for ln in lines if "Vermonter" in ln)
    assert "%" not in verm and "-" in verm and "no" in verm


def test_normalize_routes_canonicalizes_and_dedupes():
    # Purpose: CLI route args accept RailRat aliases and display-name
    # spacing, and a repeated route (the usage example literally repeats
    # one) must not queue duplicate jobs.
    got = ctl.normalize_routes(["Keystone", "Empire Builder", "EmpireBuilder"])
    assert got == ["KeystoneService", "EmpireBuilder"]


def test_plan_jobs_orders_live_before_wayback():
    # Purpose: a full update front-loads every fast live scrape before any
    # slow wayback pass, so fresh data lands early even if archive.org
    # later forces the wayback queue to abort.
    assert ctl.plan_jobs(["A", "B"], wayback=True) == [
        ("live", "A"), ("live", "B"), ("wayback", "A"), ("wayback", "B")]
    assert ctl.plan_jobs(["A", "B"], wayback=False) == [("live", "A"), ("live", "B")]
