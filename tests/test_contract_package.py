from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from javes_contracts import TargetGalleryCommand, TargetGalleryResult, VideoSegmentRequest
from javes_contracts.schema import PUBLIC_MODELS, write_schemas


def test_public_contract_schemas_are_deterministic_and_valid_json(tmp_path):
    first = write_schemas(tmp_path / "first")
    second = write_schemas(tmp_path / "second")
    assert [item.name for item in first] == [item.name for item in second]
    for left, right in zip(first, second, strict=True):
        assert left.read_bytes() == right.read_bytes()
        assert json.loads(left.read_text())["title"] in PUBLIC_MODELS


def test_versioned_contracts_reject_unknown_versions():
    with pytest.raises(ValidationError, match="target-gallery.v1"):
        TargetGalleryCommand.model_validate(
            {
                "contract_version": "target-gallery.v2",
                "command_id": "command-1",
                "case_id": "case-1",
                "source": "reference",
                "image_ref": {"object_key": "reference.jpg"},
            }
        )
    with pytest.raises(ValidationError, match="video-archive-request.v1"):
        VideoSegmentRequest.model_validate(
            {
                "contract_version": "video-archive-request.v9",
                "request_id": "request-1",
                "camera_id": "A",
                "t_from": "2026-10-16T00:00:00Z",
                "t_to": "OPEN",
            }
        )
    with pytest.raises(ValidationError, match="target-gallery-result.v1"):
        TargetGalleryResult.model_validate(
            {
                "contract_version": "target-gallery-result.v2",
                "command_id": "command-1",
                "case_id": "case-1",
                "gallery_ref": "gallery:case-1:v1",
                "source_count": 1,
                "model_version": "reid-v1",
            }
        )
