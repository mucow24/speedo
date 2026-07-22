"""Position-report parsing (parse_train_page): the existing core behavior.

These are characterization tests pinning the parser that produced the
whole current dataset, so upcoming changes (station events, lossless
ingest) can't silently alter it. Expected values were captured from the
parser's output on the fixture pages and sanity-checked by hand against
the page HTML (marker counts, first/last popup times, updated stamps).
"""
import datetime as dt

import scrape_railrat as sr
from conftest import fixture_text, marker, synth_page


def test_current_page_positions():
    # Purpose: pin route/train identification and full point extraction on
    # current (2026) page markup. Page says "updated 15:23 on 07/21"; its
    # newest marker is the 15:23 report 1 mi NE of WAS.
    parsed = sr.parse_train_page(
        fixture_text("train125_current_2026-07-21.html"),
        now=dt.datetime(2026, 7, 21, 15, 30))
    assert parsed["route"] == "NortheastRegional"
    assert parsed["train"] == 125
    assert parsed["run_date"] == "2026-07-21"
    assert len(parsed["points"]) == 85
    assert parsed["points"][0] == {
        "ts": "2026-07-21T15:23:00", "lat": 38.912071, "lon": -76.996874,
        "mph": 27, "heading": "SW", "desc": "1 mi NE of WAS"}
    assert parsed["points"][-1]["ts"] == "2026-07-21T07:35:00"


def test_wayback_page_positions_and_year_inference():
    # Purpose: pin parsing of the 2020-era markup, and that infer_year
    # anchors dates to the snapshot's era (page says "updated 20:13 on
    # 09/25" with no year; the archive.org snapshot is 2020-09-26).
    parsed = sr.parse_train_page(
        fixture_text("train125_wayback_2020-09-26.html"),
        now=dt.datetime(2020, 9, 26))
    assert parsed["train"] == 125
    assert parsed["run_date"] == "2020-09-25"
    assert len(parsed["points"]) == 96
    assert parsed["points"][0]["ts"] == "2020-09-25T20:13:00"
    assert parsed["points"][0]["mph"] == 15


def test_infer_year_skips_invalid_and_future_dates():
    # Purpose: page dates are MM/DD with no year; infer_year must pick the
    # most recent year where the date exists and isn't in the future.
    # Feb 29 seen mid-2025 can only be 2024 (2025/2026 aren't leap years).
    assert sr.infer_year(2, 29, dt.datetime(2025, 6, 1)) == dt.date(2024, 2, 29)
    # A date just ahead of "now" resolves to last year, not the future.
    assert sr.infer_year(8, 1, dt.datetime(2026, 7, 21)) == dt.date(2025, 8, 1)


def test_misreported_route_slugs_are_canonicalized():
    # Purpose: train pages sometimes self-declare a route slug that differs
    # from the route-index slug the rest of the pipeline keys on (rosters,
    # build_map ROUTES, the scrape route filter). Both known offenders must
    # canonicalize: Keystone pages say "Keystone", and 2020-era Michigan
    # pages say "MichiganServices" -- without the alias, those wayback runs
    # get filed under a route name nothing else recognizes.
    for wrong, canonical in [("Keystone", "KeystoneService"),
                             ("MichiganServices", "WolverineMichiganService")]:
        page = synth_page(updated="10:30&nbsp;on&nbsp;7/21",
                          markers=marker(10, 0, 40.1, -74.0, 100),
                          route_slug=wrong, route_name="X")
        parsed = sr.parse_train_page(page, now=dt.datetime(2026, 7, 21, 11, 0))
        assert parsed["route"] == canonical


def test_positions_crossing_midnight_get_dated_backward():
    # Purpose: the date-walk decrements the date when, walking into the
    # past, the clock jumps far forward -- i.e. we crossed midnight.
    # Newest-first document order: 00:05 (today), then 23:50 (yesterday).
    page = synth_page(
        updated="0:15&nbsp;on&nbsp;7/22",
        markers=marker(0, 5, 40.2, -74.1, 90) + "\n" + marker(23, 50, 40.1, -74.0, 110))
    parsed = sr.parse_train_page(page, now=dt.datetime(2026, 7, 22, 0, 20))
    assert [p["ts"] for p in parsed["points"]] == [
        "2026-07-22T00:05:00", "2026-07-21T23:50:00"]


def test_points_dated_from_updated_stamp_not_newest_point_clock():
    # Purpose: pin that position points are dated by walking back from the
    # page's dated "updated HH:MM on MM/DD" stamp (as ARCHITECTURE.md
    # describes), NOT from the newest point's bare clock. A report that
    # arrived at 23:55 while the page didn't refresh until 00:12 the next
    # day belongs to the PRIOR day. Anchoring at the newest point's own
    # clock (23:55) with the update stamp's date (07/22) mis-dates every
    # report a full day forward -- the root of run_date instability.
    page = synth_page(
        updated="0:12&nbsp;on&nbsp;7/22",
        markers="\n".join([marker(23, 55, 45.1, -93.3, 79),
                           marker(23, 50, 45.0, -93.2, 70),
                           marker(23, 40, 44.9, -93.1, 60)]))
    parsed = sr.parse_train_page(page, now=dt.datetime(2026, 7, 22, 0, 20))
    assert [p["ts"] for p in parsed["points"]] == [
        "2026-07-21T23:55:00", "2026-07-21T23:50:00", "2026-07-21T23:40:00"]
    assert parsed["run_date"] == "2026-07-21"


def test_run_date_is_stable_across_partial_and_full_snapshots():
    # Purpose: the core regression. run_date must identify the physical run
    # (its departure date) identically no matter which snapshot of the run is
    # parsed. RailRat serves one accumulating run per train page, so an early
    # scrape just after midnight (reports only from the prior evening) and a
    # later scrape (whose points now span midnight) are the SAME run and must
    # get the SAME run_date. Before the fix, the early snapshot mis-dates its
    # pre-midnight reports forward and reports 2026-07-22, while the later,
    # midnight-spanning snapshot reports 2026-07-21 -- so build_map groups one
    # run under two (train, run_date) keys and double-counts it.
    early = sr.parse_train_page(
        synth_page(updated="0:20&nbsp;on&nbsp;7/22",
                   markers="\n".join([marker(23, 55, 45.1, -93.3, 79),
                                      marker(23, 50, 45.0, -93.2, 70),
                                      marker(23, 40, 44.9, -93.1, 60)])),
        now=dt.datetime(2026, 7, 22, 0, 25))
    later = sr.parse_train_page(
        synth_page(updated="8:00&nbsp;on&nbsp;7/22",
                   markers="\n".join([marker(8, 0, 46.0, -94.0, 50),
                                      marker(0, 10, 45.2, -93.4, 80),
                                      marker(23, 55, 45.1, -93.3, 79),
                                      marker(23, 50, 45.0, -93.2, 70),
                                      marker(23, 40, 44.9, -93.1, 60)])),
        now=dt.datetime(2026, 7, 22, 8, 5))
    assert early["run_date"] == later["run_date"] == "2026-07-21"
