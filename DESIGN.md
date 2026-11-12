# Chorography design record

This document records the design decisions settled for version 0.1.0 and the work deliberately deferred beyond it. It is a record of choices and their reasons, so that later versions can argue with them on paper rather than by archaeology in the code.

## Settled decisions

The region is the unit of the mirror. Following the term itself, Chorography models one region in full detail rather than the whole world thinly. Geography is composed from chorographies: a wider mirror can be assembled from regional mirrors that share the event and state formats defined here.

One tick is one day, and every tick runs the same six named stages in the same order: ingest, environment, institutions, population, interaction, bookkeeping. A fixed stage list makes a run readable as a schedule and makes the point where any influence enters the world auditable. Exogenous events enter only at ingest. Courses of action in excursions are expressed as the same event shapes, so an intervention and a real event are handled by one mechanism.

Interacting stages use two-phase commit. Intents are computed for every agent from the tick-start state and then applied together. The alternative, updating agents in place in iteration order, makes outcomes depend on agent ordering and lets early agents react to states that later agents have not reached, which is a modelling artefact rather than a finding. The test suite spies on the intent functions to prove that every agent sees tick-start state.

Determinism is treated as a feature that the science depends on. Excursion contrasts are differences between runs; if runs are not reproducible, contrasts cannot be attributed to the intervention. Random streams are therefore derived from seed, purpose and day, agents are iterated in integer id order, and state is identified by a SHA-256 hash of a canonical serialisation. Common random numbers follow from the same derivation: a branch and the reference draw the same numbers on the same day when their seeds match, and the suite proves an intervention-free branch reproduces the reference run exactly.

The synthetic population is sample-free. Where a statistical office publishes marginals but microdata is unavailable, restricted or unsafe to hold, a seed table fitted to the marginals by iterative proportional fitting gives a population that matches every published total it was fitted to. Composite agents with fractional weight are the native resolution of the model; individuals are an expansion used when a question needs them, and collapse by aggregation returns to composites conserving weight. This mirrors the coarse and fine granularity coupling of the reference design, in which the same person can be carried by both a coarse and a fine model.

PMESII state is held per geography as named scalars, each with a raw value, a normalised index, units and a source. Cross-category coupling is a sparse effect table stored as data in the package. Couplings are assumptions and are therefore kept where they can be read, cited, diffed and changed without a code edit. The military category is read as safety and security for a civilian region, covering public safety and emergency readiness.

Storage is event sourced. The event record is append-only with versioned schemas, snapshots hold canonical states, and restart correctness is defined as replay reproducing the stored hash. This makes the verify command a real check of the whole chain from record to engine rather than a file existence test.

Ingest and fetching are separated from the core. Fetchers write files and nothing else; ingest turns files into events with provenance and tolerates every failure mode it has met, including error bodies saved in place of payloads, truncated files and feeds that have gone stale. A feed outage therefore degrades a run's information, never its ability to run. That separation is also what allows the entire system and test suite to work offline from recorded fixtures.

Calibration is pattern oriented rather than point fitting. Named patterns with bands are harder to overfit than a single series, divergence flags require several consecutive out-of-band ticks so that one noisy day does not raise an alarm, grid search is kept small enough to read whole, and the held-out check is reported beside every fit. Reports name equifinality and model discrepancy as standing limits rather than treating fit as validation.

The core has no third-party dependencies and no network code. Fetching, the Census region builder and the command line are edges around a core that can be read in one sitting and run anywhere Python 3 runs.

## Deferred work

A society-of-models plugin system, in which external models written by other authors join the world through the SimBridge idea of the reference design and exchange state through the ontology, is deferred. Version 0.1.0 couples its model families inside one process with documented interfaces, which is enough to prove the tick, branching and calibration machinery; a plugin boundary designed before a second real model exists would be a guess.

A general discrete-event engine is deferred. The daily tick is the right grain for the demographic, economic and institutional dynamics modelled here. Processes that genuinely need sub-day event scheduling, such as tactical movement or message-level information spread, should arrive with the model that needs them and be integrated at the interaction stage rather than replacing the tick.

A live one-to-one population, in which every real resident has a corresponding individual agent continually updated, is deferred and may remain out of reach honestly. It would require person-level data feeds that are neither openly available nor acceptable to hold, and its marginal value over well-fitted composites is unproven. Expansion and collapse exist so that individual resolution can be used where a question needs it without pretending to a live census of persons.

Feeds that need keys or accounts, including the CDC data services and the keyed tiers of the Bureau of Labor Statistics and FRED APIs, are deferred. The keyless forms of those sources are used where they exist. The Census region builder likewise defers margins of error, TIGERweb boundary retrieval and sub-county geographies, and states those limits where its output is written.
