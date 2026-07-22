"""Observation loading for the map build (load_observations).

With lossless ingest, the GPS-glitch ceiling is applied here -- at build
time -- instead of at scrape time, so a wrong threshold is a rebuild away
from being fixed rather than permanent data loss.
"""
import json

import build_map as bm


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")


def rec(route="AcelaExpress", mph=100, ts="2026-07-21T10:00:00"):
    return {"route": route, "train": 2151, "run_date": "2026-07-21",
            "ts": ts, "lat": 40.0, "lon": -74.0, "mph": mph,
            "heading": "N", "desc": "x", "src": "live"}


def test_load_filters_glitches_and_other_routes(tmp_path):
    # Purpose: the build sees only this route's plausible points -- a 999
    # mph glitch (stored by lossless ingest) and another route's points
    # are both excluded; a top-of-scale-legal 160 mph point survives.
    f = tmp_path / "obs.jsonl"
    write_jsonl(f, [rec(mph=120), rec(mph=999), rec(mph=160),
                    rec(route="NortheastRegional", mph=80)])
    obs = bm.load_observations(f, "AcelaExpress")
    assert [o["mph"] for o in obs] == [120, 160]


def test_glitch_ceiling_is_inclusive(tmp_path):
    # Purpose: pin the boundary -- exactly MAX_PLAUSIBLE_MPH (170) is kept,
    # one above is dropped, matching the old scrape-time filter's rule so
    # the dataset's contents don't shift meaning.
    f = tmp_path / "obs.jsonl"
    write_jsonl(f, [rec(mph=bm.MAX_PLAUSIBLE_MPH),
                    rec(mph=bm.MAX_PLAUSIBLE_MPH + 1)])
    obs = bm.load_observations(f, "AcelaExpress")
    assert [o["mph"] for o in obs] == [bm.MAX_PLAUSIBLE_MPH]
