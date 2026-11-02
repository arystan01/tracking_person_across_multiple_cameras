"""Generate machine-readable JSON Schemas for external adapter handoff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from . import (
    ArtifactRef,
    Candidate,
    CaseCommandBody,
    CreateCaseBody,
    ClipRecord,
    ClipStateCommand,
    CutClipCommand,
    ExportClipsCommand,
    ExportClipsResult,
    LiveFeedUpdate,
    PerceptionProbeJob,
    ProbeRequest,
    ProbeResult,
    RetentionCommand,
    RetentionResource,
    RetentionResult,
    ReviewDecisionBody,
    SiteGraph,
    TargetGalleryCommand,
    TargetGalleryResult,
    Tracklet,
    VideoSegmentRequest,
    VideoSegmentResult,
)


PUBLIC_MODELS = {
    model.__name__: model
    for model in (
        ArtifactRef,
        Candidate,
        CaseCommandBody,
        CreateCaseBody,
        ClipRecord,
        ClipStateCommand,
        CutClipCommand,
        ExportClipsCommand,
        ExportClipsResult,
        LiveFeedUpdate,
        PerceptionProbeJob,
        ProbeRequest,
        ProbeResult,
        RetentionCommand,
        RetentionResource,
        RetentionResult,
        ReviewDecisionBody,
        SiteGraph,
        TargetGalleryCommand,
        TargetGalleryResult,
        Tracklet,
        VideoSegmentRequest,
        VideoSegmentResult,
    )
}


def write_schemas(output: str | Path) -> list[Path]:
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in sorted(PUBLIC_MODELS.items()):
        path = directory / f"{name}.schema.json"
        text = json.dumps(
            model.model_json_schema(by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        path.write_text(text + "\n", encoding="utf-8")
        written.append(path)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate JAVES public JSON Schemas")
    parser.add_argument("--out", default="schemas")
    args = parser.parse_args(argv)
    for path in write_schemas(args.out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
