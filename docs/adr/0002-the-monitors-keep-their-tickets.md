# ADR-0002: The monitors keep their tickets
Status: accepted
Date: 2026-10-22

## Context

Before the ledger, the owner already ran per-institution statement monitors. They file downloaded
statement PDFs into the statement archive and raise tickets in the owner's ticket tracker for
overdrafts, NSF charges, new fee types, late charges and statements that need extraction review.
The ledger sees the same statements, so it could take over both the filing and the alerts. Doing
that would mean two systems racing to file one file, or one system silently retiring a working
one, and duplicate tickets for one overdraft during any overlap.

## Decision

The monitors keep their jobs. They keep filing the statement filename patterns they own and keep
every alert ticket they raise today. The ledger's filer skips those patterns and adopts the
archive as it finds it; `finance status` lists such a file only once it has sat in Downloads for
a day, which means a monitor missed it. The ledger files no alert tickets. Its only tickets are
obligation reminders, and its alerts go to Telegram.

## Consequences

Alerts live in two places: monitor tickets for account events, Telegram for ledger alerts. The
ledger depends on the monitors to file their institutions' statements and only reports when they
fall behind. Retiring a monitor later is its own decision and its own ADR, not a side effect of
the ledger growing.
