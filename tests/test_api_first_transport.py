from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import httpx
import pytest

from javes_contracts import (
    ArtifactManifest,
    ArtifactRef,
    Candidate,
    CaseCommandBody,
    CaseInfo,
    CaseWindow,
    CreateCaseBody,
    LastKnown,
    PerceptionProbeJob,
    ProbeRequest,
    ProbeResult,
    ProbeStatus,
    RoutingInitRequest,
    RoutingInitResponse,
    RoutingStepRequest,
    RoutingStepResponse,
    TargetGalleryResult,
    Tracklet,
    WorkerTaskEnvelope,
)
from javes_main.api import create_app
from javes_main.adapters.artifacts import InMemoryArtifactStore, InMemoryLiveFeedStore
from javes_main.adapters.http_routing import HttpRoutingAdapter
from javes_main.adapters.remote_only import RemoteWorkerOnlyPort
from javes_main.adapters.system import ContentIdGenerator, FixedClock
from javes_main.application import AuthService, CaseManager
from javes_main.persistence import InMemoryRepository
from javes_main.domain import JobRecord, JobState
from javes_main.scheduler import RoutingTriggerDispatcher
from javes_retention.repository import SQLiteRetentionRepository
from javes_retention.storage import ArtifactStoreDeletionBackend, DeletionRouter
from javes_retention.worker import RetentionWorker
from javes_routing_api import create_routing_app
from javes_site_config import FilesystemSiteConfigRegistry, create_site_config_app
from javes_site_config.client import HttpSiteConfigClient
from routing.step import init_state
from routing.worker import step_and_render
from tests.test_main_infra import BASE, create_body, system


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_worker_pull_lease_result_and_duplicate_callback():
    app_system = system()
    app_system.manager.create_case(create_body(), idempotency_key="api-first-create")
    api = create_app(
        app_system.manager,
        auth=app_system.auth,
        worker_tokens={
            "perception": "perception-secret",
            "clip": "clip-secret",
            "retention": "retention-secret",
        },
        worker_lease_secret="lease-secret-for-tests",
    )
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://main") as client:
        unauthorized = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            json={"worker_id": "p-1"},
        )
        assert unauthorized.status_code == 401

        wrong_component_token = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            headers={"authorization": "Bearer clip-secret"},
            json={"worker_id": "p-1"},
        )
        assert wrong_component_token.status_code == 401

        claimed = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            headers={"authorization": "Bearer perception-secret"},
            json={"worker_id": "p-1", "limit": 1, "lease_seconds": 30},
        )
        assert claimed.status_code == 200
        task = WorkerTaskEnvelope.model_validate(claimed.json()["tasks"][0])
        assert task.operation == "GALLERY_BUILD"

        invalid_lease = await client.post(
            f"/internal/v1/workers/perception/tasks/{task.task_id}/heartbeat",
            headers={"authorization": "Bearer perception-secret"},
            json={"lease_token": task.lease_token + "x"},
        )
        assert invalid_lease.status_code == 409

        heartbeat = await client.post(
            f"/internal/v1/workers/perception/tasks/{task.task_id}/heartbeat",
            headers={"authorization": "Bearer perception-secret"},
            json={"lease_token": task.lease_token, "extend_seconds": 60},
        )
        assert heartbeat.status_code == 200
        lease_token = heartbeat.json()["lease_token"]
        superseded = await client.post(
            f"/internal/v1/workers/perception/tasks/{task.task_id}/heartbeat",
            headers={"authorization": "Bearer perception-secret"},
            json={"lease_token": task.lease_token, "extend_seconds": 60},
        )
        assert superseded.status_code == 409

        gallery_key = "cases/case-1/perception/gallery/gallery.npy"
        gallery_bytes = b"real-gallery-bytes"
        app_system.artifacts.put(gallery_key, gallery_bytes, "application/x-npy")
        result = TargetGalleryResult(
            command_id=task.payload["command_id"],
            case_id="case-1",
            gallery_ref=gallery_key,
            source_count=1,
            model_version="osnet_ain_x1_0_msmt17",
        )
        manifest = ArtifactManifest(
            object_key=gallery_key,
            kind="gallery",
            size_bytes=len(gallery_bytes),
            sha256=hashlib.sha256(gallery_bytes).hexdigest(),
            created_at=BASE + timedelta(seconds=60),
            retention_class="case_14d",
            mime_type="application/x-npy",
        )
        payload = {
            "lease_token": lease_token,
            "result": result.model_dump(mode="json"),
            "artifacts": [manifest.model_dump(mode="json")],
        }
        completed = await client.post(
            f"/internal/v1/workers/perception/tasks/{task.task_id}/result",
            headers={"authorization": "Bearer perception-secret"},
            json=payload,
        )
        assert completed.status_code == 200
        assert completed.json()["duplicate"] is False
        assert app_system.repository.get("case-1").gallery_ref == gallery_key

        duplicate = await client.post(
            f"/internal/v1/workers/perception/tasks/{task.task_id}/result",
            headers={"authorization": "Bearer perception-secret"},
            json=payload,
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True


def _write_registry(root) -> FilesystemSiteConfigRegistry:
    site_dir = root / "sites" / "site-api" / "v1"
    params_dir = root / "routing_params"
    site_dir.mkdir(parents=True)
    params_dir.mkdir(parents=True)
    (site_dir / "site.yaml").write_text(
        """
site: site-api
version: v1
timezone: UTC
cameras:
  A: {zone: lobby}
edges: []
max_gap_s: 2400
""".strip(),
        encoding="utf-8",
    )
    (params_dir / "1.yaml").write_text(
        "version: '1'\nK: 4\nB: 5\n",
        encoding="utf-8",
    )
    return FilesystemSiteConfigRegistry(root)


@pytest.mark.anyio
async def test_site_config_is_exact_immutable_and_routing_is_http_owned(tmp_path):
    registry = _write_registry(tmp_path)
    site_api = create_site_config_app(registry, service_token="site-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=site_api), base_url="http://site"
    ) as client:
        denied = await client.get("/internal/v1/sites/site-api/versions/v1")
        assert denied.status_code == 401
        snapshot = await client.get(
            "/internal/v1/sites/site-api/versions/v1",
            headers={"authorization": "Bearer site-secret"},
        )
        assert snapshot.status_code == 200
        assert snapshot.json()["checksum"]
        missing = await client.get(
            "/internal/v1/sites/site-api/versions/latest",
            headers={"authorization": "Bearer site-secret"},
        )
        assert missing.status_code == 422

    routing_api = create_routing_app(registry, service_token="routing-secret")
    case = CaseInfo(
        case_id="case-routing-api",
        target_id="target",
        start_time=BASE,
        last_known_camera="A",
        last_known_time=BASE,
        window_start=BASE,
        window_end=BASE + timedelta(minutes=5),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=routing_api), base_url="http://routing"
    ) as client:
        init_request = RoutingInitRequest(
            case=case,
            anchors={
                "last_known_camera": "A",
                "last_known_time": BASE,
            },
            site_id="site-api",
            site_version="v1",
            params_version="1",
            now=BASE + timedelta(seconds=1),
        )
        init_response = await client.post(
            "/internal/v1/routing/init",
            headers={"authorization": "Bearer routing-secret"},
            json=init_request.model_dump(mode="json", by_alias=True),
        )
        assert init_response.status_code == 200
        initial = RoutingInitResponse.model_validate(init_response.json())

        step_request = RoutingStepRequest(
            state=initial.state,
            site_id="site-api",
            site_version="v1",
            params_version="1",
            now=BASE + timedelta(seconds=1),
        )
        first = await client.post(
            "/internal/v1/routing/step",
            headers={"authorization": "Bearer routing-secret"},
            json=step_request.model_dump(mode="json", by_alias=True),
        )
        second = await client.post(
            "/internal/v1/routing/step",
            headers={"authorization": "Bearer routing-secret"},
            json=step_request.model_dump(mode="json", by_alias=True),
        )
        assert first.status_code == second.status_code == 200
        one = RoutingStepResponse.model_validate(first.json())
        two = RoutingStepResponse.model_validate(second.json())
        assert one == two
        assert one.site_checksum == initial.site_checksum


