# ADR-0016: Reader projections and last-derive queue counts
Status: accepted
Date: 2026-11-06

## Context

The reader role must monitor the ledger without base-table access or restricted identity text. Confirm eligibility depends on application rules, settings, decisions and the local date. Duplicating those rules in SQL would make reader counts disagree with the owner queue. Computing them through an owner connection during a reader request would weaken the connection boundary.

## Decision

Reporting operations use only reader-granted reporting views. Monetary outputs deliberately retain restricted tiers at exact named source/output pairs. No description, merchant key, counterparty, pattern value, identifier, alias, address or TXID is exposed under any alias. The existing pattern-emptiness boolean and mismatch-presence count are bounded non-value disclosures. Safe line labels come from the established name-stripping stage.

Each successful derive stores the complete seven-kind counts from the canonical counts-only queue computation in the same transaction. Every row carries the derive's sampled local `as_of_date`. Before the first successful derive, an empty projection means snapshot unavailable. A complete snapshot includes zero counts; partial kinds or inconsistent dates are unavailable.

These are last-derive snapshot counts, not live eligibility. Date, timezone, settings, decision and source changes require another derive before equivalence to the live owner queue can be claimed. Schema currency does not imply queue freshness. Reader requests never open an owner connection to refresh counts. Count changes participate in the ledger content digest; the snapshot evaluation date is bookkeeping and does not.

Crypto aggregation preserves currency, movement kind, nullable state and pair kind, so one-sided own movements remain separate. It sums native units only within a currency and leaves CAD unknown when any contributing CAD amount is missing.

## Consequences

A future consumer must label the counts as the last derive snapshot, show its date and distinguish unavailable from zero. Derive failures restore the preceding complete snapshot with the rest of the ledger. No new scheduler, status line, freshness flag or outbound policy is introduced.
