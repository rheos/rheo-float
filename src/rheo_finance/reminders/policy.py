"""The four-field reminder payload boundary, independent of any sink client."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Collection
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rheo_finance.contracts import Tiered
from rheo_finance.operations import OperationError
from rheo_finance.tier import Tier

_DIGITS = re.compile(r"\d{6,}")
_CARD = re.compile(r"(?<!\w)(?:[0-9xX*][ -]?){12,18}[0-9xX*](?!\w)")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class PayloadRefused(OperationError):
    """A policy refusal whose message never repeats a rejected value."""


class PayloadPolicy(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, revalidate_instances="always"
    )

    payee_label: Annotated[str, Tiered(Tier.PUBLIC)]
    amount: Annotated[Decimal, Tiered(Tier.RESTRICTED)] = Field(
        ge=0, max_digits=20, decimal_places=2, allow_inf_nan=False
    )
    due_date: Annotated[dt.date, Tiered(Tier.PUBLIC)]
    period: Annotated[str, Tiered(Tier.PUBLIC)]


class BillPayload(PayloadPolicy):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Annotated[str, Tiered(Tier.RESTRICTED)]
    body: Annotated[str, Tiered(Tier.RESTRICTED)]


def safe_token(value: str) -> bool:
    """Source keys are single tokens, never free text or identifier-shaped runs."""
    return (
        bool(_TOKEN.fullmatch(value))
        and not _DIGITS.search(value)
        and not _CARD.search(value)
    )


def build_payload(
    policy: PayloadPolicy, *, registered_payees: Collection[str]
) -> BillPayload:
    """Render only registered payee, threshold amount, due date and period.

    Registration is supplied by the repository, never by the payload's caller input.
    Rejecting extra fields at the typed boundary keeps memos and balances out.
    """
    if type(policy) is not PayloadPolicy:
        raise PayloadRefused("reminder payload requires a typed policy")
    try:
        policy = PayloadPolicy.model_validate(policy)
    except ValidationError:
        raise PayloadRefused("invalid reminder payload policy") from None
    if policy.payee_label not in registered_payees:
        raise PayloadRefused("reminder payee is not registered")
    if not safe_token(policy.period):
        raise PayloadRefused("reminder period must be a safe token")
    amount = f"{policy.amount:.2f} CAD"
    due = policy.due_date.isoformat()
    for value in (policy.payee_label, amount, due, policy.period):
        if (
            not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "\u2014" in value
            or _DIGITS.search(value)
            or _CARD.search(value)
        ):
            raise PayloadRefused("restricted shape in reminder payload")
    return BillPayload(
        **policy.model_dump(),
        title=f"{policy.payee_label}: {amount} due {due}",
        body=f"{policy.period}: {amount}",
    )
