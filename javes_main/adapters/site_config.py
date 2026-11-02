from __future__ import annotations

from javes_contracts import SiteGraph
from routing.contracts.params import RoutingParams


class InMemorySiteConfig:
    def __init__(self, params: RoutingParams) -> None:
        self._params: dict[str, RoutingParams] = {}
        self._current_params_version = params.version
        self.add_params(params)
        self._graphs: dict[tuple[str, str], SiteGraph] = {}
        self._assets: dict[tuple[str, str], dict[str, bytes]] = {}

    def add(
        self,
        graph: SiteGraph,
        *,
        floor_assets: dict[str, bytes] | None = None,
    ) -> None:
        key = (graph.site_id, graph.version)
        value = graph.model_copy(deep=True)
        assets = {
            name: bytes(payload) for name, payload in (floor_assets or {}).items()
        }
        previous = self._graphs.get(key)
        if previous is not None and previous != value:
            raise ValueError(f"site version {key!r} is immutable")
        previous_assets = self._assets.get(key)
        if previous_assets is not None and previous_assets != assets:
            raise ValueError(f"site assets for version {key!r} are immutable")
        self._graphs[key] = value
        self._assets[key] = assets

    def get_graph(self, site_id: str, version: str) -> SiteGraph:
        try:
            return self._graphs[(site_id, version)].model_copy(deep=True)
        except KeyError as exc:
            raise KeyError(f"unknown site/version {site_id!r}/{version!r}") from exc

    def get_floor_assets(self, site_id: str, version: str) -> dict[str, bytes]:
        if (site_id, version) not in self._graphs:
            raise KeyError(f"unknown site/version {site_id!r}/{version!r}")
        return {
            name: bytes(payload)
            for name, payload in self._assets[(site_id, version)].items()
        }

    def add_params(self, params: RoutingParams, *, make_current: bool = True) -> None:
        value = params.model_copy(deep=True)
        previous = self._params.get(value.version)
        if previous is not None and previous != value:
            raise ValueError(f"Routing params version {value.version!r} is immutable")
        self._params[value.version] = value
        if make_current:
            self._current_params_version = value.version

    def get_params(self, version: str | None = None) -> RoutingParams:
        selected = version or self._current_params_version
        try:
            return self._params[selected].model_copy(deep=True)
        except KeyError as exc:
            raise KeyError(f"unknown Routing params version {selected!r}") from exc
