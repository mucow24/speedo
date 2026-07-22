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
