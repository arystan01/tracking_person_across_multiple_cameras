from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone


class RotationConfigurationError(ValueError):
    pass


def secret_fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RotationConfigurationError(
            "rotation overlap deadline must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RotationWindow:
    current: str
    previous: str | None = None
    previous_valid_until: datetime | None = None

    @classmethod
    def from_strings(
        cls,
        current: str,
        previous: str | None = None,
        previous_valid_until: str | datetime | None = None,
    ) -> RotationWindow:
        parsed = previous_valid_until
        if isinstance(parsed, str):
            parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
        result = cls(current, previous, parsed)
        result.validate()
        return result

    def validate(self) -> None:
        if not self.current:
            raise RotationConfigurationError("current secret is required")
        if self.previous is None and self.previous_valid_until is not None:
            raise RotationConfigurationError(
                "rotation deadline is invalid without a previous secret"
            )
        if self.previous is not None:
            if self.previous == self.current:
                raise RotationConfigurationError(
                    "current and previous secrets must differ"
                )
            if self.previous_valid_until is None:
                raise RotationConfigurationError(
                    "previous secret requires a bounded overlap deadline"
                )
            _utc(self.previous_valid_until)

    def accepted(self, *, now: datetime | None = None) -> tuple[str, ...]:
        self.validate()
        values = [self.current]
        current_time = now or datetime.now(timezone.utc)
        if (
            self.previous is not None
            and self.previous_valid_until is not None
            and _utc(self.previous_valid_until) > _utc(current_time)
        ):
            values.append(self.previous)
        return tuple(values)

    def require_ready(self, *, now: datetime | None = None) -> None:
        self.validate()
        current_time = now or datetime.now(timezone.utc)
        if (
            self.previous is not None
            and self.previous_valid_until is not None
            and _utc(self.previous_valid_until) <= _utc(current_time)
        ):
            raise RotationConfigurationError(
                "previous secret overlap has expired; retire the previous secret"
            )

    def status(self, *, now: datetime | None = None) -> dict[str, object]:
        accepted = self.accepted(now=now)
        return {
            "current_key_id": secret_fingerprint(self.current),
            "previous_key_id": (
                secret_fingerprint(self.previous) if self.previous is not None else None
            ),
            "previous_valid_until": (
                _utc(self.previous_valid_until).isoformat()
                if self.previous_valid_until is not None
                else None
            ),
            "previous_accepted": len(accepted) == 2,
        }
