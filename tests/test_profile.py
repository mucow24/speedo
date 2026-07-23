"""Speed-profile graph logic, evaluated under V8 (the real shipped COMMON_JS).

The graph, its stats, the hover cursor and the drag/station selection all live
in build_map.COMMON_JS so they can be tested against the actual browser code
rather than string-pinned. These exercise the pure math -- which branch is the
profile, each bin's plotted speed, the distance/max/avg/time stats, the
mile<->pixel and mile<->route-point mappings, and the selection wash. The SVG
drawing and pointer wiring in the template are thin orchestration over these
and are excluded per TESTING.md.
"""
import json

import build_map as bm
from py_mini_racer import MiniRacer


def js_ctx(bins, sections):
    """V8 context with COMMON_JS and a CFG carrying the given bins/sections."""
    cfg = {
        "maxMph": bm.MAX_MPH, "binMiles": bm.BIN_MILES,
        "anchors": [[v, "#{:02x}{:02x}{:02x}".format(*c)] for v, c in bm.COLOR_ANCHORS],
        "display": "Test Route", "totalMiles": 10.0,
        "stats": {"obs": 1, "runs": 1, "from": "2026-01-01", "to": "2026-01-02"},
        "bins": bins, "sections": sections,
    }
    ctx = MiniRacer()
    ctx.eval("const CFG = " + json.dumps(cfg) + ";")
    ctx.eval("const WASH_BG = '#151515';")
    ctx.eval(bm.COMMON_JS)
    return ctx


def data_bin(m, mx):
    return {"m": m, "mx": mx, "n": 2, "med": mx, "top": ["1", "t"]}


# A 6-bin longest branch (miles 0.0..2.5): four 60-mph bins, a gap, a 30-mph
# bin -- plus a shorter 2-bin branch that must never enter the profile.
LONG = [data_bin(0.0, 60), data_bin(0.5, 60), data_bin(1.0, 60),
        data_bin(1.5, 60), {"m": 2.0}, data_bin(2.5, 30)]
SHORT = [data_bin(3.0, 99), data_bin(3.5, 99)]
BINS = LONG + SHORT
SECTIONS = [[0, 5], [6, 7]]


def ctx():
    return js_ctx(BINS, SECTIONS)


def jeval(c, expr):
    return json.loads(c.eval("JSON.stringify(" + expr + ")"))


def test_longest_section_is_the_profile_branch():
    # Purpose: the graph is one branch -- the longest -- chosen by SIZE, not by
    # list position. longestSection() must return the 6-bin range whether that
    # range is listed first or (perturbed below) second; the second assertion is
    # what a regression to `return CFG.sections[0]` would fail on, since with the
    # order flipped the first-listed section is the short 2-bin one.
    c = ctx()
    assert jeval(c, "longestSection()") == [0, 5]     # longest listed first
    c.eval("CFG.sections = [[6, 7], [0, 5]]")         # now list the short one first
    assert jeval(c, "longestSection()") == [0, 5]     # still picked, by size not position


def test_bin_speed_tracks_binstate():
    # Purpose: the plotted speed per bin is its displayed speed -- max where
    # measured, the interpolated fill across a gap, null where the toggles leave
    # it blank -- so the graph line and the map track can never disagree.
    c = ctx()
    assert c.eval("binSpeed(%s)" % json.dumps(data_bin(1.0, 60))) == 60
    assert c.eval("binSpeed({m: 2.0})") is None            # no data -> gap
    assert c.eval("binSpeed({m: 2.0, ia: [70, 2]})") == 70  # interp fill shows


def test_profile_points_break_at_gaps():
    # Purpose: profilePoints walks the longest branch in mile order and emits a
    # null mph exactly where a bin has no plotted speed, so the SVG can break
    # the line there instead of drawing a false segment across the gap.
    pts = jeval(ctx(), "profilePoints()")
    assert [p["m"] for p in pts] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    assert [p["mph"] for p in pts] == [60, 60, 60, 60, None, 30]


def test_profile_stats_full_branch():
    # Purpose: pin the four headline stats over the whole branch. Five bins
    # carry a speed (the gap contributes nothing): distance = 5*0.5 = 2.5 mi,
    # max = 60, time = 4*(0.5/60)+(0.5/30) = 0.05 h = 3 min, and average =
    # distance/time = 50 mph (the harmonic mean, so avg*time == distance).
    s = jeval(ctx(), "profileStats(0, 100)")
    assert s["nbins"] == 5
    assert s["miles"] == 2.5
    assert s["maxMph"] == 60
    assert abs(s["hours"] - 0.05) < 1e-9
    assert s["avgMph"] == 50


def test_profile_stats_windowed_excludes_out_of_range_and_other_branch():
    # Purpose: a mile window [0, 1.0] keeps only bins 0..2 (all 60 mph) and must
    # ignore both the slower bins past mile 1.0 and the entire short branch --
    # the stats describe exactly the stretch the graph is zoomed to.
    s = jeval(ctx(), "profileStats(0, 1.0)")
    assert s["nbins"] == 3
    assert s["miles"] == 1.5
    assert s["avgMph"] == 60


def test_fmt_dur_minutes_and_hours():
    # Purpose: the time readout stays compact -- under an hour reads "M min",
    # an hour or more reads "Hh Mm", rounded to whole minutes.
    c = ctx()
    assert c.eval("fmtDur(0.05)") == "3 min"          # 3.0 min
    assert c.eval("fmtDur(1.5)") == "1h 30m"
    assert c.eval("fmtDur(2.0 + 5/60)") == "2h 5m"


