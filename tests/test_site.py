import pytest
import yaml
from pathlib import Path
from javes_routing.core.site import parse_site
from javes_routing.service.site_loader import load_site
from javes_contracts import SiteGraph
from tests.support.nvidia_12cam_site import (
    CAMERAS as NVIDIA_CAMERAS,
    calibration_checksums,
    canonical_site,
)

# Helpers to generate valid configs
def valid_config():
    return {
        "cameras": {
            "cam_A": {"zone": "lobby", "map_xy": [10, 20]},
            "cam_B": {"zone": "corridor", "map_xy": [10, 50]},
            "cam_C": {"zone": "office", "map_xy": [10, 80]},
        },
        "edges": [
            {"id": "A_to_B", "source": "cam_A", "target": "cam_B", "t_min": 5, "t_typ": 10, "t_max": 30},
            {"id": "B_to_C", "source": "cam_B", "target": "cam_C", "t_min": 10, "t_typ": 20, "t_max": 50},
        ],
        "max_gap_s": 2400
    }

def test_1_valid_simple_site():
    # Test 1: Valid simple site with 3 cameras and 2 edges
    config = valid_config()
    site = parse_site(config)
    assert len(site.cameras) == 3
    assert len(site.edges) == 2
    assert site.max_gap_s == 2400

def test_2_edge_references_nonexistent_camera():
    # Test 2: Edge references non-existent camera → ValueError
    config = valid_config()
    config["edges"].append({
        "id": "A_to_X", "source": "cam_A", "target": "cam_X", "t_min": 5, "t_typ": 10, "t_max": 30
    })
    with pytest.raises(ValueError, match="invalid target 'cam_X'"):
        parse_site(config)

def test_3_invalid_timings():
    # Test 3: t_min > t_typ → ValueError
    config = valid_config()
    config["edges"][0]["t_min"] = 15
    config["edges"][0]["t_typ"] = 10
    with pytest.raises(ValueError, match="invalid timings"):
        parse_site(config)

def test_4_entrance_probabilities_normalized(caplog):
    # Test 4: Entrance probabilities don't sum to 1 → normalized with warning
    config = valid_config()
    config["outside"] = {
        "entrances": {"cam_A": 0.5, "cam_B": 0.1},
        "dwell_median_s": 300,
        "dwell_sigma": 1.0
    }
    site = parse_site(config)
    assert site.outside.entrances["cam_A"] == pytest.approx(0.5 / 0.6)
    assert site.outside.entrances["cam_B"] == pytest.approx(0.1 / 0.6)
    assert "Outside entrances probabilities sum to" in caplog.text

def test_5_duplicate_edge_ids():
    # Test 5: Duplicate edge IDs → ValueError
    config = valid_config()
    config["edges"].append(
        {"id": "A_to_B", "source": "cam_B", "target": "cam_C", "t_min": 5, "t_typ": 10, "t_max": 30}
    )
    with pytest.raises(ValueError, match="Duplicate edge ID found: A_to_B"):
        parse_site(config)

def test_6_edge_compute_t_min():
    # Test 6: Edge with d_m but no t_min → t_min computed using gate_speed (4.0 default)
    config = valid_config()
    config["edges"].append({
        "id": "C_to_A", "source": "cam_C", "target": "cam_A", "t_typ": 10, "t_max": 30, "d_m": 8.0
    })
    site = parse_site(config)
    edge = next(e for e in site.edges if e.id == "C_to_A")
    assert edge.t_min == 2.0

    # Also test max_speed fallback
    config_fallback = valid_config()
    config_fallback["max_speed"] = 2.0
    config_fallback["edges"].append({
        "id": "C_to_A", "source": "cam_C", "target": "cam_A", "t_typ": 10, "t_max": 30, "d_m": 8.0
    })
    site_fallback = parse_site(config_fallback)
    edge_fallback = next(e for e in site_fallback.edges if e.id == "C_to_A")
    assert edge_fallback.t_min == 4.0

def test_7_full_site_yaml_load_from_file(tmp_path):
    # Test 7: Full site.yaml load from file (create temp file)
    fixture_path = Path(__file__).parent / "fixtures" / "simple_site.yaml"
    site = load_site(fixture_path)
    assert len(site.cameras) == 3
    assert len(site.edges) == 4
    assert len(site.blind_nodes) == 1
    assert site.outside.entrances["cam_A"] == 0.7

