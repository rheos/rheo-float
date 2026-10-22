"""The ``ledger`` fixture for the derive tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Connection

from rheo_finance.contracts import Role

from .support import Ledger, MakeContext


@pytest.fixture
def ledger(
    owner_conn: Connection,
    rolled_back_context: MakeContext,
    tmp_path: Path,
) -> Ledger:
    return Ledger(owner_conn, rolled_back_context(Role.OWNER), tmp_path)
