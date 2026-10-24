"""Bank of Canada Valet rates under the ``rates`` sink (FR-14, FR-65).

**The series.** One ``FX<CCY>CAD`` daily series per fiat currency other than CAD
present in ``txn`` (``currencies``), each observation the CAD price of one unit. The
``FX`` series start on ``FX_SERIES_START``; a line dated before that uses the legacy
noon series named in ``rates.LEGACY_NOON_SERIES`` (USD only), fetched up to the day
before. Which series a date belongs to, and the rule for a usable rate, are in
``rates``.

**The spec.** ``spec`` is the sink's ``SinkSpec`` provider: the Valet host, one GET
template (``/valet/observations/:series/json`` with query keys ``start_date`` and
``end_date``), no key file, and two public data inputs, the sorted currency set and
the series it implies. Adding a currency changes the digest, so an approval given for
the old set goes stale until the owner approves again.

**The plan.** ``plan`` asks only for what a line lacks: for each series, the dates of
the lines that ``rates.RateBook.on_or_before`` cannot convert (a date in no covered
range, or after the latest observation fetched), from ``LOOKBACK_DAYS`` before the
earliest of them up to the fetch day (the legacy series: up to its last day). A line
whose issuer states its CAD amount needs no rate and is left out. ``--from`` and
``--to`` replace either bound and request every series of the set. ``finance sink
preview rates`` lists the planned series and their start dates; the end is the fetch
day, so it is shown and not digested.

**The fetch.** ``finance.rate.fetch`` asks the gate first and sends nothing while the
sink is off. Otherwise it sends one GET per planned series through ``http.Client``
(sink ``rates``, the one template on its allowlist) and inserts each observation as a
``rate`` row (source ``boc``), keeping a row already stored, and records the range the
reply covered as a ``rate_coverage`` row: from the requested start to the latest
observation returned, so days not yet published stay uncovered. A closed series (the
legacy one, whose window ends before ``FX_SERIES_START``) publishes nothing more for its
range, so its coverage extends to the requested end, but only when its latest
observation lies within ``LOOKBACK_DAYS`` of that end: the days between are the
weekend and holidays after its last business day. A short or empty reply covers no
more than it showed, whatever the series, so no later line converts across days the
fetch never saw. A reply with no observation in the requested range is outcome
``empty``, not ``stored``: nothing is covered and the next fetch asks again. A failed
request, a reply it cannot read, or a redirect to another host fails that series alone
and records no coverage; the others are stored.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert

from rheo_finance.clock import local_today
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.http import Client, HostRefused, RequestFailed
from rheo_finance.operations import OperationSpec, register
from rheo_finance.parsers.base import FIAT
from rheo_finance.rates import (
    FX_SERIES_START,
    LEGACY_NOON_SERIES,
    LOOKBACK_DAYS,
    QUOTE_CAD,
    SOURCE_BOC,
    fx_series,
    rate_book,
    series_ids,
)
from rheo_finance.sinks.gate import is_enabled
from rheo_finance.sinks.preview import PREVIEW_BUILDERS, PreviewItem, SinkPreview
from rheo_finance.sinks.spec import SINK_SPECS, RequestTemplate, SinkSpec, current_spec
from rheo_finance.storage.schema import (
    document_line,
    rate,
    rate_coverage,
    txn,
    txn_source,
)
from rheo_finance.tier import Tier

P = Tier.PUBLIC

SINK: Final = "rates"
DOCS_URL: Final = "https://www.bankofcanada.ca/valet/docs"
OBSERVATIONS: Final = RequestTemplate(
    method="GET",
    path="/valet/observations/:series/json",
    query_keys=("start_date", "end_date"),
)
ALLOWLIST: Final = ((OBSERVATIONS.method, OBSERVATIONS.path),)
PAYLOAD_POLICY_VERSION: Final = 1
_CURRENCY: Final = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class Endpoint:
    """Where Valet answers. Tests replace it with a local stub's host."""

    host: str
    scheme: str = "https"


ENDPOINT = Endpoint("www.bankofcanada.ca")


