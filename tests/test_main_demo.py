from __future__ import annotations

import pytest

from javes_main.demo import run_demo


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_complete_fake_http_demo(anyio_backend):
    result = await run_demo()
    assert result["best_visit_cameras"] == ["A", "B"]
    assert result["clip_state"] == "CONFIRMED"
    assert result["export_lifecycle"] == "SAVED"
    assert result["final_lifecycle"] == "DELETED"
    assert result["route_maps"][0]["mime_type"] == "image/svg+xml"
    assert result["metrics"]["camera_seconds_processed"] > 0
    assert result["metrics"]["routing_runs"] >= 2
