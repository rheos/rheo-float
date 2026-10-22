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

## Code rules the guard tests enforce

- `rheo_finance.render.emit` (and `emit_json`) is the only way text reaches a stream; no other
  module prints or writes to stdout or stderr. U+2014 never gets through it.
- A module that calls `operations.register` adds its dotted name to `catalogue.MODULES` in the
  same change, so the invariant tests load every operation.
- `rheo_finance.parsers.docextract_adapter` is the only module that imports `docextract`, and
  only the names its docstring lists (never `docextract.extractor` or `monitor`). A parser takes
  extracted text, never a path. A module that calls `parsers.registry.register_parser` is
  listed in `parsers.registry.PARSER_MODULES`.
- Migration revisions are frozen. Each revision file is pinned by the sha256 of its bytes in
  `tests/guards/test_frozen_views.py`; a view's SQL lives in its revision's `VIEWS` dict and
  must match what the code compiles. Change a rule by adding a revision, not by editing one.
  Re-pin a hash only for a revision no database has run yet (an unmerged phase branch).

## Data lives outside the repo

All real data lives in `$FINANCE_DATA_DIR`, default `~/Finances/`, kept outside iCloud: settings,
statements, mail, dumps, staging, packs, logs and the database credentials. Never commit data:
no statement, export, dump, key, account number, balance or TXID.

**Real-files rule.** You may read real statements locally to learn a layout. Nothing real is ever
committed or printed into a transcript: report counts and pass/fail only. Fixtures are synthetic
layout text with fake values (digit runs start with `999`).

## Repo check and the pre-commit hook

`scripts/repo_check.py` fails when a tracked or staged file looks like private data (FR-45).
Enable the hook once per clone (worktrees share the setting):

```sh
git config core.hooksPath .githooks
```

The hook runs the check in `--staged` mode before every commit. Run it yourself over the whole
tree with `uv run python scripts/repo_check.py --tracked`. It reads content from the git index.
It blocks statement-type files (pdf, csv, eml, dump, sql, qfx, ofx, jsonl, heic, jpg, jpeg, png,
xlsx, xls, zip, json outside `src/rheo_finance/reference/`) outside `tests/fixtures/synthetic/`,
any fixture not listed in that directory's `MANIFEST.toml`, any other file under
`tests/fixtures/` (every fixture lives in `synthetic/`), and, in data-shaped files, digit runs
of 8 or more that do not start with `999` (dates as `YYYYMMDD` and 13-digit millisecond epochs
pass; fractional runs count too) and Luhn-valid card numbers, contiguous or in card groups with
spaces or hyphens. Fixtures are text: a data file with NUL bytes that is not UTF-16 or UTF-32
fails as unscannable. `.claude/*.json` is not scanned. Never bypass it with `--no-verify`; fix
the file instead.

It also refuses any path under a `.bureau/` directory except `.bureau/regression/run.sh` and `.bureau/regression/NN-<slug>.md` fixtures, so Bureau run data never gets tracked.

## Running the tests

```sh
uv sync
uv run finance db up && uv run finance db init   # once per machine; both are idempotent
uv run ruff check && uv run mypy --strict src tests scripts && uv run pytest -q
```

That full line is the phase-gate check. `.bureau/regression/run.sh` runs it, then the repo check
in `--tracked` mode, and prints counts only; the Bureau's integration gate calls that script.
Database tests run against the local Postgres container, database `finance_test`, each test inside
a rolled-back transaction, with a fake clock. Tests make no network calls except to local stub
servers.

`finance db init` sets up both databases (`finance` and `finance_test`); `--database NAME`
(repeatable) limits it to the ones named, so a checkout that only runs the suite can pass
`--database finance_test` and leave the live ledger alone.

`docker-compose.yml` pins the compose project (`rheo-finance`), the container
(`rheo-finance-postgres`) and the volume (`finance-pgdata`), so every checkout, the primary
clone and any worktree alike, shares one container and one database on 127.0.0.1:55440.

## Sinks and the approval boundary

Every sink (every outbound connection: reminders, notifications, rate and price fetches, vendor
APIs, mail) is off until the owner runs `finance sink approve <sink>` in a terminal and sets
`finance.sinks.<sink>.enabled = true` in their settings. `finance confirm bulk` is the other
interactive-only command; it arrives in Phase 5, and only `finance sink approve` exists today.
Never run either from a session, and never set a sink's `enabled` key to true outside a test
fixture.

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
