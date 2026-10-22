"""The sensitivity tier of a stored value or output field (FR-46).

A leaf module: it imports nothing from the package, so settings, storage and the CLI
can all use this one enum without pulling each other in.
"""

from __future__ import annotations

from enum import StrEnum


class Tier(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
