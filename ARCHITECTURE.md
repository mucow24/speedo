# Architecture

> **This document must stay true.** Before creating any PR, verify that
> ARCHITECTURE.md still describes the code as it will exist after the PR
> merges, and update it in the same PR if not. No PR without this check.
> (The rule is enforced via `CLAUDE.md` and the PR template checklist.)

## What this is

speedo maps how fast Amtrak trains actually go, mile by mile, from
position/speed reports scraped off RailRat.net. It is deliberately
infrastructure-free: three stdlib-only Python scripts and a growing JSONL
file. No pip installs, no database, no server — output is self-contained
HTML.

## The pipeline

```
railrat.net ──► scrape_railrat.py ──┬─► data/observations.jsonl ───► build_map.py ──► out/speed_map_<route>.html
archive.org ──┘        │            └─► data/station_events.jsonl        ▲
  (--wayback)          └──► data/raw/**.html ──(--reparse: rebuilds      │
                            (disposable cache)  both datasets)           │
                                                      NTAD ArcGIS ───────┘
                                               (route geometry, cached in
                                                  data/geometry/)
```

Two independently runnable stages whose only data hand-off is the JSONL
datasets (build also imports the scraper's `ROUTE_ALIASES` so both stages
canonicalize route slugs identically — code shared, but no data flows through
it):

1. **Scrape** (`scrape_railrat.py`) — fetch RailRat train pages, parse the
   embedded position reports *and* the per-station Progress Tracker, append
   what's new to `observations.jsonl` and `station_events.jsonl`. Ingest is
   **lossless**: everything parseable is stored; policy filtering happens
   downstream.
2. **Build** (`build_map.py`) — project all accumulated observations onto the
   official route line and render interactive speed maps.

`speedo_ctl.py` sits above both stages as an optional manager (dataset
status, queued multi-route updates, batch map builds); it owns no data and
no parsing of its own.

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
  with its route index (`Keystone` → `KeystoneService`, the 2020-era
  `MichiganServices` → `WolverineMichiganService`).
- **Station events** (`parse_station_entries`) — the page's Progress Tracker
  lists per-station *actual* arrival/departure times and delay-vs-schedule
  ("HFD, departed 08:14, 8 min. late, arrived 08:10"). Both markup eras are
  parsed (current spans-and-est. style and the 2020 one-verb-per-row style);
  "est." rows are predictions, not events, and are skipped. Dates are
  assigned by the same backward clock-walk as positions, anchored at the
  page's "updated" stamp.
- **Store** (`append_parsed`, `append_station_events`) — append to
  `data/observations.jsonl` (deduped on `train|ts|lat(4dp)|lon(4dp)`) and
  `data/station_events.jsonl` (deduped on full event content; a station's
  record gains fields across a run's page fetches and each variant is kept —
  consumers merge by train/run/station). Nothing parseable is discarded:
  even implausible speeds are stored, and filtered only at build time. Raw
  page HTML is saved under `data/raw/<scrape-date>/` first.
- **Wayback** (`--wayback`) — optional archive.org backfill, in two phases
  so progress totals are known up front: `wayback_plan` resolves every
  roster train's CDX snapshot list into one flat work list, then the fetch
  phase reports "X snapshots, Y on disk, Z to fetch" with a per-fetch
  counter and throttle-based ETA. CDX lookups and snapshot bodies are
  cached under `data/raw/wayback/`; transient failures are *not* cached, so
  an aborted pass resumes cleanly. Aborts after 3 consecutive CDX failures
  (archive.org rate-limiting), and `scrape_wayback` returns that abort flag
  to callers (speedo_ctl uses it to cancel remaining queued wayback jobs).
- **Reparse** (`--reparse`) — rebuild both datasets from scratch from
  whatever is under `data/raw/**`, no network. Useful for re-applying parser
  improvements to pages still on disk, but no longer load-bearing: the JSONL
  datasets are the source of truth, and raw/ is a disposable cache.

## build_map.py

Pipeline per run, for one route:

