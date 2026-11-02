"""Validate and rewrite the canonical example JSON with current contracts."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from javes_fakes.contracts import ArtifactRef, CaseWindow, CreateCaseBody, LastKnown
from javes_fakes.dashboard import FakeDashboardRequests
from routing.contracts import ProbeRequest, ProbeResult


ROOT = Path(__file__).resolve().parent


def main() -> None:
    for filename, model in (
        ("probe_request.json", ProbeRequest),
        ("probe_result.json", ProbeResult),
    ):
        path = ROOT / filename
        value = model.model_validate_json(path.read_text(encoding="utf-8"))
        path.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")

    now = datetime(2026, 10, 16, 5, 16, tzinfo=timezone.utc)
    case = CreateCaseBody(
        case_id="case-demo-001",
        operator_id="operator-demo",
        target_id="target-demo",
        reference_image_ref=ArtifactRef(
            object_key="cases/case-demo-001/reference.jpg"
        ),
        last_known=LastKnown(camera_id="A", time=now),
        window=CaseWindow(start=now, end=now + timedelta(minutes=20)),
        site_id="demo-site",
        site_version="1",
    )
    dashboard = FakeDashboardRequests.standard_scenario(
        case=case,
        candidate_id="candidate-demo-001",
        tracklet_id="tracklet-demo-001",
        operator_id="operator-demo",
        now=now + timedelta(minutes=5),
    )
    (ROOT / "dashboard_requests.json").write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in dashboard],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
