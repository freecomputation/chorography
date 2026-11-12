# Chorography

Chorography is a continuously running, continually calibrated synthetic mirror of a region, built in pure software. It keeps a reference world for one region: a synthetic population, its institutions and infrastructure, and a graph of political, military, economic, social, information and infrastructure state, all advancing one day at a time and all continually corrected by dated events from the real region. From any point in that reference world it can branch excursion copies, apply a different course of action to each copy, run them faster than real time, and compare what emerges. The design follows the approach set out for the Synthetic Environment for Analysis and Simulations and the Sentient World Simulation concept paper of 2006, rebuilt here as a small, readable, offline-first program.

Chorography is written in Python 3 using only the standard library. There is nothing to install and there are no third-party dependencies. The test suite and the bundled sample region run with no network access at all.

Version 0.2.0.

## Honest limits

The outputs of Chorography are scenario contrasts under stated assumptions, never predictions. An excursion answers the question of what this model, with these parameters and these behavioural rules, does differently when one course of action is changed and everything else is held equal. It does not answer what the real region will do.

Two named problems bound every result. Equifinality means that different parameter sets and different behavioural rules can fit the same observed patterns, so a good fit does not identify a unique mechanism. Model discrepancy means that the structure of the model itself differs from the structure of the real region, so even a perfect parameter fit leaves a gap that no calibration removes. The calibration tools in this program report pattern fit and held-out fit separately and flag sustained divergence, and they do not claim more than that.

The behavioural rules in version 0.1.0 are deliberately simple first approximations, stated in code where they can be read and challenged. The synthetic population is fitted to aggregate marginals only, so joint distributions beyond the fitted dimensions are modelled, not measured. Anyone using Chorography on a real region should expect to replace both the rules and the starting state with local knowledge before trusting any contrast.

## Usage

Every command runs through the package entry point from the repository root. A world lives in a single sqlite store file created by the init command.

The init command builds a region directory into a fresh store. The bundled sample region is a demonstration region with invented numbers, described further under Data.

```text
python3 -m chorography init --region data/sample-region --db world.db
```

The run command advances the reference world by a number of days. The inbox option names a folder of feed files to ingest on the first of those days. The live flag first fetches the feeds listed in the region manifest into an inbox folder, tolerating any feed that is down.

```text
python3 -m chorography run --db world.db --days 30 --inbox data/sample-region/inbox
python3 -m chorography run --db world.db --days 7 --live
```

The excursion command runs a course-of-action script as an ensemble and prints the reference summary, the branch summary and the contrast between them. A script is JSON data with a name, a number of days, a list of branch seeds, dated interventions and parameter overrides.

```text
python3 -m chorography excursion --db world.db --script coa.json
```

The calibrate command scores the recorded history against the pattern scorecard, runs the held-out check, and runs the small parameter grid search.

```text
python3 -m chorography calibrate --db world.db
```

The report command prints the reference world summary and the PMESII dashboard as plain text, or as JSON with the json flag.

```text
python3 -m chorography report --db world.db
python3 -m chorography report --db world.db --json
```

The verify command replays the recorded run from the initial snapshot, re-applying every recorded exogenous event, and compares the replayed state hash with the stored final hash. A passing verify is the program's own proof that its record and its engine agree.

```text
python3 -m chorography verify --db world.db
```

The harvest command runs the harvester, the tier that keeps the inbox filled without a person fetching by hand. It reads the region's source registry, fetches only the sources that are due, and exits, so it is meant to be woken by a scheduler such as cron every fifteen minutes. The state option names the harvest state file and the archive option the payload archive folder; when a database is named with the db option, both default to files beside that store. The loop flag runs passes in the foreground and exists for development.

```text
python3 -m chorography harvest --region data/sample-region --db world.db --once
```

When a harvest state file exists beside the store, the report command gains a harvest health section: for each source, its last success, the age of the newest item it has produced, its error count over the last day, and a stale flag for a source that answers but whose newest item is older than its cadence allows.

The test suite runs from the repository root and needs no network.

```text
python3 -m unittest discover -s tests
```

## Architecture

The overview follows the spirit of the ODD protocol used to describe agent-based models: entities, scales, process order and scheduling are stated plainly so that another reader can rebuild the model from the description.

The entities are household composites, institutions, infrastructure nodes and geographies. A household composite is a group of households sharing one synthesis cell of size, income band and tenure, carrying a fractional weight and fractional behavioural state such as the share employed. An institution is a school, clinic, employer, agency or media outlet with capacity, service level, stress and trust. An infrastructure node is a power, water, road or hospital plant with condition and outage share. A geography is a census tract holding PMESII state: for each of the six categories, named scalars with a raw value, a normalised index, units and a source. For a civilian region the military category is read as safety and security state, namely public safety and emergency readiness.

