"""Shared helpers: fixture loading and a minimal synthetic RailRat page.

Fixtures under tests/fixtures/ are real raw pages kept verbatim:
- train125_current_2026-07-21.html -- current page markup (est. entries,
  span-wrapped delays, arrival times inside viewport spans).
- train125_wayback_2020-09-26.html -- the 2020-era markup (one bold verb
  per station line, "ET" suffixes, a "completed" destination entry).

synth_page() builds the smallest page the parser accepts, for cases the
real fixtures don't cover (midnight crossings, glitch speeds, etc.).
"""
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def synth_page(updated, markers="", tracker_lis="",
               route_slug="AcelaExpress", route_name="Acela Express"):
    """Minimal train page with exactly the elements scrape_railrat keys on:
    the h1 route/train header, the "updated" stamp, Leaflet marker calls,
    and a Progress Tracker <ol>."""
    return f"""<html><body>
<h1><a href="/routes/{route_slug}/">{route_name}</a> Train 2151</h1>
<p>Latest status for Acela Express Train 2151, updated {updated} (unofficial).</p>
<div id="map"></div>
<script>
{markers}
</script>
<div id="train_progress">
<h2>Progress Tracker</h2>
<ol>
{tracker_lis}
</ol>
</div>
</body></html>"""


def marker(hh, mm, lat, lon, mph, desc="1 mi N of PHL", heading="N"):
    """One Leaflet position-report marker call in RailRat's exact format."""
    return (f'L.circleMarker([{lat:.6f},{lon:.6f}],blueCircle).addTo(mymap)'
            f'.bindPopup("<b>Acela Express 2151</b><small><br>'
            f'{hh:02d}:{mm:02d} {desc}, {mph}&nbsp;mph&nbsp;{heading}</small>");')
