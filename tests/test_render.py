"""Shipped-template pins: inline JS parse-validity, and the Leaflet basemap.

The whole map UI chrome is dark (COMMON_CSS), so the backing tiles have to be
dark too or they fight the theme. These pin the basemap baked into the Leaflet
template: it must be CARTO Dark Matter *with* labels (the `dark_all` style),
not the light OpenStreetMap raster tiles it used to serve. The basemap choice
is a shipped decision, not incidental, so a revert or a broken tile URL should
turn a test red rather than silently ship a light map under dark chrome.
"""
import json
import re

from py_mini_racer import MiniRacer

import build_map as bm


def test_inline_scripts_parse_as_valid_js():
    # Purpose: the front-end lives in inline <script> blocks that no
    # Python-side test executes -- a template typo ships a blank map. Compile
    # (not run) every block under V8 to pin parse-validity of the shipped JS.
    ctx = MiniRacer()
    html = bm.LEAFLET_TMPL.replace("__CONFIG__", "{}")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "template lost its inline scripts"
    for script in blocks:
        ctx.eval("new Function(" + json.dumps(script) + ")")


# A minimal Leaflet + DOM stub: every map/marker method chains, control.addTo
# actually invokes onAdd (so the init path inside it runs), and DOM nodes answer
# every query. Enough to *execute* the shipped inline script, not just compile
# it -- so an init-time runtime fault (an undefined ref, or a const/let used
# before its declaration during the controls' first render) throws here.
LEAFLET_STUB = r"""
var __onAddCount = 0;
function makeNode(){
  return new Proxy({ style: {}, classList: {toggle(){}, contains(){return false},
                                            add(){}, remove(){}} }, {
    get(t, p){
      if (p in t) return t[p];
      if (p === 'querySelectorAll') return () => [];
      if (p === 'querySelector') return () => makeNode();
      if (p === 'getBoundingClientRect') return () => ({left:0, top:0, width:400, height:150});
      if (p === 'parentNode') return makeNode();
      if (p === 'children') return [];
      if (p === 'checked') return false;
      if (p === 'value') return '10';
      // numeric layout reads (fitProfile sizes the box to the legend's height)
      if (p === 'offsetHeight' || p === 'clientHeight') return 150;
      if (p === 'offsetWidth' || p === 'clientWidth') return 380;
      return () => {};
    },
    set(){ return true; }
  });
}
var document = makeNode();  // supports .title and querySelector('.legend')
function chain(){
  const c = new Proxy(function(){}, {
    get(t, p){ return p === 'getLatLng' ? (() => ({lat:0, lng:0})) : (() => c); },
    apply(){ return c; }
  });
  return c;
}
var L = {
  map: () => chain(), tileLayer: () => chain(), polyline: () => chain(),
  circleMarker: () => chain(), layerGroup: () => chain(), popup: () => chain(),
  DomUtil: { create: () => makeNode() },
  DomEvent: { disableClickPropagation(){}, disableScrollPropagation(){},
              stop(){}, stopPropagation(){} },
  control: (opts) => ({ onAdd: null,
    addTo(){ __onAddCount++; if (this.onAdd) this.onAdd(); return this; } })
};
L.control.layers = () => ({ addTo(){ return this; } });
"""


