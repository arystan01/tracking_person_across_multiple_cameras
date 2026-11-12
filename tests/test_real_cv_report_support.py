from __future__ import annotations

import hashlib

from tests.support.real_cv_e2e import ReportVideo, publish_inspectable_report


def test_report_uses_content_addressed_route_svg(tmp_path) -> None:
    output_root = tmp_path / "latest"
    run_root = output_root / "runs" / "run-1"
    run_root.mkdir(parents=True)
    video = tmp_path / "camera.mp4"
    video.write_bytes(b"fixture")
    route_svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    digest = hashlib.sha256(route_svg).hexdigest()[:16]
    route_filename = f"route-{digest}.svg"

    publish_inspectable_report(
        output_root,
        run_root,
        route_svg=route_svg,
        case_view={},
        probe_results=[],
        report={},
        videos=[ReportVideo("Cam001", video, "evidence")],
        timeline=[],
    )

    page = (output_root / "index.html").read_text(encoding="utf-8")
    assert f'data="{route_filename}"' in page
    assert (output_root / f"index-{digest}.html").read_text() == page
    assert (output_root / route_filename).read_bytes() == route_svg
    assert (run_root / route_filename).read_bytes() == route_svg
