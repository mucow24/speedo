"""--reparse provenance: date anchors come from where a snapshot lives.

Rebuilding the datasets offline only works if each raw page gets the same
date anchor it had when fetched: the scrape-day directory name for live
pages, the archive.org timestamp embedded in the filename for wayback
pages. Anchoring everything to "today" instead would mis-year every
historical page (their MM/DD stamps carry no year).
"""
import json

import scrape_railrat as sr
from conftest import marker, synth_page


def test_reparse_anchors_dates_by_snapshot_provenance(tmp_path, monkeypatch):
    # Purpose: two snapshots of the same train, one live from 2026-07-21
    # and one wayback from 2020 -- after reparse, each observation's date
    # must come from its snapshot's own era, and src must record the
    # provenance. Also pins that reparse rewrites (not appends) the files.
    monkeypatch.setattr(sr, "DATA", tmp_path)
    monkeypatch.setattr(sr, "OBS_FILE", tmp_path / "observations.jsonl")
    monkeypatch.setattr(sr, "STN_FILE", tmp_path / "station_events.jsonl")

    live_dir = tmp_path / "raw" / "2026-07-21"
    live_dir.mkdir(parents=True)
    (live_dir / "2151-0721-1100.html").write_text(
        synth_page(updated="11:00&nbsp;on&nbsp;7/21",
                   markers=marker(10, 55, 40.1, -74.0, 120)),
        encoding="utf-8")
    wb_dir = tmp_path / "raw" / "wayback"
    wb_dir.mkdir(parents=True)
    (wb_dir / "2151-20200926175945.html").write_text(
        synth_page(updated="20:13&nbsp;on&nbsp;9/25",
                   markers=marker(20, 10, 36.9, -76.3, 80)),
        encoding="utf-8")

    sr.reparse_raw()

    recs = [json.loads(l) for l in
            (tmp_path / "observations.jsonl").open(encoding="utf-8")]
    assert len(recs) == 2
    by_src = {r["src"]: r for r in recs}
    assert by_src["live"]["ts"] == "2026-07-21T10:55:00"
    assert by_src["wayback:20200926175945"]["ts"] == "2020-09-25T20:10:00"
