from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from javes_contracts import (
    CameraDef,
    EdgeDef,
    Hypothesis,
    HypothesisStatus,
    ProbeRequest,
    ProbeResult,
    ProbeStatus,
    ProbedWindow,
    SearchState,
    SiteGraph,
    Visit,
)
from javes_routing.core.params import RoutingParams
from javes_routing.core.site import parse_site
from javes_routing.core.update import _process_probe_results, recompute_hypothesis_score
from javes_routing.service.shadow_benchmark import build_shadow_report
from javes_routing.service.timing_calibration import fit_development_timing
from javes_routing.service.config_loader import load_routing_params
from tests.support.nvidia_12cam_site import canonical_site


T0 = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _graph(*, overlap: bool) -> SiteGraph:
    return SiteGraph(
        site="nvidia-test",
        version="v1",
        cameras={
            "A": CameraDef(id="A"),
            "B": CameraDef(id="B", overlap_camera_ids=["C"] if overlap else []),
            "C": CameraDef(id="C", overlap_camera_ids=["B"] if overlap else []),
        },
        edges=[
            EdgeDef(id="A-B", source="A", target="B", t_min=10, t_typ=20, t_max=60),
            EdgeDef(id="A-C", source="A", target="C", t_min=10, t_typ=20, t_max=60),
        ],
    )


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="hypothesis-A",
        visits=[
            Visit(
                visit_id="visit-A",
                camera_id="A",
                t_in=T0 - timedelta(seconds=5),
                t_out=T0,
                appearance_score=1.0,
                source="LAST_KNOWN",
            )
        ],
        current_node="A",
        status=HypothesisStatus.ALIVE,
    )


def _window(camera: str) -> ProbedWindow:
    return ProbedWindow(
        camera_id=camera,
        t_from=T0 + timedelta(seconds=10),
        t_to=T0 + timedelta(seconds=60),
        probe_id=f"probe-{camera}",
        status=ProbeStatus.DONE,
        coverage_ratio=1.0,
        found=False,
    )


def test_overlap_group_contributes_one_correlated_miss() -> None:
    params = RoutingParams(p_det=0.8)
    hypothesis = _hypothesis()
    graph = _graph(overlap=True)
    one = recompute_hypothesis_score(
        hypothesis, graph, params, [_window("B")], T0 + timedelta(seconds=70)
    )
    overlap_pair = recompute_hypothesis_score(
        hypothesis,
        graph,
        params,
        [_window("B"), _window("C")],
        T0 + timedelta(seconds=70),
    )
    independent_pair = recompute_hypothesis_score(
        hypothesis,
        _graph(overlap=False),
        params,
        [_window("B"), _window("C")],
        T0 + timedelta(seconds=70),
    )
    assert overlap_pair == pytest.approx(one)
    assert independent_pair < overlap_pair


@pytest.mark.parametrize("status", [ProbeStatus.UNAVAILABLE, ProbeStatus.FAILED, ProbeStatus.TIMEOUT])
def test_outage_results_never_create_negative_evidence(status: ProbeStatus) -> None:
    probe = ProbeRequest(
        probe_id="probe-B",
        camera_id="B",
        t_from=T0,
        t_to=T0 + timedelta(seconds=60),
    )
    state = SearchState(case_id="case-001", pending_probes=[probe])
    _process_probe_results(
        state,
        [ProbeResult(probe_id=probe.probe_id, status=status, coverage_ratio=0.0)],
        _graph(overlap=True),
        RoutingParams(max_retries=0),
    )
    assert state.probed_windows[0].status == status
    assert state.probed_windows[0].coverage_ratio == 0.0
    assert any(item["reason"] == status.value for item in state.unobserved_windows)


def test_partial_done_is_unobserved_with_zero_miss_coverage() -> None:
    probe = ProbeRequest(
        probe_id="probe-B",
        camera_id="B",
        t_from=T0,
        t_to=T0 + timedelta(seconds=60),
    )
    state = SearchState(case_id="case-001", pending_probes=[probe])
    _process_probe_results(
        state,
        [
            ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.DONE,
                coverage_ratio=0.75,
                processed_duration_s=45,
            )
        ],
        _graph(overlap=True),
        RoutingParams(),
    )
    assert state.probed_windows[0].coverage_ratio == 0.0
    assert state.unobserved_windows[0]["reason"] == "PARTIAL_COVERAGE"


