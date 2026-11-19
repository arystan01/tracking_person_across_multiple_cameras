"""Offline atomic publisher for immutable Site Config and Routing params."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from javes_routing.service.config_loader import load_routing_params
from javes_routing.service.site_loader import load_site

from .registry import model_checksum


class PublishConflictError(FileExistsError):
    """The requested immutable version already exists."""


def _safe_segment(value: str, label: str) -> str:
    if not value or value in {".", "..", "latest"} or "/" in value or "\\" in value:
        raise ValueError(f"invalid {label}")
    return value


def _safe_source_file(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise ValueError("invalid bundle-relative path")
    cursor = root
    for part in Path(relative).parts:
        if part in {"", ".", ".."}:
            raise ValueError("bundle path escapes source directory")
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Site Config source must not contain symlinks")
    path = (root / relative).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("bundle path escapes source directory") from exc
    if not path.is_file():
        raise FileNotFoundError(f"missing bundle file {relative!r}")
    return path


def _descriptor(data: bytes) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def preview_site(source: str | Path) -> dict:
    source_root = Path(source).resolve(strict=True)
    site_path = _safe_source_file(source_root, "site.yaml")
    graph = load_site(site_path)
    _safe_segment(graph.site_id, "site_id")
    _safe_segment(graph.version, "site version")
    assets: list[dict[str, object]] = []
    files = {"site.yaml": site_path.read_bytes()}
    if graph.map is not None:
        for floor in graph.map.floors:
            if floor.image is None:
                continue
            path = _safe_source_file(source_root, floor.image)
            data = path.read_bytes()
            files[floor.image] = data
            assets.append(
                {
                    "asset_id": floor.id,
                    "relative_path": floor.image,
                    **_descriptor(data),
                }
            )
    calibrated = sum(
        camera.calibration is not None for camera in graph.cameras.values()
    )
    return {
        "format": "javes-site-preview.v1",
        "site_id": graph.site_id,
        "version": graph.version,
        "graph_checksum": model_checksum(graph),
        "cameras": sorted(graph.cameras),
        "floors": sorted(
            floor.id for floor in (graph.map.floors if graph.map else [])
        ),
        "assets": sorted(assets, key=lambda item: str(item["asset_id"])),
        "calibration": {
            "configured": calibrated,
            "total_cameras": len(graph.cameras),
        },
        "validation": {
            "site_loaded": True,
            "walk_geometry_valid": True,
            "source_symlinks": False,
        },
        "files": {
            relative: _descriptor(data)
            for relative, data in sorted(files.items())
        },
    }


def publish_site(source: str | Path, registry_root: str | Path) -> dict:
    source_root = Path(source).resolve(strict=True)
    preview = preview_site(source_root)
    root = Path(registry_root).resolve()
    parent = root / "sites" / str(preview["site_id"])
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / str(preview["version"])
    if target.exists():
        raise PublishConflictError(f"immutable site version already exists: {target.name}")
    staging = parent / f".staging-{target.name}-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        file_descriptors = preview["files"]
        assert isinstance(file_descriptors, dict)
        for relative in sorted(file_descriptors):
            source_path = _safe_source_file(source_root, relative)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination)
            _fsync_file(destination)
        publish_manifest = {
            "format": "javes-site-bundle.v1",
            "site_id": preview["site_id"],
            "version": preview["version"],
            "graph_checksum": preview["graph_checksum"],
            "files": file_descriptors,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(publish_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _fsync_file(manifest_path)
        # Validate the bytes copied into staging before making them visible.
        staged_graph = load_site(staging / "site.yaml")
        if model_checksum(staged_graph) != preview["graph_checksum"]:
            raise ValueError("staged Site Config checksum changed during publish")
        for directory in sorted(
            {path.parent for path in staging.rglob("*") if path.is_file()},
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        try:
            os.rename(staging, target)
        except OSError as exc:
            if target.exists():
                raise PublishConflictError(
                    f"immutable site version already exists: {target.name}"
                ) from exc
            raise
        _fsync_directory(parent)
        return preview
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def preview_params(source: str | Path) -> dict:
    path = Path(source)
    if path.is_symlink():
        raise ValueError("Routing params source must not be a symlink")
    path = path.resolve(strict=True)
    params = load_routing_params(path)
    _safe_segment(params.version, "Routing params version")
    data = path.read_bytes()
    return {
        "format": "javes-routing-params-preview.v1",
        "version": params.version,
        "checksum": model_checksum(params),
        "source": _descriptor(data),
        "validation": {"params_loaded": True, "source_symlink": False},
    }


def publish_params(source: str | Path, registry_root: str | Path) -> dict:
    source_path = Path(source).resolve(strict=True)
    preview = preview_params(source)
    parent = Path(registry_root).resolve() / "routing_params"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / str(preview["version"])
    legacy_target = parent / f"{preview['version']}.yaml"
    if target.exists() or legacy_target.exists():
        raise PublishConflictError(
            f"immutable Routing params version already exists: {preview['version']}"
        )
    staging = parent / f".staging-{preview['version']}-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        staged_path = staging / "params.yaml"
        shutil.copyfile(source_path, staged_path)
        _fsync_file(staged_path)
        staged = load_routing_params(staged_path)
        if model_checksum(staged) != preview["checksum"]:
            raise ValueError("staged Routing params checksum changed during publish")
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "format": "javes-routing-params-bundle.v1",
                    "version": preview["version"],
                    "params_checksum": preview["checksum"],
                    "files": {"params.yaml": preview["source"]},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _fsync_file(manifest_path)
        _fsync_directory(staging)
        try:
            os.rename(staging, target)
        except OSError as exc:
            if target.exists() or legacy_target.exists():
                raise PublishConflictError(
                    f"immutable Routing params version already exists: {preview['version']}"
                ) from exc
            raise
        _fsync_directory(parent)
        return preview
    finally:
        if staging.exists():
            shutil.rmtree(staging)
