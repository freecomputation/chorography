"""Gazetteer construction and item relevance classification.

The gazetteer is generated from a region directory, never written by
hand: the region name and tract names from the region's own files,
county and state names parsed out of census tract names where they
follow the census form, FIPS codes from tract identifiers and feed
specifications, zone and site codes from feed specifications, and
the bounding box of the tract geometry when geometry is present. A
region built from the census API carries null geometry until
boundaries are added, so the box falls back to the feed coordinates
the manifest declares, widened by half a degree.

Classification is deliberately simple and auditable. An item is
local when a local name matches on word boundaries, when a region
code appears in its text, or when its coordinates fall inside the
bounding box. It is state when the state name matches. Otherwise it
takes the fallback tier the caller supplies, which the harvester
sets to national for national and global sources and to nothing for
local and state sources, so unmatched items from a local shelf are
discarded while national shelves keep their context. When the
gazetteer itself is empty, because the region directory carries no
usable geography, the harvester falls back to the source's own
scope, so a bare region still aggregates rather than discarding
everything.
"""

import json
import os
import re

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

_STATE_NAMES = frozenset(US_STATES.values())

_CODE_PARAMS = ("site", "station", "zone", "county", "county_fips")


def _clean_name(name):
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", str(name)).strip()
    return cleaned


def _add_name(names, name):
    cleaned = _clean_name(name)
    if len(cleaned) >= 3:
        names.add(cleaned)


def _walk_coordinates(node, points):
    if isinstance(node, (list, tuple)):
        if (len(node) >= 2
                and all(isinstance(v, (int, float)) for v in node[:2])):
            points.append((float(node[0]), float(node[1])))
        else:
            for child in node:
                _walk_coordinates(child, points)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def build_gazetteer(region_dir):
    """Build the gazetteer dict for a region directory.

    Missing or unreadable files narrow the gazetteer rather than
    failing: a region without tract geometry simply has no box, and
    a region without names classifies by fallback alone.
    """
    local_names = set()
    state_names = set()
    codes = set()
    points = []

    manifest = _read_json(os.path.join(region_dir, "manifest.json")) or {}
    if manifest.get("name"):
        _add_name(local_names, manifest["name"])
    for spec in manifest.get("feeds", []):
        if not isinstance(spec, dict):
            continue
        for param in _CODE_PARAMS:
            value = spec.get(param)
            if value:
                codes.add(str(value))
        state_code = spec.get("state")
        if state_code in US_STATES:
            state_names.add(US_STATES[state_code])
        if "lat" in spec and "lon" in spec:
            try:
                points.append((float(spec["lon"]), float(spec["lat"])))
            except (TypeError, ValueError):
                pass

    tracts = _read_json(os.path.join(region_dir, "tracts.geojson")) or {}
    geometry_points = []
    for feature in tracts.get("features", []):
        props = feature.get("properties", {})
        if props.get("name"):
            _add_name(local_names, props["name"])
            for part in str(props["name"]).split(","):
                part = part.strip()
                if part.endswith(" County") or part.endswith(" Parish"):
                    _add_name(local_names, part)
                if part in _STATE_NAMES:
                    state_names.add(part)
        tract_id = str(props.get("id", ""))
        if tract_id:
            codes.add(tract_id)
            match = re.match(r"tract-(\d{2})-(\d{3})-", tract_id)
            if match:
                codes.add(match.group(1) + match.group(2))
        geometry = feature.get("geometry")
        if geometry:
            _walk_coordinates(geometry.get("coordinates"), geometry_points)

    bbox = None
    box_points = geometry_points if geometry_points else [
        (lon - 0.5, lat - 0.5) for lon, lat in points
    ] + [
        (lon + 0.5, lat + 0.5) for lon, lat in points
    ]
    if box_points:
        lons = [p[0] for p in box_points]
        lats = [p[1] for p in box_points]
        bbox = [min(lons), min(lats), max(lons), max(lats)]

    return {
        "region_name": manifest.get("name"),
        "local_names": sorted(local_names),
        "state_names": sorted(state_names),
        "codes": sorted(codes),
        "bbox": bbox,
        "empty": not local_names and not codes and bbox is None,
    }


def _name_matches(name, text):
    pattern = r"\b" + re.escape(name) + r"\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def point_in_box(lat, lon, bbox):
    if bbox is None:
        return False
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def classify(item, gazetteer, fallback=None):
    """Classify one item as local, state, national or discarded.

    The item is a dict with a title and optionally a summary or text,
    a latitude and a longitude. The fallback tier applies when no
    geographic signal matches; without a fallback the item is
    discarded.
    """
    text = " ".join(str(item.get(field, "")) for field in
                    ("title", "summary", "text"))
    lat, lon = item.get("lat"), item.get("lon")
    if lat is not None and lon is not None:
        if point_in_box(float(lat), float(lon), gazetteer.get("bbox")):
            return "local"
    for name in gazetteer.get("local_names", []):
        if _name_matches(name, text):
            return "local"
    lowered = text.lower()
    for code in gazetteer.get("codes", []):
        if len(code) >= 4 and code.lower() in lowered:
            return "local"
    for name in gazetteer.get("state_names", []):
        if _name_matches(name, text):
            return "state"
    return fallback if fallback is not None else "discarded"
