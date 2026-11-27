from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
import pytest
from PIL import Image

from javes_contracts import (
    ArtifactRef,
    CaseCommandBody,
    CaseWindow,
    CreateCaseBody,
    CreateLegalHoldBody,
    LastKnown,
    LiveFeedUpdate,
    ReviewDecisionBody,
    RevokeDecisionBody,
    ReleaseLegalHoldBody,
)
from javes_main.adapters import (
    FileArtifactStore,
    InMemoryLiveFeedStore,
    InMemorySiteConfig,
)
from javes_main.adapters.system import FixedClock
from javes_main.api import create_app
from javes_main.domain import JobState, ROLE_PERMISSIONS, UserRole
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository
from javes_contracts import CameraDef, EdgeDef, MapDef, MapFloor, SiteGraph
from javes_routing.core.params import RoutingParams
from tests.support.fake_workers import (
    FakeClipAdapter,
    FakePerceptionAdapter,
    FakeRetentionAdapter,
)
from tests.support.fakes import (
    FakeClipWorker,
    FakeObservation,
    FakePerceptionWorker,
    FakeRetentionWorker,
)
from tests.support.main_system import assemble_system, build_in_memory_system


UTC = timezone.utc
BASE = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_system():
    graph = SiteGraph(
        site_id="api-site",
        version="v1",
        cameras={
            "A": CameraDef(id="A", floor="1F", map_xy=(100, 100)),
            "B": CameraDef(id="B", floor="1F", map_xy=(400, 100)),
        },
        edges=[
            EdgeDef(
                id="A-B",
                source="A",
                target="B",
                t_min=10,
                t_typ=20,
                t_max=60,
                map_path=[(100, 100), (400, 100)],
            )
        ],
        map=MapDef(floors=[MapFloor(id="1F", size_px=(640, 360))]),
    )
    return build_in_memory_system(
        graph=graph,
        params=RoutingParams(),
        now=BASE + timedelta(seconds=60),
        observations=[
            FakeObservation(
                observation_id="api-observation",
                camera_id="B",
                t_start=BASE + timedelta(seconds=20),
                t_end=BASE + timedelta(seconds=30),
                appearance_score=0.92,
                clip_ref=ArtifactRef(archive_id="archive-B"),
            )
        ],
    )


@pytest.mark.anyio
async def test_production_http_boundary_tls_rotation_cors_and_trace_headers():
    system = make_system()
    app = create_app(
        system.manager,
        auth=system.auth,
        worker_tokens={"perception": ("current-token", "previous-token")},
        worker_lease_secret="lease-secret-for-test",
        require_https=True,
        cors_origins=("https://dashboard.example",),
    )
    transport = httpx.ASGITransport(app=app, client=("198.51.100.10", 443))
    async with httpx.AsyncClient(transport=transport, base_url="http://main.test") as client:
        rejected = await client.get("/health/ready")
        assert rejected.status_code == 426
        assert rejected.json()["code"] == "HTTPS_REQUIRED"
    async with httpx.AsyncClient(transport=transport, base_url="https://main.test") as client:
        for token in ("current-token", "previous-token"):
            claimed = await client.post(
                "/internal/v1/workers/perception/tasks/claim",
                headers={"authorization": f"Bearer {token}", "x-request-id": "request-001"},
                json={"worker_id": "worker-001", "limit": 1, "lease_seconds": 30},
            )
            assert claimed.status_code == 200
            assert claimed.headers["x-request-id"] == "request-001"
            assert claimed.headers["content-security-policy"].startswith("default-src 'none'")
            assert claimed.headers["traceparent"].startswith("00-")
        denied = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            headers={"authorization": "Bearer retired-token"},
            json={"worker_id": "worker-001"},
        )
        assert denied.status_code == 401
        invalid = await client.post(
            "/v1/auth/login",
            json={"username": "operator", "password": {"token": "must-not-leak"}},
        )
        assert invalid.status_code == 422
        assert "must-not-leak" not in invalid.text
        preflight = await client.options(
            "/v1/cases",
            headers={
                "origin": "https://dashboard.example",
                "access-control-request-method": "POST",
            },
        )
        assert preflight.headers["access-control-allow-origin"] == "https://dashboard.example"
