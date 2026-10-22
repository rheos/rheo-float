"""What a parser takes and returns (FR-3), shared by the registry, the docextract
adapter and every parser.

A parser is a function ``(text, accounts) -> ParseResult``. It reads extracted text,
never a path, and matches the accounts the document names through ``AccountLookup``.
It raises ``NotRecognised`` when the text is not a layout it reads (the document is
``unparsed``) and ``Rejected`` when it read the layout but the document must not load
(``unknown_account``, a failed check). The registry checks a ``summary`` control
itself; a parser does not need to.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from rheo_finance.manual.accounts import LAST_FOUR_KINDS, institution_key, last_four

ControlKind = Literal["summary", "running_balance", "bill_identity", "none"]
LineKind = Literal[
    "transaction", "payment_notice", "invoice", "receipt", "crypto_movement",
    "crypto_fee", "holding", "notice_fact", "trading_result",
]  # fmt: skip
# Why a document did not load. A rejected document records one of RejectReason in a
# ``finance.document.rejected`` event; the others go in ``document.reject_reason``
# with status ``unparsed`` or ``needs_manual_entry``.
RejectReason = Literal[
    "summary_mismatch", "checks_failed", "unknown_account", "ambiguous_account",
]  # fmt: skip
UnparsedReason = Literal[
    "no_parser", "not_recognised", "not_text", "file_missing", "file_changed",
    "path_withheld",
]  # fmt: skip
ManualReason = Literal["no_text_layer"]

REJECT_REASONS: Final[tuple[RejectReason, ...]] = (
    "summary_mismatch", "checks_failed", "unknown_account", "ambiguous_account",
)  # fmt: skip


class NotRecognised(Exception):
    """The text is not a layout this parser reads."""


class NoTextLayer(Exception):
    """A PDF with no usable text layer (a scan): it goes to manual entry (FR-9)."""


class Rejected(Exception):
    """The parser read the layout and the document must not load.

    ``period`` is the stated period when the parser read one, so coverage can show
    the months as rejected. The message is the reason code alone: it never carries a
    name, a number or an amount from the document.
    """

    def __init__(
        self, reason: RejectReason, period: tuple[dt.date, dt.date] | None = None
    ) -> None:
        super().__init__(reason)
        self.reason: RejectReason = reason
        self.period = period


@dataclass(frozen=True)
class RegisteredAccount:
    id: uuid.UUID
    slug: str


@dataclass(frozen=True)
class RegisteredIdentifier:
    """One ``account_identifier`` row with its account and institution."""

    account: RegisteredAccount
    institution: str
    identifier: str
    kind: str
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None

    def covers(self, start: dt.date, end: dt.date) -> bool:
        """Whether the identifier was valid at some point of ``start`` to ``end``."""
        after_start = self.valid_to is None or self.valid_to >= start
        before_end = self.valid_from is None or self.valid_from <= end
        return after_start and before_end


class AccountLookup:
    """The registered accounts a parser may match, by the rule the loader enforces.

    ``by_last_four`` matches the last four digits of an identifier of a
    ``LAST_FOUR_KINDS`` kind at an institution compared by ``institution_key``
    (``manual.accounts``), valid during the period: the same scope in which the
    loader refuses two accounts sharing a last four. Never by a printed name.
    """

    def __init__(self, identifiers: Iterable[RegisteredIdentifier]) -> None:
        self._identifiers = tuple(identifiers)

    def by_last_four(
        self, institution: str, four: str, start: dt.date, end: dt.date
    ) -> tuple[RegisteredAccount, ...]:
        """Every distinct registered account the four digits name, sorted by slug."""
        wanted = institution_key(institution)
        found = {
            each.account
            for each in self._identifiers
            if each.kind in LAST_FOUR_KINDS
            and institution_key(each.institution) == wanted
            and last_four(each.identifier) == four
            and each.covers(start, end)
        }
        return tuple(sorted(found, key=lambda account: account.slug))


@dataclass(frozen=True)
class OverdrawnSpell:
    """A run of negative balances: from the first negative line to the line that ends
    it, or None when the period ends overdrawn."""

    start: dt.date
    end: dt.date | None


@dataclass(frozen=True)
class ParsedPeriod:
    """One ``statement_period`` row before it is written, plus the overdrawn spells
    the statement shows (kept here for the overdraft events; they follow from the
    lines' running balances)."""

    account: RegisteredAccount
    currency: str
    period_start: dt.date
    period_end: dt.date
    control_kind: ControlKind
    opening: Decimal | None = None
    closing: Decimal | None = None
    total_debits: Decimal | None = None
    total_credits: Decimal | None = None
    total_payments: Decimal | None = None
    balance_due: Decimal | None = None
    minimum_due: Decimal | None = None
    due_date: dt.date | None = None
    # Set by the parser for running_balance, bill_identity and none; a summary period
    # is tied out by the registry's own check.
    tied_out: bool = False
    overdrawn: tuple[OverdrawnSpell, ...] = ()


@dataclass(frozen=True)
class ParsedLine:
    """One ``document_line`` row before it is written: every column but ``id``,
    ``document_id``, ``line_no`` (the registry numbers lines in order) and the stage 3
    columns ``description_norm`` and ``line_key_id``."""

    line_kind: LineKind
    account: RegisteredAccount | None
    posting_date: dt.date | None = None
    txn_date: dt.date | None = None
    occurred_at: dt.datetime | None = None
    amount: Decimal | None = None
    currency: str | None = None
    page: int | None = None
    asset_identifier: str | None = None
    original_amount: Decimal | None = None
    original_currency: str | None = None
    stated_cad: Decimal | None = None
    stated_cad_price: Decimal | None = None
    description_raw: str | None = None
    counterparty: str | None = None
    txid: str | None = None
    from_address: str | None = None
    to_address: str | None = None
    source_type: str | None = None
    is_fake: bool = False
    fee_stated: bool = False
    running_balance: Decimal | None = None
    fee_type: str | None = None
    transfer_hint: bool = False
    reversal_hint: bool = False
    conversion_hint: bool = False
    etransfer_hint: bool = False
    hint_account_slug: str | None = None
    trade_ref: str | None = None
    instrument: str | None = None
    result: Decimal | None = None
    trading_fee: Decimal | None = None
    funding_fee: Decimal | None = None


@dataclass(frozen=True)
class VerifiedSegment:
    """A continuous run of a running-balance account's lines."""

    account: RegisteredAccount
    start_date: dt.date
    end_date: dt.date


@dataclass(frozen=True)
class ParseResult:
    periods: tuple[ParsedPeriod, ...]
    lines: tuple[ParsedLine, ...]
    control_kind: ControlKind
    segments: tuple[VerifiedSegment, ...] = field(default=())
    official: bool = True


Parse = Callable[[str, AccountLookup], ParseResult]
