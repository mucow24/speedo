"""Which basemap the rendered Leaflet map ships.

The whole map UI chrome is dark (COMMON_CSS), so the backing tiles have to be
dark too or they fight the theme. These pin the basemap baked into the Leaflet
template: it must be CARTO Dark Matter *with* labels (the `dark_all` style),
not the light OpenStreetMap raster tiles it used to serve. The basemap choice
is a shipped decision, not incidental, so a revert or a broken tile URL should
turn a test red rather than silently ship a light map under dark chrome.
"""
import build_map as bm


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
