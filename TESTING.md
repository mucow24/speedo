# Testing policy

This project started ultra-casual and is growing into a small project. These
rules keep the tests worth having. They apply to every change — human- or
agent-authored.

## What must be tested

**All logic must be covered by unit or integration tests.** "Logic" means
anything that transforms data or makes a decision: HTML parsing, date
inference, dedup keys, geometry math, stitching, binning, projection, color
mapping, and so on.

*Within reason.* Acceptable exclusions:

- Weird, deeply buried error cases (e.g., the exact backoff behavior on the
  third retry of a flaky socket).
- Code that would require a huge or complex harness out of all proportion to
  what the test would prove (e.g., live scraping against railrat.net or
  archive.org).
- Topmost orchestration code: `main()`, argparse wiring, progress printing —
  thin layers that only sequence already-tested pieces.

Exclusion is a judgment call, not a loophole. If a function contains a
nontrivial decision, the default is that it gets a test; the burden is on the
author to justify skipping it.

## TDD

Use test-driven development whenever possible:

1. Write a test that fails because the behavior you're about to add or fix
   doesn't exist yet.
2. Watch it fail **for the right reason** (see below).
3. Implement until it passes.
4. Keep the test.

For bug fixes this is close to mandatory: the red test is the reproduction of
the bug, and it's the proof the fix works.

## What counts as a red test

A red test must actually test **the change being performed**. It fails because
the behavior under change is missing or wrong — and for no other reason.

**Not** red:

- "My module failed to load" (ImportError, SyntaxError, missing fixture).
- "It didn't run" (zero tests collected, harness crash, typo in the test).
- A test failing on some unrelated pre-existing behavior.

If your new test fails for one of those reasons, the test itself is broken.
Fix it until the failure message names the behavior you're about to implement,
*then* write the implementation.

## No bullshit tests

Every test must earn its place:

- **No tautologies.** No `assert True`, no asserting a constant equals itself,
  no mocking the unit under test and then asserting the mock was called with
  what you just passed it.
- **No tests that test nothing.** A test with no meaningful assertion, or one
  that passes no matter what the code does, is deleted on sight. "It runs
  without crashing" is only a valid claim when crash-freedom on that exact
  input *is* the regression being pinned — and the test must say so.
- **No nonsensical tests.** A test whose inputs could never occur, or whose
  expected output was copied from the code's current (unverified) behavior
  without checking it's actually correct, is worse than no test.
- **Every test states its purpose.** A docstring or comment saying what
  behavior is being pinned and why it matters. If you can't write that
  sentence, the test shouldn't exist.

## Mechanics

- Tests live in `tests/`, named `test_*.py`.
- **stdlib `unittest` only.** The project's no-dependency rule (README:
  "stdlib only, no pip installs") extends to the test suite.
  `python -m unittest` from the repo root must discover and run everything.
  Running via pytest locally is fine; depending on it is not.
- Tests run **offline and fast**. No network, ever. Real RailRat HTML
  snippets, NTAD geometry fragments, etc. live as fixtures under
  `tests/fixtures/`.
- Restructuring the scripts for testability (extracting pure functions,
  keeping side effects behind `main()`) is encouraged — testability is a
  legitimate reason to refactor.
