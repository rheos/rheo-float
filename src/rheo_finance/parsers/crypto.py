"""What every crypto parser shares: a withdrawal as its lines (FR-4, FR-15(b); SPEC
Derivation stage 1 "Crypto quantities and fees", edge case 48).

A withdrawal line's quantity is what left the account for the destination, net of any
network or withdrawal fee; the fee is its own ``crypto_fee`` line on the same account,
``occurred_at`` and TXID, so the account's balance moves by both and the destination's
deposit pairs with the net quantity. A source prints its figure either gross (the fee
included) or net, and says which; ``withdrawal_lines`` turns either into the two lines.

When a source states no fee at all, the withdrawal is one line carrying the one figure
the source prints, with ``fee_stated`` false: pairing then allows
``finance.crypto.fee_tolerance_pct`` between it and the deposit. A stated fee of zero
is a stated fee: one line, ``fee_stated`` true, and no fee line.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rheo_finance.parsers.base import ParsedLine, RegisteredAccount

MOVEMENT: Final = "crypto_movement"
FEE: Final = "crypto_fee"
# The ``fee_type`` of a withdrawal's fee line.
WITHDRAWAL_FEE: Final = "withdrawal_fee"


@dataclass(frozen=True)
class Withdrawal:
    """One withdrawal as its source prints it. ``quantity`` is the printed figure,
    positive; ``fee`` the printed fee (None when the source states none), in
    ``fee_asset`` when that differs from the withdrawn asset; ``gross`` says the
    printed quantity includes the fee."""

    account: RegisteredAccount
    occurred_at: dt.datetime
    asset_identifier: str
    quantity: Decimal
    fee: Decimal | None = None
    gross: bool = False
    fee_asset: str | None = None
    txid: str | None = None
    to_address: str | None = None
    from_address: str | None = None
    source_type: str | None = None
    description_raw: str | None = None


def withdrawal_lines(withdrawal: Withdrawal) -> tuple[ParsedLine, ...]:
    """The withdrawal's net movement line, then its fee line when it has a fee.

    Raises ``ValueError`` when the figures cannot be one withdrawal: a quantity that is
    not positive, a negative fee, or a gross figure smaller than its fee. A parser
    turns that into ``NotRecognised`` or a rejection; it never stores such a line."""
    w = withdrawal
    if w.quantity <= 0:
        raise ValueError("a withdrawal's quantity is positive")
    if w.fee is not None and w.fee < 0:
        raise ValueError("a withdrawal's fee is not negative")
    fee_asset = w.fee_asset or w.asset_identifier
    net = w.quantity
    if w.gross and w.fee is not None:
        if fee_asset != w.asset_identifier:
            raise ValueError("a gross figure includes a fee in its own asset only")
        net = w.quantity - w.fee
        if net <= 0:
            raise ValueError("a gross withdrawal is larger than its fee")
    movement = ParsedLine(
        line_kind=MOVEMENT,
        account=w.account,
        occurred_at=w.occurred_at,
        amount=-net,
        asset_identifier=w.asset_identifier,
        txid=w.txid,
        to_address=w.to_address,
        from_address=w.from_address,
        source_type=w.source_type,
        description_raw=w.description_raw,
        fee_stated=w.fee is not None,
    )
    if not w.fee:
        return (movement,)
    fee = dataclasses.replace(
        movement,
        line_kind=FEE,
        amount=-w.fee,
        asset_identifier=fee_asset,
        fee_type=WITHDRAWAL_FEE,
    )
    return (movement, fee)
