from __future__ import annotations

from io import BytesIO
from datetime import timedelta

import httpx
import pytest
from PIL import Image, PngImagePlugin

from javes_contracts import ArtifactRef
from javes_main.api import create_app
from tests.test_controls_api import BASE, body, headers, login, make_system


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def png_with_metadata() -> bytes:
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-note", "must be stripped")
    Image.new("RGBA", (40, 80), (10, 120, 200, 128)).save(
        output, format="PNG", pnginfo=metadata
    )
    return output.getvalue()


def jpeg_with_orientation() -> bytes:
    output = BytesIO()
    image = Image.new("RGB", (20, 40), (80, 20, 150))
    exif = Image.Exif()
    exif[274] = 6
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


def animated_png() -> bytes:
    output = BytesIO()
    frames = [
        Image.new("RGB", (20, 20), "red"),
        Image.new("RGB", (20, 20), "blue"),
    ]
    frames[0].save(
        output,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    return output.getvalue()


async def upload(client: httpx.AsyncClient, token: str, *, key: str = "upload-1"):
    return await client.post(
        "/v1/cases/api-case/reference-image",
        headers=headers(key, token),
        files={"file": ("reference.png", png_with_metadata(), "image/png")},
    )


async def test_authenticated_upload_is_canonical_and_consumed_atomically():
    system = make_system()
    app = create_app(
        system.manager,
        auth=system.auth,
        scheduler=system.scheduler,
        run_fake_jobs_inline=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = await login(client, "root", "root-password-for-tests")
        first = await upload(client, token)
        replay = await upload(client, token)
        assert first.status_code == replay.status_code == 201
        assert first.json() == replay.json()
        payload = first.json()
        reference = ArtifactRef.model_validate(payload["reference_image_ref"])
        data, mime = system.artifacts.get(reference.object_key)
        assert mime == "image/jpeg"
        with Image.open(BytesIO(data)) as image:
            assert image.format == "JPEG"
            assert image.mode == "RGB"
            assert image.size == (40, 80)
            assert not image.info.get("private-note")

        request = body(system.root_user.user_id).model_copy(
            update={"reference_image_ref": reference}
        )
        created = await client.post(
            "/v1/cases",
            headers=headers("create-with-upload", token),
            json=request.model_dump(mode="json"),
        )
        assert created.status_code == 201, created.text
        record = system.repository.get_reference_upload_by_key(reference.object_key)
        assert record.status.value == "CONSUMED"
        assert record.consumed_at is not None

        reused = await client.post(
            "/v1/cases",
            headers=headers("reuse-consumed-upload", token),
            json=request.model_dump(mode="json"),
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "REFERENCE_UPLOAD_ALREADY_USED"


async def test_exif_orientation_is_applied_and_metadata_is_removed():
    system = make_system()
    app = create_app(system.manager, auth=system.auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = await login(client, "root", "root-password-for-tests")
        response = await client.post(
            "/v1/cases/api-case/reference-image",
            headers=headers("oriented-upload", token),
            files={"file": ("reference.jpg", jpeg_with_orientation(), "image/jpeg")},
        )
        assert response.status_code == 201
        reference = ArtifactRef.model_validate(response.json()["reference_image_ref"])
        data, _ = system.artifacts.get(reference.object_key)
        with Image.open(BytesIO(data)) as image:
            assert image.size == (40, 20)
            assert 274 not in image.getexif()


async def test_animated_and_decompression_bomb_images_are_rejected(monkeypatch):
    system = make_system()
    app = create_app(system.manager, auth=system.auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = await login(client, "root", "root-password-for-tests")
        animated = await client.post(
            "/v1/cases/api-case/reference-image",
            headers=headers("animated-upload", token),
            files={"file": ("animated.png", animated_png(), "image/png")},
        )
        assert animated.status_code == 422
        assert animated.json()["code"] == "REFERENCE_UPLOAD_INVALID"

        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
        bomb = await client.post(
            "/v1/cases/api-case/reference-image",
            headers=headers("bomb-upload", token),
            files={"file": ("bomb.png", png_with_metadata(), "image/png")},
        )
        assert bomb.status_code == 422
        assert bomb.json()["code"] == "REFERENCE_UPLOAD_INVALID"


async def test_unregistered_foreign_and_changed_reference_images_are_rejected():
    system = make_system()
    app = create_app(system.manager, auth=system.auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        root_token = await login(client, "root", "root-password-for-tests")
        direct = await client.post(
            "/v1/cases",
            headers=headers("direct-key", root_token),
            json=body(system.root_user.user_id).model_dump(mode="json"),
        )
        assert direct.status_code == 404
        assert direct.json()["code"] == "REFERENCE_UPLOAD_NOT_FOUND"

        uploaded = await upload(client, root_token)
        reference = ArtifactRef.model_validate(
            uploaded.json()["reference_image_ref"]
        )
        wrong_case = await client.post(
            "/v1/cases",
            headers=headers("wrong-case", root_token),
            json=body(system.root_user.user_id)
            .model_copy(
                update={
                    "case_id": "another-case",
                    "reference_image_ref": reference,
                }
            )
            .model_dump(mode="json"),
        )
        assert wrong_case.status_code == 409
        assert wrong_case.json()["code"] == "REFERENCE_UPLOAD_INVALID"

        system.artifacts.delete(reference.object_key)
        system.artifacts.put(reference.object_key, b"changed", "image/jpeg")
        changed = await client.post(
            "/v1/cases",
            headers=headers("changed-key", root_token),
            json=body(system.root_user.user_id)
            .model_copy(update={"reference_image_ref": reference})
            .model_dump(mode="json"),
        )
        assert changed.status_code == 409
        assert changed.json()["code"] == "REFERENCE_UPLOAD_CONTENT_MISMATCH"

        created_user = await client.post(
            "/v1/admin/users",
            headers=headers("create-other", root_token),
            json={
                "username": "other-operator",
                "temporary_password": "temporary-password-123",
                "role": "OPERATOR",
            },
        )
        other_id = created_user.json()["user_id"]
        temporary = await login(client, "other-operator", "temporary-password-123")
        await client.post(
            "/v1/auth/change-password",
            headers=headers("change-other", temporary),
            json={
                "current_password": "temporary-password-123",
                "new_password": "permanent-password-456",
            },
        )
        other_token = await login(client, "other-operator", "permanent-password-456")
        foreign = await client.post(
            "/v1/cases",
            headers=headers("foreign-key", other_token),
            json=body(other_id)
            .model_copy(update={"reference_image_ref": reference})
            .model_dump(mode="json"),
        )
        assert foreign.status_code == 404
        assert foreign.json()["code"] == "REFERENCE_UPLOAD_NOT_FOUND"


@pytest.mark.parametrize(
    ("content", "mime"),
    [
        (b"not-an-image", "image/jpeg"),
        (png_with_metadata(), "image/jpeg"),
        (png_with_metadata(), "application/octet-stream"),
        (b"x" * (11 * 1024 * 1024), "image/png"),
    ],
    ids=["invalid-bytes", "mime-mismatch", "unsupported-mime", "oversized"],
)
async def test_invalid_reference_uploads_fail_closed(content: bytes, mime: str):
    system = make_system()
    app = create_app(system.manager, auth=system.auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = await login(client, "root", "root-password-for-tests")
        response = await client.post(
            "/v1/cases/api-case/reference-image",
            headers=headers("invalid-upload", token),
            files={"file": ("reference", content, mime)},
        )
        assert response.status_code in {413, 422}
        assert response.json()["code"] == "REFERENCE_UPLOAD_INVALID"


async def test_expired_upload_is_cleaned_and_cannot_create_case():
    system = make_system()
    app = create_app(system.manager, auth=system.auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = await login(client, "root", "root-password-for-tests")
        uploaded = await upload(client, token)
        reference = ArtifactRef.model_validate(
            uploaded.json()["reference_image_ref"]
        )
        system.clock.set(BASE + timedelta(hours=25))
        assert system.manager.cleanup_expired_reference_uploads() == 1
        with pytest.raises(KeyError):
            system.artifacts.get(reference.object_key)
        token = await login(client, "root", "root-password-for-tests")
        response = await client.post(
            "/v1/cases",
            headers=headers("expired-create", token),
            json=body(system.root_user.user_id)
            .model_copy(update={"reference_image_ref": reference})
            .model_dump(mode="json"),
        )
        assert response.status_code == 410
        assert response.json()["code"] == "REFERENCE_UPLOAD_EXPIRED"
