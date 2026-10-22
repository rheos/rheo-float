# ADR-0003: Derived tables plus a small state store
Status: accepted
Date: 2026-10-22

## Context

A parser fix, a new rule or a corrected manual entry changes what the ledger should say about
months already loaded. Patching rows in place makes every fix a migration of data and leaves the
ledger dependent on the order things happened in. Rebuilding everything from scratch is simple,
but some facts cannot be recomputed: the owner's decisions, model proposals that cost a call to
get, fetched rates, tickets already sent to an outside system.

## Decision

Every table splits into one of two kinds. A derived table holds anything recomputable from the raw
statement archive, the manual-entry files and the owner's decisions; derive and rebuild truncate
and refill all of them in one transaction. A state table holds what cannot be recomputed:
decisions with their target snapshots, stored model proposals, rates, sink tickets, notices, job
runs, packs, sender candidates and connector state. State tables are never truncated. Rebuild
never calls a model or the network; it reads stored proposals and stored rates.

## Consequences

Any fix to parsing or derivation is a code change plus `finance rebuild`, with no data migration.
Rebuild cost grows with the archive, which is acceptable at personal scale. Every new table has to
be declared one kind or the other, and a state table needs a reason, because state is the part
that can drift from the archive. Derived rows need ids that survive a rebuild (ADR-0004).
