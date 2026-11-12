# Chorography Harvester - Design for 0.2

Status: design, not yet built. Written 2026-11-12 against Chorography 0.1.0 (main d3dde103).

## Purpose

Chorography 0.1.0 keeps its reference world current only when a person runs the fetch step by hand. The harvester is the tier that removes the person: an engine that continuously fills the world's inbox with the newest information available from the keyless public web, filtered to the region, aggregated to a volume the model can honestly absorb, and archived so every event can be traced to the exact bytes that produced it. The simulation core does not change. The harvester lives outside it, exactly where feeds.py already lives, and speaks to the world only through inbox files in the envelope format ingest.py already accepts.

The goal is stated plainly so it can be judged: at any moment, the reference world's information state is as current as the public keyless web allows, no event in the world lacks provenance, and no source can flood, poison, or silently stall the mirror without the stall itself being reported.

## Principles

The core never touches the network, and harvesting never touches the world. The only interface is the inbox folder and the event envelope: a chorography_event object with kind and fields, a source name, and a fetched_at timestamp.

Fetched content is data, never instruction and never code. All parsing is deterministic standard library code. Nothing fetched is executed, followed as a command, or allowed to name a file path outside the archive and inbox.

No invented magnitudes. An event's size comes only from a field the source itself publishes (a weather severity, an earthquake magnitude, a GDELT tone), or from a counting function stated in the harvester source and named in the event, so any reader can recompute it.

Politeness is a correctness property. Per host pacing, conditional requests with etags, backoff on 429 and 5xx, a descriptive User-Agent, and respect for each source's published limits (GDELT at five seconds between calls, BLS inside its 25 keyless queries per day, Overpass never at runtime).

A source outage is information, not failure. The health ledger records it, the report shows it, and the world advances on what arrived.

Determinism is preserved where it belongs. Harvesting is inherently timed by the outside world, so it is not deterministic and does not pretend to be. The world stays deterministic because replay reads the stored event log, never the network. A world's history, once ticked, can be replayed byte for byte regardless of what the harvester does later.

## Components

The Registry is a JSON document, one per region, extending the feeds list the region manifest already carries. Each entry names a source and everything the scheduler needs to run it without code changes: kind, URL or endpoint parameters, cadence tier, geography scope, the PMESII categories it may feed, a trust weight, and an enabled flag. Adding a local newspaper's RSS feed to a region is a registry edit, not a release.

The Scheduler is a new command, chorography harvest, built to be driven by an external clock. Run with --once, it walks the registry, fetches only the sources that are due, and exits. A state file beside the world database records, per source, the last attempt, the last success, the last item time, and the next due time. A lock file makes overlapping runs impossible. Each run carries a budget: a maximum request count and a maximum byte count, after which remaining sources wait for the next pass. Run under cron or a systemd timer every fifteen minutes, --once gives continuous coverage with no daemon to supervise. A --loop mode for foreground running is included for development only.

Fetchers do the wire work. The eight kinds in feeds.py today (NWS alerts and forecast, USGS earthquakes and water, GDELT, RSS and Atom, BLS, FRED) are reused unchanged. New fetchers in 0.2 follow the same shape: one function per kind, taking the client and the spec, writing envelopes, raising FeedError on failure and never anything else.

The Archive is an append-only store of raw payloads, laid out by date and source, each file named by the SHA-256 of its bytes. The harvester writes the raw bytes before any parsing happens, so a parser bug can be fixed and history re-derived from the archive without refetching. A JSON lines index records one line per fetch: source, URL, fetch time, status, byte count, hash, and the inbox files it produced.

Normalizers turn one source family's payloads into events. They are pure functions from bytes to event envelopes, tested against recorded fixtures, one module section per family. A payload that does not parse produces a provenance record and no events, exactly as ingest already treats truncated JSON and error bodies.

The Relevance pass decides what a global or national source means for this region. At region build time, a gazetteer is generated from the region file itself: county and place names, the state name, FIPS codes, a latitude and longitude bounding box, NWS zone and county codes, and USGS site identifiers. An item is local if it matches the gazetteer by name with word boundaries, by code, or by coordinates inside the box. It is state level if it matches the state alone, national if the source is a national context source, and discarded if it is neither and the source is a global firehose. Every surviving event carries its relevance tier, and lower tiers are capped harder by the aggregator.

The Aggregator is what keeps the mirror honest under volume. Hazard class events bypass it entirely: a weather alert, an earthquake, an outage notice each arrive as their own event, as they do today. Everything else is aggregated per source, per PMESII category, per day into a single news_cycle event, the kind the world already applies. Its magnitude is a saturating function of the deduplicated item count multiplied by the source trust weight, capped per tier, so the thousandth story about the same day moves the world no more than the fiftieth did. Its sentiment is the mean of source-published tone where the source publishes tone (GDELT does), and neutral where it does not. The event names its counting function and item count, so the number is auditable rather than tuned by feel.

