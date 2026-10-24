"""``rate``: stored exchange rates (FR-14, SPEC "Data Models").

One row per published observation: ``rate`` is the price of one ``base`` in ``quote``
on ``rate_date`` (a Bank of Canada ``FXUSDCAD`` observation is base USD, quote CAD),
from ``source``, fetched at ``fetched_at``. ``finance.rate.fetch`` inserts rows and
never rewrites one; rebuild never refetches. Derive stage 4 reads it, so it is an
input state table: a derive never clears it, and the input fingerprint has a part for
it. ``source`` is ``boc`` today; ``kraken`` is the crypto source the spec names for a
later phase. No reader grant: report views grant SELECT in their own revisions.
"""

from alembic import op

revision = "0016_rates"
down_revision = "0015_receipt_ref_digest"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.rate (
        rate_date date NOT NULL,
        base text NOT NULL CHECK (base <> ''),
        quote text NOT NULL CHECK (quote <> ''),
        rate numeric NOT NULL CHECK (rate > 0),
        source text NOT NULL CHECK (source IN ('boc', 'kraken')),
        fetched_at timestamptz NOT NULL,
        PRIMARY KEY (rate_date, base, quote, source),
        CHECK (base <> quote)
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
