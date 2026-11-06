"""Filesystem-backed loaders kept outside the pure Routing core."""
from __future__ import annotations

from pathlib import Path

import yaml

from ..core.params import RoutingParams
from ..core.sim.config import SimParams


def _load_yaml(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream) or {}
    if not isinstance(value, dict):
        raise ValueError("configuration root must be a mapping")
    return value


def load_routing_params(path: str | Path) -> RoutingParams:
    return RoutingParams.model_validate(_load_yaml(path))


def load_sim_params(path: str | Path) -> SimParams:
    return SimParams.model_validate(_load_yaml(path))