def https_client(uow: UnitOfWork, spec: SinkSpec) -> Client:
    return Client(SINK, spec, ALLOWLIST, uow=uow)


ClientFactory = Callable[[UnitOfWork, SinkSpec], Client]
# Replaced only by tests, with a client that may use plain http to a local stub.
make_client: ClientFactory = https_client


# --- the series set and the spec ------------------------------------------------------


def currencies(uow: UnitOfWork) -> list[str]:
    """The fiat currencies other than CAD in ``txn``, sorted."""
    found = uow.scalars(
        select(txn.c.currency)
        .where(txn.c.movement_kind == FIAT, txn.c.currency != QUOTE_CAD)
        .distinct()
    )
    return sorted(each for each in found if _CURRENCY.fullmatch(each))


def spec(uow: UnitOfWork) -> SinkSpec:
    found = currencies(uow)
    return SinkSpec(
        name=SINK,
        hosts=(ENDPOINT.host,),
        requests=(OBSERVATIONS,),
        key_file=None,
        payload_policy_version=PAYLOAD_POLICY_VERSION,
        data_inputs=(
            ("currencies", ",".join(found)),
            ("series", ",".join(sorted(s for c in found for s in series_ids(c)))),
        ),
    )


# --- the plan -------------------------------------------------------------------------


@dataclass(frozen=True)
class Planned:
    """One request: ``end`` None means up to the fetch day. ``closed``: the series
    publishes nothing more for the range (its window ends before the FX series)."""

    currency: str
    series: str
    start: dt.date
    end: dt.date | None
    closed: bool = False


@dataclass(frozen=True)
class _Window:
    series: str
    first: dt.date | None
    last: dt.date | None

    def holds(self, day: dt.date) -> bool:
        return (self.first is None or day >= self.first) and (
            self.last is None or day <= self.last
        )


def _windows(currency: str) -> list[_Window]:
    windows = [_Window(fx_series(currency), FX_SERIES_START, None)]
    legacy = LEGACY_NOON_SERIES.get(currency)
    if legacy is not None:
        windows.append(_Window(legacy, None, FX_SERIES_START - dt.timedelta(days=1)))
    return windows


def _line_dates(uow: UnitOfWork, currency: str) -> list[dt.date]:
    """The dates of the ``currency`` lines that need a Bank of Canada rate: every fiat
    txn but those whose primary line states its CAD amount (stage 4 uses that)."""
    day = func.coalesce(txn.c.txn_date, txn.c.posting_date)
    return list(
        uow.scalars(
            select(day)
            .select_from(txn)
            .outerjoin(
                txn_source,
                and_(txn_source.c.txn_id == txn.c.id, txn_source.c.role == "primary"),
            )
            .outerjoin(
                document_line, document_line.c.id == txn_source.c.document_line_id
            )
            .where(
                txn.c.movement_kind == FIAT,
                txn.c.currency == currency,
                document_line.c.stated_cad.is_(None),
            )
            .distinct()
        )
    )


def plan(
    uow: UnitOfWork, *, start: dt.date | None = None, end: dt.date | None = None
) -> list[Planned]:
    """The requests a fetch would send, before the fetch day is known."""
    planned: list[Planned] = []
    for currency in currencies(uow):
        book = rate_book(uow, currency)
        days = _line_dates(uow, currency)
        for window in _windows(currency):
            needed = [
                day
                for day in days
                if window.holds(day) and book.on_or_before(day) is None
            ]
            if start is None and not needed:
                continue
            first = (
                start
                if start is not None
                else min(needed) - dt.timedelta(days=LOOKBACK_DAYS)
            )
            if window.first is not None:
                first = max(first, window.first)
            last = end if end is not None else window.last
            if window.last is not None and last is not None:
                last = min(last, window.last)
            if last is not None and first > last:
                continue
            planned.append(
                Planned(currency, window.series, first, last, window.last is not None)
            )
    return planned


# --- the preview ----------------------------------------------------------------------


