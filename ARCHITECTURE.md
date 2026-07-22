# Architecture

> **This document must stay true.** Before creating any PR, verify that
> ARCHITECTURE.md still describes the code as it will exist after the PR
> merges, and update it in the same PR if not. No PR without this check.
> (The rule is enforced via `CLAUDE.md` and the PR template checklist.)

## What this is

speedo maps how fast Amtrak trains actually go, mile by mile, from
position/speed reports scraped off RailRat.net. It is deliberately
infrastructure-free: two stdlib-only Python scripts and a growing JSONL file.
No pip installs, no database, no server — output is self-contained HTML.

## The pipeline

```
railrat.net ──► scrape_railrat.py ──► data/observations.jsonl ──► build_map.py ──► out/speed_map_<route>.html
archive.org ──┘        │                                               ▲
  (--wayback)          └──► data/raw/**.html  ── (--reparse) ──────────┘
                                                     NTAD ArcGIS ──────┘
                                              (route geometry, cached in
                                                 data/geometry/)
```

Two independently runnable stages, joined only by `data/observations.jsonl`:

1. **Scrape** (`scrape_railrat.py`) — fetch RailRat train pages, parse the
   embedded position reports, append what's new to the dataset.
2. **Build** (`build_map.py`) — project all accumulated observations onto the
   official route line and render interactive speed maps.

RailRat only serves each train's *latest* run, so history accumulates across
repeated scrapes. That is why the dataset — not any single scrape — is the
product.

## scrape_railrat.py

Pipeline per run, for one route (`--route`, default `AcelaExpress`):

- **Roster** — the set of train numbers for the route, persisted in
  `data/roster_<route>.json`. Grown from the RailRat route page's train links,
  plus any `--trains` seeds. Never shrinks (retired train numbers keep serving
  their last-ever run, sometimes months old — free history).
- **Fetch** (`fetch`) — polite GET: 1 req/sec to railrat.net, 10 s to
  archive.org, exponential backoff on 429/503, custom User-Agent.
- **Parse** (`parse_train_page`) — regex-extract the Leaflet
  `circleMarker(...).bindPopup(...)` calls RailRat embeds in each train page:
  lat/lon, clock time, mph, heading, description. Pages carry only clock
  times, so dates are reconstructed by walking backward from the page's
  "updated HH:MM on MM/DD" stamp, decrementing the date on >12 h clock jumps
  (`infer_year` picks the year that keeps everything in the past).
  `ROUTE_ALIASES` canonicalizes slugs where RailRat's train pages disagree
  with its route index (e.g. `Keystone` → `KeystoneService`).
- **Store** (`append_parsed`) — append to `data/observations.jsonl`, deduped
  in-memory against the whole file on key
  `train|ts|lat(4dp)|lon(4dp)`. Points above 170 mph are dropped as GPS
  glitches. Raw page HTML is saved under `data/raw/<scrape-date>/` first.
- **Wayback** (`--wayback`) — optional archive.org backfill. CDX snapshot
  lookups and snapshot bodies are cached under `data/raw/wayback/`; transient
  failures are *not* cached, so an aborted pass resumes cleanly. Aborts after
  3 consecutive CDX failures (archive.org rate-limiting).
- **Reparse** (`--reparse`) — rebuild `observations.jsonl` from scratch from
  `data/raw/**`, no network. This is the escape hatch that makes parser
  changes safe: raw HTML is the source of truth; the JSONL is derived.

## build_map.py

Pipeline per run, for one route:

- **Geometry** (`fetch_route_geometry`) — download the route's official line
  from the USDOT/BTS NTAD "Amtrak Routes" ArcGIS FeatureServer, cached in
  `data/geometry/<route>.geojson`. The `ROUTES` dict maps RailRat slug →
  NTAD feature name, display name, and the mile-0 endpoint (which end mile
  markers count from). New routes need an entry here.
- **Spine construction** — NTAD features arrive as messy MultiLineString
  scraps. `dedupe_parts` drops scraps that merely re-trace a longer part
  (second track, twice-digitized stubs); `stitch` joins parts whose endpoints
  fall within 0.5 mi into chains, one chain per branch (branched routes like
  the Regional's Virginia legs become separate *sections*); chains shorter
  than 5 mi are discarded as scrap; `simplify` runs Douglas-Peucker at ~25 m.
- **Binning** (`build_bins`) — each section is sliced into 0.5-mile bins by
  arc length. Mile numbering runs continuously across sections; bins never
  span a section boundary.
- **Projection** — `SegmentIndex`, a spatial hash over route segments
  (0.05° cells), assigns each observation to its nearest segment and thus its
  bin. Observations >2 mi off-route are dropped (wrong route/GPS junk).
- **Stats & render** — each bin records max mph (which train/when set it),
  point count, and median. **Max**, not mean, is the headline: station dwells
  and delays shouldn't mask what the track can do — slow bins are slow only
  where *every* train is slow. Everything is serialized into one JSON `CFG`
  blob and substituted into two inline HTML templates: Leaflet/OSM
  (`speed_map_<route>.html`, works as-is) and Google Maps
  (`speed_map_<route>_google.html`, needs an API key). Bins with no data draw
  gray dashed.

## Data layout

| Path | What | In git? |
|---|---|---|
| `data/observations.jsonl` | The dataset. One JSON object per position report: `route, train, run_date, ts, lat, lon, mph, heading, desc, src` (`src` = `live` or `wayback:<stamp>`). Append-only, deduped, safe to re-scrape. | yes |
| `data/roster_<route>.json` | Known train numbers per route (sorted list). | yes |
| `data/geometry/<route>.geojson` | Cached NTAD route geometry. Delete to re-fetch. | yes |
| `data/raw/<date>/`, `data/raw/wayback/` | Raw scraped HTML + CDX caches; source of truth for `--reparse`. | no (bulky) |
| `out/` | Generated maps. | no (the Google one may embed an API key) |

## Invariants

These are the load-bearing assumptions; breaking one is an architectural
change and belongs in this file:

- **stdlib only.** No pip installs, for the scripts *and* the tests
  (see [TESTING.md](TESTING.md)).
- **`observations.jsonl` is derived; `data/raw/` is the source of truth.**
  Any parser change must keep `--reparse` able to rebuild the dataset.
- **Scraping is idempotent** — dedup on `(train, ts, lat, lon)` makes re-runs
  always safe, any cadence.
- **Politeness is non-negotiable**: throttles, backoff, honest User-Agent.
  RailRat and archive.org are free rides.
- **Output is self-contained HTML** — no build step, no server, no assets
  beyond CDN Leaflet/Google Maps JS.
- **Route identity is the RailRat slug**, canonicalized through
  `ROUTE_ALIASES` at every entry point (CLI args and parsed pages).

## Testing

See [TESTING.md](TESTING.md). Tests live in `tests/`, run offline with
`python -m unittest`, and use fixture HTML/geometry under `tests/fixtures/`.