def test_inline_script_executes_at_init_without_throwing():
    # Purpose: parse-validity (above) can't catch a *runtime* init fault -- e.g.
    # a variable read during the controls' first render before its own `let`
    # declaration runs, which aborts the whole script and ships a blank map with
    # no syntax error. Run the real inline script under a Leaflet/DOM stub whose
    # control.addTo invokes onAdd, so that init path actually executes. A throw
    # (like that dead-zone bug) fails the eval; the onAdd counter proves both the
    # legend and profile controls' bodies ran rather than the script no-opping.
    cfg = {
        "title": "T - observed speeds", "display": "T",
        "totalMiles": 2.0, "binMiles": bm.BIN_MILES, "maxMph": bm.MAX_MPH,
        "anchors": [[v, "#{:02x}{:02x}{:02x}".format(*c)] for v, c in bm.COLOR_ANCHORS],
        "stats": {"obs": 1, "runs": 1, "from": "2026-01-01", "to": "2026-01-02",
                  "built": "2026-01-03"},
        "sections": [[0, 3]],
        "bins": [
            {"m": 0.0, "pts": [[0, 0.0], [0, 0.1]], "mx": 60, "n": 1, "med": 60, "top": ["1", "t"]},
            {"m": 0.5, "pts": [[0, 0.1], [0, 0.2]], "mx": 80, "n": 1, "med": 80, "top": ["1", "t"]},
            {"m": 1.0, "pts": [[0, 0.2], [0, 0.3]]},                       # a gap bin
            {"m": 1.5, "pts": [[0, 0.3], [0, 0.4]], "mx": 40, "n": 1, "med": 40, "top": ["1", "t"]},
        ],
        "obsPts": [[0, 0.0, 60, "1", "t"]],
        "stations": [[0, 0.05, "A", 0.2], [0, 0.35, "B", 1.4]],
    }
    ctx = MiniRacer()
    ctx.eval(LEAFLET_STUB)
    html = bm.LEAFLET_TMPL.replace("__CONFIG__", json.dumps(cfg))
    script = [b for b in re.findall(r"<script>(.*?)</script>", html, re.S)
              if "const CFG" in b][0]
    ctx.eval(script)                     # raises on any init-time runtime fault
    assert ctx.eval("__onAddCount") == 2  # legend + profile onAdd bodies both ran


def test_template_carries_profile_control_contract():
    # Purpose: the speed-profile control's drawing/wiring code looks these ids
    # and the .pf-svg element up by hand, so the markup and the JS form a
    # contract -- dropping the svg, a stat cell, or the reset link would
    # silently break the graph, its stats, or the zoom-reset. Pin them.
    tmpl = bm.LEAFLET_TMPL
    assert "L.DomUtil.create('div', 'profile')" in tmpl
    assert 'class="pf-svg"' in tmpl
    for el_id in ("pf-dist", "pf-max", "pf-avg", "pf-time", "pf-reset"):
        assert f'id="{el_id}"' in tmpl
    # the graph and route cursor must actually be driven by the profile logic
    assert "drawProfile()" in tmpl and "routeCursor" in tmpl


def test_google_maps_engine_removed():
    # Purpose: the Google Maps output was removed on purpose (it needed a
    # billed API key and was never used). Pin the removal the same way the
    # OSM-tiles swap is pinned below: the module exposes no Google template,
    # and the sole shipped map loads no Google Maps JS -- so a stray revert
    # re-adding the keyed, can't-ship-as-is engine turns a test red.
    assert not hasattr(bm, "GOOGLE_TMPL")
    assert "maps.googleapis.com" not in bm.LEAFLET_TMPL


def test_leaflet_uses_carto_dark_matter_with_labels():
    # Purpose: pin the dark basemap. `dark_all` is CARTO Dark Matter *with*
    # place labels (dark_nolabels is the label-free variant we deliberately
    # did not pick); the {r} retina suffix + abcd subdomains are part of the
    # CARTO URL template, so pin the whole thing.
    assert ("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
            in bm.LEAFLET_TMPL)


def test_leaflet_no_longer_serves_light_osm_tiles():
    # Purpose: the change is *replacing* the light basemap -- pin its removal
    # so a stray revert to the bright OSM raster tiles under dark chrome fails.
    assert "tile.openstreetmap.org" not in bm.LEAFLET_TMPL


def test_leaflet_attribution_credits_osm_and_carto():
    # Purpose: CARTO's tile terms require crediting both OpenStreetMap (the
    # data) and CARTO (the tiles). Dropping either attribution is a licensing
    # regression, not a cosmetic one.
    assert "openstreetmap.org/copyright" in bm.LEAFLET_TMPL
    assert "carto.com/attributions" in bm.LEAFLET_TMPL
