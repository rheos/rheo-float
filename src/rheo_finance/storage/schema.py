"""The one ``MetaData`` for schema ``finance`` and the tables defined on it.

The migrations own the DDL; these ``Table`` objects mirror it so operations can build
statements, and a guard test fails when a database column is missing here or has no
tier. A module that adds a table defines it here and registers every column's tier
next to it.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from rheo_finance.storage.tiers import register_column
from rheo_finance.tier import Tier

metadata = MetaData(schema="finance")


P, R = Tier.PUBLIC, Tier.RESTRICTED


def _tiers(table: Table, tiers: dict[str, Tier]) -> None:
    """Register the listed columns. A column left off the list stays untiered, so the
    guard test names it instead of it quietly becoming public."""
    for name, tier in tiers.items():
        register_column(table.name, name, tier)


# --- decisions (state, append-only; migration 0002) -----------------------------------

decision = Table(
    "decision",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("target_ref", Text),
    Column("merchant_key", Text),
    Column("category", Text),
    Column("tag", Text),
    Column("business_unit", Text),
    Column("business_pct", Numeric),
    Column("counterpart_ref", Text),
    Column("obligation_ref", Text),
    Column("party_ref", Text),
    Column("account_ref", Text),
    Column("effective_on", Date),
    Column("sink", Text),
    Column("spec_digest", Text),
    Column("preview_digest", Text),
    Column("amount", Numeric),
    Column("about_decision_id", Uuid, ForeignKey("finance.decision.id")),
    Column("bulk_id", Uuid),
    Column("reverses_id", Uuid, ForeignKey("finance.decision.id")),
    Column("reverses_bulk_id", Uuid),
    Column("note", Text),
    Column("decided_at", DateTime(timezone=True), nullable=False),
)
_tiers(
    decision,
    {
        "id": P, "kind": P, "target_ref": P, "merchant_key": R, "category": P,
        "tag": P,
        "business_unit": P, "business_pct": P, "counterpart_ref": P,
        "obligation_ref": P, "party_ref": P, "account_ref": P, "effective_on": P,
        "sink": P, "spec_digest": P, "preview_digest": P, "amount": R,
        "about_decision_id": P, "bulk_id": P, "reverses_id": P,
        "reverses_bulk_id": P, "note": R, "decided_at": P,
    },
)  # fmt: skip

decision_item = Table(
    "decision_item",
    metadata,
    Column("decision_id", Uuid, ForeignKey("finance.decision.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("role", Text, nullable=False),
    Column("target_ref", Text),
    Column("label", Text),
    Column("amount", Numeric),
    Column("covers_ref", Text),
)
_tiers(
    decision_item,
    {
        "decision_id": P, "seq": P, "role": P, "target_ref": P, "label": P,
        "amount": R, "covers_ref": P,
    },
)  # fmt: skip

decision_target = Table(
    "decision_target",
    metadata,
    Column("decision_id", Uuid, ForeignKey("finance.decision.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("target_ref", Text, nullable=False),
    Column("account_slug", Text, nullable=False),
    Column("amount", Numeric, nullable=False),
    Column("currency", Text, nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("merchant_key", Text),
)
_tiers(
    decision_target,
    {
        "decision_id": P, "seq": P, "target_ref": P, "account_slug": P,
        "amount": R, "currency": P, "posting_date": P, "merchant_key": R,
    },
)  # fmt: skip

# --- stored exchange rates and fetch coverage (state, inputs of stage 4; 0016) -------

rate = Table(
    "rate",
    metadata,
    Column("rate_date", Date, primary_key=True),
    Column("base", Text, primary_key=True),
    Column("quote", Text, primary_key=True),
    Column("rate", Numeric, nullable=False),
    Column("source", Text, primary_key=True),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
)
# A published market rate is not personal, but the tier guard restricts every numeric
# column outside its reviewed exceptions, as it does ``txn.rate``.
_tiers(
    rate,
    {"rate_date": P, "base": P, "quote": P, "rate": R, "source": P, "fetched_at": P},
)

rate_coverage = Table(
    "rate_coverage",
    metadata,
    Column("series", Text, primary_key=True),
    Column("source", Text, primary_key=True),
    Column("covered_from", Date, primary_key=True),
    Column("covered_to", Date, primary_key=True),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
)
_tiers(
    rate_coverage,
    {
        "series": P, "source": P, "covered_from": P, "covered_to": P,
        "fetched_at": P,
    },
)  # fmt: skip

# --- sink specs (derived, stage 16; migration 0003) -----------------------------------

sink_spec = Table(
    "sink_spec",
    metadata,
    Column("sink", Text, primary_key=True),
    Column("spec_digest", Text, nullable=False),
    Column("hosts", Text, nullable=False),
    Column("request_templates", Text, nullable=False),
    Column("key_file", Text),
    Column("policy_version", Integer, nullable=False),
    Column("data_inputs", Text, nullable=False),
    Column(
        "computed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
_tiers(
    sink_spec,
    {
        "sink": P, "spec_digest": P, "hosts": P, "request_templates": P,
        "key_file": P, "policy_version": P, "data_inputs": P, "computed_at": P,
    },
)  # fmt: skip

# --- sink report views (migration 0004) ---------------------------------------------
# Views, mirrored here so a handler can select from them and the mirror and tier guards
# cover their columns. Nothing writes them; the reader may select from them.

report_sink_spec = Table(
    "report_sink_spec",
    metadata,
    Column("sink", Text),
    Column("spec_digest", Text),
    Column("hosts", Text),
    Column("request_templates", Text),
    Column("key_file", Text),
    Column("policy_version", Integer),
    Column("data_inputs", Text),
)
_tiers(
    report_sink_spec,
    {
        "sink": P, "spec_digest": P, "hosts": P, "request_templates": P,
        "key_file": P, "policy_version": P, "data_inputs": P,
    },
)  # fmt: skip

report_sink = Table(
    "report_sink",
    metadata,
    Column("sink", Text),
    Column("approved_at", DateTime(timezone=True)),
    Column("spec_digest", Text),
    Column("stale", Boolean),
)
_tiers(
    report_sink,
    {"sink": P, "approved_at": P, "spec_digest": P, "stale": P},
)

# --- account registry (derived, stage 2; migration 0005) ------------------------------

account = Table(
    "account",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("slug", Text, nullable=False, unique=True),
    Column("label", Text, nullable=False),
    Column("institution", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("currencies", ARRAY(Text), nullable=False),
    Column("cadence", Text, nullable=False),
    Column("owner", Text),
    Column("opened_on", Date),
    Column("closed_on", Date),
    Column("first_known_on", Date),
    Column("sourced_by", Text, nullable=False),
    Column("placeholder", Boolean, nullable=False, server_default="false"),
    Column("missing_before", Date),
    Column("missing_reason", Text),
)
# ``owner`` is the account holder's name: restricted (FR-46), like every name.
_tiers(
    account,
    {
        "id": P, "slug": P, "label": P, "institution": P, "kind": P,
        "currencies": P, "cadence": P, "owner": R, "opened_on": P, "closed_on": P,
        "first_known_on": P, "sourced_by": P, "placeholder": P,
        "missing_before": P, "missing_reason": P,
    },
)  # fmt: skip

# Every column restricted: an identifier row is what a statement is matched by, and
# which kinds an account carries and since when is part of that.
account_identifier = Table(
    "account_identifier",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("finance.account.id"), nullable=False),
    Column("identifier", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("valid_from", Date),
    Column("valid_to", Date),
)
_tiers(
    account_identifier,
    {
        "id": R, "account_id": R, "identifier": R, "kind": R, "valid_from": R,
        "valid_to": R,
    },
)  # fmt: skip

manual_entry_error = Table(
    "manual_entry_error",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("file", Text, nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("reason", Text, nullable=False),
)
_tiers(
    manual_entry_error,
    dict.fromkeys(("id", "file", "line_no", "reason"), Tier.INTERNAL),
)

# --- filed tax years (derived, stage 2; migration 0006) -------------------------------

filed_year = Table(
    "filed_year",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("year", Integer, nullable=False, unique=True),
    Column("filed_on", Date),
    Column("note", Text),
)
_tiers(filed_year, {"id": P, "year": P, "filed_on": P, "note": R})

# --- document registry (migration 0007) -----------------------------------------------

document = Table(
    "document",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("path", Text, nullable=False),
    Column("sha256", Text, nullable=False, unique=True),
    Column("kind", Text, nullable=False),
    Column("issuer", Text, nullable=False),
    Column("format", Text, nullable=False),
    Column("official", Boolean, nullable=False, server_default="true"),
    Column("status", Text, nullable=False),
    Column("parser_id", Text),
    Column("parser_version", Text),
    Column("extraction", Text, nullable=False, server_default="none"),
    Column("reject_reason", Text),
    Column("period_start", Date),
    Column("period_end", Date),
    Column("message_id", Text),
    Column("imported_at", DateTime(timezone=True), nullable=False),
    # sha256 of what the stored parse depended on besides the bytes (migration 0013).
    Column("parse_inputs", Text),
)
_tiers(
    document,
    {
        "id": P, "path": P, "sha256": P, "kind": P, "issuer": P, "format": P,
        "official": P, "status": P, "parser_id": P, "parser_version": P,
        "extraction": P, "reject_reason": Tier.INTERNAL, "period_start": P,
        "period_end": P, "message_id": R, "imported_at": P, "parse_inputs": P,
    },
)  # fmt: skip

# Derived on every archive walk: skipped files counted by issuer directory and reason,
# never by path.
archive_skip = Table(
    "archive_skip",
    metadata,
    Column("directory", Text, primary_key=True),
    Column("reason", Text, primary_key=True),
    Column("count", Integer, nullable=False),
)
_tiers(archive_skip, {"directory": P, "reason": P, "count": P})

# --- the parse cache and events (derived, stage 1; migration 0009) --------------------
# The account keys are deferred to commit (see the revision): stage 2 rewrites
# ``account`` in the transaction that reparses.


def _account_fk() -> ForeignKey:
    return ForeignKey("finance.account.id", deferrable=True, initially="DEFERRED")


# --- the owner's own wallet addresses (stage 2; migration 0018) -----------------------
# One row per address in its compared form (``manual.own_addresses``). Restricted,
# like every address.

own_address = Table(
    "own_address",
    metadata,
    Column("address", Text, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
)
_tiers(own_address, {"address": R, "account_id": R})


statement_period = Table(
    "statement_period",
    metadata,
    Column("document_id", Uuid, ForeignKey("finance.document.id"), primary_key=True),
    Column("account_id", Uuid, _account_fk(), primary_key=True),
    Column("currency", Text, primary_key=True),
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
    Column("opening", Numeric),
    Column("closing", Numeric),
    Column("total_debits", Numeric),
    Column("total_credits", Numeric),
    Column("total_payments", Numeric),
    Column("balance_due", Numeric),
    Column("minimum_due", Numeric),
    Column("due_date", Date),
    Column("control_kind", Text, nullable=False),
    Column("tied_out", Boolean, nullable=False),
)
_tiers(
    statement_period,
    {
        "document_id": P, "account_id": P, "currency": P, "period_start": P,
        "period_end": P, "opening": R, "closing": R, "total_debits": R,
        "total_credits": R, "total_payments": R, "balance_due": R, "minimum_due": R,
        "due_date": P, "control_kind": P, "tied_out": P,
    },
)  # fmt: skip

document_line = Table(
    "document_line",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("document_id", Uuid, ForeignKey("finance.document.id"), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("page", Integer),
    Column("line_kind", Text, nullable=False),
    Column("account_id", Uuid, _account_fk()),
    Column("posting_date", Date),
    Column("txn_date", Date),
    Column("occurred_at", DateTime(timezone=True)),
    Column("amount", Numeric),
    Column("currency", Text),
    Column("asset_identifier", Text),
    Column("original_amount", Numeric),
    Column("original_currency", Text),
    Column("stated_cad", Numeric(20, 2)),
    Column("stated_cad_price", Numeric),
    Column("description_raw", Text),
    Column("description_norm", Text),
    Column("counterparty", Text),
    Column("txid", Text),
    Column("from_address", Text),
    Column("to_address", Text),
    Column("source_type", Text),
    Column("is_fake", Boolean, nullable=False, server_default="false"),
    Column("fee_stated", Boolean, nullable=False, server_default="false"),
    Column("running_balance", Numeric),
    Column("fee_type", Text),
    Column("transfer_hint", Boolean, nullable=False, server_default="false"),
    Column("reversal_hint", Boolean, nullable=False, server_default="false"),
    Column("conversion_hint", Boolean, nullable=False, server_default="false"),
    Column("etransfer_hint", Boolean, nullable=False, server_default="false"),
    Column("hint_account_slug", Text),
    Column("trade_ref", Text),
    Column("instrument", Text),
    Column("result", Numeric),
    Column("trading_fee", Numeric),
    Column("funding_fee", Numeric),
    Column("line_key_id", Uuid),
    # A receipt payment's own reference as a digest (0015); null on other lines.
    Column("payment_ref_digest", Text),
)
# SPEC Data Models marks asset_identifier internal; it is restricted here because its
# name is an identifier's, and the tier guard restricts every such column.
_tiers(
    document_line,
    {
        "id": P, "document_id": P, "line_no": P, "page": P, "line_kind": P,
        "account_id": P, "posting_date": P, "txn_date": P, "occurred_at": P,
        "amount": R, "currency": P, "asset_identifier": R, "original_amount": R,
        "original_currency": P, "stated_cad": R, "stated_cad_price": R,
        "description_raw": R, "description_norm": R, "counterparty": R, "txid": R,
        "from_address": R, "to_address": R, "source_type": Tier.INTERNAL,
        "is_fake": P, "fee_stated": P, "running_balance": R, "fee_type": P,
        "transfer_hint": P, "reversal_hint": P, "conversion_hint": P,
        "etransfer_hint": P, "hint_account_slug": P, "trade_ref": P,
        "instrument": Tier.INTERNAL, "result": R, "trading_fee": R,
        "funding_fee": R, "line_key_id": P, "payment_ref_digest": R,
    },
)  # fmt: skip

# ``data`` is public as a column: each event type's model in an ``EVENT_MODELS``
# registry tiers its fields, and the tier guard keeps restricted ones out of it.
event = Table(
    "event",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("type", Text, nullable=False),
    Column("subject_ref", Text, nullable=False),
    Column("occurred_on", Date, nullable=False),
    Column("data", JSONB, nullable=False),
)
_tiers(
    event,
    {"id": P, "type": P, "subject_ref": P, "occurred_on": P, "data": P},
)

# --- the ledger lines (derived, stage 3; migration 0010) ------------------------------
# The columns after ``fee_type`` mirror SPEC Data Models; stage 3 sets the identity,
# date, amount and description ones and later stages fill the rest.

txn = Table(
    "txn",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("txn_date", Date),
    Column("occurred_at", DateTime(timezone=True)),
    Column("recognition_date", Date),
    Column(
        "tax_year", Integer, Computed("EXTRACT(YEAR FROM recognition_date)::integer")
    ),
    Column("amount", Numeric, nullable=False),
    Column("currency", Text, nullable=False),
    Column("asset_identifier", Text),
    Column("cad_amount", Numeric(20, 2)),
    Column("cad_source", Text),
    Column("rate", Numeric),
    Column("rate_date", Date),
    Column("conversion_state", Text),
    Column("description_norm", Text),
    Column("merchant_key", Text),
    Column("display_label", Text),
    Column("counterparty", Text),
    Column("party_id", Uuid),
    Column("txid", Text),
    Column("from_address", Text),
    Column("to_address", Text),
    Column("origin", Text, nullable=False),
    Column("basis", Text),
    Column("provisional", Boolean, nullable=False, server_default="false"),
    Column("movement_kind", Text, nullable=False),
    Column("transfer_like", Boolean, nullable=False, server_default="false"),
    Column("hint_account_id", Uuid, _account_fk()),
    Column("transfer_state", Text),
    Column("pair_kind", Text),
    Column("allocation_state", Text),
    Column("exclusion", Text),
    Column("orphan_candidate", Boolean, nullable=False, server_default="false"),
    Column("category", Text),
    Column("category_basis_ref", Text),
    Column("category_basis_class", Text),
    Column("tag", Text),
    Column("business_unit", Text),
    Column("business_pct", Numeric),
    Column("tag_basis_ref", Text),
    Column("tag_basis_class", Text),
    Column("classification_state", Text, nullable=False, server_default="unclassified"),
    Column("fee_type", Text),
    Column("trade_ref", Text),
)
# SPEC marks asset_identifier internal and cad_source public; the tier guard restricts
# every name shaped like an identifier or a CAD figure, so they are restricted here.
_tiers(
    txn,
    {
        "id": P, "account_id": P, "posting_date": P, "txn_date": P, "occurred_at": P,
        "recognition_date": P, "tax_year": P, "amount": R, "currency": P,
        "asset_identifier": R, "cad_amount": R, "cad_source": R, "rate": R,
        "rate_date": P, "conversion_state": P, "description_norm": R,
        "merchant_key": R, "display_label": Tier.INTERNAL, "counterparty": R,
        "party_id": P, "txid": R, "from_address": R, "to_address": R, "origin": P,
        "basis": P, "provisional": P, "movement_kind": P, "transfer_like": P,
        "hint_account_id": P, "transfer_state": P, "pair_kind": P,
        "allocation_state": P, "exclusion": P, "orphan_candidate": P, "category": P,
        "category_basis_ref": P, "category_basis_class": P, "tag": P,
        "business_unit": P, "business_pct": P, "tag_basis_ref": P,
        "tag_basis_class": P, "classification_state": P, "fee_type": P,
        "trade_ref": P,
    },
)  # fmt: skip

txn_source = Table(
    "txn_source",
    metadata,
    Column("txn_id", Uuid, ForeignKey("finance.txn.id"), nullable=False),
    Column("document_line_id", Uuid, ForeignKey("finance.document_line.id")),
    Column("manual_ref", Text),
    Column("line_key_id", Uuid, nullable=False),
    Column("role", Text, nullable=False),
)
_tiers(
    txn_source,
    {"txn_id": P, "document_line_id": P, "manual_ref": P, "line_key_id": P, "role": P},
)

# --- the pairing graph (derived, stage 8 on; migration 0017) --------------------------

pair_edge = Table(
    "pair_edge",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("a_txn_id", Uuid, ForeignKey("finance.txn.id")),
    Column("loan_key", Text),
    Column("b_txn_id", Uuid, ForeignKey("finance.txn.id")),
    Column("b_part_id", Uuid),
    Column("b_account_id", Uuid, _account_fk()),
    Column("group_id", Uuid),
    Column("applied", Boolean, nullable=False, server_default="false"),
    Column("basis_ref", Text),
    Column("fee_txn_id", Uuid, ForeignKey("finance.txn.id")),
    Column("spread_cad", Numeric(20, 2)),
)
_tiers(
    pair_edge,
    {
        "id": P, "kind": P, "a_txn_id": P, "loan_key": P, "b_txn_id": P,
        "b_part_id": P, "b_account_id": P, "group_id": P, "applied": P,
        "basis_ref": P, "fee_txn_id": P, "spread_cad": R,
    },
)  # fmt: skip

# --- source discrepancies (derived, stage 3; migration 0011) --------------------------
# Keys and codes only: the line's amount and description stay on ``document_line``.

source_discrepancy = Table(
    "source_discrepancy",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("document_id", Uuid, ForeignKey("finance.document.id"), nullable=False),
    Column(
        "document_line_id",
        Uuid,
        ForeignKey("finance.document_line.id"),
        nullable=False,
        unique=True,
    ),
    Column("kind", Text, nullable=False),
)
_tiers(
    source_discrepancy,
    {"id": P, "account_id": P, "document_id": P, "document_line_id": P, "kind": P},
)

# --- rejected documents' accounts (parse cache) and coverage (stage 6; 0012) ----------

rejected_period = Table(
    "rejected_period",
    metadata,
    Column("document_id", Uuid, ForeignKey("finance.document.id"), primary_key=True),
    Column("account_id", Uuid, _account_fk(), primary_key=True),
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
)
_tiers(
    rejected_period,
    {"document_id": P, "account_id": P, "period_start": P, "period_end": P},
)

coverage = Table(
    "coverage",
    metadata,
    Column("account_id", Uuid, _account_fk(), primary_key=True),
    Column("month", Date, primary_key=True),
    Column("state", Text, nullable=False),
    Column("reason_code", Text),
)
_tiers(coverage, {"account_id": P, "month": P, "state": P, "reason_code": P})

# --- the status, coverage and document report views (migration 0014) -----------------
# ``reports.views`` defines their bodies; codes, counts, dates, labels and registered
# paths only.

report_status = Table(
    "report_status",
    metadata,
    Column("fact", Text),
    Column("subject", Text),
    Column("count", BigInteger),
    Column("at", DateTime(timezone=True)),
)
_tiers(report_status, {"fact": P, "subject": P, "count": P, "at": P})

report_coverage = Table(
    "report_coverage",
    metadata,
    Column("account", Text),
    Column("account_label", Text),
    Column("month", Date),
    Column("state", Text),
    Column("reason", Text),
)
_tiers(
    report_coverage,
    {"account": P, "account_label": P, "month": P, "state": P, "reason": P},
)

report_document = Table(
    "report_document",
    metadata,
    Column("path", Text),
    Column("kind", Text),
    Column("status", Text),
    Column("period_start", Date),
    Column("period_end", Date),
    Column("official", Boolean),
    Column("extraction", Text),
)
_tiers(
    report_document,
    {
        "path": P, "kind": P, "status": P, "period_start": P, "period_end": P,
        "official": P, "extraction": P,
    },
)  # fmt: skip
