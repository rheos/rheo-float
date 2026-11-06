"""Typed restricted alias facts and their non-writing Postgres adapter."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, text

from rheo_finance.context import UnitOfWork
from rheo_finance.db import READER_ROLE
from rheo_finance.operations import RoleRefused
from rheo_finance.storage.schema import account, payee, txn


@dataclass(frozen=True)
class AliasLine:
    description_norm: str | None
    amount: Decimal
    account_kind: str
    institution: str


@dataclass(frozen=True)
class AliasFacts:
    patterns: tuple[str, ...]
    lines: tuple[AliasLine, ...]


class AliasRepository(Protocol):
    def facts(self) -> AliasFacts: ...


class PostgresAliasRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def facts(self) -> AliasFacts:
        # Check the actual connection before reading any restricted columns.
        if self.uow.scalar(text("SELECT current_user")) == READER_ROLE:
            raise RoleRefused("finance.alias.draft refuses the reader connection")
        patterns = tuple(
            pattern
            for row in self.uow.execute(select(payee.c.patterns))
            for pattern in row.patterns
        )
        lines = tuple(
            AliasLine(
                description_norm=row.description_norm,
                amount=row.amount,
                account_kind=row.kind,
                institution=row.institution,
            )
            for row in self.uow.execute(
                select(
                    txn.c.description_norm,
                    txn.c.amount,
                    account.c.kind,
                    account.c.institution,
                ).join(account, account.c.id == txn.c.account_id)
            )
        )
        return AliasFacts(patterns=patterns, lines=lines)
