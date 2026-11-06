# rheo-float

Float is a local-first personal finance ledger. It reads your own bank, card and bill statements into one
Postgres database on your machine, shows what you spend and what you owe, sends due-date
reminders, and watches crypto wallet holdings through CoinStats.

Built as a module for [rheoStream](https://github.com/rheos/rheostream), beside Leads and Current, and runnable on its own
first.

## Principles

- Your data stays on your machine. Statements, exports, keys and the ledger itself are never
  committed; a check runs before every commit to keep it that way.
- Read-only. It never moves money and never holds a bank password.
- Nothing goes out until you approve it. Every outbound connection (reminders, Telegram, rate
  and price fetches, vendor APIs) is off until you turn it on once, from your own terminal.

## Status

In development. Stack: Python 3.12 with uv, Postgres 17 in Docker, a CLI and a launchd job.

## Reminders

Float owns the amount-due Current item for each payee period in the `bills` area.
The stable `finance:<payee-slug>:<period>` key appears in both `source_ref` and the
body's `Period-key:` line. Intake can skip a bill already tracked by Float or
supersede its own duplicate. Float reconciles changes by stored ref, including
settlement and later returned payments. A complete desired-state read closes
previously synced keys it omits; a failed read leaves existing items open. The
Current sink stays off until the owner approves its declared policy and enables it.

Current's preview lists exact pending changes, including priority becoming overdue
at local midnight. Its confirmation code can change at that boundary; refresh the
preview if confirmation refuses an older code. This does not invalidate lasting
sink-spec authorization. Rendered times and relative ages stay outside the digest.
