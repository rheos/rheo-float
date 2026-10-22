# ADR-0006: The phase-ordered build is the cut proof
Status: accepted
Date: 2026-10-22

## Context

The spec lists features in a cut order: if time runs out, they go in that order, and the core must
stay green without them (AC-79). Proving that usually means a plugin registry, feature flags, a
manifest, or a script that deletes features and runs the suite. Each of those is machinery that
can itself break, and none of it is needed for the ledger to work.

## Decision

Every cut-order item is an ordinary package under `rheo_finance/features/`, with its tests under
`tests/features/<name>/`, wired by plain imports at the call sites that use it. There is no
registry, no discovery, no manifest and no deletion script. The build runs in reverse cut order,
and every phase gate runs the whole suite on the tree as it stands. An earlier tree never imported
what was built later, so its green run is the proof that the cut is safe. Each gate records its
green run in its PR.

Two import-graph tests hold the boundaries that matter for safety. `cli.approve` and `cli.bulk`
are imported by `cli.main` only, never from `jobs`, `sinks`, `model`, `mail`, `rates`,
`connectors` or `features`, in every tree from Phase 1. The exchange mail parser imports neither
`rheo_finance.http` nor `rheo_finance.model`, in every tree that contains it. Migrations stay in
the one core chain, so a tree without a feature keeps its empty tables.

## Consequences

The proof costs nothing extra to run but depends on build discipline: a later feature must never
be imported by core code written in an earlier phase. A cut after the fact is a revert to an
earlier gate's tree, not a flag flip. Empty feature tables sit in every database.
