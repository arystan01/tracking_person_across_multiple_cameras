"""Runnable Routing simulation with Routing-rendered SVG output."""
from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contracts import Anchors, CaseInfo, MapDef, MapFloor
from .contracts.params import RoutingParams
from .render import render_route_maps
from .sim.generator import generate_ring_graph
from .sim.perception import FakePerception
from .sim.config import SimParams
from .sim.walker import GroundTruthVisit, simulate_walk
from .step import init_state, step


@dataclass(frozen=True)
class DemoSummary:
    scenario: str
    rounds: int
    found_last_camera: bool
    camera_minutes: float
    batch_camera_minutes: float
    final_mode: str


def _scenario(name: str) -> tuple[list[str], bool, float, set[str]]:
    if name in {"normal", "ring8_chord"}:
        return ["cam_0", "cam_1", "cam_2", "cam_6"], False, 1.0, set()
    if name == "late":
        return ["cam_0", "cam_1", "cam_2", "cam_6"], False, 2.2, set()
    if name == "reentry":
        return ["cam_0", "OUTSIDE", "cam_4", "cam_5"], True, 1.0, set()
    if name == "outage":
        return ["cam_0", "cam_1", "cam_2", "cam_6"], False, 1.0, {"cam_1"}
    if name == "operator":
        return ["cam_0", "cam_1", "cam_2", "cam_6"], False, 1.0, set()
    raise ValueError(f"unknown scenario {name}")


def _write_viewer(output: Path, svg_files: list[str]) -> None:
    options = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>' for name in svg_files
    )
    document = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>JAVES-AI Routing demo</title>
