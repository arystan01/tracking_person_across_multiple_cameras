"""Durable historical-search frontier helpers."""
from __future__ import annotations

from datetime import datetime, timedelta

from .contracts import (
    Hypothesis,
    ProbeRequest,
    SearchFrontier,
    SearchState,
)
from .contracts.params import RoutingParams


def latest_observed_at(hypotheses: list[Hypothesis]) -> datetime | None:
    observations = [
        visit.t_out
        for hypothesis in hypotheses
        for visit in hypothesis.visits
    ]
    return max(observations, default=None)


def initialise_frontier(hypotheses: list[Hypothesis]) -> SearchFrontier | None:
    observed_at = latest_observed_at(hypotheses)
    if observed_at is None:
        return None
    return SearchFrontier(
        last_observed_at=observed_at,
        reached_at=observed_at,
    )


def sync_observation_frontier(
    state: SearchState,
    hypotheses: list[Hypothesis],
) -> None:
    observed_at = latest_observed_at(hypotheses)
    if observed_at is None:
        return
    frontier = state.search_frontier
    if frontier is None or observed_at > frontier.last_observed_at:
        open_probe_ids = sorted(
            {
                _root_probe_id(probe)
                for probe in state.pending_probes
                if probe.t_to is None
            }
        )
        state.search_frontier = SearchFrontier(
            last_observed_at=observed_at,
            reached_at=observed_at,
            shell_from=observed_at if open_probe_ids else None,
            required_probe_ids=open_probe_ids,
        )


def frontier_horizon(
    state: SearchState,
    params: RoutingParams,
) -> datetime | None:
    frontier = state.search_frontier
    if frontier is None:
        return None
    horizon = frontier.last_observed_at + timedelta(seconds=params.max_gap_s)
    if state.window_end is not None:
        horizon = min(horizon, state.window_end)
    return horizon


def _root_probe_id(probe: ProbeRequest) -> str:
    return probe.retry_of or probe.probe_id


def advance_finished_frontier(state: SearchState) -> None:
    frontier = state.search_frontier
    if frontier is None or not frontier.required_probe_ids:
        return
    pending_roots = {_root_probe_id(probe) for probe in state.pending_probes}

    open_roots = {
        _root_probe_id(probe)
        for probe in state.pending_probes
        if probe.t_to is None
    }
    if open_roots and open_roots.issubset(frontier.reached_at_by_probe):
        frontier.reached_at = max(
            frontier.reached_at,
            min(frontier.reached_at_by_probe[root] for root in open_roots),
        )

    completed = all(
        root not in pending_roots and root in frontier.reached_at_by_probe
        for root in frontier.required_probe_ids
    )
    if not completed:
        return
    if frontier.shell_to is not None:
        frontier.reached_at = max(frontier.reached_at, frontier.shell_to)
    frontier.shell_from = None
    frontier.shell_to = None
    frontier.required_probe_ids = []
    frontier.reached_at_by_probe = {}


def prepare_historical_shell(
    state: SearchState,
    params: RoutingParams,
    now: datetime,
) -> bool:
    """Start one finite shell. Return true while historical work is active."""
    frontier = state.search_frontier
    horizon = frontier_horizon(state, params)
    if frontier is None or horizon is None:
        return False
    if frontier.required_probe_ids:
        return frontier.shell_to is not None
    available_to = min(horizon, now)
    if frontier.reached_at >= available_to:
        frontier.shell_from = None
        frontier.shell_to = None
        return False
    frontier.shell_from = frontier.reached_at
    frontier.shell_to = min(
        frontier.reached_at + timedelta(seconds=params.historical_shell_s),
        available_to,
    )
    return True


def clamp_probe_to_shell(
    probe: ProbeRequest,
    state: SearchState,
) -> ProbeRequest | None:
    frontier = state.search_frontier
    if frontier is None:
        return probe
    if frontier.shell_to is None:
        if probe.t_to is None and probe.t_from < frontier.reached_at:
            return probe.model_copy(update={"t_from": frontier.reached_at})
        return probe
    shell_from = frontier.shell_from or frontier.reached_at
    t_from = max(probe.t_from, shell_from)
    t_to = (
        frontier.shell_to
        if probe.t_to is None
        else min(probe.t_to, frontier.shell_to)
    )
    if t_to <= t_from:
        return None
    return probe.model_copy(update={"t_from": t_from, "t_to": t_to})


def register_required_probes(
    state: SearchState,
    probes: list[ProbeRequest],
) -> None:
    frontier = state.search_frontier
    if frontier is None or not probes:
        return
    frontier.required_probe_ids = sorted(
        set(frontier.required_probe_ids)
        | {_root_probe_id(probe) for probe in probes}
    )
    if frontier.shell_from is None:
        frontier.shell_from = frontier.reached_at


def advance_empty_shell(state: SearchState) -> None:
    frontier = state.search_frontier
    if (
        frontier is None
        or frontier.shell_to is None
        or frontier.required_probe_ids
    ):
        return
    frontier.reached_at = max(frontier.reached_at, frontier.shell_to)
    frontier.shell_from = None
    frontier.shell_to = None


def record_probe_progress(
    state: SearchState,
    probe: ProbeRequest,
    *,
    processed_duration_s: float | None,
    coverage_ratio: float,
    terminal: bool,
) -> None:
    frontier = state.search_frontier
    if frontier is None:
        return
    root = _root_probe_id(probe)
    if root not in frontier.required_probe_ids:
        return
    if processed_duration_s is not None:
        reached_at = probe.t_from + timedelta(seconds=processed_duration_s)
    elif probe.t_to is not None:
        duration_s = max((probe.t_to - probe.t_from).total_seconds(), 0.0)
        reached_at = probe.t_from + timedelta(
            seconds=duration_s * coverage_ratio
        )
    else:
        reached_at = probe.t_from
    if probe.t_to is not None:
        reached_at = min(reached_at, probe.t_to)
    previous = frontier.reached_at_by_probe.get(root)
    if previous is None or reached_at > previous:
        frontier.reached_at_by_probe[root] = reached_at
    if terminal and root not in frontier.reached_at_by_probe:
        frontier.reached_at_by_probe[root] = probe.t_from
