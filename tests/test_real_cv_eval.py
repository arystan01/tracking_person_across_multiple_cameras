from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from javes_contracts import (
    CameraCalibration,
    CameraDef,
    Estimate,
    MapDef,
    MapFloor,
    MapPosition,
    ReachableNode,
    ReachableSegment,
    SiteGraph,
)

from tests.support.real_cv_eval import (
    EVALUATION_REPORT_SCHEMA_VERSION,
    EvaluationManifest,
    OracleCheckpoint,
    ScenarioEvaluation,
    aggregate_evaluation,
    evaluate_reachability,
    load_evaluation_manifest,
    run_real_cv_evaluation,
    system_payload,
    write_evaluation_report,
)


def _manifest() -> dict:
    scenarios = []
    outcome_kinds = [
        "BLIND_GAP",
        "BLIND_GAP",
        "BLIND_GAP",
        "BLIND_GAP",
        "NO_RETURN",
        "NO_RETURN",
        "REENTRY",
        "REENTRY",
        None,
        None,
        None,
        None,
    ]
    for index, camera in enumerate(
        [f"Cam{value:03d}" for value in range(1, 13)]
    ):
        outcome = outcome_kinds[index]
        scenarios.append(
            {
                "id": f"person-{index + 1:02d}",
                "split": "development" if index < 8 else "holdout",
                "system_input": {
                    "reference": {
                        "camera_id": camera,
                        "time_s": 1,
                        "bbox_xyxy": [10, 10, 30, 70],
                        "coordinate_frame_px": [1920, 1080],
                    },
                    "last_known_camera": camera,
                    "last_known_time_s": 1,
                    "window_start_s": 0,
                    "window_end_s": 60,
                    "available_cameras": [camera],
                },
                "oracle": {
                    "global_identity": f"global-{index + 1:02d}",
                    "sightings": [
                        {"camera_id": camera, "t_in_s": 0, "t_out_s": 10}
                    ],
                    "checkpoints": [
                        {
                            "camera_id": camera,
                            "time_s": 1,
                            "foot_point_px": [20, 70],
                            "coordinate_frame_px": [1920, 1080],
                        }
                    ],
                    "branch_outcomes": (
                        [
                            {
                                "lost_at_s": 10,
                                "checkpoint_times_s": [11],
                                "true_edge_ids": [f"edge-{index}"],
                                "kind": outcome,
                            }
                        ]
                        if outcome
                        else []
                    ),
                },
            }
        )
    return {
        "manifest_version": "nvidia-12cam-eval.v1",
        "dataset": "NVIDIA-MTMC-12cam",
        "profile": {},
        "scenarios": scenarios,
    }


def test_manifest_pins_shared_profile_and_keeps_oracle_out_of_system_payload():
    manifest = EvaluationManifest.model_validate(_manifest())
    assert len(manifest.scenarios) == 12
    payload = system_payload(manifest.scenarios[0])
    assert "oracle" not in payload
    assert "global_identity" not in str(payload)
    assert manifest.profile.sample_fps == 5.0
    assert manifest.profile.image_size == 1280


def test_committed_nvidia_manifest_is_complete_and_yellow_is_quarantined():
    repository_root = Path(__file__).resolve().parents[1]
    manifest = load_evaluation_manifest(
        repository_root / "tests/fixtures/nvidia_12cam_eval_v1.json"
    )
    active = [item for item in manifest.scenarios if not item.quarantined]
    assert len(active) == 12
    assert [item.id for item in manifest.scenarios if item.quarantined] == [
        "yellow-top-woman"
    ]


def test_manifest_rejects_target_specific_profile_and_incomplete_holdout():
    data = _manifest()
    data["scenarios"][0]["system_input"]["sample_fps"] = 3
    with pytest.raises(ValidationError):
        EvaluationManifest.model_validate(data)

    data = _manifest()
    data["scenarios"][8]["split"] = "development"
    with pytest.raises(ValidationError, match="8 development and 4 holdout"):
        EvaluationManifest.model_validate(data)


def test_quarantined_target_does_not_count_towards_acceptance():
    data = _manifest()
    broken = deepcopy(data["scenarios"][0])
    broken["id"] = "yellow-top-woman"
    broken["oracle"]["global_identity"] = "known-invalid-yellow-target"
    broken["quarantined"] = True
    broken["quarantine_reason"] = "known invalid target association"
    data["scenarios"].append(broken)
    manifest = EvaluationManifest.model_validate(data)
    assert manifest.scenarios[-1].quarantined


def test_aggregate_acceptance_thresholds_are_automatic():
    results = []
    for index in range(12):
        results.append(
            ScenarioEvaluation(
                scenario_id=f"person-{index}",
                split="development" if index < 8 else "holdout",
                passed=True,
                positional_errors_m=[0.5, 0.7],
                false_associations=0,
                route_associations=10,
                reachability=[
                    {
                        "contained": 9,
                        "total": 10,
                        "negative_rejected": 2,
                        "negative_total": 2,
                        "probability_mass": 1.0,
                        "true_branch_rank": 2,
                    }
                ],
            )
        )
    summary = aggregate_evaluation(results)
    assert summary.passed
    assert summary.hidden_point_containment == pytest.approx(0.9)
    assert summary.top2_branch_recall == 1.0

    failed = results[0].model_copy(
        update={"positional_errors_m": [2.0], "passed": False}
    )
    assert not aggregate_evaluation([failed, *results[1:]]).passed

    invalid_geometry = results[0].model_copy(
        update={
            "reachability": [
                results[0].reachability[0].model_copy(
                    update={"geometry_errors": ["crosses blocked area"]}
                )
            ]
        },
        deep=True,
    )
    assert not aggregate_evaluation([invalid_geometry, *results[1:]]).passed


