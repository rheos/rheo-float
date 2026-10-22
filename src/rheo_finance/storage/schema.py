"""The one ``MetaData`` for schema ``finance`` and the tables defined on it.

The migrations own the DDL; these ``Table`` objects mirror it so operations can build
statements, and a guard test fails when a database column is missing here or has no
tier. A module that adds a table defines it here and registers every column's tier
next to it.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
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
from sqlalchemy.dialects.postgresql import ARRAY

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
_tiers(
    account,
    {
        "id": P, "slug": P, "label": P, "institution": P, "kind": P,
        "currencies": P, "cadence": P, "owner": P, "opened_on": P, "closed_on": P,
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
)
_tiers(
    document,
    {
        "id": P, "path": P, "sha256": P, "kind": P, "issuer": P, "format": P,
        "official": P, "status": P, "parser_id": P, "parser_version": P,
        "extraction": P, "reject_reason": Tier.INTERNAL, "period_start": P,
        "period_end": P, "message_id": R, "imported_at": P,
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
