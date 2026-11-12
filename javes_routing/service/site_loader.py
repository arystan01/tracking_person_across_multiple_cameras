"""Filesystem shell for loading an immutable Site Config YAML file."""
from __future__ import annotations

from pathlib import Path

import yaml

from ..core.params import RoutingParams
from ..core.site import parse_site


def load_site(
    path: str | Path,
    params: RoutingParams | None = None,
):
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Site config not found: {config_path}")
    with config_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if raw is None:
        raise ValueError("Empty site config")
    if not isinstance(raw, dict):
        raise ValueError("Site config root must be a mapping")
    return parse_site(raw, params=params)