The spatial scale is the tract, with region-wide aggregates computed from tracts. The temporal scale is one day per tick, and excursions run the same daily tick faster than real time. The synthetic population is built sample-free from aggregate tables: a uniform seed table is fitted to the published marginal of each dimension by iterative proportional fitting, and each fitted cell becomes one composite. A composite can be expanded into individual agents under a seeded random stream and collapsed back by aggregation, conserving total weight.

Every tick runs six named stages in a fixed order: ingest, environment, institutions, population, interaction and bookkeeping. Exogenous events enter only at ingest. Stages that move agents work in two phases, computing intents for all agents from the tick-start state and then applying them together, so no agent reacts to a half-updated world. Cross-category coupling in the interaction stage comes from a sparse effect table held as data in the package, not from constants buried in code. Bookkeeping records daily aggregates into the run history before time advances.

The reference world is a society of one model family in this version, with heterogeneous models coupled inside it at different granularities: tract-level PMESII dynamics, agent-level household and institution behaviour, and node-level infrastructure wear and repair. Excursions branch the whole world by snapshot and deep copy, run with common random numbers day for day against reference branches, and leave the reference world untouched. That immutability, and the fairness of the comparison, are both covered by tests.

Determinism is a design requirement, not an accident. Each random stream is derived from the run seed, a stream purpose and the day index. Agents are always iterated in integer id order. The canonical serialisation of world state sorts keys and rounds floats, and its SHA-256 hash identifies a run: two runs with the same seed and the same events hash identically, and the test suite pins one golden run hash so that any behavioural change to the engine is a deliberate, visible event.

Storage is event sourced on sqlite. Exogenous events and ingest records are appended with a schema version, snapshots hold full canonical states, and restart is snapshot plus deterministic replay, checked by hash.

Ingest reads files dropped into an inbox folder. Each file becomes dated exogenous events with provenance: the source, the fetch time when known, and the SHA-256 of the raw payload bytes. Recognised shapes include National Weather Service alert documents, United States Geological Survey earthquake documents, RSS and Atom feeds, and a generic event envelope. Error bodies, truncated files and stale feeds are recorded as outages or staleness and never block the world. The fetchers that fill the inbox live outside the core in a separate module, cover the keyless feeds named in SOURCES.md, send a descriptive user agent, cache responses on disk with their validators, back off on errors, and pace the GDELT feed at the interval that service requests.

The harvester, new in version 0.2.0, is the scheduling tier above those fetchers, and its design is recorded in HARVESTER-DESIGN.md. The region manifest's feeds list becomes a registry: each entry gains a cadence tier, a geography scope, the PMESII categories it may feed, a trust weight and an enabled flag, all validated with defaults that keep the 0.1.0 manifest shape working unchanged. The scheduler fetches only due sources, under a lock, within per-run request and byte budgets, with per-host pacing and a bounded backoff after failures. Every payload is archived by content hash before anything parses it, with an append-only index recording what each fetch produced, and a state file keeps the health ledger the report renders. Harvesting never touches the world except through the inbox, and replay never touches the network, so the determinism the test suite pins is untouched by design.

Calibration is pattern oriented. A scorecard of named patterns with bands is scored over the run history, divergence flags mark series that sit outside a band for several consecutive ticks, a small grid search over the few free parameters runs as batch excursions, and a held-out check scores the later part of the history separately from the part a fit was tuned on.

## Data

The bundled sample region under data/sample-region is a demonstration region. Its manifest says so, its aggregate numbers were invented for demonstration and testing, and its tract polygons are simple invented rectangles. It exists so that the whole system and the whole test suite run offline, and it must never be cited as census data or as a description of any real place. Its inbox holds recorded feed fixtures: one active weather alert, one earthquake, one fresh news feed, one stale feed, an HTTP 429 body and a truncated JSON file.

A real region seed can be built when online with the regionfetch module, which queries the Census American Community Survey 5-year API without a key for a named state and county and writes the same region layout. Its limits are documented in DESIGN.md and in the module itself: the ACS figures are 5-year period estimates, margins of error are not carried, keyless querying is rate limited, tract polygons are not supplied by that API, and PMESII starting values other than income are neutral placeholders awaiting local data.

Data sources, licensing and attribution are recorded in SOURCES.md. The design decisions behind this version, and the work deliberately deferred, are recorded in DESIGN.md.
