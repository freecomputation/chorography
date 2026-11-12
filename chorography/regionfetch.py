"""Building a real region seed from the Census ACS API.

This module is used only when an operator is online and asks for a real
region. It queries the American Community Survey 5-year API without a
key for one state and county, at census tract level, and writes a
region directory in the same layout as the bundled sample region.

Variables used, all from the ACS 5-year subject tables named:

B01003_001E is total population. B11001_001E is total households.
B19013_001E is median household income in dollars.
B25003_001E, _002E and _003E are total, owner occupied and renter
occupied housing units, giving the tenure marginal.
B19001 gives the household income distribution in seventeen bands,
grouped here into four bands: _002E to _007E under 35,000 dollars,
_008E to _012E from 35,000 to 75,000, _013E to _015E from 75,000 to
150,000, and _016E plus _017E at 150,000 and above.
B11016 gives household size by household type. The size marginal sums
family and nonfamily households of each size: 1 person is _010E, 2 is
_003E plus _011E, 3 is _004E plus _012E, 4 is _005E plus _013E, 5 is
_006E plus _014E, 6 is _007E plus _015E, and 7 or more is _008E plus
_016E.

Limits are stated plainly. The ACS 5-year estimates are period
estimates with margins of error, which this seed does not carry. The
keyless API is rate limited and suited to occasional region builds,
not to bulk harvesting. Tract boundary polygons are not part of the
ACS API response, so the written tracts.geojson carries null geometry
with a manifest note; boundaries can be supplied later from Census
TIGERweb files. PMESII starting values other than income are neutral
placeholders sourced as such in the metric specs, to be replaced with
local data before any serious use.
"""

import json
import os
import urllib.parse
import urllib.request

USER_AGENT = "Chorography/0.1.0 (Free Computation synthetic region mirror)"

VARIABLES = [
    "NAME",
    "B01003_001E",
    "B11001_001E",
    "B19013_001E",
    "B25003_001E",
    "B25003_002E",
    "B25003_003E",
] + ["B19001_{:03d}E".format(i) for i in range(1, 18)] + [
    "B11016_{:03d}E".format(i) for i in range(1, 17)]


def _num(row, code):
    value = row.get(code)
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number)


def acs_url(year, state_fips, county_fips):
    query = urllib.parse.urlencode({
        "get": ",".join(VARIABLES),
        "for": "tract:*",
        "in": "state:{} county:{}".format(state_fips, county_fips),
    })
    return ("https://api.census.gov/data/{}/acs/acs5?".format(year)
            + query)


def fetch_acs(year, state_fips, county_fips, opener=None):
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        acs_url(year, state_fips, county_fips),
        headers={"User-Agent": USER_AGENT})
    response = opener(request)
    raw = response.read()
    rows = json.loads(raw.decode("utf-8"))
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def tract_from_row(row):
    size = {
        "1": _num(row, "B11016_010E"),
        "2": _num(row, "B11016_003E") + _num(row, "B11016_011E"),
        "3": _num(row, "B11016_004E") + _num(row, "B11016_012E"),
        "4": _num(row, "B11016_005E") + _num(row, "B11016_013E"),
        "5": _num(row, "B11016_006E") + _num(row, "B11016_014E"),
        "6": _num(row, "B11016_007E") + _num(row, "B11016_015E"),
        "7": _num(row, "B11016_008E") + _num(row, "B11016_016E"),
    }
    income = {
        "under_35k": sum(_num(row, "B19001_{:03d}E".format(i))
                         for i in range(2, 8)),
        "35k_75k": sum(_num(row, "B19001_{:03d}E".format(i))
                       for i in range(8, 13)),
        "75k_150k": sum(_num(row, "B19001_{:03d}E".format(i))
                        for i in range(13, 16)),
        "150k_plus": _num(row, "B19001_016E") + _num(row, "B19001_017E"),
    }
    tenure = {
        "owner": _num(row, "B25003_002E"),
        "renter": _num(row, "B25003_003E"),
    }
    median_income = _num(row, "B19013_001E")
    tract_id = "tract-{}-{}-{}".format(row.get("state", ""),
                                       row.get("county", ""),
                                       row.get("tract", ""))
    neutral = "neutral placeholder, replace with local data"
    pmesii = {
        "political": {"trust_institutions": 50.0,
                      "civic_participation": 50.0},
        "military": {"public_safety": 50.0, "emergency_readiness": 50.0},
        "economic": {"employment_rate": 60.0,
                     "income_level": median_income,
                     "business_activity": 50.0},
        "social": {"morale": 55.0, "health_status": 60.0,
                   "education_access": 50.0},
        "information": {"media_trust": 50.0, "connectivity": 50.0},
        "infrastructure": {"power_reliability": 90.0,
                           "water_reliability": 90.0,
                           "transport_access": 50.0,
                           "hospital_capacity": 50.0},
    }
    return {
        "id": tract_id,
        "name": row.get("NAME", tract_id),
        "population": _num(row, "B01003_001E"),
        "households": _num(row, "B11001_001E"),
        "marginals": {"size": size, "income": income, "tenure": tenure},
        "pmesii": pmesii,
        "neutral_source_note": neutral,
    }


