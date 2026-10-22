"""References between records: ``<system>.<type>:<id>`` (FR-53, FR-56).

The id's shape depends on the system: a UUIDv7 for ``finance.*``, a UUID for the
rheoStream modules (``relationships.party``, ``current.item``), and for any other
system (a Tuttle id) an opaque non-empty string with no whitespace and no colon. UUIDs
are written in canonical lower-case form, so ``str(parse(text)) == text``. An
external ref is plain text: nothing here looks it up.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

FINANCE_SYSTEM = "finance"
RHEOSTREAM_SYSTEMS = frozenset({"relationships", "current"})

_NAME = re.compile(r"[a-z][a-z0-9_]*")


class RefError(ValueError):
    """A string that is not a well-formed reference."""


@dataclass(frozen=True, slots=True)
class Ref:
    system: str
    type: str
    id: str

    def __str__(self) -> str:
        return f"{self.system}.{self.type}:{self.id}"


def _canonical_uuid(text: str, what: str) -> uuid.UUID:
    try:
        value = uuid.UUID(text)
    except ValueError:
        raise RefError(f"{what} id is not a UUID") from None
    if str(value) != text:
        raise RefError(f"{what} id is not a canonical lower-case UUID")
    return value


def _check_id(system: str, ref_id: str) -> None:
    if system == FINANCE_SYSTEM:
        value = _canonical_uuid(ref_id, "a finance")
        # The raw version nibble: UUID.version is None for a non-RFC variant, which
        # would hide the variant check behind the version check.
        if (value.int >> 76) & 0xF != 7:
            raise RefError("a finance id is not a UUIDv7")
        if value.variant != uuid.RFC_4122:
            raise RefError("a finance id does not carry the RFC 9562 variant")
    elif system in RHEOSTREAM_SYSTEMS:
        _canonical_uuid(ref_id, f"a {system}")
    elif not ref_id:
        raise RefError("an external id is empty")
    elif ":" in ref_id or any(char.isspace() for char in ref_id):
        raise RefError("an external id holds whitespace or a colon")


def parse(text: str) -> Ref:
    """Parse ``<system>.<type>:<id>``, refusing an id of the wrong shape for its
    system."""
    head, colon, ref_id = text.partition(":")
    system, dot, ref_type = head.partition(".")
    names_ok = _NAME.fullmatch(system) and _NAME.fullmatch(ref_type)
    if not colon or not dot or not names_ok:
        raise RefError("a reference is <system>.<type>:<id> with lower-case names")
    _check_id(system, ref_id)
    return Ref(system, ref_type, ref_id)


def format_ref(system: str, ref_type: str, ref_id: uuid.UUID | str) -> str:
    """The reference string for one record, checked as ``parse`` checks it."""
    text = f"{system}.{ref_type}:{ref_id}"
    parse(text)
    return text