def body(operator_id: str = "operator-1") -> CreateCaseBody:
    return CreateCaseBody(
        case_id="api-case",
        operator_id=operator_id,
        target_id="target-1",
        reference_image_ref=ArtifactRef(object_key="cases/api-case/reference.jpg"),
        last_known=LastKnown(camera_id="A", time=BASE),
        window=CaseWindow(start=BASE, end=BASE + timedelta(minutes=5)),
        site_id="api-site",
        site_version="v1",
    )


def headers(key: str, token: str) -> dict[str, str]:
    return {
        "idempotency-key": key,
        "authorization": f"Bearer {token}",
    }


def reference_jpeg() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 64), (120, 30, 40)).save(output, format="JPEG")
    return output.getvalue()


async def create_case_http(
    client: httpx.AsyncClient,
    request: CreateCaseBody,
    token: str,
    key: str,
) -> httpx.Response:
    uploaded = await client.post(
        f"/v1/cases/{request.case_id}/reference-image",
        headers=headers(f"{key}-reference", token),
        files={"file": ("reference.jpg", reference_jpeg(), "image/jpeg")},
    )
    assert uploaded.status_code == 201, uploaded.text
    request = request.model_copy(
        update={
            "reference_image_ref": ArtifactRef.model_validate(
                uploaded.json()["reference_image_ref"]
            )
        }
    )
    return await client.post(
        "/v1/cases",
        json=request.model_dump(mode="json"),
        headers=headers(key, token),
    )


