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
