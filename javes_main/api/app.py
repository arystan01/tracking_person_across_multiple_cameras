from __future__ import annotations

import hashlib
import json
import secrets
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from javes_contracts import (
    CaseCommandBody,
    CreateCaseBody,
    LiveFeedUpdate,
    ProbeResult,
    ReviewDecisionBody,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerTaskAck,
    WorkerTaskFailureRequest,
    WorkerTaskResultRequest,
)
from javes_main.application import (
    Actor,
    AuthenticationError,
    AuthorizationError,
    AuthService,
    CaseManager,
    MetricsService,
    LeaseError,
    WorkerTaskService,
)
from javes_main.domain import CaseLifecycle, CaseSummary, CaseView
from javes_main.persistence import RevisionConflict
from javes_main.scheduler import SchedulerRunner

from .auth import (
    BearerAuthenticator,
    ChangePasswordRequest,
    CreateUserRequest,
    LoginRequest,
    LoginResponse,
    ResetPasswordRequest,
    SetRoleRequest,
    actor_view,
    require_permission,
    require_same_actor,
    user_view,
)


async def _idempotency(value: str | None = Header(default=None, alias="idempotency-key")) -> str:
    if not value or not value.strip():
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    value = value.strip()
    if len(value) > 200:
        raise HTTPException(status_code=400, detail="idempotency-key is too long")
    return value


