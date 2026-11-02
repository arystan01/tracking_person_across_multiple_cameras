from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from javes_contracts import (
    ArtifactRef,
    CaseCommandBody,
    CaseWindow,
    CreateCaseBody,
    FakeObservation,
    LastKnown,
    LiveFeedUpdate,
    ReviewDecisionBody,
)
from javes_fakes import FakeClipWorker, FakePerceptionWorker, FakeRetentionWorker
from javes_main import assemble_system, build_in_memory_system
from javes_main.adapters import (
    FakeClipAdapter,
    FakePerceptionAdapter,
    FakeRetentionAdapter,
    FileArtifactStore,
    InMemoryLiveFeedStore,
    InMemorySiteConfig,
)
from javes_main.adapters.system import FixedClock
from javes_main.api import create_app
from javes_main.domain import JobState
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository
from routing.contracts import CameraDef, EdgeDef, MapDef, MapFloor, SiteGraph
from routing.contracts.params import RoutingParams


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


async def login(client: httpx.AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


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

        created = await client.post(
            "/v1/cases",
            json=body(operator_id).model_dump(mode="json"),
            headers=headers("create-1", operator_token),
        )
        assert created.status_code == 201, created.text
        view = created.json()
        assert view["case_id"] == "api-case"
        assert view["best_hypothesis"]["visits"][-1]["camera_id"] == "B"
        assert view["clips"][0]["state"] == "HOLD"

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

        replay = await client.post(
            "/v1/cases",
            json=body(operator_id).model_dump(mode="json"),
            headers=headers("create-1", operator_token),
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
async def test_worker_result_endpoint_requires_token_and_is_idempotent():
    system = make_system()
    transport = httpx.ASGITransport(
        app=create_app(
            system.manager,
            auth=system.auth,
            scheduler=system.scheduler,
            run_fake_jobs_inline=False,
            worker_token="secret",
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
        response = await client.post(
            "/v1/cases",
            json=body(system.root_user.user_id).model_dump(mode="json"),
            headers=headers("create-worker", root_token),
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
            headers={"x-worker-token": "secret"},
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
            worker_token="worker-secret",
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root_token = await login(client, "root", "root-password-for-tests")
        created = await client.post(
            "/v1/cases",
            json=body(system.root_user.user_id).model_dump(mode="json"),
            headers=headers("live-create", root_token),
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
            headers={"x-worker-token": "worker-secret"},
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
        created = await client.post(
            "/v1/cases",
            json=body(root.user_id).model_dump(mode="json"),
            headers=headers("sql-create", token),
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
