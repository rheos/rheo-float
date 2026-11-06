"""Output-sensitive privacy checks tied to exact typed reporting expressions."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Boolean, ColumnElement, Numeric, Table, case, cast, func, select
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import ClauseElement, ColumnClause, TextClause
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import (
    CompoundSelect,
    SelectBase,
    SelectStatementGrouping,
    Subquery,
)

from rheo_finance.reports.views import VIEW_SELECTS as REPORTS
from rheo_finance.sinks.views import VIEW_SELECTS as SINKS
from rheo_finance.sinks.views import view_sql
from rheo_finance.storage.schema import payee, txn
from rheo_finance.storage.tiers import tier_of
from rheo_finance.tier import Tier

VIEWS = {**REPORTS, **SINKS}
Source = tuple[str, str]
Money = dict[str, dict[str, Source]]
MONEY: Money = {
    "report_txn": {"amount": ("txn", "amount"), "cad_amount": ("txn", "cad_amount")},
    "report_crypto_movement": {
        "amount": ("txn", "amount"),
        "cad_amount": ("txn", "cad_amount"),
    },
    "report_open_item": {
        "amount": ("open_item", "amount"),
        "cad_amount": ("open_item", "cad_amount"),
    },
    **{
        view: {
            name: ("obligation", name)
            for name in (
                "amount_due",
                "minimum_due",
                "threshold_due",
                "paid",
                "remaining",
                "threshold_remaining",
                "manual_mismatch",
                "payments_mismatch",
            )
        }
        for view in ("report_owed", "report_owed_trace")
    },
}
MONEY["report_owed_trace"].update(
    {
        "applied_amount": ("payment_application", "amount"),
        "payment_amount": ("payment", "amount"),
    }
)


def output_arms(statement: SelectBase) -> Iterator[list[ColumnElement[object]]]:
    """All emitted arms, unwrapping grouped compounds instead of only arm one."""
    if isinstance(statement, SelectStatementGrouping):
        yield from output_arms(statement.element)
    elif isinstance(statement, CompoundSelect):
        for arm in statement.selects:
            yield from output_arms(arm)
    else:
        yield list(statement.selected_columns)


def restricted_lineage(expression: ClauseElement) -> set[Source]:
    """Resolve proxy columns, union/subquery labels and nested expressions to bases."""
    found: set[Source] = set()
    seen: set[int] = set()

    def visit(element: ClauseElement) -> None:
        if id(element) in seen:
            return
        seen.add(id(element))
        for node in visitors.iterate(element):
            if not isinstance(node, ColumnClause):
                continue
            table = node.table
            if isinstance(table, Subquery):
                # A subquery column's proxy set can retain only the first union
                # arm. Resolve its position through every emitted inner arm.
                position = list(table.c.keys()).index(node.key)
                for arm in output_arms(table.element):
                    visit(arm[position])
            if isinstance(table, Table) and table.schema == "finance":
                source = (table.name, node.name)
                if tier_of(*source) is Tier.RESTRICTED:
                    found.add(source)
            for proxy in node.proxy_set:
                if proxy is not node:
                    visit(proxy)

    visit(expression)
    return found


def output_problems() -> list[str]:
    problems: list[str] = []
    boolean = view_sql(
        select((func.cardinality(payee.c.patterns) == 0).label("payments_unmatchable"))
    )
    crypto_money = {
        "amount": func.sum(txn.c.amount).label("amount"),
        "cad_amount": case(
            (func.count(txn.c.cad_amount) == func.count(), func.sum(txn.c.cad_amount)),
            else_=cast(None, txn.c.cad_amount.type),
        ).label("cad_amount"),
    }
    for view, build in VIEWS.items():
        statement = build()
        count_stars = {
            id(argument)
            for function in visitors.iterate(statement)
            if isinstance(function, FunctionElement)
            and getattr(function, "name", None) == "count"
            for argument in function.clauses
            if isinstance(argument, ColumnClause)
            and argument.is_literal
            and argument.name == "*"
        }
        for node in visitors.iterate(statement):
            if isinstance(node, TextClause) or (
                isinstance(node, ColumnClause)
                and node.is_literal
                and node.name != "1"
                and id(node) not in count_stars
            ):
                problems.append(f"{view}: untraceable textual SQL expression")
        names = [str(column.key) for column in statement.selected_columns]
        for arm in output_arms(statement):
            # Compound output names come from arm one, regardless of later labels.
            columns = {
                name: column.label(name)
                for name, column in zip(names, arm, strict=True)
            }
            for name, source in MONEY.get(view, {}).items():
                column = columns.get(name)
                if column is None or not isinstance(column.type, Numeric):
                    problems.append(f"{view}.{name}: missing numeric monetary output")
                elif restricted_lineage(column) != {source}:
                    problems.append(f"{view}.{name}: stale monetary source exception")
                if view == "report_crypto_movement" and column is not None:
                    if view_sql(select(column)) != view_sql(select(crypto_money[name])):
                        problems.append(
                            f"{view}.{name}: not the bounded numeric aggregate"
                        )
            for name, column in columns.items():
                sources = restricted_lineage(column)
                if (
                    view in {"report_owed", "report_owed_trace"}
                    and name == "payments_unmatchable"
                ):
                    if (
                        not isinstance(column.type, Boolean)
                        or view_sql(select(column)) != boolean
                        or sources != {("payee", "patterns")}
                    ):
                        problems.append(
                            f"{view}.{name}: not the exact emptiness boolean"
                        )
                    continue
                allowed = MONEY.get(view, {}).get(name)
                if sources != ({allowed} if allowed else set()):
                    problems.append(f"{view}.{name}: forbidden restricted source")
    return problems