- **Route identity** (`canonical_route`) — the `--route` argument is first
  normalized (spacing collapsed, so the display name `Empire Builder` becomes
  the slug `EmpireBuilder`) and run through `ROUTE_ALIASES` (imported from
  `scrape_railrat`, the one shared alias table), then required to be a known
  `ROUTES` key. A non-canonical or unknown name is corrected or rejected with
  a `SystemExit` — never fetched under its raw name into a parallel
  `data/geometry/<raw name>.geojson`, which would also match zero observations
  (those are stored under the slug). This is the CLI half of the "canonicalize
  at every entry point" invariant.
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
  bin. `load_observations` drops out-of-band speeds at load — GPS glitches
  (>170 mph) above and stopped/stuck trains (<10 mph, `--min-mph`) below, the
  latter being station dwells and held signals rather than track limits — and
  observations >2 mi off-route are dropped (wrong route/GPS junk).
- **Stats** — each bin records max mph (which train/when set it), point
  count, and median. **Max**, not mean, is the headline: station dwells and
  delays shouldn't mask what the track can do — slow bins are slow only
  where *every* train is slow.
- **Post-processing** (`find_outliers`, `interpolate_gaps`, `annotate_bins`)
  — sparse data leaves two artifacts, and the build annotates both so the
  front-end can toggle them. *Outliers*: a single-point bin both of whose
  neighbors are >1.7× (`OUTLIER_RATIO`) faster is flagged — one braking
  train, not a slow stretch (real restrictions slow every train). *Gaps*:
  interior runs of empty bins get linear interpolations between the
  bookending speeds, tagged with gap length. Outlier removal runs before
  interpolation, so a hidden outlier becomes a fillable gap — both
  interpolation variants are precomputed (`ia` outliers-in, `ib`
  outliers-hidden where different). Neither crosses a section boundary. The
  raw stats always ship too; the toggles are pure display.
- **Render** — everything is serialized into one JSON `CFG` blob and
  substituted into the inline HTML template (`LEAFLET_TMPL` →
  `speed_map_<route>.html`, works as-is, no API key). The map draws on CARTO
  Dark Matter tiles (dark basemap over OSM data, no API key) to match the dark
  UI chrome. Bins with no data draw gray dashed; interpolated bins draw
  color-dashed and their popups say "interpolated". The legend hosts the
  toggles: hide outliers (default on), interpolate gaps (default on), and a
  max-gap slider (1–100 bins, active only while interpolating). The legend's
  gradient bar doubles as a **speed-range highlighter**: two draggable handles
  select an inclusive [lo, hi] mph band (default full scale = inactive);
  segments, interpolated bins, no-data bins, and raw-observation dots outside
  the band wash out, restyled live during the drag. The wash pre-blends each
  color 85% (`WASH_MIX`) toward the basemap tone (`WASH_BG`, the dark CARTO
  tone declared in the template) at full opacity, rather than dropping alpha:
  translucent lines additively brighten where they overlap at high zoom. All
  of this is display-time state in the front-end JS (`COMMON_JS`) — nothing
  about the filter is baked into the build.

## speedo_ctl.py

A thin manager over the two pipeline stages; it owns no data and does no
parsing. Route discovery is a `data/geometry/` folder scan — a route exists
for speedo_ctl iff its cached NTAD geometry file does.

- **Status** (no args) — one table row per discovered route, labeled by
  RailRat slug (not display name) so rows copy-paste straight into the
  update/map commands: stored points, distinct trains, coverage, the most
  recent point timestamp, and whether any wayback-sourced observations
  exist. The counts come from a single
  pass over `observations.jsonl`. Coverage is the fraction of half-mile
  bins holding at least one plausible on-route observation, computed with
  the same spine/binning/projection code and plausibility band as
  `build_map.build`, so the percentage predicts what the map will paint
  (colored bins vs. the gray no-data dash). Status never touches the
  network: geometry is read straight from the cache files that define the
  route list.
- **Updates** (`--live-update`, `--full-update`) — a strictly sequential
  job queue; the scrapers' politeness throttles are per-process, so a
  parallel queue would multiply the request rate. A full update runs every
  route's live scrape first (fast, freshest data), then the slow wayback
  passes; a wayback abort (archive.org rate-limiting) cancels the remaining
  queued wayback jobs.
- **Maps** (`--make-map`) — `build_map.build` per route, skipping routes
  with no usable observations or no `ROUTES` entry (`--make-map all` builds
  the whole cache).

CLI route arguments are canonicalized through `canonical_route` and
deduped, keeping the "canonicalize at every entry point" invariant. The
literal token `all` (accepted anywhere a route list is — `--live-update`,
`--full-update`, `--make-map`) expands to every discovered route instead;
it's intercepted before canonicalization, since `canonical_route` would
reject it as an unknown route.

