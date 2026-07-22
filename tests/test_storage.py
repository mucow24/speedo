"""Dataset append/dedup behavior (append_parsed, append_station_events).

The JSONL files are becoming the source of truth (raw/ is being demoted),
so ingest must be lossless: scrape-time no longer discards anything.
Plausibility filtering (GPS glitches) moves to build time.
"""
import io
import json

import scrape_railrat as sr


def parsed_with_points(points):
    return {"route": "AcelaExpress", "train": 2151,
            "run_date": "2026-07-21", "points": points,
            "station_events": []}


POINT = {"ts": "2026-07-21T10:00:00", "lat": 40.0, "lon": -74.0,
         "mph": 120, "heading": "N", "desc": "1 mi N of PHL"}


def test_ingest_keeps_implausible_speeds():
    # Purpose: lossless ingest -- a 999 mph GPS glitch is stored, not
    # discarded. Whether it's plausible is a build-time policy; dropping
    # it at scrape time would be permanent data loss.
    glitch = dict(POINT, mph=999)
    out = io.StringIO()
    added = sr.append_parsed(parsed_with_points([glitch]), set(), "live", out)
    assert added == 1
    assert json.loads(out.getvalue())["mph"] == 999


def test_ingest_dedupes_on_train_time_position():
    # Purpose: re-scraping is always safe -- the same point (same train,
    # timestamp, position) is written once no matter how often it's seen.
    seen = set()
    out = io.StringIO()
    assert sr.append_parsed(parsed_with_points([POINT]), seen, "live", out) == 1
    assert sr.append_parsed(parsed_with_points([POINT]), seen, "live", out) == 0
    assert len(out.getvalue().splitlines()) == 1


def test_station_events_appended_and_deduped_on_content():
    # Purpose: station events dedupe on full content. As a run progresses
    # a station's record gains fields across pages (arrival first, then
    # departure + delay); each distinct variant is kept -- consumers merge
    # by (train, run_date, station) -- but an identical record is not
    # re-appended.
    partial = {"station": "HFD", "name": "Hartford, CT",
               "arr": "2026-07-21T08:10:00", "arr_delay": None,
               "dep": None, "dep_delay": None}
    full = dict(partial, dep="2026-07-21T08:14:00", dep_delay=8)
    seen = set()
    out = io.StringIO()

    def parsed(events):
        return {"route": "AcelaExpress", "train": 2151,
                "run_date": "2026-07-21", "points": [],
                "station_events": events}

    assert sr.append_station_events(parsed([partial]), seen, "live", out) == 1
    assert sr.append_station_events(parsed([partial]), seen, "live", out) == 0
    assert sr.append_station_events(parsed([full]), seen, "live", out) == 1
    recs = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [r["dep"] for r in recs] == [None, "2026-07-21T08:14:00"]
    assert all(r["route"] == "AcelaExpress" and r["train"] == 2151 for r in recs)