def preview_items(uow: UnitOfWork) -> list[PreviewItem]:
    return [
        PreviewItem(
            kind="series",
            key=each.series,
            fields=(
                ("currency", each.currency),
                ("start_date", each.start.isoformat()),
                *(() if each.end is None else (("end_date", each.end.isoformat()),)),
            ),
        )
        for each in plan(uow)
    ]


def preview_note(ctx: FinanceContext, item: PreviewItem) -> str | None:
    if any(name == "end_date" for name, _ in item.fields):
        return None
    today = local_today(ctx.clock, ctx.settings.timezone)
    return f"end_date {today.isoformat()} (the fetch day)"


SAMPLE: Final = (
    PreviewItem(
        kind="series",
        key=fx_series("USD"),
        fields=(("currency", "USD"), ("start_date", "2026-01-01")),
    ),
)


# --- reading a reply ------------------------------------------------------------------


class ValetReplyError(Exception):
    """A reply that is not Valet's observations shape."""


def observations(body: bytes, series: str) -> list[tuple[dt.date, Decimal]]:
    """The (date, rate) pairs of a Valet observations reply for ``series``. A date
    with no value for the series is skipped; anything malformed raises."""
    try:
        payload: Any = json.loads(
            body.decode("utf-8"), parse_float=Decimal, parse_int=Decimal
        )
    except (UnicodeDecodeError, ValueError):
        raise ValetReplyError("the reply is not JSON") from None
    rows = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValetReplyError("the reply has no observations list")
    found: list[tuple[dt.date, Decimal]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("d"), str):
            raise ValetReplyError("an observation has no date")
        try:
            day = dt.date.fromisoformat(row["d"])
        except ValueError:
            raise ValetReplyError("an observation's date does not parse") from None
        cell = row.get(series)
        if cell is None:
            continue
        if not isinstance(cell, dict):
            raise ValetReplyError("an observation's value is not an object")
        value = cell.get("v")
        if value is None or value == "":
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValetReplyError("an observation's value is not a number") from None
        if not number.is_finite() or number <= 0:
            raise ValetReplyError("an observation's value is not a positive number")
        found.append((day, number))
    return found


# --- finance.rate.fetch ---------------------------------------------------------------


class FetchIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_date: dt.date | None = None
    to_date: dt.date | None = None

    @model_validator(mode="after")
    def _order(self) -> Self:
        if (
            self.from_date is not None
            and self.to_date is not None
            and self.from_date > self.to_date
        ):
            raise ValueError("from_date is after to_date")
        return self


# ``empty``: the reply held no observation in the requested range, so nothing was
# stored or covered and the series is asked again next time.
Outcome = Literal["stored", "empty", "failed"]


class SeriesFetched(BaseModel):
    model_config = ConfigDict(frozen=True)

    series: Annotated[str, Tiered(P)]
    currency: Annotated[str, Tiered(P)]
    start_date: Annotated[dt.date, Tiered(P)]
    end_date: Annotated[dt.date, Tiered(P)]
    outcome: Annotated[Outcome, Tiered(P)]
    received: Annotated[int, Tiered(P)] = 0
    stored: Annotated[int, Tiered(P)] = 0
    # Why a series failed, in fixed words: never a URL, header or reply text.
    reason: Annotated[str | None, Tiered(P)] = None


class FetchOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: Annotated[bool, Tiered(P)]
    planned: Annotated[int, Tiered(P)]
    requests: Annotated[int, Tiered(P)]
    stored: Annotated[int, Tiered(P)]
    empty: Annotated[int, Tiered(P)]
    failed: Annotated[int, Tiered(P)]
    series: Annotated[list[SeriesFetched], Tiered(P)]


def _url(series: str, start: dt.date, end: dt.date) -> str:
    path = OBSERVATIONS.path.replace(":series", series)
    return (
        f"{ENDPOINT.scheme}://{ENDPOINT.host}{path}"
        f"?start_date={start.isoformat()}&end_date={end.isoformat()}"
    )


