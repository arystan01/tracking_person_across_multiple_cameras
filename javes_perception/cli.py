from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from javes_contracts import ArtifactRef, PerceptionProbeJob, TargetGalleryCommand
from javes_contracts.acceptance import check_probe_result
from javes_main.adapters.artifacts import FileArtifactStore
from routing.contracts import ProbeRequest

from .config import load_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run one Perception probe on local video")
    parser.add_argument("--config", required=True)
    parser.add_argument("--reference", required=True, help="photo of the target person")
    parser.add_argument("--camera", required=True)
    parser.add_argument("--from", dest="t_from", required=True, help="ISO time, e.g. 2026-11-05T05:16:00Z")
    parser.add_argument("--to", dest="t_to", required=True, help="ISO time or OPEN")
    parser.add_argument("--case", default="case_local")
    parser.add_argument("--out", default="perception_out")
    args = parser.parse_args(argv)

    from .worker import PerceptionWorker

    out = Path(args.out)
    artifacts = FileArtifactStore(out / "artifacts")
    worker = PerceptionWorker(load_settings(args.config), artifacts)

    ref_key = f"cases/{args.case}/reference{Path(args.reference).suffix or '.jpg'}"
    artifacts.put(ref_key, Path(args.reference).read_bytes(), "image/jpeg")
    gallery = worker.update_gallery(
        TargetGalleryCommand(
            command_id=f"gallery_{args.case}",
            case_id=args.case,
            source="reference",
            image_ref=ArtifactRef(object_key=ref_key),
            model_version=worker.settings.model_version,
        )
    )
    job = PerceptionProbeJob(
        job_id=f"job_{args.camera}",
        case_id=args.case,
        target_gallery_ref=gallery.gallery_ref,
        probe=ProbeRequest(probe_id=f"p_{args.camera}", camera=args.camera, t_from=args.t_from, t_to=args.t_to),
    )
    result = worker.execute(job, datetime.now(timezone.utc))
    check_probe_result(job, result)

    data = result.model_dump(mode="json")
    (out / "result.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    (out / "tracklets.json").write_text(json.dumps(data["tracklets"], indent=2), encoding="utf-8")
    (out / "candidates.json").write_text(json.dumps(data["candidates"], indent=2), encoding="utf-8")
    print(f"{result.status.value}: {result.n_tracklets_total} people, {len(result.candidates)} candidates")
    for candidate in sorted(result.candidates, key=lambda c: -c.score):
        print(f"  {candidate.tracklet_id}  score={candidate.score:.2f}  cosine={candidate.cosine:.2f}")
    print(f"saved to {out}")


if __name__ == "__main__":
    main()
