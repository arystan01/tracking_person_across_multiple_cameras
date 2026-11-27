from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from javes_dashboard import DashboardSettings, create_dashboard_app
from javes_dashboard.session import DashboardSession, SessionCodec
from javes_main.api import create_app
from tests.test_controls_api import BASE, make_system, reference_jpeg


UTC = timezone.utc


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _controls_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health/ready":
        return httpx.Response(200, json={"status": "ready"})
    if request.url.path == "/v1/auth/login":
        return httpx.Response(
            200,
            json={
                "access_token": "controls-secret-token",
                "token_type": "bearer",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "user": {
                    "user_id": "operator-1",
                    "username": "operator",
                    "role": "OPERATOR",
                    "permissions": ["case.create", "case.read", "case.review"],
                    "must_change_password": False,
                },
            },
        )
    if request.headers.get("authorization") != "Bearer controls-secret-token":
        return httpx.Response(401, json={"detail": "missing bearer"})
    if request.url.path == "/v1/auth/me":
        return httpx.Response(
            200,
            json={
                "user_id": "operator-1",
                "username": "operator",
                "role": "OPERATOR",
                "permissions": ["case.create", "case.read", "case.review"],
                "must_change_password": False,
            },
        )
    if request.url.path.endswith("/reference-image"):
        return httpx.Response(
            201,
            json={
                "upload_id": "reference-upload-1",
                "case_id": "case-1",
                "reference_image_ref": {
                    "object_key": "cases/case-1/reference/private.jpg",
                    "sha256": "a" * 64,
                },
                "mime_type": "image/jpeg",
                "size_bytes": 12,
                "width_px": 2,
                "height_px": 3,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    if request.url.path == "/v1/cases" and request.method == "POST":
        payload = json.loads(request.content)
        assert payload["operator_id"] == "operator-1"
        assert payload["reference_upload_id"] == "reference-upload-1"
        assert "reference_image_ref" not in payload
        return httpx.Response(
            201,
            json={
                "case_id": "case-1",
                "revision": 0,
                "route_revision": 0,
                "lifecycle": "PREPARING",
                "done": False,
                "candidates": [],
                "rendered_route_maps": [],
                "search_progress": {},
                "watch": [],
                "unobserved_windows": [],
                "errors": [],
            },
        )
    if request.url.path.endswith("/routes/1/1F.svg"):
        return httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"})
    return httpx.Response(404, json={"detail": "not found"})


def _settings(tmp_path: Path, *, production: bool = False) -> DashboardSettings:
    (tmp_path / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")
    return DashboardSettings(
        controls_url="http://controls.test",
        session_key=Fernet.generate_key().decode(),
        environment="production" if production else "development",
        static_root=tmp_path,
        require_https=production,
    )


@pytest.mark.anyio
async def test_bff_keeps_bearer_private_and_enforces_csrf(tmp_path: Path):
    controls = httpx.AsyncClient(
        base_url="http://controls.test", transport=httpx.MockTransport(_controls_handler)
    )
    app = create_dashboard_app(_settings(tmp_path), controls_client=controls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test"
    ) as client:
        login = await client.post(
            "/dashboard-api/session/login",
            json={"username": "operator", "password": "valid-password"},
        )
        assert login.status_code == 200
        assert "controls-secret-token" not in login.text
        assert "HttpOnly" in login.headers["set-cookie"]
        csrf = login.json()["csrf_token"]

        rejected = await client.post(
            "/dashboard-api/controls/v1/cases",
            json={"case_id": "case-1"},
        )
        assert rejected.status_code == 403

        created = await client.post(
            "/dashboard-api/controls/v1/cases",
            headers={"x-javes-csrf": csrf, "idempotency-key": "create-1"},
            json={
                "case_id": "case-1",
                "target_id": "target-1",
                "reference_upload_id": "reference-upload-1",
            },
        )
        assert created.status_code == 201
        assert "operator_id" not in created.json()
    await controls.aclose()


@pytest.mark.anyio
async def test_bff_strips_artifact_identity_and_streams_media(tmp_path: Path):
    controls = httpx.AsyncClient(
        base_url="http://controls.test", transport=httpx.MockTransport(_controls_handler)
    )
    app = create_dashboard_app(_settings(tmp_path), controls_client=controls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test"
    ) as client:
        login = await client.post(
            "/dashboard-api/session/login", json={"username": "operator", "password": "pw"}
        )
        csrf = login.json()["csrf_token"]
        upload = await client.post(
            "/dashboard-api/controls/v1/cases/case-1/reference-image",
            headers={"x-javes-csrf": csrf, "idempotency-key": "upload-1"},
            files={"file": ("reference.jpg", b"jpeg", "image/jpeg")},
        )
        assert upload.status_code == 201
        assert upload.json()["upload_id"] == "reference-upload-1"
        assert "reference_image_ref" not in upload.json()
        assert "object_key" not in upload.text

        media = await client.get(
            "/dashboard-api/controls/v1/cases/case-1/routes/1/1F.svg"
        )
        assert media.status_code == 200
        assert media.content == b"<svg/>"
        assert media.headers["cache-control"] == "private, no-store"

        denied = await client.get("/dashboard-api/controls/v1/internal/secrets")
        assert denied.status_code == 404
    await controls.aclose()


def test_session_key_rotation_and_production_cookie(tmp_path: Path):
    previous = Fernet.generate_key().decode()
    current = Fernet.generate_key().decode()
    session = DashboardSession(
        session_id="session-1",
        access_token="private",
        user_id="operator-1",
        csrf_token="csrf",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    old_value = SessionCodec(previous).encode(session)
    assert SessionCodec(current, previous).decode(old_value).user_id == "operator-1"

    settings = _settings(tmp_path, production=True)
    assert settings.production_cookie


@pytest.mark.anyio
async def test_production_health_bypasses_tls_redirect_and_upstream_errors_are_redacted(
    tmp_path: Path,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/cases":
            return httpx.Response(
                500,
                json={
                    "detail": "ffmpeg failed at /srv/private/cases/case-secret",
                    "code": "INTERNAL_ERROR",
                    "debug": {"token": "secret"},
                },
            )
        return _controls_handler(request)

    controls = httpx.AsyncClient(
        base_url="http://controls.test", transport=httpx.MockTransport(handler)
    )
    app = create_dashboard_app(
        _settings(tmp_path, production=True), controls_client=controls
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/")).status_code == 400
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://dashboard.test"
    ) as client:
        login = await client.post(
            "/dashboard-api/session/login",
            json={"username": "operator", "password": "valid-password"},
        )
        assert "Secure" in login.headers["set-cookie"]
        failed = await client.get("/dashboard-api/controls/v1/cases")
        assert failed.status_code == 500
        assert failed.headers["content-type"].startswith("application/problem+json")
        assert failed.json()["detail"] == "Controls is temporarily unavailable"
        assert "/srv/private" not in failed.text
        assert "secret" not in failed.text
    await controls.aclose()


@pytest.mark.anyio
async def test_real_bff_controls_upload_id_case_creation_flow(tmp_path: Path):
    system = make_system()
    system.clock.set(datetime.now(UTC))
    main = create_app(
        system.manager,
        auth=system.auth,
        scheduler=system.scheduler,
        allowed_site_versions=(("api-site", "v1"),),
    )
    controls = httpx.AsyncClient(
        base_url="http://controls.test", transport=httpx.ASGITransport(app=main)
    )
    dashboard = create_dashboard_app(_settings(tmp_path), controls_client=controls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dashboard),
        base_url="http://dashboard.test",
    ) as client:
        login = await client.post(
            "/dashboard-api/session/login",
            json={"username": "root", "password": "root-password-for-tests"},
        )
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]
        sites = await client.get("/dashboard-api/controls/v1/sites")
        assert sites.status_code == 200
        assert sites.json()[0]["site_version"] == "v1"

        upload = await client.post(
            "/dashboard-api/controls/v1/cases/dashboard-e2e/reference-image",
            headers={"x-javes-csrf": csrf, "idempotency-key": "upload-e2e"},
            files={"file": ("reference.jpg", reference_jpeg(), "image/jpeg")},
        )
        assert upload.status_code == 201, upload.text
        assert "reference_image_ref" not in upload.json()
        assert "object_key" not in upload.text

        created = await client.post(
            "/dashboard-api/controls/v1/cases",
            headers={"x-javes-csrf": csrf, "idempotency-key": "case-e2e"},
            json={
                "case_id": "dashboard-e2e",
                "target_id": "target-e2e",
                "reference_upload_id": upload.json()["upload_id"],
                "last_known": {"camera_id": "A", "time": BASE.isoformat()},
                "window": {
                    "start": BASE.isoformat(),
                    "end": (BASE + timedelta(minutes=5)).isoformat(),
                },
                "site_id": "api-site",
                "site_version": "v1",
                "profile": "ADULT",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["case_id"] == "dashboard-e2e"
        assert "object_key" not in created.text

        view = await client.get(
            "/dashboard-api/controls/v1/cases/dashboard-e2e/view"
        )
        assert view.status_code == 200
        assert view.json()["case_id"] == "dashboard-e2e"
    await controls.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", [httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError])
async def test_bff_retries_interrupted_get_before_returning_a_response(tmp_path, error_type):
    attempts = []

    def handler(request):
        if request.url.path.endswith("/events"):
            attempts.append(request)
            if len(attempts) == 1:
                raise error_type("private transport diagnostic", request=request)
            return httpx.Response(200, json=[{"event_type": "CASE_CLOSED"}])
        return _controls_handler(request)

    async with httpx.AsyncClient(base_url="http://controls.test", transport=httpx.MockTransport(handler)) as controls:
        settings = _settings(tmp_path).model_copy(update={"controls_retry_backoff_s": 0})
        app = create_dashboard_app(settings, controls_client=controls)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test") as client:
            await client.post("/dashboard-api/session/login", json={"username": "operator", "password": "pw"})
            result = await client.get("/dashboard-api/controls/v1/cases/case-1/events")
            assert result.status_code == 200
            assert result.json() == [{"event_type": "CASE_CLOSED"}]
            assert len(attempts) == 2
            assert attempts[0] is not attempts[1]
            assert all(item.headers["authorization"] == "Bearer controls-secret-token" for item in attempts)
            assert "controls-secret-token" not in result.text


@pytest.mark.anyio
@pytest.mark.parametrize("error_type,status,code", [
    (httpx.ReadError, 503, "CONTROLS_UNAVAILABLE"),
    (httpx.ReadTimeout, 504, "CONTROLS_TIMEOUT"),
])
async def test_bff_get_retry_exhaustion_is_bounded_redacted_problem(tmp_path, error_type, status, code):
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path.endswith("/events"):
            attempts += 1
            raise error_type("/private/case-secret token=controls-secret-token", request=request)
        return _controls_handler(request)

    async with httpx.AsyncClient(base_url="http://controls.test", transport=httpx.MockTransport(handler)) as controls:
        app = create_dashboard_app(_settings(tmp_path).model_copy(update={"controls_retry_backoff_s": 0}), controls_client=controls)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test") as client:
            await client.post("/dashboard-api/session/login", json={"username": "operator", "password": "pw"})
            result = await client.get("/dashboard-api/controls/v1/cases/case-1/events")
            assert attempts == 3
            assert result.status_code == status
            assert result.headers["content-type"].startswith("application/problem+json")
            assert result.headers["cache-control"] == "private, no-store"
            assert result.headers["x-content-type-options"] == "nosniff"
            assert result.json()["code"] == code
            assert result.json()["retryable"] is True
            assert "/private" not in result.text and "secret" not in result.text
            assert "set-cookie" not in result.headers  # A transport failure is not a logout.


@pytest.mark.anyio
@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/cases/case-1/commands/close"),
    ("POST", "/v1/cases/case-1/decisions"),
    ("PUT", "/v1/admin/users/user-1/role"),
])
async def test_bff_never_automatically_retries_a_mutation(tmp_path, method, path):
    attempts = []

    def handler(request):
        if request.url.path == path:
            attempts.append(request)
            raise httpx.ReadError("reply lost after commit", request=request)
        return _controls_handler(request)

    async with httpx.AsyncClient(base_url="http://controls.test", transport=httpx.MockTransport(handler)) as controls:
        app = create_dashboard_app(_settings(tmp_path), controls_client=controls)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test") as client:
            login = await client.post("/dashboard-api/session/login", json={"username": "operator", "password": "pw"})
            result = await client.request(method, "/dashboard-api/controls" + path, json={"command_id": "same-command"},
                headers={"x-javes-csrf": login.json()["csrf_token"], "idempotency-key": "same-action"})
            assert len(attempts) == 1
            assert attempts[0].headers["idempotency-key"] == "same-action"
            assert result.status_code == 503
            assert "action may have completed" in result.json()["detail"]


@pytest.mark.anyio
async def test_bff_get_retries_share_one_total_timeout_budget(tmp_path):
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path.endswith("/events"):
            attempts += 1
            raise httpx.ReadError("connection lost", request=request)
        return _controls_handler(request)

    async with httpx.AsyncClient(base_url="http://controls.test", transport=httpx.MockTransport(handler)) as controls:
        settings = _settings(tmp_path).model_copy(update={"controls_timeout_s": 0.03, "controls_retry_backoff_s": 0.2})
        app = create_dashboard_app(settings, controls_client=controls)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test") as client:
            await client.post("/dashboard-api/session/login", json={"username": "operator", "password": "pw"})
            result = await client.get("/dashboard-api/controls/v1/cases/case-1/events")
            assert result.status_code == 504
            assert result.json()["code"] == "CONTROLS_TIMEOUT"
            assert attempts == 1  # The deadline expires during backoff, not after N full budgets.


@pytest.mark.anyio
async def test_bff_retries_media_only_before_stream_headers(tmp_path):
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path.endswith(".svg"):
            attempts += 1
            if attempts == 1:
                raise httpx.RemoteProtocolError("stale connection", request=request)
            return httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"})
        return _controls_handler(request)

    async with httpx.AsyncClient(base_url="http://controls.test", transport=httpx.MockTransport(handler)) as controls:
        app = create_dashboard_app(_settings(tmp_path).model_copy(update={"controls_retry_backoff_s": 0}), controls_client=controls)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test") as client:
            await client.post("/dashboard-api/session/login", json={"username": "operator", "password": "pw"})
            result = await client.get("/dashboard-api/controls/v1/cases/case-1/routes/1/1F.svg")
            assert attempts == 2
            assert result.status_code == 200 and result.content == b"<svg/>"


def test_dashboard_transport_retry_settings_from_env(monkeypatch):
    monkeypatch.setenv("JAVES_DASHBOARD_CONTROLS_URL", "http://controls.test")
    monkeypatch.setenv("JAVES_DASHBOARD_SESSION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JAVES_DASHBOARD_CONTROLS_GET_RETRIES", "1")
    monkeypatch.setenv("JAVES_DASHBOARD_CONTROLS_RETRY_BACKOFF_S", "0.2")
    settings = DashboardSettings.from_env()
    assert settings.controls_get_retries == 1
    assert settings.controls_retry_backoff_s == 0.2


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_bff_does_not_retry_http_auth_or_application_failures(tmp_path, status):
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path.endswith("/events"):
            attempts += 1
            return httpx.Response(status, json={"detail": "Request denied"})
        return _controls_handler(request)

    async with httpx.AsyncClient(base_url="http://controls.test", transport=httpx.MockTransport(handler)) as controls:
        app = create_dashboard_app(_settings(tmp_path), controls_client=controls)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test") as client:
            await client.post("/dashboard-api/session/login", json={"username": "operator", "password": "pw"})
            result = await client.get("/dashboard-api/controls/v1/cases/case-1/events")
            assert attempts == 1
            assert result.status_code == status
            assert ("set-cookie" in result.headers) is (status == 401)
