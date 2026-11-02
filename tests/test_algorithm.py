"""Integration tests for the algorithm."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from routing.contracts import (
    Anchors,
    Candidate,
    CaseInfo,
    Hypothesis,
    HypothesisStatus,
    ProbeRequest,
    ProbeResult,
    ProbeStatus,
    SearchMode,
    SearchState,
    StopReason,
    Tracklet,
    Visit,
)
from routing.contracts.params import RoutingParams
from routing.sim.generator import generate_ring_graph
from routing.sim.perception import FakePerception
from routing.sim.walker import GroundTruthVisit, simulate_walk
from routing.step import init_state, step
from routing.update import gate_visit_to_hypothesis

def test_1_gate():
    graph = generate_ring_graph(4)
    # A=cam_0, B=cam_1, C=cam_2. 0->1, 1->2. modify 0->2 to have t_min=150
    # Actually graph has e_0_to_1 and e_0_to_3 (since it's a ring of 4).
    # Let's add chord 0->2
    graph = generate_ring_graph(4, chords=[(0, 2)])
    for e in graph.edges:
        if e.source == 'cam_0' and e.target == 'cam_2':
            e.t_min = 150.0

    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    v1 = Visit(
        id='v1', camera='cam_0', t_in=now, t_out=now+timedelta(seconds=10),
        tracklet_ids=['t1'], entry_edges=[], exit_edges=[], score=0.9
    )
    v_c = Visit(
        id='vc', camera='cam_2', t_in=now+timedelta(seconds=70), t_out=now+timedelta(seconds=80),
        tracklet_ids=['tc'], entry_edges=[], exit_edges=[], score=0.81
    )
    h = Hypothesis(id='h1', visits=[v1], current_node='cam_0', status=HypothesisStatus.ALIVE, score=0.0)

    params = RoutingParams()
    trans_lik, via, reason = gate_visit_to_hypothesis(h.visits[-1], v_c, graph, params, now)

    assert trans_lik == 0.0
    assert reason is not None
    assert "hard gate" in reason or "no path" in reason

def test_2_ring_chord():
    graph = generate_ring_graph(8, chords=[(2, 6)])
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    case = CaseInfo(case_id='c', target_id='tgt', start_time=now)
    params = RoutingParams()
    state = init_state(case, graph, params)

    gt = [
        GroundTruthVisit('cam_0', now, now+timedelta(seconds=10)),
        GroundTruthVisit('cam_1', now+timedelta(seconds=30), now+timedelta(seconds=40)),
        GroundTruthVisit('cam_2', now+timedelta(seconds=60), now+timedelta(seconds=70)),
        GroundTruthVisit('cam_6', now+timedelta(seconds=120), now+timedelta(seconds=130)),
    ]
    perc = FakePerception(gt, p_det=1.0, noise_std=0.0)

    state.pending_probes = [ProbeRequest(probe_id='p1', camera='cam_0', t_from=now-timedelta(minutes=1), t_to=now+timedelta(minutes=1))]
    pr = perc.process_probes(state.pending_probes)
    res = step(state, pr, graph, Anchors(), params, now)
    state = res.state

    for i in range(15):
        now += timedelta(seconds=20)
        pr = perc.process_probes(state.pending_probes)
        res = step(state, pr, graph, Anchors(), params, now)
        state = res.state
        if any(v.camera == 'cam_6' for h in state.hypotheses for v in h.visits):
            break

    best = res.best
    assert best is not None
    cameras = [v.camera for v in best.visits]
    assert cameras == ['cam_0', 'cam_1', 'cam_2', 'cam_6']

def test_3_empty_probe():
    graph = generate_ring_graph(3)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    case = CaseInfo(case_id='c', target_id='tgt', start_time=now)
    params = RoutingParams(p_det=0.9)
    state = init_state(case, graph, params)

    # Hypothesis at cam_0
    v1 = Visit(id='v1', camera='cam_0', t_in=now, t_out=now, tracklet_ids=['t1'], entry_edges=[], exit_edges=[], score=0.9)
    h = Hypothesis(id='h1', visits=[v1], current_node='cam_0', status=HypothesisStatus.ALIVE, score=0.0)
    state.hypotheses = [h]

    # Probe on cam_1 covering arrival window
    probe = ProbeRequest(probe_id='p1', camera='cam_1', t_from=now, t_to=now+timedelta(minutes=5))
    state.pending_probes = [probe]

    pr = ProbeResult(probe_id='p1', status=ProbeStatus.DONE, tracklets=[], candidates=[])
    res = step(state, [pr], graph, Anchors(), params, now)

    h_after = res.hypotheses[0]
    from routing.update import compute_appearance_log_lik
    expected_base_score = compute_appearance_log_lik(0.9, params.score_clip_min)
    assert h_after.score < expected_base_score

def test_4_lateness():
    graph = generate_ring_graph(3)
    # A=cam_0, B=cam_1. e_0_to_1 has t_min=10, t_typ=20, t_max=60
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    v1 = Visit(id='v1', camera='cam_0', t_in=now, t_out=now, tracklet_ids=['t1'], entry_edges=[], exit_edges=[], score=0.9)
    h = Hypothesis(id='h1', visits=[v1], current_node='cam_0', status=HypothesisStatus.ALIVE, score=0.0)

    # arrive at t_typ*3 = 60s
    v2 = Visit(id='v2', camera='cam_1', t_in=now+timedelta(seconds=60), t_out=now+timedelta(seconds=70), tracklet_ids=['t2'], entry_edges=[], exit_edges=[], score=0.9)
    
    params = RoutingParams()
    trans_lik, _, _ = gate_visit_to_hypothesis(h.visits[-1], v2, graph, params, now)
    assert trans_lik < 0.0 # penalty

def test_5_re_entry():
    graph = generate_ring_graph(3, with_outside=True)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    case = CaseInfo(case_id='c', target_id='tgt', start_time=now)
    params = RoutingParams(max_gap_s=2400)
    state = init_state(case, graph, params)

    gt = [
        GroundTruthVisit('cam_0', now, now+timedelta(seconds=10)),
        GroundTruthVisit('cam_1', now+timedelta(seconds=2000), now+timedelta(seconds=2010)),
    ]
    perc = FakePerception(gt, p_det=1.0)

    # Manually build state to just after first exit to OUTSIDE
    v1 = Visit(id='v1', camera='cam_0', t_in=now, t_out=now+timedelta(seconds=10), tracklet_ids=['t1'], entry_edges=[], exit_edges=[], score=0.9)
    h = Hypothesis(id='h1', visits=[v1], current_node='OUTSIDE', status=HypothesisStatus.DORMANT, score=0.0)
    state.hypotheses = [h]

    # Process probe at cam_1 at 2000s
    now += timedelta(seconds=2000)
    pr = ProbeResult(probe_id='px', status=ProbeStatus.DONE, tracklets=[
        Tracklet(id='tx', camera='cam_1', t_in=now, t_out=now+timedelta(seconds=10), bbox_first=(0,0,10,10), bbox_last=(0,0,10,10))
    ], candidates=[Candidate(tracklet_id='tx', score=0.9, camera='cam_1', t_in=now, t_out=now+timedelta(seconds=10))])

    res = step(state, [pr], graph, Anchors(), params, now)
    # Check it merged or created new
    found = any(v.camera == 'cam_1' for hyp in res.hypotheses for v in hyp.visits)
    assert found

def test_6_operator():
    graph = generate_ring_graph(3)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    
    v1 = Visit(id='v1', camera='cam_0', t_in=now, t_out=now, tracklet_ids=['t1'], entry_edges=[], exit_edges=[], score=0.9)
    h1 = Hypothesis(id='h1', visits=[v1], current_node='cam_0', status=HypothesisStatus.ALIVE, score=0.0)

    v2 = Visit(id='v2', camera='cam_0', t_in=now, t_out=now, tracklet_ids=['t2'], entry_edges=[], exit_edges=[], score=0.8)
    h2 = Hypothesis(id='h2', visits=[v2], current_node='cam_0', status=HypothesisStatus.ALIVE, score=0.0)

    case = CaseInfo(case_id='c', target_id='tgt', start_time=now)
    params = RoutingParams()
    state = init_state(case, graph, params)
    state.hypotheses = [h1, h2]

    # confirm t1
    res = step(state, [], graph, Anchors(confirm=['t1']), params, now)
    assert len(res.hypotheses) == 1
    assert res.hypotheses[0].id == 'h1'

    # reject t2
    state.hypotheses = [h1, h2]
    res2 = step(state, [], graph, Anchors(reject=['t2']), params, now)
    assert len(res2.hypotheses) == 1
    assert res2.hypotheses[0].id == 'h1'

def test_7_modes_and_stop():
    graph = generate_ring_graph(3)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    case = CaseInfo(case_id='c', target_id='tgt', start_time=now)
    params = RoutingParams(camera_minute_budget=5)
    state = init_state(case, graph, params)

    state.camera_minutes_used = 10.0
    res = step(state, [], graph, Anchors(), params, now)
    assert res.done
    assert res.stop_reason == StopReason.BUDGET_SPENT
    assert res.mode == SearchMode.PAST_SUMMARY

def test_8_baseline():
    graph = generate_ring_graph(3)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    case = CaseInfo(case_id='c', target_id='tgt', start_time=now)
    params = RoutingParams(use_st=False)
    state = init_state(case, graph, params)

    h1 = Hypothesis(id='h1', visits=[], current_node='cam_0', status=HypothesisStatus.ALIVE)
    state.hypotheses = [h1]

    res = step(state, [], graph, Anchors(), params, now)
    assert len(res.probes) == 3

def test_9_purity():
    graph = generate_ring_graph(3)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    case = CaseInfo(case_id='c', target_id='tgt', start_time=now)
    params = RoutingParams()
    state = init_state(case, graph, params)

    res1 = step(state, [], graph, Anchors(), params, now)
    res2 = step(state, [], graph, Anchors(), params, now)

    # Identical output
    assert res1.state.model_dump() == res2.state.model_dump()

    # JSON round trip
    json_data = json.dumps(res1.state.model_dump(mode='json'))
    state_rt = SearchState.model_validate_json(json_data)
    assert state_rt.case_id == state.case_id

def test_10_efficiency():
    graph = generate_ring_graph(25, with_blind_nodes=True)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    path = ['cam_0', 'cam_1', 'cam_2', 'cam_3', 'cam_4']
    gt = simulate_walk(graph, path, now)
    
    perc = FakePerception(gt)
    case = CaseInfo(case_id='c', target_id='tgt', start_time=now)
    params = RoutingParams()
    state = init_state(case, graph, params)
    
    state.pending_probes = [ProbeRequest(probe_id='p1', camera='cam_0', t_from=now-timedelta(minutes=1), t_to=now+timedelta(minutes=1))]
    
    iters = 0
    while not state.done and iters < 20:
        now += timedelta(minutes=1)
        pr = perc.process_probes(state.pending_probes)
        res = step(state, pr, graph, Anchors(), params, now)
        state = res.state
        iters += 1
        
        if any(v.camera == 'cam_4' for h in state.hypotheses for v in h.visits):
            break

    smart_budget = state.camera_minutes_used
    batch_budget = 25 * 5  # 25 cameras * 5 minutes approx
    print(f"\nSmart: {smart_budget:.1f} min vs Batch: {batch_budget:.1f} min")
    assert smart_budget < batch_budget
