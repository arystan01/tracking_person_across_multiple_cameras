"""Run the complete fake stack through the public Controls HTTP API."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Sequence

import httpx

from javes_contracts import ArtifactRef, FakeObservation
from routing.contracts import CameraDef, EdgeDef, MapDef, MapFloor, SiteGraph
from routing.contracts.params import RoutingParams

from .api import create_app
from .bootstrap import build_in_memory_system


BASE = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)


def _system():
    graph = SiteGraph(
        site_id="demo-site",
        version="v1",
        cameras={
            "A": CameraDef(id="A", floor="1F", map_xy=(100, 100)),
            "B": CameraDef(id="B", floor="1F", map_xy=(500, 100)),
        },
        edges=[
            EdgeDef(
                id="A-B",
                source="A",
                target="B",
                t_min=10,
                t_typ=20,
                t_max=60,
                map_path=[(100, 100), (500, 100)],
            )
        ],
        map=MapDef(floors=[MapFloor(id="1F", size_px=(800, 450))]),
    )
    return build_in_memory_system(
        graph=graph,
        params=RoutingParams(),
        now=BASE + timedelta(seconds=60),
        observations=[
            FakeObservation(
                observation_id="demo-observation",
                camera_id="B",
                t_start=BASE + timedelta(seconds=20),
                t_end=BASE + timedelta(seconds=30),
                appearance_score=0.94,
                clip_ref=ArtifactRef(archive_id="demo-archive-B"),
            )
        ],
    )


async def run_demo() -> dict:
    system = _system()
    application = create_app(
        system.manager,
        auth=system.auth,
        scheduler=system.scheduler,
        run_fake_jobs_inline=True,
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
        root_login = await client.post(
            "/v1/auth/login",
            json={"username": "root", "password": "root-password-for-tests"},
        )
        root_login.raise_for_status()
        root_token = root_login.json()["access_token"]
        root_headers = {"authorization": f"Bearer {root_token}"}

        operator = await client.post(
            "/v1/admin/users",
            json={
                "username": "demo-operator",
                "temporary_password": "temporary-password-1",
                "role": "OPERATOR",
            },
            headers={**root_headers, "idempotency-key": "demo-create-operator"},
        )
        operator.raise_for_status()
        operator_id = operator.json()["user_id"]
        temporary = await client.post(
            "/v1/auth/login",
            json={
                "username": "demo-operator",
                "password": "temporary-password-1",
            },
        )
        temporary.raise_for_status()
        temporary_token = temporary.json()["access_token"]
        changed = await client.post(
            "/v1/auth/change-password",
            json={
                "current_password": "temporary-password-1",
                "new_password": "operator-password-2",
            },
            headers={
                "authorization": f"Bearer {temporary_token}",
                "idempotency-key": "demo-change-password",
            },
        )
        changed.raise_for_status()
        operator_login = await client.post(
            "/v1/auth/login",
            json={
                "username": "demo-operator",
                "password": "operator-password-2",
            },
        )
        operator_login.raise_for_status()
        operator_token = operator_login.json()["access_token"]

        def operator_headers(key: str) -> dict[str, str]:
            return {
                "authorization": f"Bearer {operator_token}",
                "idempotency-key": key,
            }

        created = await client.post(
            "/v1/cases",
            json={
                "case_id": "demo-case",
                "operator_id": operator_id,
                "target_id": "demo-target",
                "reference_image_ref": {"object_key": "demo/reference.jpg"},
                "last_known": {"camera_id": "A", "time": BASE.isoformat()},
                "window": {
                    "start": BASE.isoformat(),
                    "end": (BASE + timedelta(minutes=5)).isoformat(),
                },
                "profile": "ADULT",
                "site_id": "demo-site",
                "site_version": "v1",
            },
            headers=operator_headers("demo-create"),
        )
        created.raise_for_status()
        view = created.json()
        candidate = view["candidates"][0]
        confirmed = await client.post(
            "/v1/cases/demo-case/decisions",
            json={
                "decision_id": "demo-confirm",
                "candidate_id": candidate["candidate_id"],
                "tracklet_id": candidate["tracklet_id"],
                "decision": "CONFIRM",
                "operator_id": operator_id,
                "occurred_at": (BASE + timedelta(seconds=70)).isoformat(),
            },
            headers=operator_headers("demo-confirm"),
        )
        confirmed.raise_for_status()
        closed = await client.post(
            "/v1/cases/demo-case/commands/close",
            json={
                "command_id": "demo-close",
                "action": "CLOSE",
                "operator_id": operator_id,
                "occurred_at": (BASE + timedelta(seconds=75)).isoformat(),
            },
            headers=operator_headers("demo-close"),
        )
        closed.raise_for_status()
        exported = await client.post(
            "/v1/cases/demo-case/commands/export",
            json={
                "command_id": "demo-export",
                "action": "EXPORT",
                "operator_id": operator_id,
                "occurred_at": (BASE + timedelta(seconds=80)).isoformat(),
            },
            headers=operator_headers("demo-export"),
        )
        exported.raise_for_status()
        metrics = await client.get("/internal/v1/metrics", headers=root_headers)
        metrics.raise_for_status()
        deleted = await client.post(
            "/v1/cases/demo-case/commands/delete",
            json={
                "command_id": "demo-delete",
                "action": "DELETE",
                "operator_id": system.root_user.user_id,
                "occurred_at": (BASE + timedelta(seconds=90)).isoformat(),
            },
            headers={**root_headers, "idempotency-key": "demo-delete"},
        )
        deleted.raise_for_status()
        return {
            "case_id": "demo-case",
            "route_revision": view["route_revision"],
            "best_visit_cameras": [
                item["camera_id"] for item in view["best_hypothesis"]["visits"]
            ],
            "route_maps": view["rendered_route_maps"],
            "clip_state": confirmed.json()["clips"][0]["state"],
            "export_lifecycle": exported.json()["lifecycle"],
            "final_lifecycle": deleted.json()["lifecycle"],
            "metrics": metrics.json(),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run JAVES Main fake HTTP demo")
    parser.parse_args(argv)
    print(json.dumps(asyncio.run(run_demo()), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