Deduplication runs before aggregation. Items are keyed by canonical URL and by a hash of the normalized title; near duplicates, the same story syndicated across outlets, are collapsed by title shingling. A story counted once is signal. A story counted thirty times is an echo, and the aggregator must not hear it as thirty events.

The Health ledger is the harvester's own report card, kept in the state file and surfaced by chorography report: per source, last success, age of the newest item seen, error count over the last day, and a stale flag when a source returns success but its newest item is older than its cadence allows. Feed rot, the known RSS failure where a feed answers 200 with years old items, is caught by the stale flag rather than by faith.

## Source catalog for 0.2

Cadence tiers are set by how fast a source actually changes, not by enthusiasm.

The fifteen minute tier holds GDELT document search for the region's query set, NWS active alerts for the region's zones, and USGS earthquake feeds for the region's bounding box.

The hourly tier holds NWS forecast and station observations, USGS water instantaneous values for the region's gauges, and the region's RSS shelf (local papers, county and state emergency management, state agencies, utilities serving the region).

The daily tier holds Federal Register documents matching the region's agencies and place names, Treasury Daily Statement and FiscalData releases, USAspending new awards by place of performance in the region's FIPS codes, and slower RSS sources.

The release tier is fetched on its own calendars and cached hard: BLS series for the county and state in one batched call, and FRED macro series through the CSV endpoint with the bundled fallback, each respecting that these are context, not news.

The annual tier is a check for a newer ACS release and newer TIGER boundaries, reported as available, never auto-applied, because a new baseline is a region rebuild decision, not a tick event.

Deferred, with reasons recorded: CDC datasets until one or two specific datasets are pinned by name (the platform is 1,500 schemas, not a feed); every keyed API, including EIA, until a key is an accepted design input; OpenStreetMap Overpass at runtime, which its usage policy forbids in spirit; social platforms, which offer no keyless reader path; Common Crawl, whose volume is a research corpus, not a calibration signal.

## Operating pattern

A world that uses the harvester runs three rhythms. The timer rhythm: harvest --once every fifteen minutes, fetching what is due and nothing more. The day rhythm: once a day, after the day's last scheduled harvest pass, the world ticks once, consuming the day's inbox in deterministic file order. The reading rhythm: report and verify run whenever a person asks, over a world whose provenance chain runs from a PMESII index back through an event, an envelope, an archive hash, to the exact bytes a public server sent.

Where it runs is a deployment decision, not a design property. The harvester is a Python standard library program with a state file and a folder tree; it runs anywhere Python runs. Continuous operation on any particular machine is approved separately when the build lands.

## Verification

Every normalizer ships with recorded fixtures, including the failure fixtures: a 429 body, a 500 body, truncated JSON, a feed whose newest item is years old, a GDELT page of duplicates and syndicated copies of one story. The scheduler's due logic is tested against a fake clock. The relevance pass is tested against the sample region's gazetteer with items written to hit each tier and each rejection. The aggregator is tested at its caps: volume beyond saturation changes nothing, and a duplicated story counts once. An end-to-end test runs harvest --once against a local fixture HTTP server and then ticks the sample world on the produced inbox, proving the chain from wire to world without any external network. The existing 65 tests must pass unchanged, including the golden run hash, which is the proof that the harvester did not reach into the world.

## Phasing

0.2.0 builds the registry schema, the scheduler with its state file and lock, the archive and index, the health ledger, and moves the existing eight feed kinds under the scheduler unchanged. 0.2.1 adds the new sources: NWS observations, Federal Register, Treasury, and USAspending. 0.2.2 adds the gazetteer relevance pass and the aggregator with deduplication, which is the point where GDELT's volume becomes safe to open fully. Each phase lands as its own pull request with its own tests, in the house pattern.

## Limits, stated in advance

The harvester reads the keyless public web. Paywalled reporting, keyed datasets, and anything behind a login are outside it, and the design does not pretend otherwise. Local coverage depends on local sources existing in machine readable form; a county with no RSS publishing emergency management office will have a thinner local tier than one that does, and the health ledger will show that thinness rather than hide it. Aggregation is a modeling choice with real costs: a single extraordinary story and fifty routine ones can produce similar counts, which is why hazards bypass aggregation and why the archive keeps the raw record for any day a person wants to read closely. The engine fills the mirror with the newest information it can lawfully and politely read. It does not make that information true, and Chorography's standing rule survives unchanged: outputs are scenario contrasts under stated assumptions, never predictions.
