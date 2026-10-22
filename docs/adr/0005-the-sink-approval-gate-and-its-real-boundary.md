# ADR-0005: The sink approval gate and its real boundary
Status: accepted
Date: 2026-10-22

## Context

The ledger talks to the outside world: reminder tickets, Telegram notifications, rate and price
fetches, vendor APIs and mail. Every one of these can leak private data or act on the owner's
behalf, and the ledger is built and run partly by Claude sessions. A config flag alone is too easy
to flip, and an approval that survives a code change would let new code ride an old yes. A
terminal check on the approval command looks like it keeps sessions out, but a session on the same
machine can open a terminal.

## Decision

Every sink is off until the owner approves it in a terminal. One gate, `sinks.gate`, is asked
before every outbound request by the only two places that open a socket: the `http` wrapper, which
every HTTP sink goes through, and the IMAP source. A sink is enabled only when all three hold:
`finance.sinks.<sink>.enabled` is true in the settings; the newest unreversed sink decision for it
is a `sink_approve`; and that decision's `spec_digest` equals the digest of the sink's current
declared request spec. Code that widens a sink changes the digest and needs a fresh approval. The
approval is the `finance sink approve <sink>` command and nothing else: it shows a preview, and the
owner types a line carrying the preview digest. A disabled sink is "skip and leave pending", not a
failure.

The real boundary is stated plainly. The command refuses unless stdin and stdout are terminals,
and that only stops a plain tool call with no terminal. A Claude session can drive a terminal, so
the check cannot tell the owner from a session that chose to open one. What keeps sessions off the
approval (and off `finance confirm bulk`) is process: every first live use of a sink is an
external-action checkpoint cleared with the owner, backed by permission deny rules in
`.claude/settings.json`.

## Consequences

The code guarantees the role and the record: no job, sink, model step or import path reaches the
approval, and every approval is a decision row. It does not guarantee who sat at the terminal.
Every sink widening costs one more approval. Rebuild and every report work with every sink off.
