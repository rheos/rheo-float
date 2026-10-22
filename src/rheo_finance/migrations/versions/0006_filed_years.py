"""The filed tax years: ``filed_year``.

A derived table. Derivation stage 2 (``manual.stage2.load_filed_years``) deletes and
rewrites it from ``filed_years.csv`` on every derive; the tax packs read it for their
order (oldest unfiled year first). No reader grant: report views grant SELECT in their
own revisions.

The CHECKs repeat the loader's row rules, so a row that slipped past the loader fails
the insert instead of landing.
"""

from alembic import op

revision = "0006_filed_years"
down_revision = "0005_accounts"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.filed_year (
        id uuid PRIMARY KEY,
        year integer NOT NULL UNIQUE CHECK (year BETWEEN 1900 AND 2100),
        filed_on date,
        note text CHECK (note <> ''),
        CHECK (filed_on > make_date(year, 12, 31))
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