def test_reachability_oracle_rejects_blocked_and_early_geometry() -> None:
    graph = SiteGraph(
        site="oracle",
        version="v1",
        map=MapDef(
            floors=[
                MapFloor(
                    id="1F",
                    size_px=(100, 100),
                    m_per_px=0.1,
                    walkable_polygons=[
                        [(0, 0), (100, 0), (100, 100), (0, 100)]
                    ],
                    blocked_polygons=[
                        [(40, 40), (60, 40), (60, 60), (40, 60)]
                    ],
                )
            ]
        ),
        cameras={
            "Cam001": CameraDef(
                id="Cam001",
                floor="1F",
                calibration=CameraCalibration(
                    frame_size_px=(100, 100),
                    homography=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                    floor_id="1F",
                    error_m=0.0,
                ),
            )
        },
        edges=[],
    )
    checkpoint = OracleCheckpoint(
        camera_id="Cam001",
        time_s=1.0,
        foot_point_px=(90, 10),
        coordinate_frame_px=(100, 100),
        calibration_error_m=0.0,
        hidden_from_system=True,
    )
    early = Estimate(
        zones=[],
        reachable_segments=[
            ReachableSegment(
                edge_id="safe-but-early",
                source="a",
                target="b",
                probability=1.0,
                floor_id="1F",
                points=[(10, 10), (90, 10)],
                width_m=1.0,
            )
        ],
        reachable_nodes=[ReachableNode(node_id="b", probability=1.0)],
        reachable_origin=MapPosition(x=10, y=10, floor_id="1F", error_m=0.0),
        reachable_horizon_s=1.0,
    )
    early_metric = evaluate_reachability(
        early, graph, [checkpoint], [], max_speed_mps=1.0
    )
    assert early_metric.contained == 1
    assert early_metric.geometry_errors == []
    assert len(early_metric.early_arrival_errors) == 1

    blocked = early.model_copy(
        update={
            "reachable_segments": [
                early.reachable_segments[0].model_copy(
                    update={
                        "edge_id": "blocked",
                        "points": [(10, 50), (90, 50)],
                    }
                )
            ],
            "reachable_horizon_s": 10.0,
        },
        deep=True,
    )
    blocked_metric = evaluate_reachability(blocked, graph, [], [])
    assert len(blocked_metric.geometry_errors) == 1
    assert "crosses a blocked area" in blocked_metric.geometry_errors[0]


def test_evaluation_summary_is_machine_readable_and_counts_failures(tmp_path):
    passing = ScenarioEvaluation(
        scenario_id="passing",
        split="development",
        passed=True,
        positional_errors_m=[0.5],
        route_associations=1,
        cache_hit=True,
        cache_hits=1,
        duration_s=1.25,
    )
    failing = ScenarioEvaluation(
        scenario_id="failing",
        split="holdout",
        passed=False,
        positional_errors_m=[2.0],
        route_associations=1,
        failures=["positional gate"],
        cache_misses=1,
        duration_s=2.75,
    )
    summary = aggregate_evaluation(
        [passing, failing],
        quarantined_scenarios=1,
    )
    assert summary.schema_version == EVALUATION_REPORT_SCHEMA_VERSION
    assert summary.active_scenarios == 2
    assert summary.passed_scenarios == 1
    assert summary.failed_scenarios == 1
    assert summary.quarantined_scenarios == 1
    assert summary.extraction_cache_hits == 1
    assert summary.extraction_cache_misses == 1
    assert summary.duration_s == pytest.approx(4.0)
    assert not summary.passed

    write_evaluation_report(tmp_path, summary)
    report = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert EVALUATION_REPORT_SCHEMA_VERSION in report
    assert '"failed_scenarios": 1' in report
    assert '"quarantined_scenarios": 1' in report
    assert "positional gate" in report


@pytest.mark.real_cv
def test_nvidia_12cam_real_cv_evaluation() -> None:
    from tests.support.real_cv_e2e import require_real_cv

    require_real_cv()
    repository_root = Path(__file__).resolve().parents[1]
    manifest = load_evaluation_manifest(
        repository_root / "tests/fixtures/nvidia_12cam_eval_v1.json"
    )
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S-%fZ")
    output = Path(
        os.environ.get(
            "JAVES_REAL_CV_EVAL_OUT",
            repository_root / "artifacts/real-cv-eval" / run_id,
        )
    ).resolve()
    summary = run_real_cv_evaluation(
        repository_root, manifest, output
    )
    assert summary.passed, (
        f"real-CV evaluation failed; inspect {output / 'index.html'}"
    )
