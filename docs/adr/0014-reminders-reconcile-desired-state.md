# ADR-0014: Reminders reconcile desired state
Status: accepted
Date: 2026-11-05

## Context

Payments, reversals and superseding statements change the state of a bill period.
An outbound queue of commands can replay stale state after a failed or disabled
run. Repeating a create for a settled period can reopen it at the destination.

## Decision

Compute one desired reminder per eligible payee-period from current obligation
facts. The stable source key is finance:<payee-slug>:<period_label>. Reconciliation
compares this desired state with the destination's last acknowledged state. A
settled period is never created again; a later reversal updates its existing item.
An unacknowledged create must be recovered before applying its final state. A
failed or disabled run leaves the difference available for the next run, without a
second queue of stale reminder commands. This is Key decision 6.

The port uses the unpaid threshold, including the minimum for a card, and returns
a done intent for an eligible paid, zero-threshold or superseded period. A done
intent closes an existing reminder; it does not request a new item for a carried
balance. Presumed-paid rows and rows before the reminders floor produce no intent,
as required by AC-29 and the inherited reader contract. That explicit rule takes
precedence over earlier desired-state wording that included presumed-paid done
intents. Disputed rows also produce none, preserving the owed reader's inactive
state policy. An unset floor refuses the read.

The typed payload boundary admits only a registered payee display label, threshold
amount, due date and a period token. It rejects restricted identifier shapes,
free-text periods and extra fields such as memos or balances. The owner repository
reads only those facts and eligibility fields. Amount-bearing title and body fields
remain restricted operation output even though the reviewed sink policy permits
them. The operation is READ on the owner connection for owner and service roles.

Current is the reminder destination under the later product amendment. The port
retains the desired-ticket and policy contracts; a destination adapter owns its
mapping, acknowledgements and outbound gate. This decision introduces no client,
network access, approval or enabled sink.

## Consequences

The destination adapter must retain period identity and acknowledged state across
retries and use updates for reopened settled periods. Desired-state reads can run
while outbound sinks remain off. An invalid policy value refuses the read instead
of emitting a partially sanitized payload.
