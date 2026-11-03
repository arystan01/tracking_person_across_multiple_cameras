from __future__ import annotations

from urllib.parse import quote

import httpx

from javes_contracts import RoutingParamsResponse, SiteVersionResponse
from routing.contracts.params import RoutingParams


class HttpSiteConfigClient:
    """Synchronous client used by Main Infra and the Routing service shell."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 10.0,
        client: httpx.Client | None = None,
        default_params_version: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)
        self._headers = {"authorization": f"Bearer {token}"}
        self.default_params_version = default_params_version
        self._sites: dict[tuple[str, str], SiteVersionResponse] = {}
        self._params: dict[str, RoutingParamsResponse] = {}

    def site_snapshot(self, site_id: str, version: str) -> SiteVersionResponse:
        key = (site_id, version)
        if key not in self._sites:
            response = self._client.get(
                f"{self.base_url}/internal/v1/sites/{quote(site_id, safe='')}/versions/{quote(version, safe='')}",
                headers=self._headers,
            )
            response.raise_for_status()
            self._sites[key] = SiteVersionResponse.model_validate(response.json())
        return self._sites[key].model_copy(deep=True)

    def params_snapshot(self, version: str) -> RoutingParamsResponse:
        if version not in self._params:
            response = self._client.get(
                f"{self.base_url}/internal/v1/routing-params/{quote(version, safe='')}",
                headers=self._headers,
            )
            response.raise_for_status()
            self._params[version] = RoutingParamsResponse.model_validate(response.json())
        return self._params[version].model_copy(deep=True)

    def get_graph(self, site_id: str, version: str):
        return self.site_snapshot(site_id, version).graph

    def get_floor_assets(self, site_id: str, version: str) -> dict[str, bytes]:
        snapshot = self.site_snapshot(site_id, version)
        result: dict[str, bytes] = {}
        for item in snapshot.assets:
            response = self._client.get(
                f"{self.base_url}/internal/v1/sites/{quote(site_id, safe='')}/versions/{quote(version, safe='')}/assets/{quote(item.asset_id, safe='')}",
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.content
            import hashlib

            if len(data) != item.size_bytes or hashlib.sha256(data).hexdigest() != item.sha256:
                raise RuntimeError("Site Config asset checksum mismatch")
            result[item.asset_id] = data
        return result

    def get_params(self, version: str | None = None) -> RoutingParams:
        version = version or self.default_params_version
        if version is None:
            raise ValueError("an exact Routing params version is required")
        return RoutingParams.model_validate(self.params_snapshot(version).params)
