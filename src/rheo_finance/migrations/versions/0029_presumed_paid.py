"""Presumed paid, the reminders floor and the paying accounts (FR-19, FR-26, FR-67;
SPEC Derivation stage 14 "Presumed paid" and "The paying account of a payee"; AC-10,
AC-82).

``obligation.presumed_paid_decision_id`` is the unreversed ``presumed_paid`` decision
naming the obligation: its basis in ``owed --trace`` while its status is
``presumed_paid``, and kept on record once a payment applies and the status reverts.
``obligation.before_floor`` is true when it is due before
``finance.reminders.min_due_date``: ``owed`` marks it "before reminders floor", and it
gets no ticket or nudge. ``payee.paid_from`` holds the ids of the accounts
``payees.csv`` names in ``paid_from``, the whole set of the payee's paying accounts
when set.
"""

from alembic import op

revision = "0029_presumed_paid"
down_revision = "0028_unsettled_decision"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    ALTER TABLE finance.obligation
        ADD COLUMN presumed_paid_decision_id uuid REFERENCES finance.decision (id),
        ADD COLUMN before_floor boolean NOT NULL DEFAULT false
    """,
    """
    ALTER TABLE finance.payee
        ADD COLUMN paid_from uuid[] NOT NULL DEFAULT '{}'
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
