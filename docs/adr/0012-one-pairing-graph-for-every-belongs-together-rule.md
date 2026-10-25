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

Every kind of "belongs together" is an edge kind in one table, `pair_edge`. Stage 9
stores the fiat own-transfer and same-account conversion candidates without applying
them. The crypto edge stage does the same for crypto transfers. Stage 8 is the exception: it stores
the reversal candidates and itself applies a reversal that is its lines' only one.
Stage 11 never auto-applies a reversal edge; it counts them for uniqueness and applies
one only when the owner's `pair` names it. For every other kind stage 11 decides, from
the stored edge set and the owner's decisions alone:

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
   target against all of them once and books at most one fee line on the outbound side.
   A conversion edge inside the pair keeps its own rule (FR-15(c)) only when it is
   isolated: neither of its ends is an end of another conversion edge of the pair. Its
   printed fee is then the only fee line of its date, else its spread is booked on the
   edge, and the net takes its two legs out at their own CAD values, so neither its
   spread nor the rate of an in-leg posted another day reaches the net. The net is keyed
   to the first edge that is not such a conversion and names no printed fee. A
   conversion that shares an end with another (one leg fanned out to two on the
   account, either way round) has no rule of its own, because a spread measured against
   the whole shared leg would be wrong: the whole pair nets once. The net of a pair with
   legs is floored at zero, as a conversion spread is: a favourable residual books
   nothing, never income. The floor is for pairs with legs only: a two-line pair,
   decided or applied on its own, keeps the signed net of FR-15. Tests pin both, so
   flooring a two-line pair fails one.

The pairing defaults (transfer window and tolerance, conversion window and spread
tolerance, reversal window, import overlap tolerance) were tuned at the Phase 3b gate on
a real archive, in the settings declarations only. The spec's 5.00 CAD absolute
transfer tolerance and 5.0 percent conversion spread (FR-15, FR-15(c), AC-16, AC-85 and
the Settings section) are superseded as initial values; the spec's 7-day transfer
window likewise.

- **Transfer tolerance floor, 5.00 to 0.00 CAD** (the 1.5 percent alone). Same-currency
  own transfers net exactly once a parser prints the fee as its own line: 76 of 87
  applied own transfers had equal legs. Of the 11 unequal ones, 10 were inside only the
  5.00 floor, and 9 of those joined small lines (larger leg under 20.00) whose amounts
  differed by 20 to 100 percent, two movements rather than one with a fee. 222 of 417
  candidate edges existed only by the floor. Every floor from 0.50 up still applied such
  a pair; at 0.00 paired lines rose from 241 to 324 and ambiguous fell from 240 to 131.
- **Transfer window, 7 to 5 days.** Applied pairs more than 5 days apart: 2; rival
  candidates 6 or 7 days apart: 23. At 5 days paired lines were 348 (324 at 7) and
  ambiguous 103 (131 at 7).
- **Conversion spread, 5.0 to 6.0 percent.** The real payment-service spreads ran from
  3.0 to just over 5 percent; 6.0 pairs all 44 conversions with no worded leg left
  unresolved (2 at 5.0) and no rival edge (10.0 added 6).
- The conversion window (1 day), reversal window (35 days), transfer percent (1.5) and
  import overlap tolerance (3 days) keep their values: the counts did not move or got
  worse.

AC-16 and AC-85 are proven at the spec's worked values in the rule tests (the derive
test ledger pins them as `RULE_SETTINGS`) and at the shipped values in
`test_tuned_defaults`.

## Consequences

Adding a kind (peer trades, loan legs in Phase 7) means adding its candidate stage and
its edges, and, when it may auto-apply, adding it to stage 11's auto kinds; mutual
uniqueness across kinds then holds without new code. A line two rules want is
`ambiguous` and goes to a confirm batch, which costs the owner a decision but never
counts it twice or drops it silently. The tuned tolerances favour `unresolved` over a
wrong pair: a real movement with unequal legs beyond the percent stays visible in the
counts beside every total until the owner pairs it.
