from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import BaseModel, ConfigDict, field_validator


class DashboardSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    access_token: str
    user_id: str
    csrf_token: str
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session expiry must be timezone-aware")
        return value.astimezone(timezone.utc)


class SessionCodec:
    def __init__(self, current_key: str, previous_key: str | None = None) -> None:
        try:
            keys = [Fernet(current_key.encode("ascii"))]
            if previous_key:
                keys.append(Fernet(previous_key.encode("ascii")))
        except (ValueError, TypeError) as exc:
            raise ValueError("Dashboard session keys must be Fernet keys") from exc
        self._fernet = MultiFernet(keys)

    def encode(self, value: DashboardSession) -> str:
        payload = json.dumps(
            value.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decode(self, value: str) -> DashboardSession:
        try:
            payload = self._fernet.decrypt(value.encode("ascii"))
            session = DashboardSession.model_validate_json(payload)
        except (InvalidToken, ValueError, TypeError) as exc:
            raise ValueError("invalid Dashboard session") from exc
        if session.expires_at <= datetime.now(timezone.utc):
            raise ValueError("expired Dashboard session")
        return session

    @staticmethod
    def new_csrf_token() -> str:
        return secrets.token_urlsafe(32)
