"""The prompt guard through ``ModelClient.call`` (FR-47; AC-48a, AC-9 model part).

Seam: ``call`` on the stub and on ``ClaudeCLIClient`` with the fake executable. A
refused prompt raises ``PromptRefused`` naming its class, and nothing reached the
client: the stub recorded no prompt and the fake ``claude`` never ran. An ordinary
merchant record passes, so a guard that refused everything would fail here too.
Every value is invented.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.model.claude_cli import ClaudeCLIClient
from rheo_finance.model.client import ModelClient
from rheo_finance.model.guard import (
    PromptGuard,
    PromptRefused,
    ProtectedTerms,
    RefusalKind,
    TermsProvider,
    manual_accounts_terms,
)
from rheo_finance.model.stub import StubModelClient
from rheo_finance.settings import default_settings

from .conftest import FakeClaude, Verdict, write_manual

SYSTEM = "Classify the merchant record."
ADMITTED = "merchant sample grocer, -45.00 CAD, 2026-03-15"
REPLY = {"label": "groceries", "confidence": 0.9}
OWNER = "Testa Ownerly"
ACCOUNT_ROW = (
    f"sample-chequing,Sample chequing,Sample Credit Union,chequing,CAD,monthly,{OWNER}"
    ",,,documents,,,,,"
)
IDENTIFIER_ROW = "sample-chequing,SAMPLEACCT-XQ,file_token,,"

# (class, text holding it and no other class)
SHAPES: list[tuple[RefusalKind, str]] = [
    ("digit_run", "merchant sample grocer ref 9991234"),
    ("card_shape", "card 9999 9999 9999 9999 at sample grocer"),
    ("card_shape", "card 9999-9999-9999-9999 at sample grocer"),
    ("card_shape", "card 9999999999999999 at sample grocer"),
    ("card_shape", "card 9999 999999 99999 at sample grocer"),
    ("phone_shape", "call (999) 555-0100 about the order"),
    ("phone_shape", "call 999-555-0100 about the order"),
    ("phone_shape", "call +44 20 9999 0100 about the order"),
    ("phone_shape", "call 555-0100 about the order"),
    ("long_token", "token Q9ZXW8VY7UT6SR5QP4ON3ML2KJ end"),
    ("binary", "merchant\x00sample"),
    ("binary", f"merchant{chr(0xD800)}sample"),
    ("pdf", "%PDF-1.7 sample"),
    ("pdf", "JVBERi0xLjcK"),
    ("image", "\x89PNG\r\n"),
    ("image", "iVBORw0KGgoAAAANSUhEUg"),
    ("image", "data:image/jpeg;base64,/9j/4AAQ"),
    ("image", "receipt /9j/4AAQSkZJRgABAQ"),
]
SHAPE_IDS = [f"{kind}-{index}" for index, (kind, _) in enumerate(SHAPES)]


def stub(*providers: TermsProvider) -> StubModelClient:
    return StubModelClient({"classify": REPLY}, guard=PromptGuard(providers))


def refused(client: ModelClient, text: str, system: str = SYSTEM) -> RefusalKind:
    with pytest.raises(PromptRefused) as caught:
        client.call("classify", system, text, Verdict)
    return caught.value.kind


def test_an_ordinary_record_is_admitted_and_sent_once() -> None:
    client = stub(lambda: ProtectedTerms(owner_names=(OWNER,)))

    out = client.call("classify", SYSTEM, ADMITTED, Verdict)

    assert out == Verdict(label="groceries", confidence=0.9)
    assert [(p.stage, p.system, p.text) for p in client.prompts] == [
        ("classify", SYSTEM, ADMITTED),
    ]  # fmt: skip


@pytest.mark.parametrize(("kind", "text"), SHAPES, ids=SHAPE_IDS)
def test_each_shape_is_refused_before_the_stub_is_called(
    kind: RefusalKind, text: str
) -> None:
    client = stub()
    assert refused(client, text) == kind
    assert client.prompts == []


@pytest.mark.parametrize(("kind", "text"), SHAPES, ids=SHAPE_IDS)
def test_each_shape_is_refused_before_claude_runs(
    fake_claude: FakeClaude, kind: RefusalKind, text: str
) -> None:
    client = ClaudeCLIClient(PromptGuard(), default_settings())
    assert refused(client, text) == kind
    assert fake_claude.runs() == []


def test_the_system_prompt_is_guarded_too() -> None:
    client = stub()
    assert refused(client, ADMITTED, system=f"{SYSTEM} Account 9991234.") == "digit_run"
    assert client.prompts == []


ZERO_WIDTH_SPACE = chr(0x200B)
FULLWIDTH_DIGITS = "".join(chr(0xFF10 + int(digit)) for digit in "9991234")


@pytest.mark.parametrize(
    "text",
    [f"ref 999{ZERO_WIDTH_SPACE}1234", f"ref {FULLWIDTH_DIGITS}"],
    ids=["zero-width-space", "fullwidth-digits"],
)
def test_a_lookalike_or_invisible_break_does_not_hide_a_digit_run(text: str) -> None:
    client = stub()
    assert refused(client, text) == "digit_run"
    assert client.prompts == []


@pytest.mark.parametrize(
    "text",
    [
        "merchant sample grocer, -45.00 CAD, 2026-03-15",
        "dates 2026-03-15 2026-03-16 2026-03-17, amounts 12.50 7.25",
        "invoice SP0042 total 750.00 CAD",
    ],
)
def test_dates_amounts_and_short_codes_are_admitted(text: str) -> None:
    client = stub()
    client.call("classify", SYSTEM, text, Verdict)
    assert len(client.prompts) == 1


# --- terms from providers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "terms", "text"),
    [
        ("identifier", ProtectedTerms(identifiers=("SAMPLEACCT-XQ",)),
         "from sampleacct xq"),
        ("party_alias", ProtectedTerms(party_aliases=("Sample Alias",)),
         "sent to SAMPLE  ALIAS today"),
        ("address", ProtectedTerms(addresses=("0xsampleaddr",)),
         "withdrawal to 0xSampleAddr"),
        ("txid", ProtectedTerms(txids=("sampletxid42",)), "txn sampletxid42 confirmed"),
        ("owner_name", ProtectedTerms(owner_names=(OWNER,)), "from testa ownerly"),
        ("owner_name", ProtectedTerms(owner_names=(OWNER,)), "Ownerly's account"),
    ],
    ids=["identifier", "alias", "address", "txid", "owner", "owner-part"],
)  # fmt: skip
def test_each_provided_term_is_refused_before_the_stub_is_called(
    kind: RefusalKind, terms: ProtectedTerms, text: str
) -> None:
    client = stub(lambda: ProtectedTerms(), lambda: terms)
    assert refused(client, text) == kind
    assert client.prompts == []


def test_a_term_inside_a_longer_word_is_not_refused() -> None:
    """``Sam`` names a part of an owner name only as a whole word."""
    client = stub(lambda: ProtectedTerms(owner_names=("Sam Quell",)))
    client.call("classify", SYSTEM, "merchant sample grocer", Verdict)
    assert len(client.prompts) == 1


def test_providers_are_read_at_each_check() -> None:
    names: list[str] = []
    client = stub(lambda: ProtectedTerms(party_aliases=tuple(names)))
    client.call("classify", SYSTEM, "sent to sample alias", Verdict)
    names.append("Sample Alias")
    assert refused(client, "sent to sample alias") == "party_alias"
    assert len(client.prompts) == 1


# --- the manual accounts provider -----------------------------------------------------


@pytest.fixture
def accounts_guard(tmp_path: Path) -> Callable[..., StubModelClient]:
    def build(
        *account_rows: str, identifier_rows: tuple[str, ...] = ()
    ) -> StubModelClient:
        write_manual(tmp_path, ACCOUNTS_FILE, *account_rows)
        write_manual(tmp_path, IDENTIFIERS_FILE, *identifier_rows)
        return stub(manual_accounts_terms(tmp_path))

    return build


def test_the_owner_column_of_the_accounts_file_is_refused(
    accounts_guard: Callable[..., StubModelClient],
) -> None:
    client = accounts_guard(ACCOUNT_ROW)

    assert refused(client, "e-transfer from Testa Ownerly") == "owner_name"
    assert refused(client, "for testa") == "owner_name"
    client.call("classify", SYSTEM, ADMITTED, Verdict)
    assert [p.text for p in client.prompts] == [ADMITTED]


def test_a_registered_identifier_is_refused(
    accounts_guard: Callable[..., StubModelClient],
) -> None:
    client = accounts_guard(ACCOUNT_ROW, identifier_rows=(IDENTIFIER_ROW,))
    assert refused(client, "statement sampleacct-xq") == "identifier"
    assert client.prompts == []


def test_an_accounts_file_with_a_row_that_fails_refuses_everything(
    accounts_guard: Callable[..., StubModelClient],
) -> None:
    """A row that does not load hides its values from the guard: it fails closed."""
    bad = ACCOUNT_ROW.replace("sample-chequing", "second").replace(
        "chequing,CAD", "nope,CAD"
    )
    client = accounts_guard(ACCOUNT_ROW, bad)
    assert refused(client, ADMITTED) == "terms_unavailable"
    assert client.prompts == []


def test_no_manual_files_means_no_terms(
    tmp_path: Path,
) -> None:
    client = stub(manual_accounts_terms(tmp_path))
    client.call("classify", SYSTEM, "from testa ownerly", Verdict)
    assert len(client.prompts) == 1
