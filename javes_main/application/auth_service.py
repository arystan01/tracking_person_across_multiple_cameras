"""Internal IAM: pre-created users, Argon2id passwords and opaque sessions."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

from javes_main.domain import (
    ROLE_PERMISSIONS,
    AuditEvent,
    AuthSession,
    UserAccount,
    UserRole,
)
from javes_main.ports import CaseRepository, Clock, IdGenerator


class AuthenticationError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class Actor:
    user_id: str
    username: str
    role: UserRole
    permissions: frozenset[str]
    must_change_password: bool = False

    def allows(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    token_type: str
    expires_at: datetime
    actor: Actor


class AuthService:
    def __init__(
        self,
        *,
        repository: CaseRepository,
        clock: Clock,
        ids: IdGenerator,
        session_ttl_s: int = 8 * 60 * 60,
        max_failed_logins: int = 5,
        lockout_s: int = 15 * 60,
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock
        self.ids = ids
        self.session_ttl_s = session_ttl_s
        self.max_failed_logins = max_failed_logins
        self.lockout_s = lockout_s
        self.password_hasher = password_hasher or PasswordHasher()
        self._dummy_hash = self.password_hasher.hash("not-a-real-javes-password")

    @staticmethod
    def _normalise_username(username: str) -> str:
        value = username.strip().casefold()
        if len(value) < 3 or len(value) > 128:
            raise ValueError("username must contain 3 to 128 characters")
        return value

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        if len(password) > 1024:
            raise ValueError("password is too long")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _audit(self, event_type: str, actor_id: str | None, payload: dict) -> None:
        now = self.clock.now()
        self.repository.append_event(
            AuditEvent(
                event_id=self.ids.new(
                    "iam_evt", event_type, actor_id or "system", now.isoformat(), payload
                ),
                case_id="__iam__",
                event_type=event_type,
                actor_id=actor_id,
                occurred_at=now,
                payload=payload,
            )
        )

    def bootstrap_root(self, username: str, password: str) -> UserAccount:
        username = self._normalise_username(username)
        self._validate_password(password)
        now = self.clock.now()
        user = UserAccount(
            user_id=self.ids.new("user", username),
            username=username,
            password_hash=self.password_hasher.hash(password),
            role=UserRole.ROOT,
            must_change_password=False,
            created_at=now,
            updated_at=now,
        )
        with self.repository.transaction():
            self.repository.lock_idempotency("__iam_bootstrap_root__")
            if self.repository.list_users():
                raise ValueError("IAM is already bootstrapped")
            self.repository.add_user(user)
            self._audit("IAM_ROOT_BOOTSTRAPPED", user.user_id, {"user_id": user.user_id})
        return user

    @staticmethod
    def actor_for(user: UserAccount) -> Actor:
        return Actor(
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            permissions=ROLE_PERMISSIONS[user.role],
            must_change_password=user.must_change_password,
        )

    @staticmethod
    def require(actor: Actor, permission: str) -> None:
        if not actor.allows(permission):
            raise AuthorizationError(f"permission {permission!r} is required")

    def create_user(
        self,
        actor: Actor,
        *,
        username: str,
        password: str,
        role: UserRole,
    ) -> UserAccount:
        self.require(actor, "user.create")
        username = self._normalise_username(username)
        self._validate_password(password)
        now = self.clock.now()
        user = UserAccount(
            user_id=self.ids.new("user", username),
            username=username,
            password_hash=self.password_hasher.hash(password),
            role=role,
            must_change_password=role != UserRole.SERVICE,
            created_at=now,
            updated_at=now,
        )
        with self.repository.transaction():
            self.repository.add_user(user)
            self._audit(
                "IAM_USER_CREATED",
                actor.user_id,
                {"user_id": user.user_id, "role": role.value},
            )
        return user

    def authenticate(self, username: str, password: str) -> LoginResult:
        now = self.clock.now()
        if len(password) > 1024:
            raise AuthenticationError("invalid username or password")
        try:
            user = self.repository.get_user_by_username(
                self._normalise_username(username)
            )
        except (KeyError, ValueError):
            try:
                self.password_hasher.verify(self._dummy_hash, password)
            except (VerifyMismatchError, InvalidHash):
                pass
            raise AuthenticationError("invalid username or password") from None

        if not user.active:
            raise AuthenticationError("account is disabled")
        if user.locked_until is not None and user.locked_until > now:
            raise AuthenticationError("account is temporarily locked")
        try:
            valid = self.password_hasher.verify(user.password_hash, password)
        except (VerifyMismatchError, InvalidHash):
            valid = False
        if not valid:
            failures = user.failed_login_count + 1
            locked_until = (
                now + timedelta(seconds=self.lockout_s)
                if failures >= self.max_failed_logins
                else None
            )
            with self.repository.transaction():
                self.repository.save_user(
                    user.model_copy(
                        update={
                            "failed_login_count": failures,
                            "locked_until": locked_until,
                            "updated_at": now,
                        }
                    )
                )
                self._audit(
                    "IAM_LOGIN_FAILED",
                    user.user_id,
                    {"locked": bool(locked_until), "failed_login_count": failures},
                )
            raise AuthenticationError("invalid username or password")

        if self.password_hasher.check_needs_rehash(user.password_hash):
            user.password_hash = self.password_hasher.hash(password)
        user.failed_login_count = 0
        user.locked_until = None
        user.updated_at = now
        token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=self.session_ttl_s)
        auth_session = AuthSession(
            session_id=self.ids.new("session", user.user_id, self._token_hash(token)),
            token_hash=self._token_hash(token),
            user_id=user.user_id,
            created_at=now,
            expires_at=expires_at,
        )
        with self.repository.transaction():
            self.repository.save_user(user)
            self.repository.add_session(auth_session)
            self._audit(
                "IAM_LOGIN_SUCCEEDED", user.user_id, {"session_id": auth_session.session_id}
            )
        return LoginResult(
            access_token=token,
            token_type="bearer",
            expires_at=expires_at,
            actor=self.actor_for(user),
        )

    def authenticate_token(self, token: str) -> Actor:
        auth_session = self.repository.get_session_by_token_hash(self._token_hash(token))
        now = self.clock.now()
        if (
            auth_session is None
            or auth_session.revoked_at is not None
            or auth_session.expires_at <= now
        ):
            raise AuthenticationError("invalid or expired session")
        user = self.repository.get_user(auth_session.user_id)
        if not user.active:
            raise AuthenticationError("account is disabled")
        return self.actor_for(user)

    def logout(self, token: str) -> None:
        auth_session = self.repository.get_session_by_token_hash(self._token_hash(token))
        if auth_session is None or auth_session.revoked_at is not None:
            return
        now = self.clock.now()
        with self.repository.transaction():
            self.repository.save_session(auth_session.model_copy(update={"revoked_at": now}))
            self._audit(
                "IAM_LOGOUT", auth_session.user_id, {"session_id": auth_session.session_id}
            )

    def change_own_password(
        self, actor: Actor, *, current_password: str, new_password: str
    ) -> None:
        self._validate_password(new_password)
        user = self.repository.get_user(actor.user_id)
        try:
            valid = self.password_hasher.verify(user.password_hash, current_password)
        except (VerifyMismatchError, InvalidHash):
            valid = False
        if not valid:
            raise AuthenticationError("current password is incorrect")
        now = self.clock.now()
        user.password_hash = self.password_hasher.hash(new_password)
        user.must_change_password = False
        user.updated_at = now
        with self.repository.transaction():
            self.repository.save_user(user)
            self.repository.revoke_user_sessions(user.user_id, now=now)
            self._audit("IAM_PASSWORD_CHANGED", user.user_id, {})

    def set_role(self, actor: Actor, user_id: str, role: UserRole) -> UserAccount:
        self.require(actor, "role.assign")
        if actor.user_id == user_id:
            raise AuthorizationError("users cannot change their own role")
        user = self.repository.get_user(user_id)
        if user.role == UserRole.ROOT and role != UserRole.ROOT:
            self._require_another_root(user_id)
        user.role = role
        user.updated_at = self.clock.now()
        with self.repository.transaction():
            self.repository.save_user(user)
            self.repository.revoke_user_sessions(user_id, now=user.updated_at)
            self._audit(
                "IAM_ROLE_CHANGED",
                actor.user_id,
                {"user_id": user_id, "role": role.value},
            )
        return user

    def reset_password(
        self, actor: Actor, user_id: str, *, temporary_password: str
    ) -> UserAccount:
        self.require(actor, "user.reset_password")
        if actor.user_id == user_id:
            raise AuthorizationError("use change-password for your own account")
        self._validate_password(temporary_password)
        user = self.repository.get_user(user_id)
        now = self.clock.now()
        user.password_hash = self.password_hasher.hash(temporary_password)
        user.must_change_password = user.role != UserRole.SERVICE
        user.failed_login_count = 0
        user.locked_until = None
        user.updated_at = now
        with self.repository.transaction():
            self.repository.save_user(user)
            self.repository.revoke_user_sessions(user_id, now=now)
            self._audit("IAM_PASSWORD_RESET", actor.user_id, {"user_id": user_id})
        return user

    def disable_user(self, actor: Actor, user_id: str) -> UserAccount:
        self.require(actor, "user.disable")
        if actor.user_id == user_id:
            raise AuthorizationError("users cannot disable their own account")
        user = self.repository.get_user(user_id)
        if user.role == UserRole.ROOT:
            self._require_another_root(user_id)
        now = self.clock.now()
        user.active = False
        user.disabled_at = now
        user.updated_at = now
        with self.repository.transaction():
            self.repository.save_user(user)
            self.repository.revoke_user_sessions(user_id, now=now)
            self._audit("IAM_USER_DISABLED", actor.user_id, {"user_id": user_id})
        return user

    def enable_user(self, actor: Actor, user_id: str) -> UserAccount:
        """Reactivate a pre-created account without changing its identity or role."""

        self.require(actor, "user.enable")
        user = self.repository.get_user(user_id)
        now = self.clock.now()
        user.active = True
        user.disabled_at = None
        user.failed_login_count = 0
        user.locked_until = None
        user.updated_at = now
        with self.repository.transaction():
            self.repository.save_user(user)
            self._audit("IAM_USER_ENABLED", actor.user_id, {"user_id": user_id})
        return user

    def _require_another_root(self, excluded_user_id: str) -> None:
        if not any(
            item.user_id != excluded_user_id
            and item.role == UserRole.ROOT
            and item.active
            for item in self.repository.list_users()
        ):
            raise AuthorizationError("the last active ROOT account must be preserved")
