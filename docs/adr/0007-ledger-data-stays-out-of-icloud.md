# ADR-0007: Ledger data stays out of iCloud
Status: accepted
Date: 2026-10-22

## Context

The ledger keeps database credentials, manual-entry files, dumps, tax packs, staging, a mail inbox
and logs. On a Mac the natural places for these, Desktop and Documents, are often synced to iCloud.
Syncing them sends private financial data to a third party, and a synced Postgres data directory
can be corrupted by a sync client writing under a running server. The statement archive is a
different case: the ledger adopts it in place and does not own its location.

## Decision

The data directory is `$FINANCE_DATA_DIR`, default `~/Finances/`, which iCloud's Desktop and
Documents sync does not cover. It holds the database credentials, manual files, dumps, packs,
staging, the mail inbox and logs. The Postgres data lives in a Docker named volume, inside
Docker's VM disk, not in any synced folder. The statement archive stays wherever the settings say
it is, because the ledger adopts it in place; whether that folder syncs is the owner's choice and
outside this decision.

## Consequences

Nothing the ledger writes is synced by default, so there is no backup by default either; a backup
is a separate, explicit step. Moving the statement archive later is one setting plus the monitors'
paths. A data directory pointed into a synced folder by hand defeats this decision, so the setting
is documented and the default is safe.
