"""What the credit card parsers share (FR-8, FR-19, FR-20).

Sign convention. A card line is signed from the owner's side, as every other
account's: a purchase, a fee or interest is negative, a payment or a refund positive.
So a card statement's ``opening`` and ``closing`` are the balance owed, negated (a
card owing 100.00 has balance -100.00), and its lines sum from one to the other. The
card fields stay as printed: ``balance_due`` is the statement balance owed,
``minimum_due`` the printed minimum (None when the statement prints none),
``total_payments`` the payments the statement reports, all positive. A card
statement's date is its period end.

A card is matched to the registry by the last four digits of its masked number at the
card's institution (``AccountLookup.by_last_four``: identifiers of kinds ``number``
and ``masked_card``, the same scope in which the loader refuses two accounts sharing a
last four), never by the cardholder's printed name, which no card parser stores.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Final

from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    RegisteredAccount,
    Rejected,
)

ACCOUNT_KIND: Final = "credit_card"
CURRENCY: Final = "CAD"
# A printed amount without its currency sign: 1,234.56 or 1234.56.
AMOUNT_TEXT: Final = r"\d{1,3}(?:,\d{3})*\.\d\d|\d+\.\d\d"
_AMOUNT: Final = re.compile(AMOUNT_TEXT)
MONTH_ABBREVIATIONS: Final = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}  # fmt: skip


def amount(text: str) -> Decimal:
    """A printed amount (``1,234.56``) as an exact Decimal."""
    if _AMOUNT.fullmatch(text) is None:
        raise NotRecognised("not a card layout amount")
    return Decimal(text.replace(",", ""))


def written_date(text: str, form: str) -> dt.date:
    """A printed date in ``strptime`` ``form``; ``NotRecognised`` when it does not
    read. Month names are English, as every card layout here prints them."""
    try:
        return dt.datetime.strptime(" ".join(text.split()), form).date()
    except ValueError:
        raise NotRecognised("not a card layout date") from None


def day_in_period(month: int, day: int, period_end: dt.date) -> dt.date:
    """A date printed without its year, on a statement whose period ends on
    ``period_end``: the latest such date no more than a month after the period end
    (a statement's dates run up to its end, and a period crossing the new year prints
    December days with January's year)."""
    for year in (period_end.year + 1, period_end.year, period_end.year - 1):
        try:
            found = dt.date(year, month, day)
        except ValueError:
            continue
        if found <= period_end + dt.timedelta(days=31):
            return found
    raise NotRecognised("not a card layout date")


def card_account(
    accounts: AccountLookup,
    institution: str,
    four: str,
    period: tuple[dt.date, dt.date],
) -> RegisteredAccount:
    """The one registered account the card's last four names in the period;
    ``unknown_account`` or ``ambiguous_account`` otherwise."""
    found = accounts.by_last_four(institution, four, *period)
    if not found:
        raise Rejected("unknown_account", period)
    if len(found) > 1:
        raise Rejected("ambiguous_account", period)
    return found[0]


def one_value[T](values: list[T]) -> T:
    """The value a layout prints in several places, which must agree."""
    if not values or any(each != values[0] for each in values):
        raise NotRecognised("a card layout value is missing or disagrees")
    return values[0]
