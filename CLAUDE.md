# rheo-float (Float)

A local-first personal finance ledger. The product and repo are Float / `rheo-float`; inside the
code the distribution is `rheo-finance`, the package is `rheo_finance` (under `src/`), the module id
is `finance` and the console script is `finance`. Keep those internal names.

This repo is public. Nothing personal goes in it: no names, account names or numbers, institutions
tied to a person, amounts, balances, addresses or real file contents, in code, fixtures, docs,
commit messages or PR text.

## Stack

- Python `>=3.12,<3.13` with uv. Lint with ruff, type-check with mypy strict.
- Postgres 17 in Docker on 127.0.0.1:55440. SQLAlchemy 2.x Core with psycopg 3, one Alembic chain.
  Every table lives in schema `finance`.
- Pydantic v2 for operation inputs and outputs. stdlib argparse for the CLI.
- Runtime dependencies are only sqlalchemy, psycopg, alembic, pydantic and `doc-extraction-api`
  (a git dependency pinned to a commit; that repo is private, so resolving it needs a GitHub read
  credential). Adding another one needs a stated reason.
- Money is `Decimal`, never float. CAD amounts are `numeric(20,2)`, rounded half-even per line.
- `rheo_finance/cli/main.py` never imports sqlalchemy or `rheo_finance.storage`.

## Data lives outside the repo

All real data lives in `$FINANCE_DATA_DIR`, default `~/Finances/`, kept outside iCloud: settings,
statements, mail, dumps, staging, packs, logs and the database credentials. Never commit data:
no statement, export, dump, key, account number, balance or TXID.

**Real-files rule.** You may read real statements locally to learn a layout. Nothing real is ever
committed or printed into a transcript: report counts and pass/fail only. Fixtures are synthetic
layout text with fake values (digit runs start with `999`).

## Running the tests

```sh
uv sync
uv run ruff check && uv run mypy --strict src tests && uv run pytest -q
```

That full line is the phase-gate check. Database tests run against the local Postgres container,
database `finance_test`, each test inside a rolled-back transaction, with a fake clock. Tests make
no network calls except to local stub servers.

## Sinks and the approval boundary

Every sink (every outbound connection: reminders, notifications, rate and price fetches, vendor
APIs, mail) is off until the owner runs `finance sink approve <sink>` in a terminal and sets
`finance.sinks.<sink>.enabled = true` in their settings. `finance confirm bulk` is the other
interactive-only command. Never run either from a session, and never set a sink's `enabled` key to
true outside a test fixture.

The real boundary: both commands refuse unless stdin and stdout are terminals, and that check only
stops a plain Bash-tool call. A Claude session can open a terminal (terminal tools, `script`,
`expect`, a pseudo-terminal), so the check cannot tell the owner from a session that chose to open
one. What keeps sessions off these commands is the process: every first live use of a sink is an
external-action checkpoint cleared with the owner, plus the permission deny rules in
`.claude/settings.json`. Those rules cover the `finance`, `uv run finance`, `.venv/bin/finance`,
`python -m rheo_finance.cli.main` and `uv run python -m rheo_finance.cli.main` forms; they are
prefix matches, so they back up the process and do not replace it. The code guarantees the role and the record (no job, sink, model step or
import path reaches the approval, and every approval is a decision row). It does not guarantee who
sat at the terminal.
