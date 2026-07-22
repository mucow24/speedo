"""Arc-length binning and the segment spatial index (build_map).

Characterization tests for the machinery that maps observations to
half-mile bins. Spines lie on the equator so degrees convert to miles
exactly (1 deg lon = 69.05 mi); lengths are chosen to make bin counts and
labels hand-computable.
"""
import pytest

import build_map as bm

MI = bm.MI_PER_DEG_LAT


def lon(miles):
    """Longitude offset spanning the given miles along the equator."""
    return miles / MI


def test_build_bins_slices_and_labels_continuously():
    # Purpose: a 2.6 mi section yields 5 full half-mile bins plus a 0.1 mi
    # remainder (6 bins); a second 0.7 mi section (2 bins) must continue
    # the mile labels from 2.6 rather than restart at 0, and bins must
    # never span the section gap.
    s1 = [(0.0, 0.0), (0.0, lon(2.6))]
    s2 = [(0.0, 10.0), (0.0, 10.0 + lon(0.7))]
    bins, labels, segs, total = bm.build_bins([s1, s2])
    assert len(bins) == 8
    assert labels == pytest.approx([0, 0.5, 1.0, 1.5, 2.0, 2.5, 2.6, 3.1])
    assert total == pytest.approx(3.3)
    assert bins[5][-1] == pytest.approx((0.0, lon(2.6)))  # section 1 ends
    assert bins[6][0] == (0.0, 10.0)                      # section 2 starts

    # The flat segment list drives projection: each spine segment carries
    # its section's first and last bin index so projected miles clamp to
    # the right section.
    assert len(segs) == 2
    _a, _b, seg_mi, bin_base, mile_at_a, last_bin = segs[0]
    assert (seg_mi, bin_base, mile_at_a, last_bin) == \
        (pytest.approx(2.6), 0, 0.0, 5)
    assert (segs[1][3], segs[1][5]) == (6, 7)


def test_segment_index_finds_nearest_segment():
    # Purpose: SegmentIndex must return the closest segment with its
    # distance and along-fraction. A point 0.069 mi above the second
    # segment's midpoint must pick segment 1, not the nearer-endpoint
    # segment 0.
    spine = [(0.0, 0.0), (0.0, 0.02), (0.0, 0.04)]
    _bins, _labels, segs, _total = bm.build_bins([spine])
    index = bm.SegmentIndex(segs, tol_mi=2.0)
    d, i, t = index.nearest((0.001, 0.03))
    assert i == 1
    assert d == pytest.approx(0.001 * MI)
    assert t == pytest.approx(0.5)


def test_segment_index_misses_far_points():
    # Purpose: a point whose grid cell holds no segments (far off-route)
    # must come back with segment None so the build counts it off-route
    # instead of force-assigning a bin.
    spine = [(0.0, 0.0), (0.0, 0.02)]
    _bins, _labels, segs, _total = bm.build_bins([spine])
    index = bm.SegmentIndex(segs, tol_mi=2.0)
    d, i, _t = index.nearest((3.0, 3.0))
    assert i is None and d == float("inf")
