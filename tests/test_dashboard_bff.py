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
