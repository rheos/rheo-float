# ADR-0008: Every operation names its connection
Status: accepted
Date: 2026-10-22

## Context

The ledger has two database roles: the owner role, which reads and writes everything, and a
read-only reader role that sees only the `report_*` views, where restricted columns are masked.
A household member gets read access through the `member` role. If an operation picks its
connection at runtime, or by command, it is easy to route a member call through the owner
connection and leak restricted columns, or to give a READ operation the reader connection and have
it fail later for want of a grant.

## Decision

Each entry in the operations `SPECS` table carries `connection` from {`owner`, `reader`}, and
`dispatch` opens the DSN the spec names. A MUTATE or DRAFT operation always names `owner`. A READ
operation names `reader` only when its handler selects from `report_*` views and nothing else;
those are the member-facing reads behind `status`, `owed`, `coverage`, `report`, `lines`,
`holdings`, `lent`, `position` and `upcoming`. Every other READ (the ledger digest, the confirm
queue and bulk preview, sink previews, desired reminders, due notices, wanted mail, the digest
renderer) reads base tables or restricted columns, so it names `owner` and lists no `member` role.
A test asserts the invariant both ways: every spec with role `member` has `connection = reader`,
and every `reader` spec runs green under the reader DSN in the suite.

## Consequences

Masking is structural: a member call cannot reach the owner connection, and no reader operation
fails at runtime for a missing grant. Every new operation has to choose a connection, and a READ
that later needs a base table must move to `owner` and drop `member`, which is a visible change in
review.