@pytest.mark.anyio
async def test_controls_to_worker_to_routing_http_to_dashboard_view(tmp_path):
    root = tmp_path / "registry"
    site_dir = root / "sites" / "pipeline-site" / "v1"
    params_dir = root / "routing_params"
    site_dir.mkdir(parents=True)
    params_dir.mkdir(parents=True)
    (site_dir / "site.yaml").write_text(
        """
site: pipeline-site
version: v1
timezone: UTC
cameras:
  A: {zone: west}
  B: {zone: east}
edges:
  - {id: A-B, source: A, target: B, t_min: 5, t_typ: 10, t_max: 30}
max_gap_s: 2400
""".strip(),
        encoding="utf-8",
    )
    (params_dir / "1.yaml").write_text(
        "version: '1'\nK: 4\nB: 5\n",
        encoding="utf-8",
    )
    registry = FilesystemSiteConfigRegistry(root)

    def site_handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if parts[:3] == ["internal", "v1", "sites"]:
            snapshot = registry.site(parts[3], parts[5])
            return httpx.Response(200, json=snapshot.model_dump(mode="json", by_alias=True))
        if parts[:3] == ["internal", "v1", "routing-params"]:
            params = registry.params(parts[3])
            return httpx.Response(200, json=params.model_dump(mode="json"))
        return httpx.Response(404)

    def routing_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path.endswith("/init"):
            body = RoutingInitRequest.model_validate(payload)
            site = registry.site(body.site_id, body.site_version)
            params_data = registry.params(body.params_version)
            from routing.contracts.params import RoutingParams

            params = RoutingParams.model_validate(params_data.params)
            response = RoutingInitResponse(
                state=init_state(body.case, site.graph, params),
                site_checksum=site.checksum,
                params_checksum=params_data.checksum,
            )
        else:
            body = RoutingStepRequest.model_validate(payload)
            site = registry.site(body.site_id, body.site_version)
            params_data = registry.params(body.params_version)
            from routing.contracts.params import RoutingParams

            params = RoutingParams.model_validate(params_data.params)
            response = RoutingStepResponse(
                result=step_and_render(
                    body.state,
                    body.probe_results,
                    site.graph,
                    body.anchors,
                    params,
                    body.now,
                    {},
                ),
                site_checksum=site.checksum,
                params_checksum=params_data.checksum,
            )
        return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))

    repository = InMemoryRepository()
    artifacts = InMemoryArtifactStore()
    clock = FixedClock(BASE + timedelta(seconds=40))
    ids = ContentIdGenerator()
    sites = HttpSiteConfigClient(
        "http://site",
        "site-secret",
        default_params_version="1",
        client=httpx.Client(transport=httpx.MockTransport(site_handler)),
    )
    routing = HttpRoutingAdapter(
        "http://routing",
        "routing-secret",
        client=httpx.Client(transport=httpx.MockTransport(routing_handler)),
    )
    manager = CaseManager(
        repository=repository,
        artifacts=artifacts,
        sites=sites,
        routing=routing,
        perception=RemoteWorkerOnlyPort("perception"),
        clip=RemoteWorkerOnlyPort("clip"),
        retention=RemoteWorkerOnlyPort("retention"),
        live_feed=InMemoryLiveFeedStore(),
        clock=clock,
        ids=ids,
    )
    auth = AuthService(repository=repository, clock=clock, ids=ids)
    root_user = auth.bootstrap_root("root", "root-password-for-pipeline")
    routing_dispatcher = RoutingTriggerDispatcher(
        repository=repository, manager=manager, clock=clock
    )
    api = create_app(
        manager,
        auth=auth,
        worker_tokens={
            "perception": "perception-secret",
            "clip": "clip-secret",
            "retention": "retention-secret",
        },
        worker_lease_secret="pipeline-lease-secret",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://main"
    ) as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "root", "password": "root-password-for-pipeline"},
        )
        access = login.json()["access_token"]
        user_headers = {
            "authorization": f"Bearer {access}",
            "idempotency-key": "pipeline-create",
        }
        create = await client.post(
            "/v1/cases",
            headers=user_headers,
            json=CreateCaseBody(
                case_id="pipeline-case",
                operator_id=root_user.user_id,
                target_id="target",
                reference_image_ref=ArtifactRef(
                    object_key="cases/pipeline-case/reference.jpg"
                ),
                last_known=LastKnown(camera_id="A", time=BASE),
                window=CaseWindow(
                    start=BASE, end=BASE + timedelta(minutes=5)
                ),
                site_id="pipeline-site",
                site_version="v1",
            ).model_dump(mode="json"),
        )
        assert create.status_code == 201

        worker_headers = {"authorization": "Bearer perception-secret"}
        gallery_claim = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            headers=worker_headers,
            json={"worker_id": "perception-pipeline"},
        )
        gallery_task = WorkerTaskEnvelope.model_validate(
            gallery_claim.json()["tasks"][0]
        )
        gallery_result = TargetGalleryResult(
            command_id=gallery_task.payload["command_id"],
            case_id="pipeline-case",
            gallery_ref="cases/pipeline-case/perception/gallery/g.npy",
            source_count=1,
            model_version="osnet_ain_x1_0_msmt17",
        )
        gallery_bytes = b"pipeline-gallery"
        gallery_key = gallery_result.gallery_ref
        artifacts.put(gallery_key, gallery_bytes, "application/x-npy")
        gallery_manifest = ArtifactManifest(
            object_key=gallery_key,
            kind="gallery",
            size_bytes=len(gallery_bytes),
            sha256=hashlib.sha256(gallery_bytes).hexdigest(),
            created_at=clock.now(),
            retention_class="case_14d",
            mime_type="application/x-npy",
        )
        gallery_done = await client.post(
            f"/internal/v1/workers/perception/tasks/{gallery_task.task_id}/result",
            headers=worker_headers,
            json={
                "lease_token": gallery_task.lease_token,
                "result": gallery_result.model_dump(mode="json"),
                "artifacts": [gallery_manifest.model_dump(mode="json")],
            },
        )
        assert gallery_done.status_code == 200
        assert routing_dispatcher.drain().completed == 1

        probe_claim = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            headers=worker_headers,
            json={"worker_id": "perception-pipeline"},
        )
        probe_task = WorkerTaskEnvelope.model_validate(probe_claim.json()["tasks"][0])
        assert probe_task.operation == "PROBE"
        probe_id = probe_task.payload["probe"]["probe_id"]
        tracklet = Tracklet(
            id="pipeline-tracklet",
            case_id="pipeline-case",
            probe_id=probe_id,
            camera="B",
            t_in=BASE + timedelta(seconds=10),
            t_out=BASE + timedelta(seconds=20),
            bbox_first=(1, 2, 3, 4),
            bbox_last=(2, 3, 3, 4),
            clip_ref={"archive_id": "external-read-only"},
        )
        probe_result = ProbeResult(
            probe_id=probe_id,
            status=ProbeStatus.DONE,
            tracklets=[tracklet],
            candidates=[
                Candidate(
                    id="pipeline-candidate",
                    tracklet_id=tracklet.id,
                    score=0.91,
                )
            ],
            coverage_ratio=1.0,
            processed_duration_s=25,
            n_tracklets_total=1,
        )
        probe_done = await client.post(
            f"/internal/v1/workers/perception/tasks/{probe_task.task_id}/result",
            headers=worker_headers,
            json={
                "lease_token": probe_task.lease_token,
                "result": probe_result.model_dump(mode="json", by_alias=True),
            },
        )
        assert probe_done.status_code == 200
        assert routing_dispatcher.drain().completed == 1

        dashboard_view = await client.get(
            "/v1/cases/pipeline-case/view",
            headers={"authorization": f"Bearer {access}"},
        )
        assert dashboard_view.status_code == 200
        view = dashboard_view.json()
        assert view["best_hypothesis"]["visits"][-1]["camera_id"] == "B"
        assert view["candidates"][0]["candidate_id"] == "pipeline-candidate"
        assert view["rendered_route_maps"]

        clip_claim = await client.post(
            "/internal/v1/workers/clip/tasks/claim",
            headers={"authorization": "Bearer clip-secret"},
            json={"worker_id": "clip-unavailable"},
        )
        clip_task = WorkerTaskEnvelope.model_validate(clip_claim.json()["tasks"][0])
        assert clip_task.operation == "CLIP_CUT"
        unavailable = await client.post(
            f"/internal/v1/workers/clip/tasks/{clip_task.task_id}/failure",
            headers={"authorization": "Bearer clip-secret"},
            json=(failure_payload := {
                "lease_token": clip_task.lease_token,
                "error_code": "CLIP_WORKER_UNAVAILABLE",
                "retryable": False,
            }),
        )
        assert unavailable.status_code == 200
        assert unavailable.json()["duplicate"] is False
        duplicate_failure = await client.post(
            f"/internal/v1/workers/clip/tasks/{clip_task.task_id}/failure",
            headers={"authorization": "Bearer clip-secret"},
            json=failure_payload,
        )
        assert duplicate_failure.status_code == 200
        assert duplicate_failure.json()["duplicate"] is True
        assert duplicate_failure.json()["terminal"] is True
        final_view = await client.get(
            "/v1/cases/pipeline-case/view",
            headers={"authorization": f"Bearer {access}"},
        )
        assert any(
            item.get("source") == "outbox"
            and item.get("state") == "FAILED"
            and "CLIP_WORKER_UNAVAILABLE" in (item.get("error") or "")
            for item in final_view.json()["errors"]
        )


