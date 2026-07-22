"""Station-event extraction from the Progress Tracker (new dataset).

RailRat's tracker carries per-station *actual* arrival/departure times and
delay-vs-schedule -- data the position markers don't have. These tests
define the extraction across both page formats we hold snapshots of.
All expected values were read by hand from the fixture HTML.

Event fields per station: arr/dep (ISO timestamps or None), arr_delay/
dep_delay (minutes, late positive / early negative / None if the page
doesn't state one). "est." entries are predictions, not events, and must
be skipped -- the actuals appear on later pages of the same run.
"""
import datetime as dt

import scrape_railrat as sr
from conftest import fixture_text, synth_page

NOW_2026 = dt.datetime(2026, 7, 21, 15, 30)


def events_by_station(parsed):
    return {e["station"]: e for e in parsed["station_events"]}


def test_current_format_extracts_only_actual_events():
    # Purpose: of the 33 tracker rows, 23 are actuals (SPG..NCR) and 10 are
    # "est." predictions (WAS..NPN) that must not become events.
    parsed = sr.parse_train_page(
        fixture_text("train125_current_2026-07-21.html"), now=NOW_2026)
    assert len(parsed["station_events"]) == 23
    codes = {e["station"] for e in parsed["station_events"]}
    assert "WAS" not in codes and "NPN" not in codes


def test_current_format_departure_delay_and_plain_arrival():
    # Purpose: current markup states delay only for the departure; the
    # arrival is a bare time inside the viewport span. "HFD, departed
    # 08:14, 8 min. late, arrived 08:10".
    parsed = sr.parse_train_page(
        fixture_text("train125_current_2026-07-21.html"), now=NOW_2026)
    hfd = events_by_station(parsed)["HFD"]
    assert hfd["dep"] == "2026-07-21T08:14:00"
    assert hfd["dep_delay"] == 8
    assert hfd["arr"] == "2026-07-21T08:10:00"
    assert hfd["arr_delay"] is None
    assert hfd["name"] == "Hartford, CT"


def test_current_format_origin_and_on_time():
    # Purpose: the origin has a departure only ("SPG, departed 07:28 ET,
    # on time") -- no arrival, and "on time" parses as delay 0.
    parsed = sr.parse_train_page(
        fixture_text("train125_current_2026-07-21.html"), now=NOW_2026)
    spg = events_by_station(parsed)["SPG"]
    assert spg["dep"] == "2026-07-21T07:28:00"
    assert spg["dep_delay"] == 0
    assert spg["arr"] is None and spg["arr_delay"] is None


def test_current_format_span_wrapped_delay():
    # Purpose: big delays come color-wrapped -- 'BAL, departed 14:45,
    # <span class="yellow">17 min. late</span>' -- the markup must not
    # hide the number.
    parsed = sr.parse_train_page(
        fixture_text("train125_current_2026-07-21.html"), now=NOW_2026)
    assert events_by_station(parsed)["BAL"]["dep_delay"] == 17


def test_old_format_departure_arrival_early_and_completed():
    # Purpose: 2020-era markup has one bold verb per row with "ET"
    # suffixes; delays can be negative ("WBG, arrived 19:45 ET, 4 min.
    # early"); the destination row is "NPN, completed, 20:17 ET" which is
    # an arrival with no stated delay. NPN's 20:17 is later than the
    # page's own "updated 20:13" stamp -- the date-walk must tolerate a
    # slightly stale stamp without shifting the date.
    parsed = sr.parse_train_page(
        fixture_text("train125_wayback_2020-09-26.html"),
        now=dt.datetime(2020, 9, 26))
    ev = events_by_station(parsed)
    assert len(parsed["station_events"]) == 19
    assert ev["NYP"]["dep"] == "2020-09-25T11:35:00"
    assert ev["NYP"]["dep_delay"] == 0
    assert ev["ALX"]["dep_delay"] == 3
    assert ev["WBG"]["arr"] == "2020-09-25T19:45:00"
    assert ev["WBG"]["arr_delay"] == -4
    assert ev["WBG"]["dep"] is None
    assert ev["NPN"]["arr"] == "2020-09-25T20:17:00"
    assert ev["NPN"]["arr_delay"] is None and ev["NPN"]["dep"] is None


def test_station_events_crossing_midnight():
    # Purpose: station times are clock-only; dates come from walking
    # backward from the updated stamp, crossing midnight when the clock
    # jumps far forward into the past. Alpha's events land yesterday,
    # Beta's today.
    lis = (
        '<li><a href="/stations/AAA/" title="Alpha, PA">AAA</a>, '
        '<b>departed</b> 23:50, on time'
        '<span class="viewport-1"><i>, arrived 23:45 (Alpha)</i></span>.\n'
        '<li><a href="/stations/BBB/" title="Beta, PA">BBB</a>, '
        '<b>arrived</b> 00:05, 2 min. late'
        '<span class="viewport-1"><i> (Beta)</i></span>.')
    page = synth_page(updated="0:15&nbsp;on&nbsp;7/22", tracker_lis=lis)
    parsed = sr.parse_train_page(page, now=dt.datetime(2026, 7, 22, 0, 20))
    ev = events_by_station(parsed)
    assert ev["AAA"]["arr"] == "2026-07-21T23:45:00"
    assert ev["AAA"]["dep"] == "2026-07-21T23:50:00"
    assert ev["AAA"]["dep_delay"] == 0
    assert ev["BBB"]["arr"] == "2026-07-22T00:05:00"
    assert ev["BBB"]["arr_delay"] == 2


def test_page_with_events_but_no_positions_still_parses():
    # Purpose: a page is worth parsing if it has *either* dataset. (The
    # old rule -- no position markers means return None -- would discard
    # tracker data on position-less pages.)
    lis = ('<li><a href="/stations/AAA/" title="Alpha, PA">AAA</a>, '
           '<b>departed</b> 10:00, on time.')
    page = synth_page(updated="10:30&nbsp;on&nbsp;7/21", tracker_lis=lis)
    parsed = sr.parse_train_page(page, now=dt.datetime(2026, 7, 21, 11, 0))
    assert parsed is not None
    assert parsed["points"] == []
    assert parsed["station_events"][0]["dep"] == "2026-07-21T10:00:00"
    assert parsed["run_date"] == "2026-07-21"
