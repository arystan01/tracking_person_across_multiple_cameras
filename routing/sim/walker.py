"""Simulates a person walking through the graph."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from routing.contracts import SiteGraph

@dataclass
class GroundTruthVisit:
    camera: str
    t_in: datetime
    t_out: datetime

    def __post_init__(self) -> None:
        if self.t_in.tzinfo is None or self.t_in.utcoffset() is None:
            raise ValueError("ground-truth times must be timezone-aware")
        if self.t_out.tzinfo is None or self.t_out.utcoffset() is None:
            raise ValueError("ground-truth times must be timezone-aware")
        if self.t_out < self.t_in:
            raise ValueError("ground-truth visit ends before it starts")

def simulate_walk(
    graph: SiteGraph,
    path: list[str],
    start_time: datetime,
    seed: int = 42,
    late_factor: float = 1.0,
    camera_dwell_min_s: float = 5.0,
    camera_dwell_max_s: float = 30.0,
) -> list[GroundTruthVisit]:
    """Simulate walking along a sequence of nodes.
    
    path can include cameras, blind nodes, or 'OUTSIDE'.
    Returns visits for cameras only.
    """
    if start_time.tzinfo is None or start_time.utcoffset() is None:
        raise ValueError("start_time must be timezone-aware")
    rng = random.Random(seed)
    visits = []
    
    current_time = start_time
    
    for i in range(len(path)):
        current_node = path[i]
        
        # Dwell in current node
        if current_node in graph.cameras:
            # Dwell in camera
            dwell_s = rng.uniform(camera_dwell_min_s, camera_dwell_max_s) * late_factor
            t_out = current_time + timedelta(seconds=dwell_s)
            visits.append(GroundTruthVisit(
                camera=current_node,
                t_in=current_time,
                t_out=t_out,
            ))
            current_time = t_out
        elif current_node == 'OUTSIDE' and graph.outside:
            # Dwell outside
            # Lognormal based on outside dwell
            mu = math.log(graph.outside.dwell_median_s)
            sigma = graph.outside.dwell_sigma
            dwell_s = rng.lognormvariate(mu, sigma) * late_factor
            current_time += timedelta(seconds=dwell_s)
        else:
            # Blind node
            bn = next((b for b in graph.blind_nodes if b.id == current_node), None)
            if bn:
                mu = math.log(bn.dwell_median_s)
                sigma = bn.dwell_sigma
                dwell_s = rng.lognormvariate(mu, sigma) * late_factor
                current_time += timedelta(seconds=dwell_s)
                
        # Transition to next node
        if i + 1 < len(path):
            next_node = path[i+1]
            
            # Find edge
            edges = [e for e in graph.get_edges_from(current_node) if e.target == next_node]
            if edges:
                edge = edges[0]
                # Log normal based on t_typ, t_max
                mu = math.log(edge.t_typ)
                sigma = math.log(edge.t_max / edge.t_typ) / 1.645
                transit_s = 0.0
                while not (edge.t_min <= transit_s <= edge.t_max):
                    transit_s = rng.lognormvariate(mu, sigma)
                transit_s *= late_factor
                current_time += timedelta(seconds=transit_s)
            else:
                # No direct edge, just add 10s default
                current_time += timedelta(seconds=10.0 * late_factor)
                
    return visits
