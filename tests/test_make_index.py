"""speedo_ctl --make-index: the maps landing page.

--make-index scans out/ for generated maps, reads back the CFG JSON blob
each one embeds, and renders a static index.html linking to them with a few
headline stats. These pin the pure pieces (map discovery, config extraction,
per-map summary, HTML rendering) offline; the thin command wrapper that
writes the file is excluded per TESTING.md.
"""
import json

import build_map as bm
import speedo_ctl as ctl


def write_map(out_dir, slug, cfg):
    """Write a stand-in generated map embedding CFG exactly the way the
    build_map template does -- a single `const CFG = <json>;` line -- so the
    extractor is tested against the real on-disk shape."""
    p = out_dir / f"speed_map_{slug}.html"
    p.write_text(
        "<!DOCTYPE html>\n<script>\nconst CFG = %s;\ndocument.title = CFG.title;\n</script>"
        % json.dumps(cfg, separators=(",", ":")),
        encoding="utf-8")
    return p


def test_discover_maps_lists_one_per_route_ignoring_strays(tmp_path):
    # Purpose: the index lists one card per generated map, keyed by route
    # slug and ordered by it (matching the status table); a prior index.html
    # and other non-map files in out/ are ignored.
    write_map(tmp_path, "KeystoneService", {"display": "Keystone Service"})
    write_map(tmp_path, "AcelaExpress", {"display": "Acela Express"})
    (tmp_path / "index.html").write_text("stale", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")
    maps = ctl.discover_maps(tmp_path)
    assert list(maps) == ["AcelaExpress", "KeystoneService"]
    assert maps["AcelaExpress"].name == "speed_map_AcelaExpress.html"


def test_extract_config_reads_embedded_blob(tmp_path):
    # Purpose: every card's numbers come from the CFG JSON the build embeds
    # in each map; pin that we recover it intact from the shipped
    # `const CFG = ...;` line (the index has no other data source).
    p = write_map(tmp_path, "AcelaExpress",
                  {"display": "Acela Express", "totalMiles": 456.7})
    cfg = ctl.extract_config(p.read_text(encoding="utf-8"))
    assert cfg["display"] == "Acela Express"
    assert cfg["totalMiles"] == 456.7


def test_extract_config_matches_the_real_leaflet_template():
    # Purpose: extract_config is coupled to how build_map embeds CFG. If the
    # template's `const CFG = __CONFIG__;` line ever changes shape, the index
    # would silently read nothing; pin extraction against the real template.
    cfg = {"display": "X", "totalMiles": 10.0, "bins": [], "stats": {}}
    html = bm.LEAFLET_TMPL.replace("__CONFIG__", json.dumps(cfg, separators=(",", ":")))
    assert ctl.extract_config(html)["display"] == "X"


def test_extract_config_returns_none_for_a_non_map():
    # Purpose: out/ may hold files that aren't speedo maps; extraction must
    # report "not one of ours" (None) rather than raise, so --make-index can
    # skip them.
    assert ctl.extract_config("<html>no config here</html>") is None


def test_map_summary_headline_stats():
    # Purpose: a card's headline numbers derive from the embedded CFG -- top
    # speed is the fastest bin max (the maps color by max-per-bin, so the
    # page headlines the same number), avg is the mean of the covered bins'
    # maxes, and coverage is covered bins over all bins. Bins with no data
    # ("mx" absent) drop out of top/avg but still count toward the total.
    cfg = {"display": "Acela Express", "totalMiles": 100.0,
           "stats": {"obs": 1200, "runs": 9, "from": "2025-01-01",
                     "to": "2026-07-01", "built": "2026-07-23"},
           "bins": [{"m": 0.0, "mx": 90}, {"m": 0.5, "mx": 150},
                    {"m": 1.0}, {"m": 1.5, "mx": 120}]}
    s = ctl.map_summary(cfg)
    assert s["top"] == 150
    assert s["avg"] == 120          # mean(90, 150, 120)
    assert s["covered"] == 3 and s["bins"] == 4
    assert s["miles"] == 100.0
    assert s["obs"] == 1200 and s["runs"] == 9
    assert s["from"] == "2025-01-01" and s["to"] == "2026-07-01"


def test_map_summary_handles_a_map_with_no_covered_bins():
    # Purpose: a map can have geometry but every bin still empty (no
    # plausible observations yet); the summary must not divide by zero --
    # top/avg come back None so the renderer shows a dash, not a crash.
    cfg = {"display": "Ghost", "totalMiles": 5.0, "stats": {},
           "bins": [{"m": 0.0}, {"m": 0.5}]}
    s = ctl.map_summary(cfg)
    assert s["top"] is None and s["avg"] is None
    assert s["covered"] == 0 and s["bins"] == 2


def _summary(**over):
    base = {"slug": "AcelaExpress", "leaflet": "speed_map_AcelaExpress.html",
            "display": "Acela Express", "miles": 456.7, "top": 150, "avg": 92,
            "covered": 700, "bins": 900, "obs": 41203, "runs": 38,
            "from": "2025-05-01", "to": "2026-07-21", "built": "2026-07-23"}
    base.update(over)
    return base


def test_render_index_lists_maps_with_links_and_stats():
    # Purpose: the page is the whole deliverable -- pin that each map becomes
    # a card linking to its Leaflet file, showing its display name, length
    # and top speed.
    html = ctl.render_index([
        _summary(),
        _summary(slug="KeystoneService", leaflet="speed_map_KeystoneService.html",
                 display="Keystone Service", miles=104.2, top=110),
    ])
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert 'href="speed_map_AcelaExpress.html"' in html
    assert "Acela Express" in html and "456.7" in html and "150" in html
    assert 'href="speed_map_KeystoneService.html"' in html


def test_render_index_colors_top_speed_like_the_map():
    # Purpose: the card's top-speed number is tinted with the *same* speed
    # gradient the maps use (build_map.speed_color), so the landing page and
    # the maps read as one palette; pin that coupling to the shared function.
    html = ctl.render_index([_summary(top=150)])
    assert bm.speed_color(150) in html


def test_render_index_escapes_display_names():
    # Purpose: display names are interpolated straight into HTML; a stray '&'
    # or '<' must be escaped so route metadata can never break (or inject
    # into) the page.
    html = ctl.render_index([_summary(display="A & B <hack>")])
    assert "A &amp; B &lt;hack&gt;" in html
    assert "<hack>" not in html


def test_render_index_empty_state_guides_the_user():
    # Purpose: with no maps built yet, --make-index still writes a valid page
    # that tells the user how to build some, rather than a blank/broken file.
    html = ctl.render_index([])
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "--make-map" in html
