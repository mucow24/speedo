# speedo

Maps how fast Amtrak trains actually go, mile by mile, using position/speed
reports scraped from [RailRat.net](https://railrat.net). Output is a
self-contained interactive HTML map: the route drawn as half-mile segments
colored by the **max speed ever observed** there (red 0 mph → orange → yellow
→ green → blue 160 mph), clickable for the details at any point.

Built for fun. No standing infrastructure — just two scripts and a growing
pair of JSONL datasets (GPS/speed points, plus per-station arrival/departure
timings and delays scraped from the same pages).

## Running it

Requires Python 3.10+. The scripts themselves have no third-party
dependencies. For development (tests, lint):

```sh
pip install -e ".[dev]"
pytest
ruff check .
```

```sh
# 1. Scrape the latest runs (safe to repeat; only new points are added)
python scrape_railrat.py

# 2. Build the maps from everything scraped so far
python build_map.py

# 3. Open the result
out/speed_map_AcelaExpress.html          # Leaflet/OSM - works as-is
out/speed_map_AcelaExpress_google.html   # needs a Google Maps API key
```

The order matters: `build_map.py` renders whatever `scrape_railrat.py` has
accumulated in `data/observations.jsonl`. RailRat only serves each train's
latest run, so **the map gets better the more often you run step 1** — once a
day for a week or two fills in most of the corridor. Any cadence works;
nothing breaks if you skip a month.

All the knobs:

```sh
python scrape_railrat.py --route NortheastRegional   # another RailRat route slug
python scrape_railrat.py --trains 2151,2153          # seed extra train numbers
python scrape_railrat.py --wayback                   # harvest archive.org snapshots
python scrape_railrat.py --reparse                   # rebuild dataset from data/raw, no network

python build_map.py --route NortheastRegional        # must match a scraped route
python build_map.py --engine leaflet                 # or: google, both (default)
python build_map.py --google-key AIza...             # bake your key into the google file
```

Notes:

- `--wayback` is slow on purpose (archive.org rate-limits aggressively) and
  fully resumable: successful lookups and snapshots are cached under
  `data/raw/wayback/`, so if it gives up mid-pass, just run it again later.
  It's a small bonus (the Archive only has a handful of snapshots per train),
  not required for a good map.
- For a route other than Acela, `build_map.py` needs to know the matching
  NTAD geometry name — see the `ROUTES` dict at the top and add an entry if
  your route isn't listed.
- The Google map needs a Maps JavaScript API key (billing-enabled Google Cloud
  account; hobby use fits the free tier). Pass `--google-key`, or edit
  `YOUR_GOOGLE_MAPS_API_KEY` in the output file. Don't publish that file
  unless the key is referrer-restricted — that's also why `out/` is
  gitignored.

## How it works

- **Source**: every RailRat train page embeds its full position history for the
  current run (and keeps the last completed run until the next one starts) as
  Leaflet marker calls — exact lat/lon, time, mph, heading, every ~3–6 min.
  `scrape_railrat.py` fetches the route's train pages (1 req/sec, ~40 pages),
  parses those markers, and appends anything new to `data/observations.jsonl`.
  Each page's "Progress Tracker" (per-station actual arrival/departure times
  and delay vs. schedule) is parsed too, into `data/station_events.jsonl`.
  Raw HTML is cached under `data/raw/` (disposable; `--reparse` rebuilds both
  datasets from whatever is there, e.g. after parser improvements).
- **History**: RailRat only serves the latest run per train number, so history
  accumulates as you re-run the scraper. Two freebies help: train numbers that
  rotated out of the schedule still hold their last-ever run (months old), and
  `--wayback` adds whatever the Internet Archive has. (There is no deep public
  archive of Amtrak GPS/speed data — TransitDocs/ASM archives station timings
  only.)
- **Map**: `build_map.py` downloads the official route line (USDOT/BTS NTAD
  "Amtrak Routes" geometry), projects every observation onto it, slices the
  line into 0.5-mile bins, and colors each bin by max observed mph. Branched
  routes (the Regional's Virginia legs, the Empire Builder's Portland leg)
  are drawn as several sections, with mile markers numbered continuously
  across them — so station
  dwells and delay slowdowns don't hide what the track can do; they only show
  up where *every* train is slow (station throats, curves, speed-restricted
  territory). Points >2 mi off-route or >170 mph (GPS glitches) are ignored
  at build time (they stay in the dataset; scraping never discards data).

## Data notes

- Timestamps come from RailRat's popup clock (Eastern for NEC routes); dates
  are inferred by walking backward from the page's "updated" stamp.
- `data/observations.jsonl` is append-only and deduped on
  (train, timestamp, position) — re-scraping is always safe.
- `data/station_events.jsonl` is append-only too; as a run progresses, a
  station's record can appear in several progressively-more-complete
  variants (arrival first, departure and delay later) — merge by
  (train, run_date, station) when consuming it.
- Gray dashed bins = no observations yet. Expect a permanent gap around miles
  229–234 (from Boston): that's Penn Station and the Hudson/East River tunnels,
  where GPS genuinely can't reach.
