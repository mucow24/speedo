"""Build-time post-processing annotations for the map front-end.

Sparse data leaves two artifacts on the rendered map: a lone slow reading in
the middle of fast track (the one train that happened to be braking there),
and stretches of no data at all. The Python side flags single-point outliers
and precomputes linear gap interpolations; the HTML toggles merely choose
which annotation to display, so all the decisions live here where pytest can
reach them. Bins are per-section: outliers and gaps never cross a section
boundary (sections are physically separate branches).
"""
import build_map as bm


# --- find_outliers ----------------------------------------------------------

def test_outlier_flagged_between_fast_neighbors():
    # Purpose: the motivating case -- one lone 30 mph point mid-run of 80+
    # track is flagged when both neighbors are >1.7x faster.
    assert bm.find_outliers([80, 30, 90], [5, 1, 3]) == [1]


def test_outlier_requires_single_point():
    # Purpose: two points agreeing the bin is slow is signal, not noise --
    # only n==1 bins are outlier candidates.
    assert bm.find_outliers([80, 30, 90], [5, 2, 3]) == []


def test_outlier_requires_both_neighbors_fast():
    # Purpose: one fast neighbor isn't enough -- a slow point at the edge of
    # a slow zone is real. 80 < 1.7 * 50, so the left neighbor disqualifies.
    assert bm.find_outliers([80, 50, 90], [5, 1, 3]) == []


def test_outlier_ratio_is_strictly_greater():
    # Purpose: pin the boundary -- neighbors at exactly 1.7x (51 = 1.7 * 30)
    # do not flag; the spec is "> 1.7x", not ">=".
    assert bm.find_outliers([51, 30, 51], [5, 1, 3]) == []
    assert bm.find_outliers([52, 30, 52], [5, 1, 3]) == [1]


def test_outlier_not_at_section_edge():
    # Purpose: edge bins have only one neighbor, so "both neighbors faster"
    # can never be established -- they are never flagged.
    assert bm.find_outliers([30, 80, 90], [1, 5, 5]) == []
    assert bm.find_outliers([80, 90, 30], [5, 5, 1]) == []


def test_outlier_requires_neighbors_with_data():
    # Purpose: an empty neighbor can't testify that the track is fast there.
    assert bm.find_outliers([None, 30, 90], [0, 1, 3]) == []


def test_zero_mph_single_point_is_outlier():
    # Purpose: a lone stopped/dwell reading flanked by fast track is exactly
    # the artifact this hides; 0 mph must not divide-by-zero or slip through.
    assert bm.find_outliers([80, 0, 90], [5, 1, 3]) == [1]


# --- interpolate_gaps -------------------------------------------------------

def test_gap_linear_interpolation():
    # Purpose: a 3-bin gap between 60 and 100 fills with the evenly spaced
    # 70/80/90, each tagged with the gap's total length for the UI threshold.
    assert bm.interpolate_gaps([60, None, None, None, 100]) == {
        1: (70, 3), 2: (80, 3), 3: (90, 3)}


def test_single_bin_gap_is_midpoint():
    # Purpose: the smallest gap interpolates to the mean of its bookends.
    assert bm.interpolate_gaps([60, None, 90]) == {1: (75, 1)}


def test_edge_gaps_not_interpolated():
    # Purpose: gaps touching a section end have only one bookend -- there is
    # nothing to interpolate toward, so they stay empty.
    assert bm.interpolate_gaps([None, None, 60, None]) == {}


def test_all_empty_section_untouched():
    # Purpose: a section with no data at all must not crash or invent speeds.
    assert bm.interpolate_gaps([None, None, None]) == {}


# --- annotate_bins ----------------------------------------------------------

def test_annotate_outlier_removal_precedes_interpolation():
    # Purpose: pin the ordering rule -- hiding the outlier at bin 1 turns it
    # into a gap, so it gets an outliers-hidden interpolation ("ib") between
    # its real neighbors; the plain gap at bin 3 gets the ordinary "ia".
    maxes = [80, 30, 90, None, 100]
    counts = [5, 1, 3, 0, 2]
    ann = bm.annotate_bins(maxes, counts, [(0, 4)])
    assert ann[1] == {"out": 1, "ib": [85, 1]}
    assert ann[3] == {"ia": [95, 1]}
    assert set(ann) == {1, 3}


def test_annotate_respects_section_boundaries():
    # Purpose: sections are separate physical branches -- a would-be gap
    # spanning bins 2|3 across the boundary must not be bridged, and the
    # lone slow point at a section's first bin is not an outlier.
    maxes = [80, None, 90, 30, 100]
    counts = [3, 0, 3, 1, 3]
    ann = bm.annotate_bins(maxes, counts, [(0, 2), (3, 4)])
    assert ann == {1: {"ia": [85, 1]}}