async def login(client: httpx.AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_dashboard_catalog_case_detail_reference_and_candidate_state():
    system = make_system()
    app = create_app(
        system.manager,
        auth=system.auth,
        scheduler=system.scheduler,
        run_fake_jobs_inline=True,
        allowed_site_versions=(("api-site", "v1"),),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = await login(client, "root", "root-password-for-tests")
        catalog = await client.get(
            "/v1/sites", headers={"authorization": f"Bearer {token}"}
        )
        assert catalog.status_code == 200
        assert catalog.json()[0]["site_version"] == "v1"
        assert [item["camera_id"] for item in catalog.json()[0]["cameras"]] == [
            "A",
            "B",
        ]

        uploaded = await client.post(
            "/v1/cases/api-case/reference-image",
            headers=headers("dashboard-upload", token),
            files={"file": ("reference.jpg", reference_jpeg(), "image/jpeg")},
        )
        request = body(system.root_user.user_id).model_copy(
            update={
                "reference_upload_id": uploaded.json()["upload_id"],
                "reference_image_ref": None,
            }
        )
        created = await client.post(
            "/v1/cases",
            headers=headers("dashboard-create", token),
            json=request.model_dump(mode="json"),
        )
        assert created.status_code == 201, created.text
        detail = await client.get(
            "/v1/cases/api-case",
            headers={"authorization": f"Bearer {token}"},
        )
        assert detail.status_code == 200
        assert detail.json()["target_id"] == "target-1"
        assert detail.json()["reference_preview_url"].endswith("/reference")
        reference = await client.get(
            detail.json()["reference_preview_url"],
            headers={"authorization": f"Bearer {token}"},
        )
        assert reference.status_code == 200
        assert reference.headers["content-type"].startswith("image/jpeg")

        candidate = created.json()["candidates"][0]
        assert candidate["decision_state"] == "UNREVIEWED"
        decision = ReviewDecisionBody(
            decision_id="dashboard-confirm",
            candidate_id=candidate["candidate_id"],
            tracklet_id=candidate["tracklet_id"],
            decision="CONFIRM",
            operator_id=system.root_user.user_id,
            occurred_at=BASE + timedelta(seconds=70),
        )
        confirmed = await client.post(
            "/v1/cases/api-case/decisions",
            headers=headers("dashboard-confirm", token),
            json=decision.model_dump(mode="json"),
        )
        assert confirmed.status_code == 200
        confirmed_candidate = confirmed.json()["candidates"][0]
        assert confirmed_candidate["decision_state"] == "CONFIRMED"
        assert confirmed_candidate["active_decision_id"] == "dashboard-confirm"
        assert confirmed_candidate["clip_state"] == "CONFIRMED"


@pytest.mark.anyio
async def test_controls_http_fake_end_to_end():
    system = make_system()
    transport = httpx.ASGITransport(
        app=create_app(
            system.manager,
            auth=system.auth,
            scheduler=system.scheduler,
            run_fake_jobs_inline=True,
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.get("/v1/cases/api-case/view")).status_code == 401

        root_token = await login(client, "root", "root-password-for-tests")
        created_operator = await client.post(
            "/v1/admin/users",
            json={
                "username": "operator",
                "temporary_password": "temporary-password-1",
                "role": "OPERATOR",
            },
            headers={
                "authorization": f"Bearer {root_token}",
                "idempotency-key": "create-operator",
            },
        )
        assert created_operator.status_code == 201, created_operator.text
        operator_id = created_operator.json()["user_id"]
        temporary_token = await login(client, "operator", "temporary-password-1")
        changed = await client.post(
            "/v1/auth/change-password",
            json={
                "current_password": "temporary-password-1",
                "new_password": "operator-password-2",
            },
            headers={
                "authorization": f"Bearer {temporary_token}",
                "idempotency-key": "change-operator-password",
            },
        )
        assert changed.status_code == 204
        operator_token = await login(client, "operator", "operator-password-2")

        created = await create_case_http(
            client, body(operator_id), operator_token, "create-1"
        )
        assert created.status_code == 201, created.text
        view = created.json()
        assert view["case_id"] == "api-case"
        assert view["best_hypothesis"]["visits"][-1]["camera_id"] == "B"
        assert view["clips"][0]["state"] == "HOLD"
        assert view["estimate"]["speed_range"]["source"] == "FALLBACK"
        assert view["estimate"]["distance_range"] is not None
        assert view["estimate"]["calculated_at"] is not None
        assert view["estimate"]["zones_operator_display"] == "TECHNICAL_ONLY"
        assert view["route_presentation"] is not None
        assert view["route_presentation"]["position_state"] in {
            "UNKNOWN",
            "LAST_SEEN",
        }

        listed = await client.get(
            "/v1/cases?lifecycle=SEARCHING",
            headers={"authorization": f"Bearer {operator_token}"},
        )
        assert listed.status_code == 200
        assert [item["case_id"] for item in listed.json()] == ["api-case"]
        case_events = await client.get(
            "/v1/cases/api-case/events",
            headers={"authorization": f"Bearer {operator_token}"},
        )
        assert case_events.status_code == 200
        assert any(item["event_type"] == "CASE_CREATED" for item in case_events.json())

        replay = await create_case_http(
            client, body(operator_id), operator_token, "create-1"
        )
        assert replay.status_code == 201
        assert replay.json()["revision"] == view["revision"]

        route = view["rendered_route_maps"][0]
        svg = await client.get(
            route["url"], headers={"authorization": f"Bearer {operator_token}"}
        )
        assert svg.status_code == 200
        assert svg.headers["content-type"].startswith("image/svg+xml")
        assert 'data-role="best-route"' in svg.text
        assert 'data-role="estimate"' not in svg.text
        visible_text = " ".join(
            text.strip()
            for text in ET.fromstring(svg.text).itertext()
            if text and text.strip()
        )
        assert "%" not in visible_text

        candidate = view["candidates"][0]
        decision = ReviewDecisionBody(
            decision_id="decision-1",
            candidate_id=candidate["candidate_id"],
            tracklet_id=candidate["tracklet_id"],
            decision="CONFIRM",
            operator_id=operator_id,
            occurred_at=BASE + timedelta(seconds=70),
        )
        confirmed = await client.post(
            "/v1/cases/api-case/decisions",
            json=decision.model_dump(mode="json"),
            headers=headers("decision-1", operator_token),
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["clips"][0]["state"] == "CONFIRMED"

        close = CaseCommandBody(
            command_id="close-1",
            action="CLOSE",
            operator_id=operator_id,
            occurred_at=BASE + timedelta(seconds=75),
        )
        closed = await client.post(
            "/v1/cases/api-case/commands/close",
            json=close.model_dump(mode="json"),
            headers=headers("close-1", operator_token),
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["lifecycle"] == "CLOSED"

        export = CaseCommandBody(
            command_id="export-1",
            action="EXPORT",
            operator_id=operator_id,
            occurred_at=BASE + timedelta(seconds=80),
        )
        exported = await client.post(
            "/v1/cases/api-case/commands/export",
            json=export.model_dump(mode="json"),
            headers=headers("export-1", operator_token),
        )
        assert exported.status_code == 200, exported.text
        assert exported.json()["lifecycle"] == "SAVED"

        metrics = await client.get(
            "/internal/v1/metrics",
            headers={
                "authorization": f"Bearer {root_token}",
                "idempotency-key": "create-service",
            },
        )
        assert metrics.status_code == 200
        assert metrics.json()["routing_runs"] >= 2
        assert metrics.json()["camera_seconds_processed"] > 0
        assert metrics.json()["job_completion_latency"]["count"] >= 1
        assert metrics.json()["routing_trigger_latency"]["count"] >= 1
        assert metrics.json()["routing_trigger_latency"]["p95_s"] >= 0
        assert set(metrics.json()["queue_depth"]) == {"jobs", "outbox", "routing"}
        assert set(metrics.json()["terminal_failures"]) == {"jobs", "outbox", "routing"}
        assert metrics.json()["expired_leases"] >= 0
        assert metrics.json()["deletion_failures"] >= 0

        delete = CaseCommandBody(
            command_id="delete-1",
            action="DELETE",
            operator_id=operator_id,
            occurred_at=BASE + timedelta(seconds=90),
        )
        denied = await client.post(
            "/v1/cases/api-case/commands/delete",
            json=delete.model_dump(mode="json"),
            headers=headers("delete-1", operator_token),
        )
        assert denied.status_code == 403
        assert denied.headers["content-type"].startswith("application/problem+json")
        deleted = await client.post(
            "/v1/cases/api-case/commands/delete",
            json=delete.model_copy(
                update={"operator_id": system.root_user.user_id}
            ).model_dump(mode="json"),
            headers=headers("delete-root", root_token),
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["lifecycle"] == "DELETED"


@pytest.mark.anyio
async def test_controls_decision_history_and_revoke_api():
    system = make_system()
    transport = httpx.ASGITransport(
        app=create_app(
            system.manager,
            auth=system.auth,
            scheduler=system.scheduler,
            run_fake_jobs_inline=True,
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root_token = await login(client, "root", "root-password-for-tests")
        me = await client.get(
            "/v1/auth/me",
            headers={"authorization": f"Bearer {root_token}"},
        )
        root_id = me.json()["user_id"]
        created = await create_case_http(
            client, body(root_id), root_token, "revoke-api-create"
        )
        assert created.status_code == 201, created.text
        candidate = created.json()["candidates"][0]
        decision = ReviewDecisionBody(
            decision_id="api-revoke-decision",
            candidate_id=candidate["candidate_id"],
            tracklet_id=candidate["tracklet_id"],
            decision="CONFIRM",
            operator_id=root_id,
            occurred_at=BASE + timedelta(seconds=70),
        )
        confirmed = await client.post(
            "/v1/cases/api-case/decisions",
            json=decision.model_dump(mode="json"),
            headers=headers("api-revoke-decision", root_token),
        )
        assert confirmed.status_code == 200, confirmed.text
        history = await client.get(
            "/v1/cases/api-case/decisions",
            headers={"authorization": f"Bearer {root_token}"},
        )
        assert history.status_code == 200
        assert history.json()[0]["active"] is True

        revoke = RevokeDecisionBody(
            revocation_id="api-revocation",
            decision_id=decision.decision_id,
            operator_id=root_id,
            reason="wrong person selected",
            occurred_at=BASE + timedelta(seconds=75),
            expected_case_revision=confirmed.json()["revision"],
        )
        revoked = await client.post(
            f"/v1/cases/api-case/decisions/{decision.decision_id}/revoke",
            json=revoke.model_dump(mode="json"),
            headers=headers("api-revocation", root_token),
        )
        assert revoked.status_code == 200, revoked.text
        history = await client.get(
            "/v1/cases/api-case/decisions",
            headers={"authorization": f"Bearer {root_token}"},
        )
        assert history.json()[0]["active"] is False
        assert history.json()[0]["revocation_id"] == "api-revocation"

        duplicate = await client.post(
            f"/v1/cases/api-case/decisions/{decision.decision_id}/revoke",
            json=revoke.model_dump(mode="json"),
            headers=headers("api-revocation", root_token),
        )
        assert duplicate.status_code == 200


@pytest.mark.anyio
async def test_controls_legal_hold_is_root_only_ticketed_and_idempotent():
    assert "case.legal_hold.manage" not in ROLE_PERMISSIONS[UserRole.ADMIN]
    assert "case.legal_hold.manage" not in ROLE_PERMISSIONS[UserRole.OPERATOR]
    system = make_system()
    transport = httpx.ASGITransport(
        app=create_app(
            system.manager,
            auth=system.auth,
            scheduler=system.scheduler,
            run_fake_jobs_inline=True,
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root_token = await login(client, "root", "root-password-for-tests")
        me = await client.get(
            "/v1/auth/me", headers={"authorization": f"Bearer {root_token}"}
        )
        root_id = me.json()["user_id"]
        created = await create_case_http(
            client, body(root_id), root_token, "legal-api-create"
        )
        assert created.status_code == 201
        hold = CreateLegalHoldBody(
            hold_id="api-hold",
            external_ticket_id="LEGAL-9001",
            reason="court preservation request",
            operator_id=root_id,
            occurred_at=BASE + timedelta(seconds=70),
            expected_case_revision=created.json()["revision"],
        )
        held = await client.post(
            "/v1/cases/api-case/legal-holds",
            json=hold.model_dump(mode="json"),
            headers=headers("api-hold", root_token),
        )
        assert held.status_code == 201, held.text
        assert held.json()["active"] is True
        assert held.json()["external_ticket_id"] == "LEGAL-9001"
        listed = await client.get(
            "/v1/cases/api-case/legal-holds",
            headers={"authorization": f"Bearer {root_token}"},
        )
        assert listed.status_code == 200
        assert [item["hold_id"] for item in listed.json()] == ["api-hold"]

        current = await client.get(
            "/v1/cases/api-case/view",
            headers={"authorization": f"Bearer {root_token}"},
        )
        release = ReleaseLegalHoldBody(
            release_id="api-hold-release",
            hold_id="api-hold",
            external_ticket_id="LEGAL-9001",
            reason="court preservation request closed",
            operator_id=root_id,
            occurred_at=BASE + timedelta(seconds=80),
            expected_case_revision=current.json()["revision"],
        )
        released = await client.post(
            "/v1/cases/api-case/legal-holds/api-hold/release",
            json=release.model_dump(mode="json"),
            headers=headers("api-hold-release", root_token),
        )
        assert released.status_code == 200, released.text
        assert released.json()["active"] is False
        assert released.json()["release_pending"] is False
        duplicate = await client.post(
            "/v1/cases/api-case/legal-holds/api-hold/release",
            json=release.model_dump(mode="json"),
            headers=headers("api-hold-release", root_token),
        )
        assert duplicate.status_code == 200


@pytest.mark.anyio
async def test_controls_allows_authorised_review_after_close_without_restarting_search():
    system = make_system()
    transport = httpx.ASGITransport(
        app=create_app(
            system.manager,
            auth=system.auth,
            scheduler=system.scheduler,
            run_fake_jobs_inline=True,
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root_token = await login(client, "root", "root-password-for-tests")

        async def create_user(username: str, role: str) -> tuple[str, str]:
            temporary_password = f"{username}-temporary-password"
            created_user = await client.post(
                "/v1/admin/users",
                json={
                    "username": username,
                    "temporary_password": temporary_password,
                    "role": role,
                },
                headers=headers(f"create-{username}", root_token),
            )
            assert created_user.status_code == 201, created_user.text
            user_id = created_user.json()["user_id"]
            temporary_token = await login(client, username, temporary_password)
            password = f"{username}-permanent-password"
            changed = await client.post(
                "/v1/auth/change-password",
                json={
                    "current_password": temporary_password,
                    "new_password": password,
                },
                headers=headers(f"change-{username}-password", temporary_token),
            )
            assert changed.status_code == 204, changed.text
            return user_id, await login(client, username, password)

        operator_id, operator_token = await create_user("reviewer", "OPERATOR")
        viewer_id, viewer_token = await create_user("observer", "VIEWER")
        created = await create_case_http(
            client, body(operator_id), operator_token, "closed-review-create"
        )
        assert created.status_code == 201, created.text
        candidate = created.json()["candidates"][0]

        closed = await client.post(
            "/v1/cases/api-case/commands/close",
            json=CaseCommandBody(
                command_id="closed-review-close",
                action="CLOSE",
                operator_id=operator_id,
                occurred_at=BASE + timedelta(seconds=70),
            ).model_dump(mode="json"),
            headers=headers("closed-review-close", operator_token),
        )
        assert closed.status_code == 200, closed.text
        closed_view = closed.json()
        assert closed_view["lifecycle"] == "CLOSED"
        assert closed_view["candidates"]
        route_revision = closed_view["route_revision"]
        job_ids = [item.job_id for item in system.repository.list_jobs("api-case")]

        viewer_read = await client.get(
            "/v1/cases/api-case/view",
            headers={"authorization": f"Bearer {viewer_token}"},
        )
        assert viewer_read.status_code == 200
        assert viewer_read.json()["candidates"]
        viewer_decision = ReviewDecisionBody(
            decision_id="viewer-cannot-review",
            candidate_id=candidate["candidate_id"],
            tracklet_id=candidate["tracklet_id"],
            decision="CONFIRM",
            operator_id=viewer_id,
            occurred_at=BASE + timedelta(seconds=80),
        )
        forbidden = await client.post(
            "/v1/cases/api-case/decisions",
            json=viewer_decision.model_dump(mode="json"),
            headers=headers("viewer-cannot-review", viewer_token),
        )
        assert forbidden.status_code == 403

        operator_decision = viewer_decision.model_copy(
            update={
                "decision_id": "operator-closed-review",
                "operator_id": operator_id,
            }
        )
        reviewed = await client.post(
            "/v1/cases/api-case/decisions",
            json=operator_decision.model_dump(mode="json"),
            headers=headers("operator-closed-review", operator_token),
        )
        assert reviewed.status_code == 200, reviewed.text
        reviewed_view = reviewed.json()
        assert reviewed_view["lifecycle"] == "CLOSED"
        assert reviewed_view["route_revision"] > route_revision
        assert reviewed_view["mode"] == "PAST_SUMMARY"
        assert reviewed_view["stop_reason"] == "OPERATOR_CLOSED"
        assert reviewed_view["candidates"]
        assert [item.job_id for item in system.repository.list_jobs("api-case")] == job_ids

        replay = await client.post(
            "/v1/cases/api-case/decisions",
            json=operator_decision.model_dump(mode="json"),
            headers=headers("operator-closed-review", operator_token),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["route_revision"] == reviewed_view["route_revision"]


@pytest.mark.anyio
async def test_worker_result_endpoint_requires_token_and_is_idempotent():
    system = make_system()
    transport = httpx.ASGITransport(
        app=create_app(
            system.manager,
            auth=system.auth,
            scheduler=system.scheduler,
            run_fake_jobs_inline=False,
            worker_tokens={"perception": "secret"},
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root_token = await login(client, "root", "root-password-for-tests")
        service_user = await client.post(
            "/v1/admin/users",
            json={
                "username": "perception-service",
                "temporary_password": "service-password-123",
                "role": "SERVICE",
            },
            headers={
                "authorization": f"Bearer {root_token}",
                "idempotency-key": "create-service",
            },
        )
        assert service_user.status_code == 201
        service_token = await login(
            client, "perception-service", "service-password-123"
        )
        response = await create_case_http(
            client,
            body(system.root_user.user_id),
            root_token,
            "create-worker",
        )
        assert response.status_code == 201
        system.outbox_dispatcher.drain()
        system.routing_dispatcher.drain()
        job = system.repository.list_jobs("api-case", states={JobState.PENDING.value})[0]
        result = system.perception_worker.process_probe(job.payload, now=system.clock.now())
        payload = result.model_dump(mode="json")

        denied = await client.post(
            f"/internal/v1/jobs/{job.job_id}/results", json=payload
        )
        assert denied.status_code == 401
        accepted = await client.post(
            f"/internal/v1/jobs/{job.job_id}/results",
            json=payload,
            headers={"authorization": f"Bearer {service_token}"},
        )
        assert accepted.status_code == 200, accepted.text
        revision = accepted.json()["revision"]
        replay = await client.post(
            f"/internal/v1/jobs/{job.job_id}/results",
            json=payload,
            headers={"authorization": "Bearer secret"},
        )
        assert replay.status_code == 200
        assert replay.json()["revision"] == revision


@pytest.mark.anyio
async def test_live_feed_metadata_is_worker_only_and_stream_is_case_authorised():
    system = make_system()
    transport = httpx.ASGITransport(
        app=create_app(
            system.manager,
            auth=system.auth,
            scheduler=system.scheduler,
            worker_tokens={"perception": "worker-secret"},
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root_token = await login(client, "root", "root-password-for-tests")
        created = await create_case_http(
            client,
            body(system.root_user.user_id),
            root_token,
            "live-create",
        )
        assert created.status_code == 201
        system.outbox_dispatcher.drain()
        system.live_feed_store.put("live:api-case:B", b"fake-jpeg", "image/jpeg")
        update = LiveFeedUpdate(
            update_id="live-update-1",
            case_id="api-case",
            camera_id="B",
            tracklet_id="tracklet-live-1",
            bbox=(10, 20, 30, 40),
            observed_at=system.clock.now(),
            stream_ref="live:api-case:B",
            expires_at=system.clock.now() + timedelta(seconds=10),
        )
        denied = await client.post(
            "/internal/v1/cases/api-case/live",
            json=update.model_dump(mode="json"),
        )
        assert denied.status_code == 401
        accepted = await client.post(
            "/internal/v1/cases/api-case/live",
            json=update.model_dump(mode="json"),
            headers={"authorization": "Bearer worker-secret"},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["live"]["stream_url"] == "/v1/cases/api-case/live"
        assert accepted.json()["live"]["bbox"] == [10.0, 20.0, 30.0, 40.0]

        stream = await client.get(
            "/v1/cases/api-case/live",
            headers={"authorization": f"Bearer {root_token}"},
        )
        assert stream.status_code == 200
        assert stream.content == b"fake-jpeg"
        assert stream.headers["cache-control"] == "no-store"

        system.clock.set(system.clock.now() + timedelta(seconds=11))
        expired = await client.get(
            "/v1/cases/api-case/live",
            headers={"authorization": f"Bearer {root_token}"},
        )
        assert expired.status_code == 404


@pytest.mark.anyio
async def test_controls_http_with_sql_survives_application_restart(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'controls.sqlite'}"
    repository = SqlAlchemyRepository.from_url(database_url)
    repository.create_schema()
    sites = InMemorySiteConfig(RoutingParams())
    template = make_system()
    sites.add(template.sites.get_graph("api-site", "v1"))
    observations = [
        FakeObservation(
            observation_id="sql-observation",
            camera_id="B",
            t_start=BASE + timedelta(seconds=20),
            t_end=BASE + timedelta(seconds=30),
            appearance_score=0.92,
            clip_ref=ArtifactRef(archive_id="sql-archive-B"),
        )
    ]

    def assembled(repo):
        return assemble_system(
            repository=repo,
            artifacts=FileArtifactStore(tmp_path / "artifacts"),
            live_feed=InMemoryLiveFeedStore(),
            sites=sites,
            perception=FakePerceptionAdapter(FakePerceptionWorker(observations)),
            clip=FakeClipAdapter(FakeClipWorker()),
            retention=FakeRetentionAdapter(FakeRetentionWorker()),
            clock=FixedClock(BASE + timedelta(seconds=60)),
        )

    first = assembled(repository)
    root = first.auth.bootstrap_root("root", "root-password-123")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(
                first.manager,
                auth=first.auth,
                scheduler=first.scheduler,
                run_fake_jobs_inline=True,
            )
        ),
        base_url="http://test",
    ) as client:
        token = await login(client, "root", "root-password-123")
        created = await create_case_http(
            client, body(root.user_id), token, "sql-create"
        )
        assert created.status_code == 201, created.text
        expected = created.json()

    restarted = assembled(SqlAlchemyRepository.from_url(database_url))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(
                restarted.manager,
                auth=restarted.auth,
                scheduler=restarted.scheduler,
            )
        ),
        base_url="http://test",
    ) as client:
        token = await login(client, "root", "root-password-123")
        loaded = await client.get(
            "/v1/cases/api-case/view",
            headers={"authorization": f"Bearer {token}"},
        )
        assert loaded.status_code == 200
        assert loaded.json() == expected
