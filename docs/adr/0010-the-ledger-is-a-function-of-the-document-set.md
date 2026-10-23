# ADR-0010: The ledger is a function of the document set
Status: accepted
Date: 2026-10-22

## Context

One purchase often reaches the archive several times: a statement PDF, a CSV export of the same
cycle, a transaction download that overlaps two statements and runs past the last one. The copies
word and date the purchase differently. If each import added its lines on top of what the ledger
already held, the result would depend on which file came first, a re-import could double a line,
and a statement arriving after an export could not take over the export's lines. Decisions point at
lines (ADR-0004), so whatever replaces a line has to keep the old reference resolvable.

## Decision

Every derive recomputes all lines from stored parse output (Key decision 4). Nothing about a line
is carried over from an earlier derive; the derived tables are cleared and rebuilt (ADR-0003).

Documents rank per account by control (a tied-out statement, then a running-balance export, then
an unverified export), then format (statement PDF, transaction PDF, CSV, other), then the larger
sha256 (`derive/precedence.py`). Two documents of equal rank for exactly the same periods on every
account they cover are one document twice: the larger sha256 stays `parsed`, the other becomes
`superseded` in stage 1.

Stage 3 (`derive/overlap.py`) then decides each line:

- A tied-out statement owns its period. Its lines dated inside its period, and outside the
  periods of statements ranked before it, become txns first. Two statements never match each
  other's lines across a period boundary.
- Every other line matches one to one against txns other documents committed on the account:
  same currency and amount, a posting or transaction date within
  `finance.import.overlap_day_tolerance` of one of the txn's. It prefers the txn its own key
  already names, then an exact date with the same normalized description, then an exact date,
  then the tolerance with the same description, then the tolerance; among equals, the earliest.
  A match is an additional `txn_source` and adds no txn.
- An unmatched line becomes a txn outside every tied-out statement period, and a
  `source_discrepancy` (`unmatched_in_statement_window`) inside one.

A line's natural key counts occurrences within its own document, so a document's keys depend on
no other document. An additional source keeps its own line's key: when a covering statement
arrives, a reference taken on a provisional export line resolves through that key to the
statement-backed txn. A line key names one txn; several sources of one txn may share it (a CSV and
a PDF that print a purchase alike). A line whose key names a txn another line of its document
already took is a `source_discrepancy` (`line_key_taken`) rather than a second txn under a key
that is not its own.

A txn's basis is its first document's standing. A txn from an export is provisional when the
account has a tied-out statement and the txn is dated after the last one ends.

## Consequences

Import order cannot change the ledger, and a repeat import adds nothing. A wrong match drops a
real line, and two unverified exports outside any statement period can still double a line whose
dates differ by more than the tolerance (Technical Risk 1); discrepancies are counted for
`status`, and the tolerance is tuned on the real card overlap before older years are trusted. An
account with no tied-out statement has no provisional lines, since there is no last statement to
be after.

Derive currently reparses every document on every run; reusing the parse cache for unchanged
documents is a later step, and it must keep this rule (output depends only on the document set).

Recorded here because the repo is public and the spec's wording does not fit it, or the build
filled a gap:

- The basis class the spec names after the person is stored as `owner`.
- `txn.cad_source` and `txn.asset_identifier` are tiered restricted, stricter than the spec,
  because the tier guard restricts every name shaped like a CAD figure or an identifier.
- A manual cash line in a currency its account does not take becomes no txn and a
  `manual_entry_error` row (the spec does not say what happens to one).
- 0010's unique index on line keys is replaced in 0011 by a deferred trigger with the same
  intent, so that sources of one txn may share a key.