def test_point_along_interpolates_by_arc_length():
    # Purpose: the hover cursor sits at a fractional distance along a bin's
    # polyline; pointAlong must interpolate by arc length (half-way along a
    # 4-unit path is the midpoint), and pin both ends.
    c = ctx()
    line = "[[0,0],[0,2],[0,4]]"
    assert jeval(c, "pointAlong(%s, 0.5)" % line) == [0, 2]
    assert jeval(c, "pointAlong(%s, 0.25)" % line) == [0, 1]
    assert jeval(c, "pointAlong(%s, 0)" % line) == [0, 0]
    assert jeval(c, "pointAlong(%s, 1)" % line) == [0, 4]


def test_bin_at_mile_maps_and_clamps_to_branch():
    # Purpose: a graph mile maps to the branch bin under it (mile 1.2 -> bin 2,
    # covering [1.0,1.5)), and miles past either end clamp to the branch's first
    # and last bins rather than escaping into the other section.
    c = ctx()
    assert c.eval("binAtMile(1.2)") == 2
    assert c.eval("binAtMile(2.5)") == 5
    assert c.eval("binAtMile(-9)") == 0
    assert c.eval("binAtMile(99)") == 5      # clamps to branch end, not bin 6/7


def test_graph_scales_round_trip():
    # Purpose: the mile<->x scales are inverse over the plot's [x0,x1] pixels,
    # and y maps 0 mph to the baseline and maxMph to the top; drags read pixels
    # back to miles, so a round trip must land where it started.
    c = ctx()
    assert c.eval("graphX(1.0, 0, 2.0, 30, 300)") == 165
    assert c.eval("xToMile(165, 0, 2.0, 30, 300)") == 1.0
    assert c.eval("xToMile(10, 0, 2.0, 30, 300)") == 0.0    # left of plot clamps
    assert c.eval("graphY(0, 160, 10, 130)") == 130         # 0 mph at bottom
    assert c.eval("graphY(160, 160, 10, 130)") == 10        # maxMph at top
    assert c.eval("graphY(80, 160, 10, 130)") == 70


def test_order_pair_sorts_selection_ends():
    # Purpose: clicking two stations in either order yields the same [lo,hi]
    # mile window -- the selection can't come out inverted.
    c = ctx()
    assert jeval(c, "orderPair(5, 2)") == [2, 5]
    assert jeval(c, "orderPair(2, 5)") == [2, 5]


def style_color(c, bin_js):
    return c.eval("binStyle(%s).color" % bin_js)


def test_selection_dims_track_outside_the_window():
    # Purpose: an active mile selection washes every bin outside it and leaves
    # those inside untouched -- the "dim the unselected parts of the route" half
    # of drag/station select. This is orthogonal to the speed-range wash.
    c = ctx()
    inside = json.dumps(data_bin(0.75, 60))
    outside = json.dumps(data_bin(2.5, 60))
    c.eval("S.selLo = 0.5; S.selHi = 1.0;")
    assert style_color(c, inside) == c.eval("speedColor(60)")          # in window
    assert style_color(c, outside) == c.eval("washColor(speedColor(60))")  # dimmed


def test_no_selection_leaves_all_track_undimmed():
    # Purpose: with no selection active (the default), the wash from this
    # feature must be entirely absent -- the map renders as it did before, so
    # the feature ships inert until the user drags or clicks two stations.
    c = ctx()
    assert style_color(c, json.dumps(data_bin(2.5, 60))) == c.eval("speedColor(60)")


def test_speed_gradient_stops_color_each_position_by_speed():
    # Purpose: the profile reads "by speed" because a horizontal gradient places
    # a stop at each point's fraction across the domain, coloured by that point's
    # speed (speedColor). Pin the offsets (0/50/100% across miles 0..2) and that
    # each stop's colour is the speed colour, so slow and fast stretches differ.
    c = ctx()
    pts = "[{m:0,mph:0},{m:1,mph:80},{m:2,mph:160}]"
    stops = c.eval(f"speedGradientStops({pts}, 0, 2)")
    assert stops.count("<stop") == 3
    for off in ("0.00%", "50.00%", "100.00%"):
        assert f'offset="{off}"' in stops
    for mph in (0, 80, 160):
        assert f'stop-color="{c.eval(f"speedColor({mph})")}"' in stops


def test_speed_gradient_stops_empty_is_neutral():
    # Purpose: a window with no plotted speed (all gaps) has no colour to show;
    # the gradient falls back to a single neutral stop rather than emitting
    # invalid empty-gradient markup.
    c = ctx()
    assert c.eval("speedGradientStops([], 0, 10)") == '<stop offset="0%" stop-color="#9aa0a6"/>'


def test_speed_gradient_stops_downsampled_to_cap():
    # Purpose: a long branch (thousands of bins) must not emit a stop per bin --
    # the stop list is capped (endpoints kept) so the gradient stays light while
    # the ramp stays smooth. 1000 points, cap 10 -> 10 stops spanning 0..100%.
    c = ctx()
    pts = "Array.from({length:1000},(_,i)=>({m:i,mph:60}))"
    stops = c.eval(f"speedGradientStops({pts}, 0, 999, 10)")
    assert stops.count("<stop") == 10
    assert 'offset="0.00%"' in stops and 'offset="100.00%"' in stops
