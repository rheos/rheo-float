# ADR-0004: Deterministic ids for derived rows
Status: accepted
Date: 2026-10-22

## Context

Derived rows are truncated and rebuilt (ADR-0003), yet decisions point at lines and the ledger
digest must come out the same after a rebuild. A random id or a sequence changes on every rebuild,
so every decision would orphan and the digest would never match. The house default for ids is
bigint, but the spec fixes UUIDv7 layout so ids sort by date and stay unique across tables.

## Decision

A derived row's id has the UUIDv7 layout. The 48-bit time field is the row's business date at
00:00 UTC; the remaining bits come from sha256 of the row's natural key, with the version and
variant bits set. Both inputs are pure functions of the natural key. For a crypto line the time
field is the UTC calendar date of `occurred_at`, never the local date, which the configured
timezone would move. Every Decimal in a natural key is canonicalized before hashing
(`Decimal.normalize()` rendered with `format(d, "f")`, so `1E-7`, `0.0000001` and `0.00000010000`
hash the same, and `0E-10` and `0` are one zero); dates are ISO and strings are NFC. State rows get
ordinary UUIDv7. All derived ids go through one helper, `derived_id`.

## Consequences

Rebuild reproduces every derived id, so decisions survive and the digest is stable. Changing a
natural key, or how one of its parts is canonicalized, changes ids and orphans decisions; that is
why orphaned decisions get a re-key path. Two rows with the same natural key collide by design, so
natural keys must carry an occurrence counter where real duplicates exist. This overrides the
bigint default in the house engineering standard for this repo.