def test_8_get_edges_from():
    # Test 8: SiteGraph.get_edges_from works correctly
    site = parse_site(valid_config())
    edges = site.get_edges_from("cam_A")
    assert len(edges) == 1
    assert edges[0].id == "A_to_B"

def test_9_get_edges_to():
    # Test 9: SiteGraph.get_edges_to works correctly
    site = parse_site(valid_config())
    edges = site.get_edges_to("cam_B")
    assert len(edges) == 1
    assert edges[0].id == "A_to_B"

def test_10_get_node_ids():
    # Test 10: SiteGraph.get_node_ids returns all nodes
    config = valid_config()
    config["blind_nodes"] = [{"id": "stairwell", "dwell_median_s": 20, "dwell_sigma": 0.5}]
    config["outside"] = {"entrances": {"cam_A": 1.0}}
    site = parse_site(config)
    nodes = site.get_node_ids()
    assert set(nodes) == {"cam_A", "cam_B", "cam_C", "stairwell", "OUTSIDE"}


def _polygon_site() -> dict:
    config = valid_config()
    for camera in config["cameras"].values():
        camera["floor"] = "1F"
    config["map"] = {
        "floors": [
            {
                "id": "1F",
                "size_px": [100, 100],
                "m_per_px": 0.1,
                "walkable_polygons": [[[0, 0], [100, 0], [100, 100], [0, 100]]],
                "blocked_polygons": [[[45, 20], [55, 20], [55, 80], [45, 80]]],
            }
        ]
    }
    config["walk_graph"] = {
        "nodes": {
            "left": {"floor_id": "1F", "map_xy": [20, 10]},
            "right": {"floor_id": "1F", "map_xy": [80, 10]},
        },
        "edges": [
            {
                "id": "safe",
                "from": "left",
                "to": "right",
                "floor_id": "1F",
                "width_m": 1.0,
                "branch_prior": 2.0,
                "bidirectional": True,
            }
        ],
    }
    return config


def test_walk_graph_accepts_corridor_inside_walkable_area() -> None:
    site = parse_site(_polygon_site())
    assert site.walk_graph is not None
    edge = site.walk_graph.edges[0]
    assert edge.width_m == 1.0
    assert edge.branch_prior == 2.0


def test_walk_graph_rejects_corridor_crossing_blocked_area() -> None:
    config = _polygon_site()
    config["walk_graph"]["nodes"]["left"]["map_xy"] = [20, 50]
    config["walk_graph"]["nodes"]["right"]["map_xy"] = [80, 50]
    with pytest.raises(ValueError, match="crosses a blocked area"):
        parse_site(config)


def test_walk_graph_validates_full_corridor_width() -> None:
    config = _polygon_site()
    config["walk_graph"]["nodes"]["left"]["map_xy"] = [20, 14]
    config["walk_graph"]["nodes"]["right"]["map_xy"] = [80, 14]
    config["walk_graph"]["edges"][0]["width_m"] = 2.0
    with pytest.raises(ValueError, match="crosses a blocked area"):
        parse_site(config)


def test_canonical_nvidia_site_has_all_cameras_and_obstacle_safe_walk_graph():
    repository_root = Path(__file__).resolve().parents[1]
    raw = canonical_site(repository_root)
    graph = parse_site(raw)
    assert set(graph.cameras) == set(NVIDIA_CAMERAS)
    assert graph.walk_graph is not None
    assert len(graph.walk_graph.edges) >= 24
    assert all(edge.bidirectional for edge in graph.walk_graph.edges)
    assert all(edge.width_m == pytest.approx(0.6) for edge in graph.walk_graph.edges)
    assert len(calibration_checksums(raw)) == 12


def test_canonical_calibration_checksum_changes_with_homography():
    repository_root = Path(__file__).resolve().parents[1]
    raw = canonical_site(repository_root)
    original = calibration_checksums(raw)
    raw["cameras"]["Cam001"]["calibration"]["homography"][0][0] += 0.001
    changed = calibration_checksums(raw)
    assert changed["Cam001"] != original["Cam001"]
    assert changed["Cam002"] == original["Cam002"]
