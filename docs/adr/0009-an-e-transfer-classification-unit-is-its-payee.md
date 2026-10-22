# ADR-0009: An e-Transfer's classification unit is its payee
Status: accepted
Date: 2026-10-22

## Context

Card and bank lines are classified by merchant key: one rule or decision per merchant covers every
line from it. An e-Transfer has no merchant, only a printed person's or business's name, and a
merchant key must never carry a printed name. Stripping the name leaves every e-Transfer with the
same key, so one decision would classify all of them, rent and a one-off payment to a friend
alike. Sending them to a model is not an option either, since no e-Transfer line reaches a model.

## Decision

The stripped key of an e-Transfer line is scoped to the party or payee the owner registered:

- `party:<slug>:<in|out>` when a party alias matched the line;
- else `etransfer:<payee-slug>:<in|out>` when a `payees.csv` pattern matches the name position;
- else the line-keyed `etransfer:<in|out>:<line_key_id>`.

So a recurring payment to one payee is one unit, and a one-off e-Transfer is its own unit that the
owner classifies with a line-keyed `category` decision. Slugs and labels are words the owner chose,
never the printed name; the pattern that matched stays a restricted column. e-Transfer lines are
classified by rule or decision only.

## Consequences

Unregistered one-off e-Transfers each need their own decision, which is a little more confirm work
for the owner. Adding a `payees.csv` row later groups that payee's lines into one unit on the next
derive, without disturbing line-keyed answers: a line-keyed decision outranks its unit's. The
printed name never appears in a key, a report or a model prompt.
