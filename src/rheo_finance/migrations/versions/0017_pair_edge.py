"""``pair_edge``: the one candidate graph of lines that belong together (Key decision
9, SPEC "Data Models").

One row per candidate edge, applied or not, rebuilt by every derive (derived, stage 8
for ``reversal`` edges, stage 9 and the peer-money stages for the other kinds). ``id``
is derived from the kind and the endpoints. ``a_txn_id`` and ``b_txn_id`` are the two
lines (for a loan edge ``a_txn_id`` is the loan's out withdrawal, null for a manual loan
with no out line; ``b_txn_id`` is null for ``own_one_sided``), ``loan_key`` a loan
edge's loan, ``b_part_id`` an allocation part, ``b_account_id`` the registered account
of an ``own_one_sided`` destination, ``group_id`` a group of edges decided together,
``applied`` whether the edge is in force, ``basis_ref`` the decision that applied or
refused it, ``fee_txn_id`` the derived fee line of a pair and ``spread_cad`` a
conversion's spread in CAD (restricted).

A ``reversal`` edge joins two distinct lines: ``a_txn_id`` the reversal line,
``b_txn_id`` the earlier line it undoes. No reader grant: report views grant SELECT in
their own revisions.
"""

from alembic import op

revision = "0017_pair_edge"
down_revision = "0016_rates"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.pair_edge (
        id uuid PRIMARY KEY,
        kind text NOT NULL CHECK (kind IN ('own_transfer', 'own_one_sided',
            'conversion', 'reversal', 'trade', 'loan_out', 'loan_repay',
            'loan_partial', 'loan_proposal')),
        a_txn_id uuid REFERENCES finance.txn (id),
        loan_key text,
        b_txn_id uuid REFERENCES finance.txn (id),
        b_part_id uuid,
        b_account_id uuid REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        group_id uuid,
        applied boolean NOT NULL DEFAULT false,
        basis_ref text,
        fee_txn_id uuid REFERENCES finance.txn (id),
        spread_cad numeric(20, 2),
        CHECK (kind <> 'reversal' OR (a_txn_id IS NOT NULL
            AND b_txn_id IS NOT NULL AND a_txn_id <> b_txn_id))
    )
    """,
    "CREATE INDEX pair_edge_a_txn ON finance.pair_edge (a_txn_id)",
    "CREATE INDEX pair_edge_b_txn ON finance.pair_edge (b_txn_id)",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
