"""CAD totals with their unconverted count (FR-14, AC-15).

``cad_total`` is the one way a CAD total is summed: the stored, already rounded
``cad_amount`` of every ``native`` and ``converted`` txn the filters select, and the
count of the selected txns left out because they have no CAD amount (``unconverted``,
or not converted yet). ``CadTotal.text`` is how a total is printed, always with that
count beside it, "0" included, so a total never hides the lines it leaves out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import ColumnElement, and_, func, select, true

from rheo_finance.context import UnitOfWork
from rheo_finance.render import money
from rheo_finance.storage.schema import txn

COUNTED: Final = ("native", "converted")
CENT: Final = Decimal("0.01")


@dataclass(frozen=True)
class CadTotal:
    sum: Decimal
    unconverted_count: int

    def text(self) -> str:
        return f"{money(self.sum, 'CAD')} ({self.unconverted_count} unconverted)"


def cad_total(uow: UnitOfWork, filters: Sequence[ColumnElement[bool]] = ()) -> CadTotal:
    """The CAD total of the txns ``filters`` (conditions on ``txn``) select."""
    counted = func.coalesce(txn.c.conversion_state, "unconverted").in_(COUNTED)
    total, left_out = uow.execute(
        select(
            func.coalesce(func.sum(txn.c.cad_amount).filter(counted), 0),
            func.count().filter(~counted),
        ).where(and_(true(), *filters))
    ).one()
    return CadTotal(sum=Decimal(total).quantize(CENT), unconverted_count=int(left_out))