def _store(
    uow: UnitOfWork,
    currency: str,
    found: list[tuple[dt.date, Decimal]],
    at: dt.datetime,
) -> int:
    if not found:
        return 0
    rows = [
        {
            "rate_date": day,
            "base": currency,
            "quote": QUOTE_CAD,
            "rate": value,
            "source": SOURCE_BOC,
            "fetched_at": at,
        }
        for day, value in found
    ]
    inserted = uow.execute(
        insert(rate).values(rows).on_conflict_do_nothing().returning(rate.c.rate_date)
    )
    return len(inserted.all())


def _cover(
    uow: UnitOfWork,
    each: Planned,
    end: dt.date,
    latest: dt.date | None,
    at: dt.datetime,
) -> None:
    """Record the range a reply covered: from the requested start to the latest
    observation it returned. A closed series extends to the requested ``end`` only
    when that observation lies within ``LOOKBACK_DAYS`` of it, so the last legacy
    days before the FX series starts still convert. No observation returned: nothing
    is recorded, for a closed series too."""
    if latest is None:
        return
    last = latest
    if each.closed and (end - latest).days <= LOOKBACK_DAYS:
        last = end
    uow.execute(
        insert(rate_coverage)
        .values(
            series=each.series,
            source=SOURCE_BOC,
            covered_from=each.start,
            covered_to=last,
            fetched_at=at,
        )
        .on_conflict_do_nothing()
    )


def _fetch_one(
    uow: UnitOfWork, client: Client, each: Planned, end: dt.date, at: dt.datetime
) -> SeriesFetched:
    failed = SeriesFetched(
        series=each.series,
        currency=each.currency,
        start_date=each.start,
        end_date=end,
        outcome="failed",
    )
    try:
        response = client.request("GET", _url(each.series, each.start, end))
    except RequestFailed:
        return failed.model_copy(update={"reason": "the request failed"})
    except HostRefused:
        # A redirect to another host (nothing is sent there) fails this series alone.
        return failed.model_copy(update={"reason": "the host was refused"})
    if response.status != 200:
        return failed.model_copy(update={"reason": f"http status {response.status}"})
    try:
        found = [
            (day, value)
            for day, value in observations(response.body, each.series)
            if each.start <= day <= end
        ]
    except ValetReplyError:
        return failed.model_copy(update={"reason": "the reply was not understood"})
    if not found:
        return failed.model_copy(update={"outcome": "empty"})
    stored = _store(uow, each.currency, found, at)
    _cover(uow, each, end, max((day for day, _ in found), default=None), at)
    return failed.model_copy(
        update={"outcome": "stored", "received": len(found), "stored": stored}
    )


def fetch(ctx: FinanceContext, uow: UnitOfWork, data: FetchIn) -> FetchOut:
    today = local_today(ctx.clock, ctx.settings.timezone)
    planned = [
        (each, min(each.end or today, today))
        for each in plan(uow, start=data.from_date, end=data.to_date)
    ]
    planned = [(each, end) for each, end in planned if each.start <= end]
    if not is_enabled(uow, SINK):
        return FetchOut(
            enabled=False,
            planned=len(planned),
            requests=0,
            stored=0,
            empty=0,
            failed=0,
            series=[],
        )
    current = current_spec(uow, SINK)
    assert current is not None  # the gate found an approval for it
    client = make_client(uow, current)
    at = ctx.clock.now_utc()
    results = [_fetch_one(uow, client, each, end, at) for each, end in planned]
    return FetchOut(
        enabled=True,
        planned=len(planned),
        requests=len(results),
        stored=sum(each.stored for each in results),
        empty=sum(each.outcome == "empty" for each in results),
        failed=sum(each.outcome == "failed" for each in results),
        series=results,
    )


SINK_SPECS[SINK] = spec
PREVIEW_BUILDERS[SINK] = SinkPreview(
    items=preview_items, note=preview_note, sample=SAMPLE
)
register(
    OperationSpec(
        name="finance.rate.fetch",
        input_model=FetchIn,
        output_model=FetchOut,
        handler=fetch,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description=(
            "Fetch the Bank of Canada rates the ledger's lines lack and store them."
            " Asks the rates sink gate first and sends nothing while it is off."
        ),
    )
)
