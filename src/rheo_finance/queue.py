"""Computed confirm items and transaction-local answers (FR-18, FR-67)."""

from __future__ import annotations

import hashlib
import itertools
import json
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from rheo_finance import decision, queue_store
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.decision import DecisionKind as K
from rheo_finance.derive import pair_decisions, presumed, stage15, stage15_apply
from rheo_finance.derive.stage4 import cents
from rheo_finance.operations import OperationError, OperationSpec, register
from rheo_finance.storage import refs
from rheo_finance.storage.ids import derived_id
from rheo_finance.tier import Tier

P = Tier.PUBLIC
R = Tier.RESTRICTED
KINDS = (
    "pairing",
    "held_notice",
    "card_debit",
    "payment",
    "presumed_paid",
    "reattach",
    "category",
)


class Choice(BaseModel):
    model_config = ConfigDict(frozen=True)
    answer: Annotated[str, Tiered(P)]
    label: Annotated[str, Tiered(R)]


class Item(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: Annotated[str, Tiered(P)]
    kind: Annotated[str, Tiered(P)]
    subject: Annotated[uuid.UUID, Tiered(P)]
    amount_at_stake: Annotated[Decimal, Tiered(R)]
    evidence: Annotated[tuple[str, ...], Tiered(R)]
    recommendation: Annotated[str | None, Tiered(R)]
    choices: Annotated[tuple[Choice, ...], Tiered(P)]


class ListIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    orphans: bool = False
    counts_only: bool = False


class Count(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Annotated[str, Tiered(P)]
    count: Annotated[int, Tiered(P)]


class ListOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: Annotated[list[Item], Tiered(P)]
    counts: Annotated[list[Count], Tiered(P)]
    total: Annotated[int, Tiered(P)]
    batch_size: Annotated[int, Tiered(P)]


class AnswerOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    decision_ids: Annotated[tuple[uuid.UUID, ...], Tiered(P)]
    recorded: Annotated[int, Tiered(P)]
    targets: Annotated[int, Tiered(P)]


class AnswerIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str = Field(min_length=1)
    answer: str = Field(min_length=1)


@dataclass(frozen=True)
class Prepared:
    item: Item
    answers: dict[str, tuple[decision.RecordIn, ...]]
    category_target: str | None = None


def _ref(kind: str, key: uuid.UUID) -> str:
    return refs.format_ref("finance", kind, key)


def _json(value: BaseModel) -> str:
    shown = value.model_dump(mode="json", exclude_none=True)
    # Notes and source merchant keys are not safe display text. A digest still pins
    # the selected classification unit without printing any historical raw key.
    shown.pop("note", None)
    merchant = shown.pop("merchant_key", None)
    if merchant is not None:
        shown["merchant_digest"] = hashlib.sha256(str(merchant).encode()).hexdigest()[
            :8
        ]
    return json.dumps(shown, sort_keys=True, separators=(",", ":"))


def _item(
    kind: str,
    subject: uuid.UUID,
    amount: Decimal,
    evidence: list[str],
    answers: Mapping[str, decision.RecordIn | tuple[decision.RecordIn, ...]],
    recommendation: str | None,
    category_target: str | None = None,
) -> Prepared:
    # Choice labels show the safe projection of recorded fields, so the digest pins
    # and recommendation shown, including replacement targets and stored fields.
    actions = {
        key: value if isinstance(value, tuple) else (value,)
        for key, value in answers.items()
    }
    choices = tuple(
        Choice(answer=key, label="; ".join(_json(row) for row in value))
        for key, value in actions.items()
    )
    item = Item(
        code="",
        kind=kind,
        subject=subject,
        amount_at_stake=amount,
        evidence=tuple(evidence),
        recommendation=recommendation,
        choices=choices,
    )
    shown = json.dumps(
        item.model_dump(mode="json", exclude={"code", "subject"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(shown.encode()).hexdigest()[:8]
    code = f"{subject.hex[-12:]}-{digest}"
    return Prepared(item.model_copy(update={"code": code}), actions, category_target)


def _line(facts: queue_store.Facts, key: uuid.UUID) -> tuple[str, str, Decimal]:
    row = facts.lines[key]
    ref = _ref("txn", facts.keys[key])
    return ref, f"{ref} {_json(row)}", abs(row.cad_amount or Decimal(0))


def _canonical(facts: queue_store.Facts, ref: str | None) -> str | None:
    if ref is None:
        return None
    parsed = refs.parse(ref)
    if parsed.system == "finance" and parsed.type == "txn":
        line = facts.aliases.get(uuid.UUID(parsed.id))
        if line in facts.keys:
            assert line is not None
            return _ref("txn", facts.keys[line])
    return ref


def _decided_notice(facts: queue_store.Facts, notice: str, money: str) -> bool:
    for row in facts.decisions:
        if row.kind not in (K.PAIR, K.UNPAIR):
            continue
        ends = {
            _canonical(facts, row.target_ref),
            _canonical(facts, row.counterpart_ref),
        }
        if ends == {notice, money} or (row.kind is K.PAIR and notice in ends):
            return True
    return False


def _pairings(
    facts: queue_store.Facts, decided: pair_decisions.Decided
) -> list[Prepared]:
    items = []
    for key, kind, a, b in facts.edges:
        if a not in facts.keys or b not in facts.keys:
            continue
        ar, text_a, stake = _line(facts, a)
        br, text_b, _ = _line(facts, b)
        if not decided.allows((a, b)) or frozenset((a, b)) in decided.joined:
            continue
        items.append(
            _item(
                "pairing",
                key,
                stake,
                [f"Pair these lines? {kind}", text_a, text_b],
                {
                    "yes": decision.RecordIn(
                        kind=K.PAIR, target_ref=ar, counterpart_ref=br
                    ),
                    "no": decision.RecordIn(
                        kind=K.UNPAIR, target_ref=ar, counterpart_ref=br
                    ),
                },
                "yes",
            )
        )
    return items


def _payments(
    ctx: FinanceContext, uow: UnitOfWork, facts: queue_store.Facts
) -> list[Prepared]:
    items: list[Prepared] = []
    evidence = {
        paid.id: [each for each in facts.evidence if each.payment_id == paid.id]
        for paid in facts.payments
    }
    rules = stage15_apply.Rules(
        ctx.settings.get_decimal(stage15.TOLERANCE_KEY),
        ctx.settings.get_int(stage15_apply.EARLY_DAYS_KEY),
        frozenset(ctx.settings.get_str_list(stage15_apply.PARTIAL_KINDS_KEY)),
    )
    for paid in facts.payments:
        seen = evidence[paid.id]
        if paid.state == stage15.CONFIRM_ONLY and paid.source == stage15.NOTICE_ONLY:
            notices = [each for each in seen if each.line_key_id is not None]
            for notice in notices:
                assert notice.line_key_id is not None
                nr = _ref(stage15.NOTICE_KIND, notice.line_key_id)
                for other in facts.payments:
                    if (
                        other.source != stage15.MONEY
                        or other.payee_id != paid.payee_id
                        or other.currency != paid.currency
                        or abs(other.amount - paid.amount) > rules.tolerance
                        or abs((other.paid_on - paid.paid_on).days)
                        > ctx.settings.get_int(stage15.HOLD_KEY)
                    ):
                        continue
                    for money in evidence[other.id]:
                        if (
                            money.role != stage15.MONEY
                            or money.txn_id not in facts.keys
                        ):
                            continue
                        assert money.txn_id is not None
                        mr, text, stake = _line(facts, money.txn_id)
                        if _decided_notice(facts, nr, mr):
                            continue
                        key = derived_id(
                            paid.paid_on,
                            ("held_notice", notice.line_key_id, money.txn_id),
                        )
                        items.append(
                            _item(
                                "held_notice",
                                key,
                                stake,
                                [
                                    "Same payment as this line?",
                                    _json(paid),
                                    _json(notice),
                                    _json(other),
                                    text,
                                ],
                                {
                                    "yes": decision.RecordIn(
                                        kind=K.PAIR, target_ref=nr, counterpart_ref=mr
                                    ),
                                    "no": decision.RecordIn(
                                        kind=K.UNPAIR, target_ref=nr, counterpart_ref=mr
                                    ),
                                },
                                "yes",
                            )
                        )
            continue
        card = paid.state == stage15.CONFIRM_ONLY and paid.source == stage15.MONEY
        if not card and paid.id not in facts.confirm_payments:
            continue
        first = next(
            (each for each in seen if each.role == stage15.MONEY),
            seen[0] if seen else None,
        )
        if first is None:
            continue
        if first.txn_id in facts.keys:
            assert first.txn_id is not None
            target, _, stake = _line(facts, first.txn_id)
        elif first.line_key_id is not None:
            target = _ref(stage15.NOTICE_KIND, first.line_key_id)
            stake = paid.amount if paid.currency == "CAD" else Decimal(0)
        else:
            continue
        if any(
            row.kind in (K.PAYMENT_APPLY, K.PAYMENT_UNAPPLY)
            and _canonical(facts, row.target_ref) == target
            for row in facts.decisions
        ):
            continue
        targets = stage15_apply.confirmation_targets(uow, paid.id, rules)
        answers = {
            str(index): decision.RecordIn(
                kind=K.PAYMENT_APPLY,
                target_ref=target,
                obligation_ref=_ref("obligation", key),
            )
            for index, key in enumerate(targets, 1)
        }
        if len(targets) == 1:
            answers["yes"] = answers.pop("1")
        answers["no"] = decision.RecordIn(kind=K.PAYMENT_UNAPPLY, target_ref=target)
        if card and not targets:
            answers["yes"] = decision.RecordIn(
                kind=K.PAYMENT_UNAPPLY, target_ref=target
            )
        lines = [
            "Payment to this card?" if card else "Apply this payment?",
            _json(paid),
        ]
        for each in seen:
            lines.append(_json(each))
            if each.txn_id in facts.keys:
                assert each.txn_id is not None
                lines.append(_line(facts, each.txn_id)[1])
        lines.extend(_json(facts.obligations[key]) for key in targets)
        items.append(
            _item(
                "card_debit" if card else "payment",
                paid.id,
                stake,
                lines,
                answers,
                "yes" if len(targets) == 1 or (card and not targets) else None,
            )
        )
    return items


def _orphans(ctx: FinanceContext, facts: queue_store.Facts) -> list[Prepared]:
    items = []
    originals = {row.id: row for row in facts.decisions}
    for key, candidates in facts.orphans.items():
        original = originals.get(key)
        if original is None:
            continue
        snapshots = [row for row in facts.snapshots if row.decision_id == key]
        if not snapshots:
            continue
        options: list[list[tuple[str, uuid.UUID]]] = []
        for snapshot in snapshots:
            found = [
                (snapshot.target_ref, candidate)
                for candidate in sorted(candidates)
                if facts.lines[candidate].account_slug == snapshot.account_slug
                and facts.lines[candidate].currency == snapshot.currency
                and cents(facts.lines[candidate].amount) == cents(snapshot.amount)
                and abs(
                    (facts.lines[candidate].posting_date - snapshot.posting_date).days
                )
                <= ctx.settings.get_int("finance.import.overlap_day_tolerance")
            ]
            # A surviving original leg is not itself an orphan candidate.
            old = uuid.UUID(refs.parse(snapshot.target_ref).id)
            survivor = facts.aliases.get(old)
            if survivor is not None and survivor not in candidates:
                found.append((snapshot.target_ref, survivor))
            options.append(found)
        for choice in itertools.product(*options):
            if len({candidate for _, candidate in choice}) != len(choice):
                continue
            replacements = {
                old: _line(facts, candidate)[0] for old, candidate in choice
            }
            values = {
                column: getattr(original, column)
                for column in decision.MATRIX_COLUMNS
                if getattr(original, column) is not None
                and column not in ("bulk_id", "about_decision_id")
            }
            for column in ("target_ref", "counterpart_ref"):
                if column in values:
                    values[column] = replacements.get(
                        str(values[column]), values[column]
                    )
            if original.merchant_key is not None:
                merchants = {
                    facts.lines[candidate].merchant_key for _, candidate in choice
                }
                if len(merchants) != 1 or None in merchants:
                    continue
                values["merchant_key"] = next(iter(merchants))
                values["newest_line_ref"] = replacements[snapshots[0].target_ref]
            values.update(
                kind=original.kind,
                about_decision_id=key,
                legs=[
                    leg.model_copy(
                        update={
                            "target_ref": replacements.get(
                                leg.target_ref, leg.target_ref
                            )
                        }
                    )
                    for leg in facts.legs.get(key, [])
                ],
            )
            accepted = decision.RecordIn.model_validate(values)
            subject = derived_id(
                original.decided_at.date(),
                (
                    "reattach",
                    key,
                    *(part for pair in sorted(replacements.items()) for part in pair),
                ),
            )
            lines = [
                "Re-attach this decision?",
                _json(original),
                *(_json(row) for row in snapshots),
                *(
                    _json(row)
                    for row in facts.decisions
                    if row.about_decision_id == key
                ),
            ]
            lines.extend(_line(facts, candidate)[1] for _, candidate in choice)
            stake = max(
                (_line(facts, candidate)[2] for _, candidate in choice),
                default=Decimal(0),
            )
            items.append(
                _item(
                    "reattach",
                    subject,
                    stake,
                    lines,
                    {
                        "yes": accepted,
                        "no": tuple(
                            decision.RecordIn(
                                kind=K.REATTACH_DECLINE,
                                about_decision_id=key,
                                target_ref=_line(facts, candidate)[0],
                            )
                            for _, candidate in choice
                            if candidate in candidates
                        ),
                    },
                    "yes",
                )
            )
    return items


def compute(
    ctx: FinanceContext, uow: UnitOfWork, *, orphans: bool = False
) -> list[Prepared]:
    facts = queue_store.read(uow)
    found = _orphans(ctx, facts)
    if not orphans:
        found.extend(_pairings(facts, pair_decisions.read(uow)))
        found.extend(_payments(ctx, uow, facts))
        today = ctx.clock.now_utc().astimezone(ctx.settings.timezone).date()
        for candidate in presumed.presumed_paid_candidates(uow, today):
            if any(
                row.kind is K.PRESUMED_PAID
                and row.obligation_ref == _ref("obligation", candidate.obligation_id)
                for row in facts.decisions
            ):
                continue
            evidence = [
                "Presume this obligation paid?",
                _json(facts.obligations[candidate.obligation_id]),
                "Coverage gaps: " + ", ".join(candidate.gaps),
            ]
            found.append(
                _item(
                    "presumed_paid",
                    candidate.obligation_id,
                    candidate.remaining,
                    evidence,
                    {
                        "yes": decision.RecordIn(
                            kind=K.PRESUMED_PAID,
                            obligation_ref=_ref("obligation", candidate.obligation_id),
                        )
                    },
                    "yes",
                )
            )
        for line in facts.lines.values():
            if (
                line.merchant_key
                != f"etransfer:{'in' if line.amount > 0 else 'out'}:{line.id}"
            ):
                continue
            if (
                line.exclusion is not None
                or line.orphan_candidate
                or line.category_basis_class == "owner"
                or line.id not in facts.keys
            ):
                continue
            target, evidence_line, stake = _line(facts, line.id)
            if any(
                row.kind is K.CATEGORY
                and _canonical(facts, row.target_ref) == target
                and row.category is not None
                for row in facts.decisions
            ):
                continue
            found.append(
                _item(
                    "category",
                    line.id,
                    stake,
                    ["Category for this e-Transfer line?", evidence_line],
                    {},
                    None,
                    target,
                )
            )
    return sorted(
        found,
        key=lambda each: (
            -each.item.amount_at_stake,
            KINDS.index(each.item.kind),
            each.item.code,
        ),
    )


def list_items(ctx: FinanceContext, uow: UnitOfWork, data: ListIn) -> ListOut:
    found = compute(ctx, uow, orphans=data.orphans)
    counts = Counter(each.item.kind for each in found)
    return ListOut(
        items=[] if data.counts_only else [each.item for each in found],
        counts=[Count(kind=kind, count=counts[kind]) for kind in KINDS],
        total=len(found),
        batch_size=min(10, max(1, ctx.settings.get_int("finance.classify.batch_size"))),
    )


def answer(ctx: FinanceContext, uow: UnitOfWork, data: AnswerIn) -> AnswerOut:
    # The fresh computation and the write share dispatch's own transaction. A stale
    # code or an invalid choice raises before record, and the caller never derives.
    matches = [each for each in compute(ctx, uow) if each.item.code == data.code]
    if len(matches) != 1:
        raise OperationError("item changed, run confirm again")
    found = matches[0]
    payloads: tuple[decision.RecordIn, ...]
    if found.category_target is not None:
        category = data.answer.strip()
        if not category or category.lower() in ("yes", "no"):
            raise OperationError("enter a category for this line")
        payloads = (
            decision.RecordIn(
                kind=K.CATEGORY, target_ref=found.category_target, category=category
            ),
        )
    else:
        chosen = found.answers.get(data.answer.lower().strip())
        if chosen is None:
            raise OperationError("choose one of the displayed answers")
        payloads = chosen
    recorded = [decision.record(ctx, uow, payload) for payload in payloads]
    return AnswerOut(
        decision_ids=tuple(each.decision_id for each in recorded),
        recorded=len(recorded),
        targets=sum(each.targets for each in recorded),
    )


register(
    OperationSpec(
        name="finance.queue.list",
        input_model=ListIn,
        output_model=ListOut,
        handler=list_items,
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description="Compute confirm items and counts from current derived evidence.",
    )
)
register(
    OperationSpec(
        name="finance.confirm.answer",
        input_model=AnswerIn,
        output_model=AnswerOut,
        handler=answer,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER}),
        connection="owner",
        description="Recheck a confirm code and record its answer in one transaction.",
    )
)
