"""Authenticated, bounded reference-image ingestion for case creation."""

from __future__ import annotations

from datetime import timedelta
from io import BytesIO
import hashlib
import json
import re
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from javes_contracts import ArtifactRef, ReferenceImageUploadResponse
from javes_main.domain import (
    AuditEvent,
    ReferenceImageUploadRecord,
    ReferenceImageUploadStatus,
)
from javes_main.ports import ArtifactStore, CaseRepository, Clock, IdGenerator

from .errors import ReferenceUploadError


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
UPLOAD_TTL = timedelta(hours=24)
_CASE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_MIME_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG"}


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_reference_image(data: bytes, content_type: str | None) -> tuple[bytes, int, int]:
    if not data:
        raise ReferenceUploadError(
            "REFERENCE_UPLOAD_INVALID", "reference image is empty", status=422
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise ReferenceUploadError(
            "REFERENCE_UPLOAD_INVALID", "reference image exceeds 10 MiB", status=413
        )
    expected_format = _MIME_FORMAT.get((content_type or "").lower())
    if expected_format is None:
        raise ReferenceUploadError(
            "REFERENCE_UPLOAD_INVALID",
            "reference image must be JPEG or PNG",
            status=422,
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format != expected_format:
                    raise ReferenceUploadError(
                        "REFERENCE_UPLOAD_INVALID",
                        "reference image content does not match its MIME type",
                        status=422,
                    )
                if getattr(source, "n_frames", 1) != 1 or getattr(
                    source, "is_animated", False
                ):
                    raise ReferenceUploadError(
                        "REFERENCE_UPLOAD_INVALID",
                        "animated or multi-frame reference images are not supported",
                        status=422,
                    )
                source.load()
                image = ImageOps.exif_transpose(source)
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ReferenceUploadError(
                        "REFERENCE_UPLOAD_INVALID",
                        "reference image dimensions are invalid or exceed 25 MP",
                        status=422,
                    )
                if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                output = BytesIO()
                image.save(output, format="JPEG", quality=95, optimize=True)
                return output.getvalue(), width, height
    except ReferenceUploadError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        ModuleNotFoundError,
        Image.DecompressionBombError,
    ) as exc:
        raise ReferenceUploadError(
            "REFERENCE_UPLOAD_INVALID",
            "reference image cannot be decoded safely",
            status=422,
        ) from exc


class ReferenceImageUploadService:
    def __init__(
        self,
        *,
        repository: CaseRepository,
        artifacts: ArtifactStore,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.clock = clock
        self.ids = ids

    def _audit(
        self,
        case_id: str,
        event_type: str,
        actor_id: str,
        payload: dict,
    ) -> None:
        now = self.clock.now()
        self.repository.append_event(
            AuditEvent(
                event_id=self.ids.new(
                    "evt", case_id, event_type, now.isoformat(), _fingerprint(payload)
                ),
                case_id=case_id,
                event_type=event_type,
                actor_id=actor_id,
                occurred_at=now,
                payload=payload,
            )
        )

    def record_rejection(self, case_id: str, actor_id: str, code: str) -> None:
        self._audit(
            case_id,
            "REFERENCE_IMAGE_UPLOAD_REJECTED",
            actor_id,
            {"error_code": code},
        )

    def upload(
        self,
        *,
        case_id: str,
        operator_id: str,
        idempotency_key: str,
        data: bytes,
        content_type: str | None,
    ) -> ReferenceImageUploadResponse:
        if not _CASE_ID.fullmatch(case_id):
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_INVALID",
                "case_id must contain only letters, digits, underscores or hyphens",
                status=422,
            )
        canonical, width, height = canonical_reference_image(data, content_type)
        digest = hashlib.sha256(canonical).hexdigest()
        fingerprint = _fingerprint(
            {
                "case_id": case_id,
                "operator_id": operator_id,
                "sha256": digest,
                "width": width,
                "height": height,
            }
        )
        storage_key = f"reference-upload:{case_id}:{operator_id}:{idempotency_key}"
        wrote_key: str | None = None
        try:
            with self.repository.transaction():
                self.repository.lock_idempotency(storage_key)
                previous = self.repository.get_idempotency(storage_key)
                if previous is not None:
                    if previous[0] != fingerprint:
                        raise ReferenceUploadError(
                            "REFERENCE_UPLOAD_INVALID",
                            "idempotency key was reused with different image content",
                        )
                    return ReferenceImageUploadResponse.model_validate(previous[1])
                now = self.clock.now()
                upload_id = self.ids.new(
                    "reference_upload", case_id, operator_id, idempotency_key, digest
                )
                object_key = f"cases/{case_id}/reference/{upload_id}.jpg"
                self.artifacts.put(object_key, canonical, "image/jpeg")
                wrote_key = object_key
                record = ReferenceImageUploadRecord(
                    upload_id=upload_id,
                    case_id=case_id,
                    operator_id=operator_id,
                    object_key=object_key,
                    sha256=digest,
                    size_bytes=len(canonical),
                    width_px=width,
                    height_px=height,
                    created_at=now,
                    expires_at=now + UPLOAD_TTL,
                )
                self.repository.add_reference_upload(record)
                response = ReferenceImageUploadResponse(
                    upload_id=upload_id,
                    case_id=case_id,
                    reference_image_ref=ArtifactRef(
                        object_key=object_key, sha256=digest
                    ),
                    size_bytes=len(canonical),
                    width_px=width,
                    height_px=height,
                    expires_at=record.expires_at,
                )
                self.repository.remember_idempotency(
                    storage_key, fingerprint, response.model_dump(mode="json")
                )
                self._audit(
                    case_id,
                    "REFERENCE_IMAGE_UPLOADED",
                    operator_id,
                    {
                        "upload_id": upload_id,
                        "size_bytes": len(canonical),
                        "width_px": width,
                        "height_px": height,
                    },
                )
                return response
        except Exception:
            if wrote_key is not None:
                try:
                    self.repository.get_reference_upload_by_key(wrote_key)
                except KeyError:
                    self.artifacts.delete(wrote_key)
            raise

    def cleanup_expired(self, *, limit: int = 100) -> int:
        now = self.clock.now()
        cleaned = 0
        for candidate in self.repository.list_expired_reference_uploads(
            now=now, limit=limit
        ):
            with self.repository.transaction():
                current = self.repository.get_reference_upload_by_key(
                    candidate.object_key, for_update=True
                )
                if (
                    current.status != ReferenceImageUploadStatus.READY
                    or current.expires_at > now
                ):
                    continue
                self.artifacts.delete(current.object_key)
                current.status = ReferenceImageUploadStatus.EXPIRED
                self.repository.save_reference_upload(current)
                self._audit(
                    current.case_id,
                    "REFERENCE_IMAGE_UPLOAD_EXPIRED",
                    current.operator_id,
                    {"upload_id": current.upload_id},
                )
                cleaned += 1
        return cleaned
