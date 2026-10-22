# Architecture decision records

Each file here records one decision that is hard to reverse, surprising without context, and the
result of a real tradeoff. Files are named `NNNN-lowercase-slug.md`; a new ADR takes the next
number after the highest one present and follows the bureau ADR template,
`templates/adr.md` in the bureau framework (heading, `Status:`, `Date:`, Context,
Decision, Consequences). ADRs 0001 to 0009 were written in Phase 1. The remaining design decisions
(Key decisions 4, 6, 8 to 13, 15 and 17 to 20 in the design spec) each get their own
next-numbered ADR in the PR of the phase that first builds them. A changed
decision gets a new ADR, and the old one's `Status:` becomes `superseded-by-NNNN`; old bodies are
not rewritten.
