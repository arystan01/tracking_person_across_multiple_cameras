from __future__ import annotations

import hashlib
from datetime import datetime, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.set(value)

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock value must be timezone-aware")
        self._value = value.astimezone(timezone.utc)


class ContentIdGenerator:
    def new(self, prefix: str, *parts: object) -> str:
        source = "\x1f".join(str(part) for part in parts)
        return f"{prefix}_{hashlib.sha256(source.encode('utf-8')).hexdigest()[:20]}"
