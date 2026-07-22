"""Geometry helpers in build_map: distance, projection, simplify, dedupe,
stitch.

Characterization tests pinning the engine that turns NTAD's messy line
scraps into a clean, oriented route spine. Synthetic inputs use latitudes
where the math is hand-computable: at the equator cos(lat)=1 so one degree
in either axis is exactly MI_PER_DEG_LAT (69.05) miles; at 60N cos=0.5.
"""
import pytest

import build_map as bm

MI = bm.MI_PER_DEG_LAT  # 69.05


def test_dist_mi_equirectangular():
    # Purpose: pin the distance approximation -- 1 deg latitude is always
    # 69.05 mi; 1 deg longitude is scaled by cos of the mean latitude
    # (exactly half at 60N).
    assert bm.dist_mi((40, -74), (41, -74)) == pytest.approx(MI)
    assert bm.dist_mi((60, -74), (60, -73)) == pytest.approx(MI / 2)


def test_project_to_segment_midpoint_clamp_and_degenerate():
    # Purpose: projection is the basis of binning, dedupe and simplify.
    # A point 1 deg above the midpoint of an equatorial segment projects to
    # t=0.5 at 69.05 mi; a point past the end clamps to t=1; a zero-length
    # segment yields t=0 and plain point distance.
    d, t = bm.project_to_segment((1, 1), (0, 0), (0, 2))
    assert (d, t) == (pytest.approx(MI), pytest.approx(0.5))
    d, t = bm.project_to_segment((0, 3), (0, 0), (0, 2))
    assert (d, t) == (pytest.approx(MI), 1.0)
    d, t = bm.project_to_segment((0, 1), (0, 0), (0, 0))
    assert (d, t) == (pytest.approx(MI), 0.0)


def test_simplify_drops_noise_keeps_kinks():
    # Purpose: Douglas-Peucker must remove vertices deviating less than the
    # tolerance (0.0001 deg lat = 0.007 mi < 0.015) and keep real geometry
    # (0.001 deg = 0.069 mi > 0.015). Endpoints always survive.
    noise = [(0.0, 0.0), (0.0001, 0.5), (0.0, 1.0)]
    assert bm.simplify(noise, 0.015) == [(0.0, 0.0), (0.0, 1.0)]
    kink = [(0.0, 0.0), (0.001, 0.5), (0.0, 1.0)]
    assert bm.simplify(kink, 0.015) == kink


def test_dedupe_parts_drops_retracing_scrap_keeps_branch():
    # Purpose: NTAD features carry duplicate scraps (second track, re-
    # digitized stubs) that dead-end the stitcher. A short part lying
    # exactly on a longer one is dropped; a genuinely distinct parallel
    # line 6.9 mi away survives.
    long_part = [(0.0, 0.0), (0.0, 0.5), (0.0, 1.0)]
    retrace = [(0.0, 0.2), (0.0, 0.4)]
    branch = [(0.1, 0.0), (0.1, 1.0)]
    kept = bm.dedupe_parts([long_part, retrace, branch])
    assert retrace not in kept
    assert long_part in kept and branch in kept


def test_stitch_joins_and_orients_from_mile0():
    # Purpose: parts sharing an endpoint (within 0.5 mi) must merge into
    # one chain regardless of each part's vertex order, and the chain is
    # oriented to start at the end nearest mile0.
    a = [(0.0, 0.0), (0.0, 1.0)]
    b = [(0.0, 2.0), (0.0, 1.0)]  # reversed orientation, touches a's end
    sections = bm.stitch([a, b], mile0=(0.0, 2.05))
    assert sections == [[(0.0, 2.0), (0.0, 1.0), (0.0, 0.0)]]


def test_stitch_branch_becomes_second_section():
    # Purpose: a branch meets the main line mid-chain, where endpoint-
    # stitching can't absorb it -- it must come out as its own section,
    # after the longer main chain, itself oriented toward mile0.
    main1 = [(0.0, 0.0), (0.0, 1.0)]
    main2 = [(0.0, 1.0), (0.0, 2.0)]
    branch = [(1.0, 1.0), (0.02, 1.0)]  # joins near (0,1), mid-main
    sections = bm.stitch([main1, main2, branch], mile0=(0.0, 0.0))
    assert len(sections) == 2
    assert sections[0][0] == (0.0, 0.0) and len(sections[0]) == 3
    assert sections[1][0] == (0.02, 1.0)  # branch end nearest mile0 first


def test_stitch_discards_sub_5mi_scraps():
    # Purpose: leftovers shorter than MIN_SECTION_MILES are digitization
    # scraps, not track -- they must not become sections.
    main = [(0.0, 0.0), (0.0, 1.0)]
    scrap = [(5.0, 5.0), (5.02, 5.0)]  # ~1.4 mi, isolated
    sections = bm.stitch([main, scrap], mile0=None)
    assert len(sections) == 1
    assert sections[0] == main
