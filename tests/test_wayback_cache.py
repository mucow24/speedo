"""Wayback pass rules: CDX caching, the two-phase plan, and abort behavior.

The wayback pass is resumable *because* transient failures (rate limits,
error pages) are never written to the on-disk cache -- the train is
retried on the next run. Caching a failure would permanently blind the
scraper to that train's archive history. The pass runs in two phases
(resolve all CDX snapshot lists, then fetch) so progress totals are known
up front, and it aborts -- visibly to callers -- when archive.org
rate-limits three CDX lookups in a row.
"""
import scrape_railrat as sr


def setup_fetch(monkeypatch, tmp_path, responses):
    monkeypatch.setattr(sr, "DATA", tmp_path)
    calls = []

    def fake_fetch(url, throttle=None, retries=4):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(sr, "fetch", fake_fetch)
    return calls


def cache_file(tmp_path, train):
    return tmp_path / "raw" / "wayback" / f"cdx-{train}.json"


def test_transient_failures_are_not_cached(tmp_path, monkeypatch):
    # Purpose: a network failure (None) and an HTML error page (non-JSON
    # body) must both return None and leave no cache file, so the lookup
    # is retried on a future run.
    setup_fetch(monkeypatch, tmp_path, [None, "<html>rate limited</html>"])
    assert sr.wayback_snapshots(101) is None
    assert not cache_file(tmp_path, 101).exists()
    assert sr.wayback_snapshots(102) is None
    assert not cache_file(tmp_path, 102).exists()


def test_wayback_plan_resolves_stamps_flat(tmp_path, monkeypatch):
    # Purpose: the fetch-phase progress UI needs the full (train, stamp)
    # work list up front, so the CDX phase resolves every roster train
    # first -- serving cached lists from disk without a fetch.
    calls = setup_fetch(monkeypatch, tmp_path, [
        '[["timestamp"],["20200926175945"],["20210117091905"]]'])
    cache_file(tmp_path, 102).parent.mkdir(parents=True, exist_ok=True)
    cache_file(tmp_path, 102).write_text('["20220101000000"]', encoding="utf-8")
    plan, aborted = sr.wayback_plan([101, 102])
    assert plan == [(101, "20200926175945"), (101, "20210117091905"),
                    (102, "20220101000000")]
    assert aborted is False
    assert len(calls) == 1  # 102 came from cache


def test_wayback_plan_aborts_after_three_consecutive_failures(tmp_path, monkeypatch):
    # Purpose: 3 consecutive CDX failures means archive.org is
    # rate-limiting; the plan stops there (resumable from cache next run)
    # instead of burning requests on the rest of the roster.
    calls = setup_fetch(monkeypatch, tmp_path, [None, None, None, "[]"])
    plan, aborted = sr.wayback_plan([201, 202, 203, 204])
    assert aborted is True
    assert plan == []
    assert len(calls) == 3  # the 4th train is never attempted


def test_wayback_plan_failure_count_resets_on_success(tmp_path, monkeypatch):
    # Purpose: the abort is 3 *consecutive* failures, not 3 total -- a
    # success in between proves archive.org is still answering.
    calls = setup_fetch(monkeypatch, tmp_path, [None, None, "", None])
    plan, aborted = sr.wayback_plan([301, 302, 303, 304])
    assert aborted is False
    assert plan == []           # 303's real answer was "no snapshots"
    assert len(calls) == 4      # every train was attempted


def test_wayback_fetch_stats_counts_cached_snapshots(tmp_path):
    # Purpose: the progress header ("X snapshots, Y on disk, Z to fetch")
    # must reflect what is actually cached, so a resumed pass shows how
    # little is left rather than the full total.
    wbdir = tmp_path / "raw" / "wayback"
    wbdir.mkdir(parents=True)
    (wbdir / "101-20200926175945.html").write_text("<html>", encoding="utf-8")
    plan = [(101, "20200926175945"), (101, "20210117091905"),
            (102, "20220101000000")]
    assert sr.wayback_fetch_stats(plan, wbdir) == (3, 1, 2)


def test_scrape_wayback_surfaces_abort_to_caller(tmp_path, monkeypatch):
    # Purpose: speedo_ctl's queue cancels the remaining wayback jobs when
    # archive.org rate-limits, so scrape_wayback must return the CDX-phase
    # abort flag instead of swallowing it in printed output.
    setup_fetch(monkeypatch, tmp_path, [None, None, None])
    monkeypatch.setattr(sr, "OBS_FILE", tmp_path / "obs.jsonl")
    monkeypatch.setattr(sr, "STN_FILE", tmp_path / "stn.jsonl")
    *_, aborted = sr.scrape_wayback("AcelaExpress", {1, 2, 3, 4}, set(), set())
    assert aborted is True


def test_real_answers_are_cached_including_empty(tmp_path, monkeypatch):
    # Purpose: a genuine CDX answer is cached and served from disk on the
    # next call (no second fetch); CDX reports "no snapshots" as an empty
    # body, which is a real answer -- cached as [] -- not a failure.
    calls = setup_fetch(monkeypatch, tmp_path, [
        '[["timestamp"],["20200926175945"],["20210117091905"]]', ""])
    stamps = ["20200926175945", "20210117091905"]
    assert sr.wayback_snapshots(103) == stamps
    assert cache_file(tmp_path, 103).exists()
    assert sr.wayback_snapshots(103) == stamps  # from cache
    assert len(calls) == 1                      # ...so no second fetch
    assert sr.wayback_snapshots(104) == []
    assert cache_file(tmp_path, 104).exists()
