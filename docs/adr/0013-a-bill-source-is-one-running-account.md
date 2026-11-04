# ADR-0013: A bill source is one running account
Status: accepted
Date: 2026-11-03

## Context

A bill's total usually holds the unpaid balance of the bill before it. Added up, a year of
bills would owe the same charges many times over, so a newer bill has to supersede the older
one. An invoice from a contractor does not work that way: it stands alone, and two of them
are both owed. Nothing on a typed row in `obligations.csv` says which of the two it is, and
the registry has to say it for the payee instead.

## Decision

A payee is a bill source when it has a parsed bill, is a bill issuer (an archive directory
of kind `bill`), or stands for an account of kind `bill_vendor`. Its bills, typed or parsed,
are one running account: they chain by issue date and only the newest is owed. Any other
payee is invoice-style, and each of its bills is owed.

So `bill_vendor` on an account in `accounts.csv` means exactly this: a vendor account billed
as one running balance. A vendor whose invoices stand alone belongs in `payees.csv`, not
`accounts.csv`. Registered as `bill_vendor`, each new invoice would supersede the one before
it and take the older one out of what is owed.

An account with its own payee (`payee_patterns`, under a slug other than the issuer key)
takes its parsed bills from the issuer's payee. A `bill` row naming the issuer would then
count one bill under two payees. When all of the issuer's parsed bills go to one other
payee, the row is read as that payee's. It is not refused: refusing would take a real bill
out of what is owed until the owner edits a file, and reading it can only merge or chain it
with bills of the same vendor.

When no one account can take a typed bill (parsed bills on two accounts, or owed to two other
payees), it stays open beside them and `status` line 9 counts it as `typed_bill_unjoined`.

## Consequences

A typed bill may sit on the issuer's payee until the first parsed bill arrives, then move to
the standing payee. Its id comes from its payee, so a decision naming the old id would need
re-attaching. A row naming the standing payee from the start avoids that.

An unjoined bill may count a bill twice. That stays visible until the owner names the right
payee or period, and the ledger does not guess.
