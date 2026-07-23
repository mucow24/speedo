"""Speed-range highlight: legend handles pick [lo, hi] mph; everything else washes out.

The feature lives entirely in the shared front-end JS (build_map.COMMON_JS),
so these tests evaluate the real shipped code in an embedded V8 (mini-racer)
instead of string-pinning the template: they exercise the actual style
decisions the browser will make. DOM wiring (pointer events, handle
positioning) is thin orchestration over these functions and is excluded per
TESTING.md.
"""
import json

import build_map as bm
from py_mini_racer import MiniRacer


def js_ctx():
    """Fresh V8 context with a minimal CFG and the shipped COMMON_JS loaded."""
    cfg = {
        "maxMph": bm.MAX_MPH,
        "anchors": [[v, "#{:02x}{:02x}{:02x}".format(*c)] for v, c in bm.COLOR_ANCHORS],
        "display": "Test Route", "binMiles": bm.BIN_MILES, "totalMiles": 10.0,
        "stats": {"obs": 1, "runs": 1, "from": "2026-01-01", "to": "2026-01-02"},
    }
    ctx = MiniRacer()
    ctx.eval("const CFG = " + json.dumps(cfg) + ";")
    ctx.eval("const WASH_BG = '#151515';")  # the template defines this; the CARTO dark tone
    ctx.eval(bm.COMMON_JS)
    return ctx


def style_of(ctx, bin_js):
    return json.loads(ctx.eval(f"JSON.stringify(binStyle({bin_js}))"))


DATA_120 = '{m: 1.0, mx: 120, n: 3, med: 90, top: ["2151", "07/01 08:00"]}'
NODATA = "{m: 1.0}"


def set_range(ctx, lo, hi):
    ctx.eval(f"S.lo = {lo}; S.hi = {hi};")


def test_default_full_range_changes_nothing():
    # Purpose: the filter ships inactive -- at the default full range the map
    # must render exactly as it did before the feature existed (no wash on
    # data, interpolated, or no-data bins).
    ctx = js_ctx()
    assert style_of(ctx, DATA_120)["opacity"] == 0.95
    assert style_of(ctx, "{m: 1.0, ia: [70, 2]}")["opacity"] == 0.85
    assert style_of(ctx, NODATA)["opacity"] == 0.95


def test_out_of_range_data_bin_washes_by_color_not_alpha():
    # Purpose: the wash pre-blends the speed color toward the basemap tone
    # (WASH_BG) at full line opacity, NOT by dropping alpha: translucent
    # lines additively brighten wherever they overlap at high zoom (bin
    # joints, parallel track), while opaque pre-blended color composites
    # identically everywhere. Both halves of this assert pin that bug fix.
    ctx = js_ctx()
    set_range(ctx, 40, 100)
    washed = style_of(ctx, DATA_120)  # 120 mph > hi=100
    assert washed["color"] == ctx.eval("washColor(speedColor(120))")
    assert washed["opacity"] == 0.95  # unchanged from normal: no alpha wash


def test_wash_color_blends_strongly_toward_basemap_tone():
    # Purpose: pin the blend itself -- 85% of the way toward WASH_BG,
    # channel-wise -- strong enough to clearly recede, weak enough that the
    # hue stays faintly readable. WASH_BG here is #151515 = (21,21,21);
    # blending the red anchor (220,30,30) gives (51,22,22).
    ctx = js_ctx()
    assert ctx.eval("washColor('rgb(220,30,30)')") == "rgb(51,22,22)"
    assert ctx.eval("washColor('#dc1e1e')") == "rgb(51,22,22)"  # hex parses too


def test_in_range_and_boundary_bins_render_normally():
    # Purpose: the range is inclusive at both ends -- a bin exactly at lo or
    # hi is "within the range" and must render normally, not washed.
    ctx = js_ctx()
    set_range(ctx, 50, 120)
    at_hi = style_of(ctx, DATA_120)  # mx == hi
    at_lo = style_of(ctx, '{m: 1.0, mx: 50, n: 1, med: 50, top: ["1", "t"]}')
    assert at_hi["opacity"] == 0.95
    assert at_lo["opacity"] == 0.95


def test_out_of_range_interp_bin_washed_but_keeps_dash():
    # Purpose: interpolated bins are judged by their interpolated speed and
    # keep their dashed "estimated" look under the wash, so dimmed track still
    # reads as estimated rather than measured.
    ctx = js_ctx()
    set_range(ctx, 100, 160)
    washed = style_of(ctx, "{m: 1.0, ia: [70, 2]}")  # interp 70 < lo=100
    assert washed["color"] == ctx.eval("washColor(speedColor(70))")
    assert washed["dash"] == "6 6"


def test_nodata_bin_washed_only_while_range_active():
    # Purpose: per the chosen design, gray no-data bins dim whenever a range
    # is active (they are not "in range"), so only in-range track stands out;
    # with no range set they keep their normal gray dash.
    ctx = js_ctx()
    assert style_of(ctx, NODATA)["color"] == "#9aa0a6"
    set_range(ctx, 40, 100)
    assert style_of(ctx, NODATA)["color"] == ctx.eval("washColor('#9aa0a6')")


def test_drag_clamps_to_color_scale_ends():
    # Purpose: dragging a handle past either end of the bar must pin the bound
    # to the scale's [0, maxMph] -- pointer x can leave the bar during a drag.
    ctx = js_ctx()
    assert json.loads(ctx.eval("JSON.stringify(dragRange('lo', -20, 40, 100, 160))")) == [0, 100]
    assert json.loads(ctx.eval("JSON.stringify(dragRange('hi', 999, 40, 100, 160))")) == [40, 160]


def test_drag_handles_cannot_cross():
    # Purpose: the lower handle stops at the upper bound and vice versa, so
    # the selection can collapse to a single speed but never invert.
    ctx = js_ctx()
    assert json.loads(ctx.eval("JSON.stringify(dragRange('lo', 130, 40, 100, 160))")) == [100, 100]
    assert json.loads(ctx.eval("JSON.stringify(dragRange('hi', 10, 40, 100, 160))")) == [40, 40]


def test_drag_rounds_to_whole_mph():
    # Purpose: pixel positions map to fractional mph; bounds are kept as whole
    # mph so the readout and the filter agree on the same integer values.
    ctx = js_ctx()
    assert json.loads(ctx.eval("JSON.stringify(dragRange('lo', 63.4, 0, 160, 160))")) == [63, 160]


def test_leaflet_declares_dark_wash_background():
    # Purpose: the wash blends toward the basemap tone behind the lines. The
    # sole engine's basemap is CARTO Dark Matter, so the template must declare
    # the matching dark WASH_BG (#151515) -- a light tone would make "dimmed"
    # out-of-range track glow against the dark tiles instead of receding.
    assert "const WASH_BG = '#151515'" in bm.LEAFLET_TMPL


def test_legend_markup_carries_range_controls():
    # Purpose: pin the contract between legendHtml() and the range wiring --
    # the drag code looks these ids up, so dropping one silently kills the
    # speed-range highlight feature.
    ctx = js_ctx()
    html = ctx.eval("legendHtml()")
    for el_id in ("h-lo", "h-hi", "rng-wrap", "rng-label", "rng-reset"):
        assert f'id="{el_id}"' in html
