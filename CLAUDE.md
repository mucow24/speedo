# speedo — project rules

Two stdlib-only Python scripts that map observed Amtrak speeds. Read
[ARCHITECTURE.md](ARCHITECTURE.md) before making changes.

## Hard rules

- **No PR without an ARCHITECTURE.md check.** Before creating any PR, verify
  [ARCHITECTURE.md](ARCHITECTURE.md) still accurately describes the code as it
  will exist after your change, and update it in the same PR if it doesn't.
  This is a gate, not a suggestion.
- **Follow [TESTING.md](TESTING.md).** All logic gets unit or integration
  tests (within the stated exclusions). Use TDD whenever possible. A red test
  must fail on the behavior being changed — "module failed to load" or "it
  didn't run" is not red. No tautological or purposeless tests; every test
  carries a comment stating what it pins and why.
- **Right tool for the job.** Dependencies are welcome where they beat the
  stdlib option; prefer stdlib only when the choices are equally good. Tests
  run offline via `pytest`.
- **Never weaken scraper politeness** (throttles, backoff, User-Agent).
- **Ingest is lossless.** The JSONL datasets in `data/` are the source of
  truth; scrape-time never discards parsed data — plausibility filters are
  build-time policy in `build_map.py`. `data/raw/` is a disposable cache.