@pytest.mark.anyio
async def test_real_retention_core_claims_and_completes_through_http(tmp_path):
    app_system = system()
    reference_key = "cases/case-1/reference.jpg"
    app_system.artifacts.put(reference_key, b"reference", "image/jpeg")
    app_system.manager.create_case(
        create_body(), idempotency_key="retention-http-create"
    )
    api = create_app(
        app_system.manager,
        auth=app_system.auth,
        worker_tokens={
            "perception": "perception-secret",
            "clip": "clip-secret",
            "retention": "retention-secret",
        },
        worker_lease_secret="retention-http-lease",
    )
    retention = RetentionWorker(
        SQLiteRetentionRepository(tmp_path / "retention.sqlite"),
        DeletionRouter(
            {"object_key": ArtifactStoreDeletionBackend(app_system.artifacts)}
        ),
        clock=app_system.clock.now,
    )
    headers = {"authorization": "Bearer retention-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://main"
    ) as client:
        register_claim = await client.post(
            "/internal/v1/workers/retention/tasks/claim",
            headers=headers,
            json={"worker_id": "retention-real", "limit": 10},
        )
        register_tasks = [
            WorkerTaskEnvelope.model_validate(item)
            for item in register_claim.json()["tasks"]
        ]
        assert [item.operation for item in register_tasks] == ["RETENTION_REGISTER"]
        from javes_contracts import RetentionResource

        for task in register_tasks:
            retention.register(RetentionResource.model_validate(task.payload))
            ack = await client.post(
                f"/internal/v1/workers/retention/tasks/{task.task_id}/result",
                headers=headers,
                json={"lease_token": task.lease_token, "result": None},
            )
            assert ack.status_code == 200

        app_system.manager.command(
            "case-1",
            CaseCommandBody(
                command_id="delete-via-retention-http",
                action="DELETE",
                operator_id="operator-1",
                occurred_at=app_system.clock.now(),
            ),
            idempotency_key="delete-via-retention-http",
        )
        delete_claim = await client.post(
            "/internal/v1/workers/retention/tasks/claim",
            headers=headers,
            json={"worker_id": "retention-real", "limit": 10},
        )
        delete_task = WorkerTaskEnvelope.model_validate(
            delete_claim.json()["tasks"][0]
        )
        assert delete_task.operation == "RETENTION_DELETE"
        from javes_contracts import RetentionCommand

        result = retention.process(
            RetentionCommand.model_validate(delete_task.payload)
        )
        completed = await client.post(
            f"/internal/v1/workers/retention/tasks/{delete_task.task_id}/result",
            headers=headers,
            json={
                "lease_token": delete_task.lease_token,
                "result": result.model_dump(mode="json"),
            },
        )
        assert completed.status_code == 200

    with pytest.raises(KeyError):
        app_system.artifacts.get(reference_key)
    assert app_system.repository.get("case-1").content_redacted
    assert app_system.repository.list_events("case-1")
    retention.close()


