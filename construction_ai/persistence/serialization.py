"""One JSON encoding shared by every store backend.

SQLite and PostgreSQL previously encoded payloads differently — SQLite passed
`default=str` and PostgreSQL passed nothing at all, so any payload carrying a
datetime (every approval packet does, via evidence timestamps) raised on the
production backend and only on the production backend. Backends that disagree
about encoding are backends that disagree about state.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set | frozenset):
        return sorted(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def to_jsonable(value: Any) -> Any:
    """Unwrap dataclasses and packet objects into plain structures."""
    if hasattr(value, "serializable"):
        return value.serializable()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), sort_keys=True, default=json_default)