def test_timing_fit_uses_development_only_and_pins_provenance() -> None:
    manifest = {
        "scenarios": [
            {
                "id": "development-person",
                "split": "development",
                "oracle": {"sightings": [
                    {"camera_id": "A", "t_in_s": 0, "t_out_s": 5},
                    {"camera_id": "B", "t_in_s": 25, "t_out_s": 30},
                ]},
            },
            {
                "id": "holdout-person",
                "split": "holdout",
                "oracle": {"sightings": [
                    {"camera_id": "A", "t_in_s": 0, "t_out_s": 5},
                    {"camera_id": "B", "t_in_s": 105, "t_out_s": 110},
                ]},
            },
        ]
    }
    payload = json.dumps(manifest, sort_keys=True).encode()
    graph = SiteGraph(
        site="timing",
        version="v1",
        cameras={"A": CameraDef(id="A"), "B": CameraDef(id="B")},
        edges=[EdgeDef(id="A-B", source="A", target="B", t_min=10, t_typ=40, t_max=120)],
    )
    fitted = fit_development_timing(
        payload, graph, RoutingParams(), version="nvidia-timing-v1"
    )
    assert fitted.edge_timing_overrides["A-B"]["t_typ"] == 20.0
    assert fitted.timing_calibration_scenario_ids == ["development-person"]
    assert fitted.timing_calibration_split == "development"
    assert fitted.timing_calibration_manifest_sha256 == hashlib.sha256(payload).hexdigest()


def test_shadow_report_uses_identical_params_for_development_and_holdout() -> None:
    manifest = json.dumps(
        {"scenarios": [
            {"id": "dev", "split": "development"},
            {"id": "hold", "split": "holdout"},
        ]},
        sort_keys=True,
    ).encode()
    evaluation = json.dumps(
        {"scenarios": [
            {"scenario_id": "dev", "passed": True},
            {"scenario_id": "hold", "passed": False},
        ]},
        sort_keys=True,
    ).encode()
    params = b"version: nvidia-timing-v1\n"
    report = build_shadow_report(manifest, evaluation, params)
    assert report["params_mutated"] is False
    assert report["params_sha256"] == hashlib.sha256(params).hexdigest()
    assert report["splits"]["development"]["pass_rate"] == 1.0
    assert report["splits"]["holdout"]["pass_rate"] == 0.0
    with pytest.raises(ValueError, match="scenario mismatch"):
        build_shadow_report(manifest, json.dumps({"scenarios": []}).encode(), params)


def test_committed_nvidia_oracle_fits_only_development_and_shadows_holdout() -> None:
    manifest = (ROOT / "tests/fixtures/nvidia_12cam_eval_v1.json").read_bytes()
    graph = parse_site(canonical_site(ROOT))
    fitted = fit_development_timing(
        manifest, graph, RoutingParams(), version="nvidia-12cam-timing-v1"
    )
    assert fitted.edge_timing_overrides
    assert len(fitted.timing_calibration_scenario_ids) == 8
    assert not {
        "cam011-local-person",
        "cam012-local-person",
        "cam006-local-person",
        "cam007-local-person",
    } & set(fitted.timing_calibration_scenario_ids)
    published = load_routing_params(
        ROOT
        / "data/nvidia_mtmc/mtmc_12cam/routing-params.nvidia-12cam-timing-v1.yaml"
    )
    assert published.edge_timing_overrides == fitted.edge_timing_overrides
    assert published.timing_calibration_manifest_sha256 == (
        fitted.timing_calibration_manifest_sha256
    )

    raw = json.loads(manifest)
    active = [item for item in raw["scenarios"] if not item.get("quarantined")]
    evaluation = json.dumps(
        {
            "scenarios": [
                {"scenario_id": item["id"], "passed": True}
                for item in active
            ]
        },
        sort_keys=True,
    ).encode()
    params = json.dumps(fitted.model_dump(mode="json"), sort_keys=True).encode()
    report = build_shadow_report(manifest, evaluation, params)
    assert report["splits"]["development"]["total"] == 8
    assert report["splits"]["holdout"]["total"] == 4
