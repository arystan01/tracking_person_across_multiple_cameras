from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path

from javes_contracts import (
    RoutingParamsResponse,
    SiteAssetDescriptor,
    SiteVersionResponse,
)
from javes_routing.service.config_loader import load_routing_params
from javes_routing.service.site_loader import load_site


def model_checksum(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)  # type: ignore[union-attr]
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


class FilesystemSiteConfigRegistry:
    """Reads only exact immutable versions from a controlled directory tree.

    Layout::

        ROOT/sites/{site_id}/{version}/site.yaml
        ROOT/sites/{site_id}/{version}/...map assets...
        ROOT/routing_params/{version}.yaml
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._sites: dict[tuple[str, str], SiteVersionResponse] = {}
        self._assets: dict[tuple[str, str, str], tuple[bytes, str]] = {}
        self._params: dict[str, RoutingParamsResponse] = {}

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Site Config path escapes registry root") from exc
        return resolved

    @staticmethod
    def _safe_segment(value: str, label: str) -> str:
        if not value or value in {".", "..", "latest"} or "/" in value or "\\" in value:
            raise ValueError(f"invalid {label}")
        return value

    def _site_dir(self, site_id: str, version: str) -> Path:
        site_id = self._safe_segment(site_id, "site_id")
        version = self._safe_segment(version, "site version")
        return self._inside(self.root / "sites" / site_id / version)

    def _safe_file(self, base: Path, relative: str) -> Path:
        if not relative or "\\" in relative:
            raise ValueError("invalid Site Config relative path")
        unresolved = base / relative
        cursor = base
        for part in Path(relative).parts:
            if part in {"", ".", ".."}:
                raise ValueError("invalid Site Config relative path")
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("Site Config bundle must not contain symlinks")
        path = self._inside(unresolved)
        try:
            path.relative_to(base.resolve())
        except ValueError as exc:
            raise ValueError("site asset escapes its immutable version") from exc
        if not path.is_file():
            raise FileNotFoundError(f"missing Site Config file {relative!r}")
        return path

    @staticmethod
    def _file_descriptor(path: Path) -> dict[str, object]:
        data = path.read_bytes()
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }

    def _validate_publish_manifest(
        self,
        base: Path,
        *,
        site_id: str,
        version: str,
        checksum: str,
    ) -> None:
        manifest_path = base / "manifest.json"
        if not manifest_path.exists():
            return
        manifest_path = self._safe_file(base, "manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format") != "javes-site-bundle.v1":
            raise ValueError("unknown Site Config publish manifest format")
        if manifest.get("site_id") != site_id or manifest.get("version") != version:
            raise ValueError("Site Config publish manifest identity mismatch")
        if manifest.get("graph_checksum") != checksum:
            raise ValueError("Site Config graph checksum mismatch")
        files = manifest.get("files")
        if not isinstance(files, dict) or "site.yaml" not in files:
            raise ValueError("Site Config publish manifest is incomplete")
        for relative, expected in sorted(files.items()):
            if not isinstance(relative, str) or not isinstance(expected, dict):
                raise ValueError("invalid Site Config publish manifest entry")
            actual = self._file_descriptor(self._safe_file(base, relative))
            if actual != expected:
                raise ValueError(
                    f"Site Config published file checksum mismatch: {relative!r}"
                )

    def site(self, site_id: str, version: str) -> SiteVersionResponse:
        cache_key = (site_id, version)
        if cache_key in self._sites:
            return self._sites[cache_key].model_copy(deep=True)
        base = self._site_dir(site_id, version)
        graph = load_site(self._safe_file(base, "site.yaml"))
        if graph.site_id != site_id or graph.version != version:
            raise ValueError("site.yaml identity does not match requested immutable version")
        assets: list[SiteAssetDescriptor] = []
        if graph.map is not None:
            for floor in graph.map.floors:
                if floor.image is None:
                    continue
                path = self._safe_file(base, floor.image)
                data = path.read_bytes()
                self._assets[(site_id, version, floor.id)] = (
                    bytes(data),
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
                assets.append(
                    SiteAssetDescriptor(
                        asset_id=floor.id,
                        sha256=hashlib.sha256(data).hexdigest(),
                        size_bytes=len(data),
                        mime_type=self._assets[(site_id, version, floor.id)][1],
                    )
                )
        result = SiteVersionResponse(
            site_id=site_id,
            version=version,
            checksum=model_checksum(graph),
            graph=graph,
            assets=assets,
        )
        self._validate_publish_manifest(
            base,
            site_id=site_id,
            version=version,
            checksum=result.checksum,
        )
        self._sites[cache_key] = result.model_copy(deep=True)
        return result

    def asset(self, site_id: str, version: str, asset_id: str) -> tuple[bytes, str]:
        snapshot = self.site(site_id, version)
        descriptor = next(
            (item for item in snapshot.assets if item.asset_id == asset_id), None
        )
        if descriptor is None or snapshot.graph.map is None:
            raise KeyError(f"unknown site asset {asset_id!r}")
        data, mime_type = self._assets[(site_id, version, asset_id)]
        return bytes(data), mime_type

    def params(self, version: str) -> RoutingParamsResponse:
        version = self._safe_segment(version, "Routing params version")
        if version in self._params:
            return self._params[version].model_copy(deep=True)
        path = self.root / "routing_params" / f"{version}.yaml"
        if not path.exists():
            raise KeyError(f"unknown Routing params version {version!r}")
        path = self._safe_file(self.root / "routing_params", f"{version}.yaml")
        params = load_routing_params(path)
        if params.version != version:
            raise ValueError("params file identity does not match requested version")
        result = RoutingParamsResponse(
            version=version,
            checksum=model_checksum(params),
            params=params.model_dump(mode="json"),
        )
        self._params[version] = result.model_copy(deep=True)
        return result