## Data layout

| Path | What | In git? |
|---|---|---|
| `data/observations.jsonl` | Position reports. One JSON object each: `route, train, run_date, ts, lat, lon, mph, heading, desc, src` (`src` = `live` or `wayback:<stamp>`). Append-only, deduped, safe to re-scrape. | yes |
| `data/station_events.jsonl` | Station timings. One JSON object each: `route, train, run_date, station, name, arr, arr_delay, dep, dep_delay, src` (times ISO, delays in minutes, late positive / early negative, null when the page didn't state one). Append-only; a station's record may appear in several progressively-more-complete variants per run. | yes |
| `data/roster_<route>.json` | Known train numbers per route (sorted list). | yes |
| `data/geometry/<route>.geojson` | Cached NTAD route geometry. Delete to re-fetch. | yes |
| `data/raw/<date>/`, `data/raw/wayback/` | Raw scraped HTML + CDX caches. A disposable debug/reprocessing cache — delete freely. | no |
| `out/` | Generated maps. | no |
| `tests/fixtures/*.html` | A curated handful of raw pages, kept verbatim forever as the parser's ground truth. | yes |

### Storage & backup policy

The datasets are irreplaceable: RailRat serves only each train's latest run,
overwriting the previous one, and no deep public archive of Amtrak GPS/speed
data exists. A missed run is gone forever. Everything of value therefore
lives in the committed JSONL files, and **the git remote is the backup** —
data commits alongside code commits are normal and expected.

`data/raw/` needs no backup. Because ingest is lossless, the datasets
capture everything the pages carry that we care about; raw pages are kept
around only as a convenience for debugging and for re-running `--reparse`
after parser improvements, and can be deleted at any time. The cost of a
lost raw page is only that *future* parser fixes can't be applied to it
retroactively — a risk consciously accepted and mitigated by the fixture
tests instead of by hoarding HTML.

Everything else is reproducible: `data/geometry/` is re-fetchable from NTAD
(committed for offline convenience), `out/` regenerates in seconds.

The datasets stay **plain-text, append-only JSONL** — this is a decision,
not an accident. Appends are crash-safe and idempotent, the files are
greppable and reviewable in PRs, and git delta-compresses each scrape to
roughly the cost of the new lines, which is what makes git-as-backup work.
Binary formats (SQLite, parquet, …) are off the table not for dependency
reasons but because they lose what matters: they defeat git delta compression
(sabotaging git-as-backup), aren't greppable or PR-reviewable, and buy
nothing at this scale (~73k points / 16 MB, read linearly once per build).
If a file ever reaches ~50–100 MB, split it per-route or per-year before
reconsidering.

## Invariants

These are the load-bearing assumptions; breaking one is an architectural
change and belongs in this file:

- **Right tool for the job.** Dependencies are welcome when they beat the
  stdlib alternative; stdlib wins ties, nothing more. Keep the dependency
  list intentional — every entry should pull real weight.
- **The JSONL datasets are the source of truth, and ingest is lossless.**
  Scrape-time never discards parsed data; plausibility and rendering policy
  (the 170 mph GPS-glitch ceiling, the 10 mph stopped-train floor, the 2 mi
  off-route cut) are build-time choices in `build_map.py`, where a wrong
  threshold is a rebuild away from fixed. The parser earns this trust through
  fixture tests, not by keeping raw HTML forever — `data/raw/` is a
  disposable cache.
- **Scraping is idempotent** — dedup on `(train, ts, lat, lon)` makes re-runs
  always safe, any cadence.
- **Politeness is non-negotiable**: throttles, backoff, honest User-Agent.
  RailRat and archive.org are free rides.
- **Output is self-contained HTML** — no build step, no server, no assets
  beyond CDN Leaflet JS.
- **Route identity is the RailRat slug**, canonicalized through
  `ROUTE_ALIASES` at every entry point (CLI args and parsed pages).

## Testing

See [TESTING.md](TESTING.md). Tests live in `tests/`, run offline with
`pytest`, and use fixture HTML/geometry under `tests/fixtures/`. Shared
front-end JS logic is tested by evaluating `COMMON_JS` under an embedded V8
(`mini-racer`, a dev dependency). CI
(`.github/workflows/ci.yml`) runs the suite on Linux and Windows across the
supported Python range, plus `ruff check`, on every push and PR.