def create_app(
    manager: CaseManager,
    *,
    auth: AuthService,
    scheduler: SchedulerRunner | None = None,
    run_fake_jobs_inline: bool = False,
    worker_token: str | None = None,
    worker_tokens: dict[str, str] | None = None,
    worker_lease_secret: str | None = None,
) -> FastAPI:
    app = FastAPI(title="JAVES-AI Controls", version="1.0.0-alpha")
    current_actor = BearerAuthenticator(auth)
    metrics = MetricsService(manager.repository, manager.clock)
    configured_worker_tokens = dict(worker_tokens or {})
    if worker_token is not None:
        for component in ("perception", "clip", "retention"):
            configured_worker_tokens.setdefault(component, worker_token)
    task_service = WorkerTaskService(
        repository=manager.repository,
        manager=manager,
        clock=manager.clock,
        lease_secret=(
            worker_lease_secret
            or worker_token
            or "development-only-worker-lease-secret"
        ),
    )
    if run_fake_jobs_inline and scheduler is None:
        raise ValueError("run_fake_jobs_inline requires a SchedulerRunner")

    def require_worker(
        component: str,
        x_worker_token: str | None,
        authorization: str | None,
        *,
        allow_service_actor: bool = False,
    ) -> None:
        component_token = configured_worker_tokens.get(component)
        bearer_token = None
        if authorization and authorization.lower().startswith("bearer "):
            bearer_token = authorization[7:].strip()
        authenticated = (
            component_token is not None
            and any(
                candidate is not None
                and secrets.compare_digest(candidate, component_token)
                for candidate in (x_worker_token, bearer_token)
            )
        )
        if not authenticated and authorization and allow_service_actor:
            try:
                service_actor = auth.authenticate_token(
                    BearerAuthenticator.token_from_header(authorization)
                )
                authenticated = service_actor.allows("worker.result.write")
            except (AuthenticationError, HTTPException):
                authenticated = False
        if not authenticated:
            raise HTTPException(status_code=401, detail="valid worker token is required")

    def iam_idempotency(
        key: str, action: str, payload: dict, operation
    ) -> dict:
        storage_key = f"iam:{action}:{key}"
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with auth.repository.transaction():
            auth.repository.lock_idempotency(storage_key)
            previous = auth.repository.get_idempotency(storage_key)
            if previous is not None:
                if not secrets.compare_digest(previous[0], fingerprint):
                    raise ValueError("idempotency key reused with different payload")
                return dict(previous[1])
            response = dict(operation())
            auth.repository.remember_idempotency(storage_key, fingerprint, response)
            return response

    def problem(status: int, title: str, detail: str, problem_type: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            media_type="application/problem+json",
            content={
                "type": f"urn:javes:problem:{problem_type}",
                "title": title,
                "status": status,
                "detail": detail,
            },
        )

    @app.exception_handler(KeyError)
    async def key_error_handler(_, exc: KeyError):
        return problem(404, "Not Found", str(exc), "not-found")

    @app.exception_handler(RevisionConflict)
    async def revision_error_handler(_, exc: RevisionConflict):
        return problem(409, "Revision Conflict", str(exc), "revision-conflict")

    @app.exception_handler(LeaseError)
    async def lease_error_handler(_, exc: LeaseError):
        return problem(409, "Invalid Worker Lease", str(exc), "worker-lease")

    @app.exception_handler(ValueError)
    async def value_error_handler(_, exc: ValueError):
        return problem(409, "Conflict", str(exc), "conflict")

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(_, exc: AuthenticationError):
        return problem(401, "Authentication Required", str(exc), "authentication")

    @app.exception_handler(AuthorizationError)
    async def authorization_error_handler(_, exc: AuthorizationError):
        return problem(403, "Forbidden", str(exc), "authorization")

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException):
        return problem(exc.status_code, "HTTP Error", str(exc.detail), "http")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "urn:javes:problem:validation",
                "title": "Validation Error",
                "status": 422,
                "detail": "request payload is invalid",
                "errors": exc.errors(),
            },
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            manager.repository.list_cases()
            manager.repository.list_users()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="persistence is unavailable") from exc
        return {"status": "ready"}

    @app.get("/internal/v1/metrics")
    async def operational_metrics(actor: Actor = Depends(current_actor)) -> dict:
        require_permission(actor, "audit.read")
        return metrics.snapshot()

    @app.post("/v1/auth/login", response_model=LoginResponse)
    async def login(body: LoginRequest) -> LoginResponse:
        result = auth.authenticate(body.username, body.password)
        return LoginResponse(
            access_token=result.access_token,
            token_type=result.token_type,
            expires_at=result.expires_at,
            user=actor_view(result.actor),
        )

    @app.get("/v1/auth/me")
    async def me(actor: Actor = Depends(current_actor)) -> dict:
        return actor_view(actor)

    @app.post("/v1/auth/logout", status_code=204)
    async def logout(
        authorization: str | None = Header(default=None),
        _: Actor = Depends(current_actor),
    ) -> Response:
        auth.logout(BearerAuthenticator.token_from_header(authorization))
        return Response(status_code=204)

    @app.post("/v1/auth/change-password", status_code=204)
    async def change_password(
        body: ChangePasswordRequest,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> Response:
        iam_idempotency(
            idempotency_key,
            "change-password",
            {"user_id": actor.user_id, "new_password": body.new_password},
            lambda: (
                auth.change_own_password(
                    actor,
                    current_password=body.current_password,
                    new_password=body.new_password,
                )
                or {"user_id": actor.user_id}
            ),
        )
        return Response(status_code=204)

    @app.get("/v1/admin/users")
    async def list_users(actor: Actor = Depends(current_actor)) -> list[dict]:
        require_permission(actor, "user.read")
        return [user_view(item) for item in auth.repository.list_users()]

    @app.get("/v1/admin/audit")
    async def iam_audit(actor: Actor = Depends(current_actor)) -> list[dict]:
        require_permission(actor, "audit.read")
        return [
            item.model_dump(mode="json")
            for item in auth.repository.list_events("__iam__")
        ]

    @app.post("/v1/admin/users", status_code=201)
    async def create_user(
        body: CreateUserRequest,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> dict:
        return iam_idempotency(
            idempotency_key,
            "create-user",
            {
                "actor": actor.user_id,
                "username": body.username,
                "role": body.role.value,
                "temporary_password": body.temporary_password,
            },
            lambda: user_view(
                auth.create_user(
                    actor,
                    username=body.username,
                    password=body.temporary_password,
                    role=body.role,
                )
            ),
        )

    @app.put("/v1/admin/users/{user_id}/role")
    async def set_user_role(
        user_id: str,
        body: SetRoleRequest,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> dict:
        return iam_idempotency(
            idempotency_key,
            "set-role",
            {"actor": actor.user_id, "user_id": user_id, "role": body.role.value},
            lambda: user_view(auth.set_role(actor, user_id, body.role)),
        )

    @app.post("/v1/admin/users/{user_id}/disable")
    async def disable_user(
        user_id: str,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> dict:
        return iam_idempotency(
            idempotency_key,
            "disable-user",
            {"actor": actor.user_id, "user_id": user_id},
            lambda: user_view(auth.disable_user(actor, user_id)),
        )

    @app.post("/v1/admin/users/{user_id}/enable")
    async def enable_user(
        user_id: str,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> dict:
        return iam_idempotency(
            idempotency_key,
            "enable-user",
            {"actor": actor.user_id, "user_id": user_id},
            lambda: user_view(auth.enable_user(actor, user_id)),
        )

    @app.post("/v1/admin/users/{user_id}/reset-password")
    async def reset_user_password(
        user_id: str,
        body: ResetPasswordRequest,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> dict:
        return iam_idempotency(
            idempotency_key,
            "reset-password",
            {
                "actor": actor.user_id,
                "user_id": user_id,
                "temporary_password": body.temporary_password,
            },
            lambda: user_view(
                auth.reset_password(
                    actor, user_id, temporary_password=body.temporary_password
                )
            ),
        )

    @app.post("/v1/cases", response_model=CaseView, status_code=201)
    async def create_case(
        body: CreateCaseBody,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> CaseView:
        require_permission(actor, "case.create")
        require_same_actor(actor, body.operator_id)
        view = manager.create_case(body, idempotency_key=idempotency_key)
        if run_fake_jobs_inline:
            assert scheduler is not None
            scheduler.run_case_once(body.case_id)
            view = manager.get_view(body.case_id)
        return view

    @app.get("/v1/cases/{case_id}")
    async def get_case(case_id: str, actor: Actor = Depends(current_actor)) -> dict:
        require_permission(actor, "case.read")
        return manager.get_case(case_id)

    @app.get("/v1/cases", response_model=list[CaseSummary])
    async def list_cases(
        lifecycle: CaseLifecycle | None = None,
        actor: Actor = Depends(current_actor),
    ) -> list[CaseSummary]:
        require_permission(actor, "case.read")
        return manager.list_cases(lifecycle=lifecycle)

    @app.get("/v1/cases/{case_id}/view", response_model=CaseView)
    async def get_view(case_id: str, actor: Actor = Depends(current_actor)) -> CaseView:
        require_permission(actor, "case.read")
        return manager.get_view(case_id)

    @app.get("/v1/cases/{case_id}/events")
    async def get_events(case_id: str, actor: Actor = Depends(current_actor)) -> list[dict]:
        require_permission(actor, "case.events.read")
        return manager.get_events(case_id)

    @app.get("/v1/cases/{case_id}/clips")
    async def get_clips(case_id: str, actor: Actor = Depends(current_actor)) -> list[dict]:
        require_permission(actor, "case.read")
        return manager.get_clips(case_id)

    @app.get("/v1/cases/{case_id}/live")
    async def live_stream(
        case_id: str, actor: Actor = Depends(current_actor)
    ) -> Response:
        require_permission(actor, "case.read")
        data, mime_type = manager.get_live_stream(case_id)
        return Response(
            content=data,
            media_type=mime_type,
            headers={"cache-control": "no-store"},
        )

    @app.post("/v1/cases/{case_id}/decisions", response_model=CaseView)
    async def decision(
        case_id: str,
        body: ReviewDecisionBody,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> CaseView:
        require_permission(actor, "case.review")
        require_same_actor(actor, body.operator_id)
        view = manager.decide(
            case_id,
            body,
            idempotency_key=idempotency_key,
        )
        if run_fake_jobs_inline:
            assert scheduler is not None
            scheduler.routing.drain()
            scheduler.outbox.drain()
            view = manager.get_view(case_id)
        return view

    def case_command(
        case_id: str,
        expected_action: str,
        body: CaseCommandBody,
        idempotency_key: str,
        actor: Actor,
    ) -> CaseView:
        require_same_actor(actor, body.operator_id)
        if body.action != expected_action:
            raise HTTPException(
                status_code=422,
                detail=f"body action must be {expected_action}",
            )
        permission = {
            "CLOSE": "case.close",
            "EXPORT": "case.export",
            "DELETE": "case.delete",
        }[expected_action]
        require_permission(actor, permission)
        view = manager.command(
            case_id,
            body,
            idempotency_key=idempotency_key,
        )
        if run_fake_jobs_inline:
            assert scheduler is not None
            scheduler.routing.drain()
            scheduler.outbox.drain()
            view = manager.get_view(case_id)
        return view

    @app.post("/v1/cases/{case_id}/commands/close", response_model=CaseView)
    async def close_case(
        case_id: str,
        body: CaseCommandBody,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> CaseView:
        return case_command(case_id, "CLOSE", body, idempotency_key, actor)

    @app.post("/v1/cases/{case_id}/commands/export", response_model=CaseView)
    async def export_case(
        case_id: str,
        body: CaseCommandBody,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> CaseView:
        return case_command(case_id, "EXPORT", body, idempotency_key, actor)

    @app.post("/v1/cases/{case_id}/commands/delete", response_model=CaseView)
    async def delete_case(
        case_id: str,
        body: CaseCommandBody,
        idempotency_key: str = Depends(_idempotency),
        actor: Actor = Depends(current_actor),
    ) -> CaseView:
        return case_command(case_id, "DELETE", body, idempotency_key, actor)

    @app.get("/v1/cases/{case_id}/routes/{revision}/{floor_id}.svg")
    async def route_svg(
        case_id: str,
        revision: int,
        floor_id: str,
        actor: Actor = Depends(current_actor),
    ) -> Response:
        require_permission(actor, "case.read")
        return Response(
            content=manager.get_route_svg(case_id, revision, floor_id),
            media_type="image/svg+xml",
            headers={"cache-control": "private, immutable"},
        )

    @app.post("/internal/v1/jobs/{job_id}/results", response_model=CaseView)
    async def worker_result(
        job_id: str,
        result: ProbeResult,
        x_worker_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> CaseView:
        require_worker(
            "perception",
            x_worker_token,
            authorization,
            allow_service_actor=True,
        )
        view = manager.submit_probe_result(job_id, result)
        if run_fake_jobs_inline:
            assert scheduler is not None
            scheduler.routing.drain()
            scheduler.outbox.drain()
            view = manager.get_view(view.case_id)
        return view

    @app.post("/internal/v1/cases/{case_id}/live", response_model=CaseView)
    async def worker_live_update(
        case_id: str,
        update: LiveFeedUpdate,
        x_worker_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> CaseView:
        require_worker("perception", x_worker_token, authorization)
        if update.case_id != case_id:
            raise HTTPException(
                status_code=422,
                detail="live update case_id does not match path",
            )
        return manager.submit_live_feed(update)

    @app.post(
        "/internal/v1/workers/{component}/tasks/claim",
        response_model=WorkerClaimResponse,
    )
    async def claim_worker_tasks(
        component: str,
        body: WorkerClaimRequest,
        x_worker_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> WorkerClaimResponse:
        require_worker(component, x_worker_token, authorization)
        return task_service.claim(component, body)

    @app.post(
        "/internal/v1/workers/{component}/tasks/{task_id}/heartbeat",
        response_model=WorkerHeartbeatResponse,
    )
    async def heartbeat_worker_task(
        component: str,
        task_id: str,
        body: WorkerHeartbeatRequest,
        x_worker_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> WorkerHeartbeatResponse:
        require_worker(component, x_worker_token, authorization)
        return task_service.heartbeat(component, task_id, body)

    @app.post(
        "/internal/v1/workers/{component}/tasks/{task_id}/result",
        response_model=WorkerTaskAck,
    )
    async def complete_worker_task(
        component: str,
        task_id: str,
        body: WorkerTaskResultRequest,
        x_worker_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> WorkerTaskAck:
        require_worker(component, x_worker_token, authorization)
        return task_service.complete(component, task_id, body)

    @app.post(
        "/internal/v1/workers/{component}/tasks/{task_id}/failure",
        response_model=WorkerTaskAck,
    )
    async def fail_worker_task(
        component: str,
        task_id: str,
        body: WorkerTaskFailureRequest,
        x_worker_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> WorkerTaskAck:
        require_worker(component, x_worker_token, authorization)
        return task_service.fail(component, task_id, body)

    return app
