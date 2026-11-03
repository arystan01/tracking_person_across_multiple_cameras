from __future__ import annotations

from datetime import datetime

import httpx

from javes_contracts import (
    Anchors,
    CaseInfo,
    ProbeResult,
    RoutingInitRequest,
    RoutingInitResponse,
    RoutingStepRequest,
    RoutingStepResponse,
    SearchState,
    SiteGraph,
)
from javes_site_config.registry import model_checksum
from routing.contracts.params import RoutingParams


class HttpRoutingAdapter:
    """Main Infra adapter for the stateless Routing HTTP service."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)
        self._headers = {"authorization": f"Bearer {token}"}

    @staticmethod
    def _validate_checksums(response, graph: SiteGraph, params: RoutingParams) -> None:
        if response.site_checksum != model_checksum(graph):
            raise RuntimeError("Routing used a different immutable Site Config")
        if response.params_checksum != model_checksum(params):
            raise RuntimeError("Routing used different immutable parameters")

    def initialise(
        self,
        case: CaseInfo,
        graph: SiteGraph,
        params: RoutingParams,
        anchors: Anchors,
        now: datetime,
    ) -> SearchState:
        body = RoutingInitRequest(
            case=case,
            anchors=anchors,
            site_id=graph.site_id,
            site_version=graph.version,
            params_version=params.version,
            now=now,
        )
        response = self._client.post(
            f"{self.base_url}/internal/v1/routing/init",
            headers=self._headers,
            json=body.model_dump(mode="json", by_alias=True),
        )
        response.raise_for_status()
        result = RoutingInitResponse.model_validate(response.json())
        self._validate_checksums(result, graph, params)
        return result.state

    def advance(
        self,
        state: SearchState,
        probe_results: list[ProbeResult],
        graph: SiteGraph,
        anchors: Anchors,
        params: RoutingParams,
        now: datetime,
        floor_assets: dict[str, bytes],
    ):
        del floor_assets  # Routing fetches pinned assets from Site Config itself.
        body = RoutingStepRequest(
            state=state,
            probe_results=probe_results,
            anchors=anchors,
            site_id=graph.site_id,
            site_version=graph.version,
            params_version=params.version,
            now=now,
        )
        response = self._client.post(
            f"{self.base_url}/internal/v1/routing/step",
            headers=self._headers,
            json=body.model_dump(mode="json", by_alias=True),
        )
        response.raise_for_status()
        result = RoutingStepResponse.model_validate(response.json())
        self._validate_checksums(result, graph, params)
        return result.result
