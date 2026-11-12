"""Deduplication and aggregation of harvested news items.

News arrives duplicated by construction: wire copy is syndicated,
the same story is republished with tracking parameters on its link,
and a developing story is reissued with a lightly edited headline.
Counting raw items would let syndication, not significance, set the
size of an information event. This module therefore counts stories,
not copies.

Deduplication runs in three passes over a source's items for one
day. Items whose links canonicalise to the same address are copies:
canonicalisation lowercases scheme and host, drops the fragment,
drops tracking parameters, sorts the remaining parameters and
strips a trailing slash. Items whose normalised titles are identical
are copies. Items whose title word shingles overlap a kept item by a
Jaccard similarity of the threshold or more are near copies and
collapse into the kept item.

Aggregation groups the surviving items by PMESII category and
relevance tier and emits one news_cycle event per group. The
magnitude is a saturating function of the deduplicated count times
the source's trust weight, capped per tier: with x the count times
the trust, the magnitude is cap times x over one plus x, so the
first items move the needle most and no volume of repetition can
push a group past its tier cap. Local groups cap at one, state at
seven tenths, national at four tenths, following the design's rule
that national context matters less to a region than its own news.
Sentiment is the mean of the source-published tone over the items
that carry one, and neutral when none do. Every event names its
counting function, its deduplicated item count and the number of
duplicates removed, so the figure can be audited back to the
archive.
"""

import hashlib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

TIER_CAPS = {"local": 1.0, "state": 0.7, "national": 0.4}

NEAR_DUPLICATE_THRESHOLD = 0.6

_TRACKING_PARAMS = {"fbclid", "gclid", "ref", "cmpid"}


def canonical_url(url):
    """Canonical form of a link, for copy detection."""
    parts = urllib.parse.urlsplit(str(url).strip())
    scheme = parts.scheme.lower() or "http"
    netloc = parts.netloc.lower()
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    params = [(k, v) for k, v in
              urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
              if not k.startswith("utm_") and k not in _TRACKING_PARAMS]
    query = urllib.parse.urlencode(sorted(params))
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def normalize_title(title):
    text = re.sub(r"[^a-z0-9]+", " ", str(title).lower())
    return " ".join(text.split())


def title_key(title):
    return hashlib.sha256(
        normalize_title(title).encode("utf-8")).hexdigest()


def title_shingles(title, size=3):
    tokens = normalize_title(title).split()
    if len(tokens) < size:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i + size])
            for i in range(len(tokens) - size + 1)}


def _jaccard(left, right):
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def dedupe_items(items, threshold=NEAR_DUPLICATE_THRESHOLD):
    """Collapse copies, keeping the first occurrence of each story.

    Returns the kept items in their original order and the number of
    items removed as copies.
    """
    kept = []
    seen_urls = set()
    seen_titles = set()
    kept_shingles = []
    removed = 0
    for item in items:
        url = canonical_url(item["url"]) if item.get("url") else None
        key = title_key(item.get("title", ""))
        shingles = title_shingles(item.get("title", ""))
        duplicate = False
        if url is not None and url in seen_urls:
            duplicate = True
        elif key in seen_titles:
            duplicate = True
        elif any(_jaccard(shingles, other) >= threshold
                 for other in kept_shingles):
            duplicate = True
        if duplicate:
            removed += 1
            continue
        kept.append(item)
        if url is not None:
            seen_urls.add(url)
        seen_titles.add(key)
        kept_shingles.append(shingles)
    return kept, removed


def saturate(count, trust, cap):
    """The aggregator's counting function, named in every event."""
    x = count * trust
    return cap * x / (1.0 + x)


def aggregate_events(items, duplicates_removed, source_id, trust, day):
    """Group deduplicated, classified items into news_cycle events.

    Each item carries a tier and a categories list. One event is
    produced per category and tier group, in sorted order so the
    output is deterministic.
    """
    groups = {}
    for item in items:
        for category in item.get("categories", []):
            groups.setdefault((category, item["tier"]), []).append(item)
    counting = ("saturating count: cap * x/(1+x) with x = deduplicated"
                " item count times trust weight; duplicates removed"
                " by canonical link, normalised title and title"
                " shingles")
    events = []
    for (category, tier) in sorted(groups):
        members = groups[(category, tier)]
        tones = [float(item["tone"]) for item in members
                 if item.get("tone") is not None]
        sentiment = (sum(tones) / len(tones)) if tones else 0.0
        events.append({
            "kind": "news_cycle",
            "series": "{}-aggregate".format(source_id),
            "category": category,
            "tier": tier,
            "day": day,
            "item_count": len(members),
            "duplicates_removed": duplicates_removed,
            "magnitude": round(
                saturate(len(members), trust, TIER_CAPS[tier]), 4),
            "sentiment": round(sentiment, 4),
            "counting": counting,
            "titles": [item.get("title", "") for item in members],
        })
    return events


def items_from_gdelt(raw):
    """Items from a GDELT artlist payload, in the aggregator's shape."""
    data = json.loads(raw.decode("utf-8"))
    items = []
    for article in data.get("articles", []):
        items.append({
            "title": article.get("title", ""),
            "url": article.get("url", ""),
            "tone": None,
            "published": article.get("seendate"),
        })
    return items


def items_from_rss(raw):
    """Items from an RSS or Atom payload, in the aggregator's shape.

    The parsing mirrors the ingest layer's feed handling: RSS items
    and Atom entries, title, link and summary extracted, nothing
    interpreted beyond structure.
    """
    root = ET.fromstring(raw.decode("utf-8", "replace"))
    items = []

    def text_of(node, *names):
        for name in names:
            for child in node:
                if child.tag.split("}")[-1] == name and child.text:
                    return child.text.strip()
        return ""

    def link_of(node):
        for child in node:
            if child.tag.split("}")[-1] == "link":
                href = child.get("href")
                if href:
                    return href
                if child.text:
                    return child.text.strip()
        return ""

    def collect(node):
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag in ("item", "entry"):
                items.append({
                    "title": text_of(child, "title"),
                    "url": link_of(child),
                    "summary": text_of(child, "description", "summary"),
                    "tone": None,
                    "published": text_of(child, "pubDate", "published",
                                         "updated"),
                })
            else:
                collect(child)

    collect(root)
    return items
