"""What a parser takes and returns (FR-3), shared by the registry, the docextract
adapter and every parser.

A parser is a function ``(text, accounts) -> ParseResult``. It reads extracted text,
never a path, and matches the accounts the document names through ``AccountLookup``.
A ``running_balance`` parser splits its lines into verified segments and the gaps
between them with ``balance_runs``, which coverage can run again over stored lines.
It raises ``NotRecognised`` when the text is not a layout it reads (the document is
``unparsed``) and ``Rejected`` when it read the layout but the document must not load
(``unknown_account``, a failed check). The registry checks a ``summary`` control
itself; a parser does not need to.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from rheo_finance.manual.accounts import (
    LAST_FOUR_KINDS,
    identifier_key,
    institution_key,
    last_four,
)

ControlKind = Literal["summary", "running_balance", "bill_identity", "none"]
# The two crypto line kinds, named once here for every module that emits or reads
# them: a crypto parser, stage 3's keys and ranking, stage 7 and the summary check.
CRYPTO_MOVEMENT: Final = "crypto_movement"
CRYPTO_FEE: Final = "crypto_fee"
CRYPTO_LINE_KINDS: Final = (CRYPTO_MOVEMENT, CRYPTO_FEE)
# The ``txn.movement_kind`` values the derive stages write and read, named once here
# beside the line kinds: a fiat line, a crypto fee line (``CRYPTO_FEE``), and the
# three kinds stage 7 gives a crypto movement line (unclassified until it runs).
FIAT: Final = "fiat"
CRYPTO_TRANSFER: Final = "crypto_transfer"
CRYPTO_UNCLASSIFIED: Final = "crypto_unclassified"
CRYPTO_SWAP_LEG: Final = "crypto_swap_leg"
LineKind = Literal[
    "transaction", "payment_notice", "invoice", "receipt", "crypto_movement",
    "crypto_fee", "holding", "notice_fact", "trading_result",
]  # fmt: skip
# Why a document did not load. A rejected document records one of RejectReason in a
# ``finance.document.rejected`` event; the others go in ``document.reject_reason``
# with status ``unparsed`` or ``needs_manual_entry``.
RejectReason = Literal[
    "summary_mismatch", "checks_failed", "unknown_account", "ambiguous_account",
    "identity_mismatch",
]  # fmt: skip
UnparsedReason = Literal[
    "no_parser", "not_recognised", "not_text", "file_missing", "file_changed",
    "path_withheld",
]  # fmt: skip
ManualReason = Literal["no_text_layer"]

REJECT_REASONS: Final[tuple[RejectReason, ...]] = (
    "summary_mismatch", "checks_failed", "unknown_account", "ambiguous_account",
    "identity_mismatch",
)  # fmt: skip


class NotRecognised(Exception):
    """The text is not a layout this parser reads."""


class NoTextLayer(Exception):
    """A PDF with no usable text layer (a scan): it goes to manual entry (FR-9)."""


class Rejected(Exception):
    """The parser read the layout and the document must not load.

    ``period`` is the stated period when the parser read one, and ``accounts`` the
    registered accounts the document names when the parser matched them before
    rejecting, so coverage can show those accounts' months in the period as rejected.
    The message is the reason code alone: it never carries a name, a number or an
    amount from the document.
    """

    def __init__(
        self,
        reason: RejectReason,
        period: tuple[dt.date, dt.date] | None = None,
        accounts: tuple[RegisteredAccount, ...] = (),
    ) -> None:
        super().__init__(reason)
        self.reason: RejectReason = reason
        self.period = period
        self.accounts = accounts


@dataclass(frozen=True)
class RegisteredAccount:
    id: uuid.UUID
    slug: str


@dataclass(frozen=True)
class RegisteredIdentifier:
    """One ``account_identifier`` row with its account, institution and (for
    ``AccountLookup.by_institution``) the account's kind."""

    account: RegisteredAccount
    institution: str
    identifier: str
    kind: str
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    account_kind: str | None = None

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
    loader refuses two accounts sharing a last four. The loader lets two distinct full
    numbers of different accounts share one, so a last-four match can find both: every
    caller then rejects ``ambiguous_account`` and never picks one. Never by a printed
    name.
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

    def by_identifier(
        self, institution: str, identifier: str, start: dt.date, end: dt.date
    ) -> tuple[RegisteredAccount, ...]:
        """Every distinct registered account holding ``identifier`` in full as a
        ``number`` identifier at ``institution``, valid during the period, sorted by
        slug. Digits and letters are compared (letters without case) and separators
        ignored, so an account ID that mixes letters and digits matches in full; an
        identifier with no digit matches nothing."""
        wanted = institution_key(institution)
        key = identifier_key(identifier)
        if not re.search(r"\d", key):
            return ()
        found = {
            each.account
            for each in self._identifiers
            if each.kind == "number"
            and institution_key(each.institution) == wanted
            and identifier_key(each.identifier) == key
            and each.covers(start, end)
        }
        return tuple(sorted(found, key=lambda account: account.slug))

    def by_institution(
        self, institution: str, account_kind: str, start: dt.date, end: dt.date
    ) -> tuple[RegisteredAccount, ...]:
        """Every distinct registered account of ``account_kind`` at ``institution``
        (compared by ``institution_key``) holding a ``number`` identifier valid during
        the period, sorted by slug: the accounts the vendor's statement parser could
        match by its printed number. An account of another kind at the same
        institution (a crypto wallet), or one whose number was replaced before the
        period, is no candidate.

        For an export that prints no account identifier at all: the parser takes the
        one account it finds and rejects none or several. Residual risk: an export of
        a second account at the vendor that is not registered lands on the registered
        one. Inside a tied-out statement window of that account its lines match no
        statement line and show as discrepancies; outside every window they load as
        the registered account's lines and nothing flags them."""
        wanted = institution_key(institution)
        found = {
            each.account
            for each in self._identifiers
            if each.kind == "number"
            and each.account_kind == account_kind
            and institution_key(each.institution) == wanted
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
    # A receipt payment's own reference, as a digest: never the reference itself.
    payment_ref_digest: str | None = None


@dataclass(frozen=True)
class VerifiedSegment:
    """A continuous run of a running-balance account's lines."""

    account: RegisteredAccount
    start_date: dt.date
    end_date: dt.date


@dataclass(frozen=True)
class BalanceGap:
    """Rows are missing between two consecutive lines of a running-balance account:
    the earlier line's balance plus the later line's amount is not the later line's
    balance. The span runs from the earlier line's date to the later line's, both
    lines loaded; coverage marks it ``missing`` (reason ``running_balance_break``)."""

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
    gaps: tuple[BalanceGap, ...] = field(default=())


def balance_runs(
    account: RegisteredAccount,
    lines: Sequence[tuple[dt.date, Decimal, Decimal]],
) -> tuple[tuple[VerifiedSegment, ...], tuple[BalanceGap, ...]]:
    """The verified segments and the gaps of one account's running-balance lines,
    given as ``(posting date, signed amount, running balance)`` in posting order.

    A line continues the one before it when that line's balance plus its own amount
    is its balance, to the cent. Each maximal run of two or more continuing lines is
    a segment from its first line's date to its last's; a lone line between two
    breaks verifies nothing and is no segment. Each break is a gap between the two
    lines either side of it.
    """
    segments: list[VerifiedSegment] = []
    gaps: list[BalanceGap] = []
    start = 0
    for index in range(1, len(lines) + 1):
        if index < len(lines):
            before, line = lines[index - 1], lines[index]
            if before[2] + line[1] == line[2]:
                continue
            gaps.append(BalanceGap(account, before[0], line[0]))
        if index - 1 > start:
            first, last = lines[start][0], lines[index - 1][0]
            segments.append(VerifiedSegment(account, first, last))
        start = index
    return tuple(segments), tuple(gaps)


Parse = Callable[[str, AccountLookup], ParseResult]
