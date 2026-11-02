"""Generator for test graphs."""
from __future__ import annotations

from routing.contracts import SiteGraph, CameraDef, EdgeDef, BlindNodeDef, OutsideDef

def generate_ring_graph(
    n_cameras: int,
    chords: list[tuple[int, int]] = None,
    with_blind_nodes: bool = False,
    with_outside: bool = False,
) -> SiteGraph:
    """Generate a ring of N cameras, with optional chords, blind nodes, and OUTSIDE."""
    if chords is None:
        chords = []

    cameras = {}
    edges = []
    blind_nodes = []
    outside = None

    for i in range(n_cameras):
        cam_id = f"cam_{i}"
        cameras[cam_id] = CameraDef(id=cam_id, zone="ring")

        # Ring edges
        next_i = (i + 1) % n_cameras
        prev_i = (i - 1) % n_cameras

        edges.append(EdgeDef(
            id=f"e_{i}_to_{next_i}",
            source=f"cam_{i}",
            target=f"cam_{next_i}",
            t_min=10.0,
            t_typ=20.0,
            t_max=60.0,
        ))
        edges.append(EdgeDef(
            id=f"e_{i}_to_{prev_i}",
            source=f"cam_{i}",
            target=f"cam_{prev_i}",
            t_min=10.0,
            t_typ=20.0,
            t_max=60.0,
        ))

    # Chords
    for (u, v) in chords:
        edges.append(EdgeDef(
            id=f"e_{u}_to_{v}",
            source=f"cam_{u}",
            target=f"cam_{v}",
            t_min=30.0,
            t_typ=50.0,
            t_max=120.0,
        ))
        edges.append(EdgeDef(
            id=f"e_{v}_to_{u}",
            source=f"cam_{v}",
            target=f"cam_{u}",
            t_min=30.0,
            t_typ=50.0,
            t_max=120.0,
        ))

    if with_blind_nodes:
        # Add a blind node connected to 0 and n//2
        bn_id = "blind_0"
        blind_nodes.append(BlindNodeDef(
            id=bn_id,
            dwell_median_s=60.0,
            dwell_sigma=0.5,
        ))
        edges.append(EdgeDef(id=f"e_0_to_{bn_id}", source="cam_0", target=bn_id, t_min=5, t_typ=10, t_max=30))
        edges.append(EdgeDef(id=f"e_{bn_id}_to_0", source=bn_id, target="cam_0", t_min=5, t_typ=10, t_max=30))
        
        half = n_cameras // 2
        edges.append(EdgeDef(id=f"e_{half}_to_{bn_id}", source=f"cam_{half}", target=bn_id, t_min=5, t_typ=10, t_max=30))
        edges.append(EdgeDef(id=f"e_{bn_id}_to_{half}", source=bn_id, target=f"cam_{half}", t_min=5, t_typ=10, t_max=30))

    if with_outside:
        entrances = {"cam_0": 0.5, f"cam_{n_cameras//2}": 0.5}
        outside = OutsideDef(
            entrances=entrances,
            dwell_median_s=300.0,
            dwell_sigma=1.0,
        )

    return SiteGraph(
        cameras=cameras,
        edges=edges,
        blind_nodes=blind_nodes,
        outside=outside,
        max_gap_s=2400.0,
    )
