# ADR-0001: Import docextract
Status: accepted
Date: 2026-10-22

## Context

The owner already has fixed-rule statement parsers in `docextract` (the `doc-extraction-api`
repo): a bank's statement parser and a card issuer's reference parser. The ledger needs the same
numbers those parsers produce. It could call the existing service over HTTP, copy the parsing
rules, or import the package. Copying gives two homes for every rule and lets totals drift.
Calling a service adds a process and a network hop to every rebuild. Importing gives identical
totals by construction, but `docextract` was not packaged as a library: it had no build system,
and its server dependencies were mandatory.

## Decision

The ledger imports `docextract` as a git dependency pinned to a commit. One upstream change makes
that possible, in its own issue and PR: a hatchling `[build-system]` that packages `docextract`
only, the unused model client dropped, and the web-server dependencies moved to an `api` extra
and also listed in the `dev` group, so a plain `uv sync` in that repo still installs them.

The ledger imports only the statement and reference parsers, their error types, the PDF-to-text
helper with its not-a-text-PDF error, and the money converter (`to_money`, `Decimal(str(v))`,
exact for two-decimal values). It imports nothing from the monitor code or the model-based
extractor: sending a statement that has a working parser to a model is forbidden. Each parsed
account is matched to the account registry by its identifier, never by its printed name, since an
institution can rename an account and keep its number; two identifiers at one institution sharing
a last four is a loader error.

## Consequences

A parser fix upstream reaches the ledger as a pin bump plus `finance rebuild`. Resolving the
dependency needs a GitHub read credential, because the upstream repo is private. The ledger is
tied to the upstream module layout; a rename there breaks the import at once, which is the failure
we want rather than a silent drift in totals.
