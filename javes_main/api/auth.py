"""FastAPI authentication DTOs and bearer-session dependency."""

from __future__ import annotations

from datetime import datetime

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from javes_main.application import Actor, AuthenticationError, AuthService
from javes_main.domain import UserAccount, UserRole


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(ApiModel):
    username: str
    password: str


class LoginResponse(ApiModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: dict


class ChangePasswordRequest(ApiModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=1024)


class CreateUserRequest(ApiModel):
    username: str = Field(min_length=3, max_length=128)
    temporary_password: str = Field(min_length=12, max_length=1024)
    role: UserRole


class SetRoleRequest(ApiModel):
    role: UserRole


class ResetPasswordRequest(ApiModel):
    temporary_password: str = Field(min_length=12, max_length=1024)


def user_view(user: UserAccount) -> dict:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "role": user.role.value,
        "active": user.active,
        "must_change_password": user.must_change_password,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
        "disabled_at": user.disabled_at.isoformat() if user.disabled_at else None,
    }


def actor_view(actor: Actor) -> dict:
    return {
        "user_id": actor.user_id,
        "username": actor.username,
        "role": actor.role.value,
        "permissions": sorted(actor.permissions),
        "must_change_password": actor.must_change_password,
    }


class BearerAuthenticator:
    def __init__(self, service: AuthService) -> None:
        self.service = service

    @staticmethod
    def token_from_header(authorization: str | None) -> str:
        if not authorization:
            raise HTTPException(status_code=401, detail="bearer session is required")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="invalid authorization header")
        return token

    async def __call__(
        self, authorization: str | None = Header(default=None)
    ) -> Actor:
        token = self.token_from_header(authorization)
        try:
            return self.service.authenticate_token(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_permission(actor: Actor, permission: str) -> None:
    if actor.must_change_password:
        raise HTTPException(status_code=403, detail="password change is required")
    if not actor.allows(permission):
        raise HTTPException(status_code=403, detail=f"permission {permission} is required")


def require_same_actor(actor: Actor, operator_id: str) -> None:
    if actor.user_id != operator_id:
        raise HTTPException(status_code=403, detail="operator does not match authenticated user")
