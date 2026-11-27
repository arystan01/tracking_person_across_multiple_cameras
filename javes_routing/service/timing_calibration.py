"""Offline NVIDIA transition timing calibration using development oracle only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Sequence

import yaml

from javes_contracts import SiteGraph
from javes_routing.core.params import RoutingParams

from .config_loader import load_routing_params
from .site_loader import load_site


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(math.ceil(len(ordered) * 0.95) - 1, 0)]


def _shortest_timing_path(graph: SiteGraph, source: str, target: str, limit: int = 5):
    paths: list[list] = []

    def visit(node: str, path: list, seen: set[str]) -> None:
        if len(path) > limit:
            return
        if node == target and path:
            paths.append(path)
            return
        for edge in sorted(graph.get_edges_from(node), key=lambda item: item.id):
            if edge.target in seen:
                continue
            visit(edge.target, [*path, edge], {*seen, edge.target})

    visit(source, [], {source})
    return min(paths, key=lambda path: (sum(edge.t_typ for edge in path), [edge.id for edge in path])) if paths else []


def fit_development_timing(
    manifest_bytes: bytes,
    graph: SiteGraph,
    base: RoutingParams,
    *,
    version: str,
    min_samples: int = 3,
) -> RoutingParams:
    if min_samples < 2:
        raise ValueError("timing calibration requires at least two independent samples")
    manifest = json.loads(manifest_bytes)
    samples: dict[str, list[float]] = defaultdict(list)
    scenario_ids: list[str] = []
    edges_by_pair: dict[tuple[str, str], list] = defaultdict(list)
    for edge in graph.edges:
        edges_by_pair[(edge.source, edge.target)].append(edge)
    for scenario in manifest.get("scenarios", []):
        if scenario.get("quarantined") or scenario.get("split") != "development":
            continue
        scenario_ids.append(str(scenario["id"]))
        sightings = scenario.get("oracle", {}).get("sightings", [])
        for sighting in sightings:
            later = [
                candidate
                for candidate in sightings
                if candidate.get("camera_id") != sighting.get("camera_id")
                and float(candidate.get("t_in_s", -1)) >= float(sighting.get("t_out_s", 0))
            ]
            if not later:
                continue
            following = min(later, key=lambda item: (float(item["t_in_s"]), item["camera_id"]))
            pair = (str(sighting["camera_id"]), str(following["camera_id"]))
            duration = float(following["t_in_s"]) - float(sighting["t_out_s"])
            direct = edges_by_pair.get(pair, [])
            path = (
                [min(direct, key=lambda item: (item.t_typ, item.id))]
                if direct
                else _shortest_timing_path(graph, pair[0], pair[1])
            )
            if not path or duration < sum(edge.t_min for edge in path):
                continue
            total_typ = sum(edge.t_typ for edge in path)
            for edge in path:
                allocated = duration * edge.t_typ / total_typ
                samples[edge.id].append(max(edge.t_min, allocated))
    overrides: dict[str, dict[str, float]] = {}
    edges_by_id = {edge.id: edge for edge in graph.edges}
    for edge_id, values in sorted(samples.items()):
        if len(values) < min_samples:
            continue
        edge = edges_by_id[edge_id]
        t_typ = max(edge.t_min, median(values))
        t_max = max(t_typ, _percentile95(values), t_typ * 1.25)
        overrides[edge_id] = {
            "t_min": edge.t_min,
            "t_typ": round(t_typ, 6),
            "t_max": round(t_max, 6),
            "sample_count": float(len(values)),
        }
    return base.model_copy(
        update={
            "version": version,
            "edge_timing_overrides": overrides,
            "timing_calibration_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "timing_calibration_split": "development",
            "timing_calibration_scenario_ids": sorted(scenario_ids),
            "timing_calibration_min_samples": min_samples,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fit immutable NVIDIA Routing timing params")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--base-params", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--min-samples", type=int, default=3)
    args = parser.parse_args(argv)
    manifest_bytes = Path(args.manifest).read_bytes()
    result = fit_development_timing(
        manifest_bytes,
        load_site(args.site),
        load_routing_params(args.base_params),
        version=args.version,
        min_samples=args.min_samples,
    )
    Path(args.out).write_text(
        yaml.safe_dump(result.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
