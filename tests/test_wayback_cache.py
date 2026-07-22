"""wayback_snapshots caching rules: only real CDX answers are cached.

The wayback pass is resumable *because* transient failures (rate limits,
error pages) are never written to the on-disk cache -- the train is
retried on the next run. Caching a failure would permanently blind the
scraper to that train's archive history.
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
