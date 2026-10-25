# ADR-0012: One pairing graph for every "these lines belong together" rule
Status: accepted
Date: 2026-10-25

## Context

Several rules say that two or more ledger lines are one movement and must count once:
a payment between two of the owner's accounts, a currency conversion inside one account,
a reversed payment and its original, a crypto withdrawal and its deposit, and later
peer trades and loan legs. Each could have been its own matcher, run in some order,
each claiming lines as it went. Then the outcome depends on which matcher runs first, a
line two rules want is taken by whichever got there first, and nothing says the owner
should look at it.

The design spec (Key decision 9) puts them in one graph instead. This ADR records that
decision as built in Phase 3b, and the clarifications that building it on a real
archive needed.

## Decision

Every kind of "belongs together" is an edge kind in one table, `pair_edge`. The
candidate stages store edges and never apply them: stage 8 the reversals, stage 9 the
fiat own transfers and same-account conversions, the crypto edge stage the crypto
transfers. Stage 11 then decides, from the stored edge set and the owner's decisions
alone:

- An edge applies on its own only when it is the only edge on each of its ends,
  counting every kind together, and its kind is one that may auto-apply. One tie-break
  exists, for equal card payments in one window (k-th debit to k-th credit by date).
- An edge exists only inside its kind's tolerance. A near match outside it is no
  candidate, so a line with nothing else ends `unresolved`, never `ambiguous`; two or
  more candidates is `ambiguous`. Every transfer-like line ends in exactly one of
  paired, ambiguous or unresolved.
- Reversals use the same table but run first, in both directions, and a candidate
  found only on the reversal's own day is offered, never netted on its own.
- The result is a function of the stored edges and the decisions, so it does not
  depend on the order documents were imported or read.

Clarifications made while building it:

1. **The stage 9 hint is a preference with a fallback.** A start line whose wording
   names a counterpart account looks there first; when nothing qualifies there it
   searches every own account. The other end's hint filters nothing.
2. **A conversion leg is never an own-transfer end.** A line with a conversion edge,
   applied or not, is settled by its wording and its one account, so the in-leg of a
   payment-service currency purchase is never offered to the bank pull that funded it.
3. **Native CAD is never unconverted.** A CAD line is its own CAD value; only a foreign
   line without a stored rate is unconverted, and a conversion with such a leg still
   pairs on its wording and books its spread once a rate exists.
4. **Rates convert only inside fetched coverage.** A line converts at the stored rate of
   the last business day on or before its date only when a fetch covered every day
   between the two, so a date outside fetched coverage is an unconverted line, never a
   rate carried across an unfetched gap.
5. **A crypto fee folds into its transfer only when the figures add up**: within its
   own asset, and only into a primary whose figure holds it.
6. **The owner's pair decisions apply in full or not at all.** A `pair` some edge of
   which no stage would store (lines of one sign, a mixed fiat and crypto pair, a
   labelled crypto line, and the other shapes `pair_decisions` lists) is inert: it
   takes no line and its lines resolve as if it were absent. The decisions are read
   once before stage 8, and the stages build the graph around them. If stage 11 ever
   finds an applied decision whose edge was not stored, it raises naming the decision
   instead of guessing.
7. **A decided pair with legs nets once.** A payment settled by several lines nets the
   target against all of them once, whatever its edges' kinds, and books at most one
   fee line on the outbound side. That fee is keyed to the first edge that names no
   printed fee, so an issuer's printed fee stays named on its conversion edge; when
   every edge names one, the printed fees are the fee lines. The net is floored at
   zero, as a conversion spread is: a favourable residual books nothing, never income.

The pairing defaults (transfer window and tolerance, conversion window and spread
tolerance, reversal window, import overlap tolerance) were tuned at the Phase 3b gate on
a real archive, in the settings declarations only. Same-currency own transfers there
net exactly once a parser prints the fee as its own line, so an absolute CAD floor on
the transfer tolerance only joined small lines of different amounts; it defaults to
0.00, leaving the percent. Legs more than five days apart were mostly rival candidates
that made real pairs ambiguous, so the transfer window is five days. Payment-service
conversion spreads run to just over five percent, so the spread tolerance is six.

## Consequences

Adding a kind (peer trades, loan legs in Phase 7) means adding its candidate stage and
its edges, and, when it may auto-apply, adding it to stage 11's auto kinds; mutual
uniqueness across kinds then holds without new code. A line two rules want is
`ambiguous` and goes to a confirm batch, which costs the owner a decision but never
counts it twice or drops it silently. The tuned tolerances favour `unresolved` over a
wrong pair: a real movement with unequal legs beyond the percent stays visible in the
counts beside every total until the owner pairs it.