@pytest.mark.anyio
async def test_open_job_accepts_multiple_idempotent_updates_under_one_lease():
    app_system = system()
    app_system.manager.create_case(
        create_body(), idempotency_key="open-worker-create"
    )
    app_system.outbox_dispatcher.drain()
    case = app_system.repository.get("case-1")
    assert case.gallery_ref
    now = app_system.clock.now()
    open_job = JobRecord(
        job_id="open-http-job",
        case_id="case-1",
        dedupe_key="open-http-job",
        payload=PerceptionProbeJob(
            job_id="open-http-job",
            case_id="case-1",
            target_gallery_ref=case.gallery_ref,
            probe=ProbeRequest(
                probe_id="open-http-probe",
                camera="B",
                t_from=BASE,
                t_to=None,
            ),
        ),
        created_at=now,
        updated_at=now,
    )
    app_system.repository.add_job(open_job)
    api = create_app(
        app_system.manager,
        auth=app_system.auth,
        worker_tokens={"perception": "perception-secret"},
        worker_lease_secret="open-http-lease-secret",
    )
    headers = {"authorization": "Bearer perception-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://main"
    ) as client:
        claim = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            headers=headers,
            json={"worker_id": "open-worker", "limit": 100},
        )
        tasks = [WorkerTaskEnvelope.model_validate(item) for item in claim.json()["tasks"]]
        task = next(item for item in tasks if item.task_id == "open-http-job")

        first_payload = {
            "lease_token": task.lease_token,
            "result": ProbeResult(
                probe_id="open-http-probe",
                status=ProbeStatus.DONE,
                coverage_ratio=0.5,
                processed_duration_s=5,
                n_tracklets_total=0,
            ).model_dump(mode="json"),
        }
        first = await client.post(
            f"/internal/v1/workers/perception/tasks/{task.task_id}/result",
            headers=headers,
            json=first_payload,
        )
        assert first.status_code == 200 and not first.json()["duplicate"]

        second_payload = {
            "lease_token": task.lease_token,
            "result": ProbeResult(
                probe_id="open-http-probe",
                status=ProbeStatus.DONE,
                coverage_ratio=1.0,
                processed_duration_s=10,
                n_tracklets_total=0,
            ).model_dump(mode="json"),
        }
        second = await client.post(
            f"/internal/v1/workers/perception/tasks/{task.task_id}/result",
            headers=headers,
            json=second_payload,
        )
        assert second.status_code == 200 and not second.json()["duplicate"]
        replay = await client.post(
            f"/internal/v1/workers/perception/tasks/{task.task_id}/result",
            headers=headers,
            json=first_payload,
        )
        assert replay.status_code == 200 and replay.json()["duplicate"]

    stored = app_system.repository.get_job("open-http-job")
    assert stored.state == JobState.LEASED
    assert len(stored.result_fingerprints) == 2