<style>body{{font:16px system-ui;margin:1rem;background:#f1f5f9}}img{{display:block;max-width:100%;margin-top:1rem;background:white;border:1px solid #cbd5e1}}</style>
<label>Routing-rendered frame <select id="frame">{options}</select></label>
<img id="map" alt="Routing route map">
<script>const s=document.getElementById('frame'),m=document.getElementById('map');function show(){{m.src=s.value}}s.onchange=show;show()</script>
</html>"""
    (output / "index.html").write_text(document, encoding="utf-8")


def run_demo(
    scenario: str = "normal",
    seed: int = 42,
    use_st: bool = True,
    max_rounds: int = 30,
    output: Path | None = None,
    late_factor: float | None = None,
    outage: set[str] | None = None,
    sim_params: SimParams | None = None,
    n_cameras: int = 8,
    path_override: list[str] | None = None,
    chords: list[tuple[int, int]] | None = None,
    two_floors: bool = False,
) -> DemoSummary:
    sim_params = sim_params or SimParams(seed=seed)
    path, with_outside, scenario_late, scenario_outages = _scenario(scenario)
    if path_override is not None:
        path = path_override
    if late_factor is not None:
        scenario_late = late_factor
    if outage:
        scenario_outages |= outage
    goal_camera = "cam_4" if scenario == "reentry" else path[-1]
    graph = generate_ring_graph(
        n_cameras,
        chords=[(2, 6)] if chords is None and n_cameras == 8 else (chords or []),
        with_blind_nodes=True,
        with_outside=with_outside,
    )
    if two_floors:
        graph.map = MapDef(
            floors=[
                MapFloor(id="1F", size_px=(1200, 800)),
                MapFloor(id="2F", size_px=(1200, 800)),
            ]
        )
        for index, camera in enumerate(graph.cameras.values()):
            camera.floor = "1F" if index < n_cameras // 2 else "2F"
            camera.map_xy = (
                100.0 + (index % max(n_cameras // 2, 1)) * 150.0,
                250.0 if camera.floor == "1F" else 500.0,
            )
    started = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)
    truth = simulate_walk(
        graph,
        path,
        started,
        seed=seed,
        late_factor=scenario_late,
        camera_dwell_min_s=sim_params.camera_dwell_min_s,
        camera_dwell_max_s=sim_params.camera_dwell_max_s,
    )
    if scenario == "reentry":
        truth = [
            GroundTruthVisit("cam_0", started, started + timedelta(seconds=10)),
            GroundTruthVisit(
                "cam_4", started + timedelta(minutes=27),
                started + timedelta(minutes=27, seconds=10),
            ),
            GroundTruthVisit(
                "cam_5", started + timedelta(minutes=28),
                started + timedelta(minutes=28, seconds=15),
            ),
        ]
    perception = FakePerception(
        truth,
        p_det=min(sim_params.p_det, 0.75) if scenario_outages else sim_params.p_det,
        appearance_mean=sim_params.appearance_mean,
        noise_std=sim_params.appearance_noise_std,
        false_positive_rate=sim_params.false_positive_rate,
        seed=seed,
        unavailable_cameras=scenario_outages,
    )
    final_time = max(visit.t_out for visit in truth)
    last_known_time = truth[0].t_out
    params = RoutingParams(use_st=use_st)
    case = CaseInfo(
        case_id=f"demo-{scenario}-{seed}",
        target_id="simulated-target",
        start_time=started,
        window_start=started,
        window_end=final_time + timedelta(minutes=2),
        last_known_camera=path[0],
        last_known_time=last_known_time,
    )
    state = init_state(case, graph, params)
    now = last_known_time + timedelta(seconds=75)
    probe_results = []
    anchors = Anchors()
    svg_files: list[str] = []
    if output:
        output.mkdir(parents=True, exist_ok=True)

    last_result = None
    for round_index in range(1, max_rounds + 1):
        if scenario == "operator" and probe_results:
            ids = sorted(tracklet.id for result in probe_results for tracklet in result.tracklets)
            anchors = Anchors(reject_tracklet_ids=ids[:1]) if ids else Anchors()
        result = step(state, probe_results, graph, anchors, params, now)
        last_result = result
        if output:
            (output / f"round-{round_index:03d}.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
            bundle = render_route_maps(result, graph, {})
            for route_map in bundle.maps:
                name = f"round-{round_index:03d}-{route_map.floor_id}.svg"
                (output / name).write_text(route_map.svg, encoding="utf-8")
                svg_files.append(name)
        state = result.state
        if result.done:
            break
        active_probes = {probe.probe_id: probe for probe in result.probes}
        active_probes.update(
            {
                probe.probe_id: probe
                for probe in state.pending_probes
                if probe.t_to is None
            }
        )
        probe_results = perception.process_probes(
            list(active_probes.values()), now=now
        )
        now += timedelta(seconds=params.heartbeat_s if result.probes else 60)
        anchors = Anchors()
        if any(
            visit.camera == goal_camera
            for hypothesis in state.hypotheses
            for visit in hypothesis.visits
        ):
            break

    if output:
        _write_viewer(output, svg_files)
    found = any(
        visit.camera == goal_camera
        for hypothesis in state.hypotheses
        for visit in hypothesis.visits
    )
    window_minutes = max((case.window_end - case.window_start).total_seconds() / 60.0, 0.0)
    return DemoSummary(
        scenario=scenario,
        rounds=state.round_number,
        found_last_camera=found,
        camera_minutes=state.camera_minutes_used,
        batch_camera_minutes=len(graph.cameras) * window_minutes,
        final_mode=(last_result.mode.value if last_result else state.mode.value),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["normal", "ring8_chord", "late", "reentry", "outage", "operator"], default="normal")
    parser.add_argument("--seed", type=int, help="overrides sim_params.yaml seed")
    parser.add_argument("--no-st", action="store_true", help="use the appearance-only batch baseline")
    parser.add_argument("--late-factor", type=float)
    parser.add_argument("--outage", action="append", default=[])
    parser.add_argument("--max-rounds", type=int, default=30)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--sim-params", type=Path, help="independent simulator YAML")
    arguments = parser.parse_args()
    default_sim_path = Path("sim_params.yaml")
    sim_params = (
        SimParams.from_yaml(arguments.sim_params)
        if arguments.sim_params
        else SimParams.from_yaml(default_sim_path) if default_sim_path.exists() else None
    )
    summary = run_demo(
        scenario=arguments.scenario,
        seed=arguments.seed if arguments.seed is not None else (sim_params.seed if sim_params else 42),
        use_st=not arguments.no_st,
        max_rounds=arguments.max_rounds,
        output=arguments.out,
        late_factor=arguments.late_factor,
        outage=set(arguments.outage),
        sim_params=sim_params,
    )
    print(json.dumps(summary.__dict__, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
