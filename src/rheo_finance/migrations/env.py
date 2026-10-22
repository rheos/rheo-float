"""The finance Alembic environment: one chain, its version table in schema ``finance``.

An orchestrator passes its own connection in ``config.attributes["connection"]``
(``finance db migrate`` does the same); otherwise this connects with the owner DSN
from ``$FINANCE_DATA_DIR/db.env``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, create_engine
from sqlalchemy.pool import NullPool

from rheo_finance.db import SCHEMA, VERSION_TABLE, owner_url


def run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


passed = context.config.attributes.get("connection")
if passed is not None:
    run(passed)
else:
    engine = create_engine(owner_url(), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            run(connection)
    finally:
        engine.dispose()
