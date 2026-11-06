"""The status, coverage and document report views, as SQLAlchemy selects that
migration 0014 compiles (FR-48, FR-51, SPEC Data Models > Report views).

Each view shows only codes, counts, dates, labels and registered paths: never an
account identifier, a description, a merchant key, a counterparty or the path of a
skipped file (a skipped file never reaches ``document``, and ``archive_skip`` holds
counts by top-level directory).

``report_status`` holds the facts ``finance.status.get`` builds its lines from, one
row per fact as ``(fact, subject, count, at)``:

- ``schema``: the revision the database is at, as ``subject``;
- ``last_import``: per top-level archive directory, how many documents it holds and
  the newest ``imported_at`` (a nested issuer's sub-directory is free text, so it is
  folded into its top-level directory);
- ``document_status``: documents per status;
- ``source_discrepancy``: discrepancies per kind;
- ``archive_skip_identifier``: per directory, the files the walk skipped because their
  path carries an identifier (only that reason: the other skip reasons are not fixed
  by a rename);
- ``account_no_data``: one row per account with no coverage month, by label, leaving
  out the built-in "Paid outside recorded accounts", which has no source by design;
- ``missing_months``: per account with any, its ``missing`` coverage months, by label;
- ``duplicate_bill_month``: per payee with any, its bills past the first of a due
  month (a period label with a ``-<n>`` suffix), by label;
- ``typed_bill_unjoined``: per payee with any, its typed bills that no account takes
  beside its parsed bills (``derive.stage14``), by label;
- ``arrangement_unsettled``: one row per arrangement, by slug, whose instalments are
  all paid but which the derive kept active because finishing it would not settle
  (``derive.stage15_apply.mark_unsettled``);
- ``arrangement_unsettled_by``: per such arrangement, each ``payment_apply`` decision
  that moved a payment off it, as ``<slug> finance.decision:<id>`` (migration 0028);
- ``invoice_decision_superseded``: per decision kind, the invoice decisions a matching
  payment took over from (``derive.evidence``);
- ``invoice_decision_refused``: per decision kind, the invoice decisions stage 3 could
  not apply for the invoice's currency (``derive.evidence``).
- ``decision_orphan``: the number of distinct orphaned decisions, including those
  without a replacement candidate, for status line 5.

``report_coverage`` is the matrix: account slug and label, month, state and the reason
as words (``running_balance_break`` reads ``running balance break``).

``report_document`` is the registry: path, kind, status, period, official and
extraction. A withheld document shows ``(withheld)`` for its path.

Migration 0014 holds what these selects compiled to as frozen SQL; the frozen-views
guard compares them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Final

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    DateTime,
    Select,
    Text,
    case,
    cast,
    column,
    func,
    literal,
    null,
    select,
    table,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by, array
from sqlalchemy.sql.elements import Label

from rheo_finance.derive.coverage import State
from rheo_finance.documents.registry import SkipReason
from rheo_finance.manual.accounts import INTERNAL_ACCOUNT
from rheo_finance.reports.line_views import (
    report_crypto_movement_select,
    report_queue_counts_select,
    report_txn_select,
)
from rheo_finance.reports.owed_views import report_owed_select, report_owed_trace_select
from rheo_finance.storage.schema import (
    account,
    archive_skip,
    coverage,
    decision,
    decision_orphan,
    document,
    document_line,
    obligation,
    open_item,
    pair_edge,
    payee,
    payment_application,
    source_discrepancy,
    txn,
    txn_source,
)

# The fact names of ``report_status``.
SCHEMA: Final = "schema"
LAST_IMPORT: Final = "last_import"
DOCUMENT_STATUS: Final = "document_status"
SOURCE_DISCREPANCY: Final = "source_discrepancy"
ARCHIVE_SKIP_IDENTIFIER: Final = "archive_skip_identifier"
ACCOUNT_NO_DATA: Final = "account_no_data"
MISSING_MONTHS: Final = "missing_months"
DUPLICATE_BILL_MONTH: Final = "duplicate_bill_month"
MANUAL_MISMATCH: Final = "manual_mismatch"
STATEMENT_CONFLICT: Final = "statement_conflict"
TYPED_BILL_UNJOINED: Final = "typed_bill_unjoined"
ARRANGEMENT_UNSETTLED: Final = "arrangement_unsettled"
ARRANGEMENT_UNSETTLED_BY: Final = "arrangement_unsettled_by"
INVOICE_DECISION_SUPERSEDED: Final = "invoice_decision_superseded"
INVOICE_DECISION_REFUSED: Final = "invoice_decision_refused"
DECISION_ORPHAN: Final = "decision_orphan"
BILL_GAP: Final = "bill_gap"

IDENTIFIER_IN_PATH: Final[SkipReason] = "identifier_in_path"
MISSING: Final[State] = "missing"
# How a decision is named (``storage.refs.format_ref``), before its id.
DECISION_REF: Final = "finance.decision"
# ``bill-YYYY-MM-<n>``, not the plain ``bill-YYYY-MM``.
SUFFIXED_BILL: Final = "^bill-[0-9]{4}-[0-9]{2}-"

# Alembic's version table: not in the shared MetaData, which mirrors ledger tables.
_version = table("alembic_version_finance", column("version_num"), schema="finance")


def _fact(name: str) -> Label[str]:
    return literal(name, Text).label("fact")


def _one() -> Label[int]:
    return cast(literal(1), BigInteger).label("count")


def _no_time() -> Label[dt.datetime]:
    return cast(null(), DateTime(timezone=True)).label("at")


def _obligations_per_payee(
    name: str, *where: ColumnElement[bool]
) -> Select[tuple[object, ...]]:
    return (
        select(
            _fact(name),
            payee.c.display_label.label("subject"),
            func.count().label("count"),
            _no_time(),
        )
        .select_from(obligation.join(payee, obligation.c.payee_id == payee.c.id))
        .where(*where)
        .group_by(payee.c.id, payee.c.display_label)
    )


def report_status_select() -> Select[tuple[object, ...]]:
    top = func.split_part(document.c.issuer, "/", 1)
    with_data = select(coverage.c.account_id).where(
        coverage.c.account_id == account.c.id
    )
    branches = [
        select(
            _fact(SCHEMA), _version.c.version_num.label("subject"), _one(), _no_time()
        ),
        select(
            _fact(DECISION_ORPHAN),
            cast(null(), Text).label("subject"),
            func.count(decision_orphan.c.decision_id.distinct()).label("count"),
            _no_time(),
        ),
        select(
            _fact(BILL_GAP),
            document.c.issuer.label("subject"),
            func.count().label("count"),
            _no_time(),
        )
        .where(*bill_gap_conditions())
        .group_by(document.c.issuer),
        select(
            _fact(LAST_IMPORT),
            top.label("subject"),
            func.count().label("count"),
            func.max(document.c.imported_at).label("at"),
        ).group_by(top),
        select(
            _fact(DOCUMENT_STATUS),
            document.c.status.label("subject"),
            func.count().label("count"),
            _no_time(),
        ).group_by(document.c.status),
        select(
            _fact(SOURCE_DISCREPANCY),
            source_discrepancy.c.kind.label("subject"),
            func.count().label("count"),
            _no_time(),
        ).group_by(source_discrepancy.c.kind),
        select(
            _fact(ARCHIVE_SKIP_IDENTIFIER),
            archive_skip.c.directory.label("subject"),
            cast(archive_skip.c.count, BigInteger).label("count"),
            _no_time(),
        ).where(archive_skip.c.reason == IDENTIFIER_IN_PATH),
        select(
            _fact(ACCOUNT_NO_DATA),
            account.c.label.label("subject"),
            _one(),
            _no_time(),
        ).where(~with_data.exists(), account.c.slug != INTERNAL_ACCOUNT.slug),
        select(
            _fact(MISSING_MONTHS),
            account.c.label.label("subject"),
            func.count().label("count"),
            _no_time(),
        )
        .select_from(coverage.join(account, coverage.c.account_id == account.c.id))
        .where(coverage.c.state == MISSING)
        .group_by(account.c.id, account.c.label),
        _obligations_per_payee(
            DUPLICATE_BILL_MONTH,
            obligation.c.kind == "bill",
            obligation.c.period_label.regexp_match(SUFFIXED_BILL),
        ),
        _obligations_per_payee(
            MANUAL_MISMATCH, obligation.c.manual_mismatch.is_not(None)
        ),
        _obligations_per_payee(
            STATEMENT_CONFLICT, obligation.c.statement_conflict.is_(True)
        ),
        _obligations_per_payee(TYPED_BILL_UNJOINED, obligation.c.unjoined.is_(True)),
        select(
            _fact(ARRANGEMENT_UNSETTLED),
            obligation.c.arrangement_ref.label("subject"),
            _one(),
            _no_time(),
        )
        .where(obligation.c.arrangement_unsettled.is_(True))
        .group_by(obligation.c.arrangement_ref),
        select(
            _fact(ARRANGEMENT_UNSETTLED_BY),
            func.concat(
                obligation.c.arrangement_ref,
                literal(f" {DECISION_REF}:", Text),
                obligation.c.unsettled_decision_id,
            ).label("subject"),
            _one(),
            _no_time(),
        )
        .where(obligation.c.unsettled_decision_id.is_not(None))
        .group_by(obligation.c.arrangement_ref, obligation.c.unsettled_decision_id),
        select(
            _fact(INVOICE_DECISION_SUPERSEDED),
            decision.c.kind.label("subject"),
            func.count().label("count"),
            _no_time(),
        )
        .select_from(
            txn_source.join(
                decision, txn_source.c.overrides_decision_id == decision.c.id
            )
        )
        .group_by(decision.c.kind),
        select(
            _fact(INVOICE_DECISION_REFUSED),
            decision.c.kind.label("subject"),
            func.count().label("count"),
            _no_time(),
        )
        .select_from(
            document_line.join(
                decision, document_line.c.refused_decision_id == decision.c.id
            )
        )
        .group_by(decision.c.kind),
    ]
    # A view's body is one select; the union is wrapped as its only source.
    unioned = union_all(*branches).subquery("facts")
    return select(unioned.c.fact, unioned.c.subject, unioned.c.count, unioned.c.at)


def report_coverage_select() -> Select[tuple[object, ...]]:
    return select(
        account.c.slug.label("account"),
        account.c.label.label("account_label"),
        coverage.c.month,
        coverage.c.state,
        func.replace(coverage.c.reason_code, "_", " ").label("reason"),
    ).select_from(coverage.join(account, coverage.c.account_id == account.c.id))


def source_named() -> ColumnElement[bool]:
    return (
        select(obligation.c.id)
        .where(obligation.c.primary_source == document.c.path)
        .exists()
    )


def bill_gap_conditions() -> tuple[ColumnElement[bool], ...]:
    # Issuer-directory policy is applied by both readers to these candidate counts.
    return (
        document.c.kind.in_(["bill", "image"]),
        document.c.status.in_(["needs_manual_entry", "unparsed"]),
        ~source_named(),
    )


def report_document_select() -> Select[tuple[object, ...]]:
    return select(
        document.c.path,
        document.c.kind,
        document.c.status,
        document.c.period_start,
        document.c.period_end,
        document.c.official,
        document.c.extraction,
        document.c.issuer,
        source_named().label("has_obligation"),
    )


def report_open_item_select() -> Select[tuple[object, ...]]:
    named = account.alias("reason_account")
    orphan_count = (
        select(func.count(decision_orphan.c.candidate_txn_id.distinct()))
        .select_from(decision_orphan)
        .where(
            decision_orphan.c.decision_id == open_item.c.decision_id,
            decision_orphan.c.candidate_txn_id.is_not(None),
        )
        .correlate(open_item)
        .scalar_subquery()
    )
    candidate_ref = func.concat("finance.txn:", decision_orphan.c.candidate_txn_id)
    candidate_refs = (
        select(
            func.coalesce(
                func.array_agg(
                    aggregate_order_by(candidate_ref.distinct(), candidate_ref)
                ),
                array([], type_=Text),
            )
        )
        .select_from(decision_orphan)
        .where(
            decision_orphan.c.decision_id == open_item.c.decision_id,
            decision_orphan.c.candidate_txn_id.is_not(None),
        )
        .correlate(open_item)
        .scalar_subquery()
    )
    edge_count = (
        select(func.count())
        .select_from(pair_edge)
        .where(
            (pair_edge.c.a_txn_id == open_item.c.txn_id)
            | (pair_edge.c.b_txn_id == open_item.c.txn_id),
            pair_edge.c.applied.is_(False),
        )
        .correlate(open_item)
        .scalar_subquery()
    )
    count = case(
        (open_item.c.kind == "orphaned_decision", orphan_count),
        (open_item.c.kind == "ambiguous_transfer", edge_count),
        else_=0,
    )
    overpaid_periods = (
        select(
            func.string_agg(
                obligation.c.period_label,
                aggregate_order_by(literal(", ", Text), obligation.c.period_label),
            )
        )
        .select_from(
            payment_application.join(
                obligation, payment_application.c.obligation_id == obligation.c.id
            )
        )
        .where(payment_application.c.payment_id == open_item.c.payment_id)
        .correlate(open_item)
        .scalar_subquery()
    )
    label = func.coalesce(named.c.label, "Counterpart")
    payee_label = func.coalesce(payee.c.display_label, "payee")
    reason = case(
        (
            open_item.c.reason_code == "counterpart_no_data",
            func.concat(label, " side not loaded: no ", label, " export filed"),
        ),
        (
            open_item.c.reason_code == "counterpart_not_loaded",
            case(
                (
                    open_item.c.reason_date.is_not(None),
                    func.concat(
                        label,
                        " side not loaded: no ",
                        label,
                        " statement before ",
                        cast(open_item.c.reason_date, Text),
                    ),
                ),
                else_=func.concat(
                    label,
                    " side not loaded: no ",
                    label,
                    " data for ",
                    func.to_char(txn.c.posting_date, "YYYY-MM"),
                ),
            ),
        ),
        (open_item.c.reason_code == "candidates", func.concat(count, " candidates")),
        (open_item.c.reason_code == "no_match", "no matching counterpart"),
        (open_item.c.reason_code == "no_rate", "no exchange rate"),
        (open_item.c.reason_code == "no_source", "no usable source"),
        (open_item.c.reason_code == "unmatched", "invoice has no matching payment"),
        (
            open_item.c.reason_code == "orphan_candidate",
            func.concat("decision orphaned: ", count, " replacement candidates"),
        ),
        (
            open_item.c.reason_code == "orphan_no_candidate",
            "decision orphaned: no replacement candidate",
        ),
        (
            open_item.c.reason_code == "excess",
            func.concat(
                "credit exceeds obligation for ",
                payee_label,
                case(
                    (
                        overpaid_periods.is_not(None),
                        func.concat(" (", overpaid_periods, ")"),
                    ),
                    else_="",
                ),
            ),
        ),
        (
            open_item.c.reason_code == "no_target",
            case(
                (
                    open_item.c.reason_date.is_not(None),
                    func.concat(
                        "no target for ",
                        payee_label,
                        " issued by ",
                        cast(open_item.c.reason_date, Text),
                    ),
                ),
                else_=func.concat("credit not applied to ", payee_label),
            ),
        ),
        else_=func.replace(open_item.c.reason_code, "_", " "),
    )
    return select(
        open_item.c.subject_ref.label("line_ref"),
        open_item.c.tax_year,
        open_item.c.kind,
        open_item.c.occurred_on,
        account.c.label.label("account_label"),
        open_item.c.direction,
        open_item.c.amount,
        open_item.c.currency,
        open_item.c.cad_amount,
        open_item.c.state,
        reason.label("reason"),
        open_item.c.category,
        open_item.c.tag,
        count.label("candidate_count"),
        cast(candidate_refs, ARRAY(Text)).label("candidate_refs"),
        func.nullif(
            func.concat("finance.payment:", open_item.c.payment_id), "finance.payment:"
        ).label("payment_ref"),
        payee.c.slug.label("payee_slug"),
        payee.c.display_label.label("payee_label"),
    ).select_from(
        open_item.outerjoin(account, open_item.c.account_id == account.c.id)
        .outerjoin(txn, open_item.c.txn_id == txn.c.id)
        .outerjoin(named, open_item.c.reason_account_id == named.c.id)
        .outerjoin(payee, open_item.c.reason_payee_id == payee.c.id)
    )


# Each view this module defines, by name, for the frozen-SQL guard test.
VIEW_SELECTS: Final[dict[str, Callable[[], Select[tuple[object, ...]]]]] = {
    "report_txn": report_txn_select,
    "report_queue_counts": report_queue_counts_select,
    "report_crypto_movement": report_crypto_movement_select,
    "report_status": report_status_select,
    "report_coverage": report_coverage_select,
    "report_document": report_document_select,
    "report_open_item": report_open_item_select,
    "report_owed": report_owed_select,
    "report_owed_trace": report_owed_trace_select,
}
