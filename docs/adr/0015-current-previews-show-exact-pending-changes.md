# ADR-0015: Current previews show exact pending changes
Status: accepted
Date: 2026-11-05

## Context

Current priority becomes high after a bill's local due date. An exact pending list
must then include a priority-only change even when ledger facts and the last
acknowledged state are unchanged. That membership cannot also be time independent.
This affects the public preview and confirmation contract across a date boundary.

## Decision

Current previews list the exact changes needed now against acknowledged desired
state, including overdue priority. Only Current permits that clock-driven list
membership to change its canonical typed-list digest and confirmation code.

The shared preview and approval interfaces, canonical hashing and other sinks'
clock-free membership remain unchanged. Wake, timestamps and relative ages stay
outside the typed fields and hash. Data changes still change the applicable code.

## Consequences

A code captured before an overdue transition can be refused at confirmation. A
fresh preview supplies the current code. The request spec and its digest do not
change at that transition, so lasting sink-spec authorization remains valid.
Runtime reconciliation and its per-request gate remain unchanged.
