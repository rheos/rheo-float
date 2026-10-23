# ADR-0011: A shared last four is refused only where a last-four match could confuse it
Status: accepted
Date: 2026-10-23

## Context

ADR-0001 and the design spec (Key decision 1, and the assumption on Vancity account matching)
made two identifiers at one institution that end in the same four digits a loader error. The
reused Vancity parser prints only a last four, so two accounts sharing one could not be told
apart, and refusing the pair at load time kept a statement from landing on the wrong account.

A credit union's statements print a full account number per section, and its chequing and share
sections can end in the same four digits. Its parser matches the whole number, so nothing about
those two numbers is ambiguous. The blanket rule refused both, and every statement from that
institution was rejected as `unknown_account`.

## Decision

The loader (`manual/accounts.py`) groups the `number` and `masked_card` identifiers by
institution and last four. A group is refused, every row of every account in it, when any two
rows of different accounts could be confused: a `masked_card`, a masked `number`, a `number` of
four digits or fewer, or the same number written twice. A group made only of distinct full
numbers loads.

A full number is defined positively: kind `number`, runs of ASCII letters and digits joined by
one space, hyphen or slash, more than four digits, and no X used as a mask (a run of X, an X
beside a digit, or X as the only letter). Anything else is not a full number, so an unfamiliar
mask glyph makes the loader refuse more, never less. Whether two rows hold the same number is
decided by one function, `identifier_key`, which the account lookup's whole-number match uses too.

Refusing the whole group, not only the confusable pair, matters: refusing the pair alone could
leave one account in the group holding the last four, and a last-four parser would then put
another account's statement on it.

Every last-four lookup that finds more than one account rejects the document as
`ambiguous_account` and never picks one, so a last four that two full numbers share fails
loudly at the parsers that match on a last four.

This amends ADR-0001's last clause and the spec's Key decision 1 and Vancity matching assumption;
the rest of ADR-0001 stands.

## Consequences

Accounts with distinct full numbers ending alike can be registered, and a parser that matches
the whole number resolves each one. A statement that prints only the shared last four is
rejected as `ambiguous_account` until the owner registers it differently. A row that a mask or an
odd shape keeps from being a full number still refuses its whole group, which is the old outcome.