def metric_specs():
    def spec(ref, units, source):
        return {"ref": ref, "units": units, "source": source}
    neutral = "neutral placeholder, replace with local data"
    return {
        "political": {
            "trust_institutions": spec(100.0, "index points", neutral),
            "civic_participation": spec(100.0, "index points", neutral),
        },
        "military": {
            "public_safety": spec(100.0, "index points", neutral),
            "emergency_readiness": spec(100.0, "index points", neutral),
        },
        "economic": {
            "employment_rate": spec(100.0, "percent", neutral),
            "income_level": spec(100000.0, "dollars per year",
                                 "ACS B19013 median household income"),
            "business_activity": spec(100.0, "index points", neutral),
        },
        "social": {
            "morale": spec(100.0, "index points", neutral),
            "health_status": spec(100.0, "index points", neutral),
            "education_access": spec(100.0, "index points", neutral),
        },
        "information": {
            "media_trust": spec(100.0, "index points", neutral),
            "connectivity": spec(100.0, "index points", neutral),
        },
        "infrastructure": {
            "power_reliability": spec(100.0, "index points", neutral),
            "water_reliability": spec(100.0, "index points", neutral),
            "transport_access": spec(100.0, "index points", neutral),
            "hospital_capacity": spec(100.0, "index points", neutral),
        },
    }


def build_region(year, state_fips, county_fips, region_name, out_dir,
                 epoch_date, seed=1, opener=None):
    rows = fetch_acs(year, state_fips, county_fips, opener=opener)
    tracts = [tract_from_row(row) for row in rows]
    os.makedirs(out_dir, exist_ok=True)
    manifest = {
        "name": region_name,
        "demonstration": False,
        "epoch_date": epoch_date,
        "seed": seed,
        "provenance": ("Built from the Census ACS {} 5-year API for "
                       "state {} county {}. Keyless query, period "
                       "estimates, margins of error not carried."
                       .format(year, state_fips, county_fips)),
        "notes": ("Tract boundary polygons are not supplied by the ACS "
                  "API; tracts.geojson carries null geometry until "
                  "TIGERweb boundaries are added. PMESII values other "
                  "than income are neutral placeholders."),
        "feeds": [],
    }
    aggregates = {
        "income_bands": ["under_35k", "35k_75k", "75k_150k", "150k_plus"],
        "size_categories": {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
                            "6": 6, "7": 7},
        "metric_specs": metric_specs(),
        "tracts": tracts,
    }
    features = [{
        "type": "Feature",
        "properties": {"id": t["id"], "name": t["name"]},
        "geometry": None,
    } for t in tracts]
    geojson = {"type": "FeatureCollection", "features": features}
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "aggregates.json"), "w",
              encoding="utf-8") as fh:
        json.dump(aggregates, fh, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "tracts.geojson"), "w",
              encoding="utf-8") as fh:
        json.dump(geojson, fh, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "infrastructure.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"nodes": [], "institutions": []}, fh, indent=1)
    return manifest
