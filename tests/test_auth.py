from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from javes_main.adapters.system import ContentIdGenerator, FixedClock
from javes_main.application import AuthenticationError, AuthorizationError, AuthService
from javes_main.domain import UserRole
from javes_main.persistence import InMemoryRepository


NOW = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)


def service():
    repository = InMemoryRepository()
    clock = FixedClock(NOW)
    auth = AuthService(repository=repository, clock=clock, ids=ContentIdGenerator())
    return auth, repository, clock


def test_internal_iam_lifecycle_permissions_and_session_revocation():
    auth, repository, _ = service()
    root = auth.bootstrap_root("Root", "root-password-123")
    with pytest.raises(ValueError, match="already bootstrapped"):
        auth.bootstrap_root("other", "other-password-123")

    root_login = auth.authenticate("ROOT", "root-password-123")
    root_actor = auth.authenticate_token(root_login.access_token)
    operator = auth.create_user(
        root_actor,
        username="operator",
        password="temporary-password-123",
        role=UserRole.OPERATOR,
    )
    operator_login = auth.authenticate("operator", "temporary-password-123")
    assert operator_login.actor.must_change_password is True
    assert operator_login.actor.allows("case.review")
    assert not operator_login.actor.allows("case.delete")

    auth.change_own_password(
        operator_login.actor,
        current_password="temporary-password-123",
        new_password="permanent-password-456",
    )
    with pytest.raises(AuthenticationError, match="session"):
        auth.authenticate_token(operator_login.access_token)
    assert auth.authenticate("operator", "permanent-password-456").actor.must_change_password is False

    with pytest.raises(AuthorizationError, match="own role"):
        auth.set_role(root_actor, root.user_id, UserRole.ADMIN)
    with pytest.raises(AuthorizationError, match="own account"):
        auth.disable_user(root_actor, root.user_id)

    viewer = auth.create_user(
        root_actor,
        username="viewer",
        password="temporary-password-789",
        role=UserRole.VIEWER,
    )
    disabled = auth.disable_user(root_actor, viewer.user_id)
    assert disabled.active is False
    with pytest.raises(AuthenticationError, match="disabled"):
        auth.authenticate("viewer", "temporary-password-789")
    enabled = auth.enable_user(root_actor, viewer.user_id)
    assert enabled.active is True
    assert auth.authenticate("viewer", "temporary-password-789").actor.role == UserRole.VIEWER
    reset = auth.reset_password(
        root_actor,
        operator.user_id,
        temporary_password="reset-password-999",
    )
    assert reset.must_change_password is True
    assert auth.authenticate("operator", "reset-password-999").actor.must_change_password
    assert any(
        event.event_type == "IAM_USER_DISABLED"
        for event in repository.list_events("__iam__")
    )


def test_failed_logins_lock_account_until_clock_advances():
    auth, _, clock = service()
    auth.bootstrap_root("root", "root-password-123")
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            auth.authenticate("root", "wrong-password")
    with pytest.raises(AuthenticationError, match="temporarily locked"):
        auth.authenticate("root", "root-password-123")
    clock.set(NOW + timedelta(minutes=16))
    assert auth.authenticate("root", "root-password-123").actor.role == UserRole.ROOT
