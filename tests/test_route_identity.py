"""Route-slug canonicalization for build_map's --route entry point.

Pins the invariant "route identity is the RailRat slug, canonicalized through
ROUTE_ALIASES at every entry point (CLI args and parsed pages)." Before this
existed, build_map's `--route` fell straight through `ROUTES.get(route,
default)`: a display name like "Empire Builder" (with a space) or an alias
like "Keystone" was treated as its own route, fetched NTAD geometry under
that raw name, and wrote a parallel, non-canonical cache file (e.g.
"Empire Builder.geojson", a byte-for-byte duplicate of EmpireBuilder.geojson)
-- while also matching zero observations, which are stored under the slug.
"""
import pytest

import build_map as bm
import scrape_railrat as sr


def test_spaced_display_name_canonicalizes_to_slug():
    # Purpose: the exact reported bug -- "Empire Builder" must resolve to the
    # canonical slug "EmpireBuilder", not fall through as its own route and
    # spawn a duplicate "Empire Builder.geojson" cache.
    assert bm.canonical_route("Empire Builder") == "EmpireBuilder"


def test_alias_is_applied_at_the_build_entry_point():
    # Purpose: ROUTE_ALIASES is the one canonicalizer for both entry points;
    # build_map's CLI must honor it too, so "Keystone" (RailRat's train-page
    # slug) builds the KeystoneService map instead of erroring.
    assert bm.canonical_route("Keystone") == "KeystoneService"


def test_already_canonical_slug_passes_through():
    # Purpose: a name already canonical is returned unchanged (idempotent) --
    # canonicalization must not mangle the common `--route AcelaExpress` case.
    assert bm.canonical_route("AcelaExpress") == "AcelaExpress"


def test_unknown_route_is_rejected_not_silently_cached():
    # Purpose: an unknown name must raise, not fall through to fetch NTAD
    # geometry under a novel name and write a parallel cache file. Erroring
    # instead of silently caching is the whole point of the fix.
    with pytest.raises(SystemExit):
        bm.canonical_route("NonexistentRoute")


def test_both_entry_points_share_one_alias_table():
    # Purpose: pin single-source-of-truth -- both entry points canonicalize
    # through the SAME ROUTE_ALIASES object, so the scraper and the builder
    # can't drift into disagreeing about what "Keystone" means.
    assert bm.ROUTE_ALIASES is sr.ROUTE_ALIASES


def test_every_route_key_is_already_canonical():
    # Purpose: every ROUTES key must be a canonical slug -- i.e. round-trip
    # through canonical_route to itself. A key that doesn't (a spaced display
    # name, or an alias source like "Keystone") would fetch NTAD geometry into
    # a parallel cache file and match zero observations, the exact bug
    # canonical_route exists to prevent. This guards every entry, so a
    # malformed newly-added route fails here instead of silently at build time.
    for slug in bm.ROUTES:
        assert bm.canonical_route(slug) == slug, slug


def test_every_route_entry_is_well_formed():
    # Purpose: pin the shape each ROUTES entry must have so a malformed addition
    # fails fast offline rather than at NTAD-fetch or render time -- a non-empty
    # NTAD feature name, a non-empty display name, and a plausible (lat, lon)
    # mile0 pair (the endpoint stitch() orients each section from).
    for slug, cfg in bm.ROUTES.items():
        assert isinstance(cfg["ntad"], str) and cfg["ntad"].strip(), slug
        assert isinstance(cfg["display"], str) and cfg["display"].strip(), slug
        lat, lon = cfg["mile0"]
        assert -90 <= lat <= 90 and -180 <= lon <= 180, slug
