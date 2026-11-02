# AGENTS.md — Routing worker (Team JAVES-AI)

You are implementing the **Routing** module of a missing-person search system over CCTV footage. Build it as a small, pure, well-tested Python package. Read this whole file before writing code.

## 1. What Routing does

Perception (another module) turns video into *tracklets*: "a person was on camera B from 14:16:52 to 14:17:11, entered and left at these pixels, looks like the target with score 0.68". Routing turns those facts into meaning: which sequences of sightings are physically possible, where the person went, which cameras to check next, where they might be now.

Search runs in **rounds**. `step()` returns *probes* (camera + time window) → Perception checks only those → results come back → `step()` again. Routing keeps several **hypotheses** (competing routes) alive at once, because one sighting can often be explained by two different paths (ring graphs, cameras joined by several corridors).

## 2. Hard rules

- `step()` is a **pure function**: no DB, network, files, GPU, no `datetime.now()` (take `now` as an argument), no unseeded randomness. Same input → same output. It never mutates its input.
- Python 3.11+, pydantic v2, pyyaml, pytest. No numpy/torch needed.
- All times are timezone-aware datetimes; use UTC internally. Camera clock offsets are already applied upstream: do not apply them again.
- Perception is noisy. Never trust one score. Never crash on odd runtime data (no tracklets, duplicate ids, unknown camera → log and skip). Raise a clear error only for an invalid **site config**.
- You own contracts 2 (`ProbeRequest`), 8 (`SiteGraph`), 9 (`Hypotheses + Estimate`) and `search_state`. Contracts 3, 6, 7 are shared with other people: do not change them alone. If something is missing, add the question to `QUESTIONS.md`, continue with a documented default.
- Do not build extras: no UI, no DB layer, no job queue (the Case manager does that), no Re-ID code.

## 3. Layout

```
routing/
  contracts/   # pydantic: ProbeRequest, ProbeResult, Tracklet, Candidate, SiteGraph, StepResult, Hypothesis, Estimate
  site.py      # load + validate site.yaml
  timemodel.py # edge and dwell likelihoods
  update.py    # new visits -> hypotheses
  expand.py    # hypotheses -> probes
  predict.py   # estimate, mode, stop
  step.py      # init_state(), step()
  sim/         # graph generator, walker, fake Perception
tests/
params.yaml    # every tunable, with defaults
QUESTIONS.md
```

If the repo already has a shared `contracts/` folder, import 3, 6, 7 from there instead of redefining them.

## 4. Public API

```python
init_state(case, graph, params) -> SearchState
step(state, probe_results, graph, anchors, params, now) -> StepResult
# StepResult: state', probes[], hypotheses[], best, ambiguities[], rejected[], estimate, mode, done, stop_reason
```

- `SearchState` is JSON-serializable (it is stored by the Case store and passed back on the next call).
- `anchors`: `last_known {camera or zone, time}` and operator `confirm` / `reject` lists of tracklet ids.
- `ProbeResult`: `probe_id`, `status` (DONE | FAILED | TIMEOUT), tracklets and candidates. **DONE with nothing found is a valid, important answer.**
- `params.use_st = False` is the baseline: ignore graph and time, request all cameras in the window as one probe, finish in one round, rank by appearance only. Same interface.

## 5. Algorithm (implement in this order)

**Visits.** Merge tracklets of one camera with a gap ≤ `merge_gap_s` (10 s) into one visit. Exit and entry edges come from the `regions` polygon that contains `bbox_last` / `bbox_first`; no polygon hit means all edges of that camera are allowed. `open=true` → person still in view (`t_out = now`). `clipped_*` → that time is only a bound: skip the lateness penalty for that transition, keep the hard gate.

**Hypothesis** = ordered visits + current node + status (ALIVE | DORMANT | ENDED) + score + hidden-path alternatives (`via_alternatives`).

**Score** (log-likelihood, ≤ 0) = Σ visits `log(a)`, with `a` the calibrated appearance score clipped to [0.01, 1] + Σ transitions `log time_lik` + Σ misses `log(1 − p_det)`.
- `time_lik(Δt)` on an edge: **0 if `Δt < t_min`** (hard gate: physically impossible). Otherwise a peak-normalised lognormal with median `t_typ` and `σ = ln(t_max / t_typ) / 1.645` (`t_max` ≈ p95). Lateness lowers the score and never rejects.
- Blind nodes and OUTSIDE use `dwell {median_s, sigma}` the same way (heavy tail).
- A **miss** counts only if a DONE probe fully covered the hypothesis's predicted arrival window and found nothing. FAILED/TIMEOUT are not misses: re-issue the probe (≤ `max_retries`), then mark the window `unobserved` and report it in `estimate`.

**Update.** Attach new visits to hypotheses: gate by graph and time, each tracklet used once, nobody in two places at once. A visit that fits nothing may start a new hypothesis (earlier missed link, re-entry) with a miss penalty; keep it only above `new_hyp_floor`. Operator anchors: `confirm` → keep only hypotheses that contain that tracklet; `reject` → ban the tracklet.

**Merge + prune.** Hypotheses with identical visit sequences merge (best score wins, other hidden paths go to `via_alternatives`). Keep the best `B` (5). Drop invalid chains.

**Expand.** For each ALIVE hypothesis at node n, left at `t_out`: for every outgoing edge, probe the target camera in `[t_out + t_min, min(t_out + t_max, now)]`. Skip windows already probed (cache in state). Merge overlapping probes of one camera. Priority = hypothesis weight (softmax of scores) × P(arrival in window). **Ambiguity first:** if the top-2 scores differ by less than `amb_delta` (1.0) and the hypotheses diverge only on unchecked cameras, probes on those cameras get a priority bonus. Return the top `K` (4).

**Dormant and re-entry.** Window passed + empty probe + target is a blind node or OUTSIDE → DORMANT. Emit watch probes on the border cameras (OUTSIDE: all entrances, ordered by entrance `p`, the entrance used to leave slightly boosted) with `t_to = OPEN`, widening time shells until `max_gap_s` (2400 s), then ENDED. A sighting after a long gap becomes a visit flagged `LONG_GAP`: it needs operator confirmation and is never added to the main route automatically.

**Predict.** `last_observed` = last visit of the best hypothesis. `estimate.zones` = probabilities over blind nodes, OUTSIDE and unchecked neighbours, with `valid_until`.

**Mode** (set by Routing, the dashboard only displays it): `CATCHING_UP` (probing the past, faster than real time) → `LIVE_TRACKING` (best hypothesis has an open tracklet: OPEN probe on that camera and its neighbours) → `WAITING` (best is DORMANT or in a blind node: OPEN probes on borders; a new sighting goes back to `LIVE_TRACKING`) → `PAST_SUMMARY`. `CATCHING_UP` can go straight to `PAST_SUMMARY` if the person left before "now".

**Stop** (`done=True` plus `stop_reason`): all hypotheses ENDED, camera-minute budget spent, `max_gap` expired, or the operator closed the case.

## 6. site.yaml

Cameras (`zone`, `map_xy`, `regions`), directed `edges` with a unique `id` (parallel corridors are separate edges; `t_min` defaults to `d_m / max_speed` if omitted), `blind_nodes`, `outside.entrances {camera: p}`, `max_gap_s`. Validate on load: unique ids, edges point at existing nodes, `0 < t_min ≤ t_typ ≤ t_max`, entrance probabilities sum to 1 (normalise with a warning). Full example: architecture page, contract 8.

## 7. Simulator (build it before the algorithm is "done")

`sim/` has: a graph generator (ring of N cameras + chords, optional blind nodes, OUTSIDE with entrances); a walker that follows a known path with random dwell times; a fake Perception that answers probes from the walker's ground truth with `p_det`, score noise and some false candidates. Every test and benchmark uses it. It must also report **camera-minutes opened** per search versus a batch search over all cameras.

## 8. Tests (pytest, written together with the code)

1. **Gate.** Path A → B → E is accepted. A candidate on C that looks like the target (0.81) at Δt = 60 s after A is rejected (`t_min` A→C = 150 s) and gets a `reject_reason`.
2. **Ring + chord.** Ring of 8, chord 2–6, start at 0, hits on 1, 2, 6 → best hypothesis goes via the chord; 0 → 7 → 6 scores lower. With close scores, the next probes target the cameras where the two differ.
3. **Empty probe** lowers hypotheses that expected arrival there. FAILED/TIMEOUT does not.
4. **Lateness.** Second camera reached at `t_typ × 3` stays ALIVE: strong penalty on a corridor, small penalty through a blind node.
5. **Re-entry.** Exit via E1 at 15:00, hit on E2 at 15:27 → DORMANT in between, `LONG_GAP` flag, main route not auto-extended.
6. **Operator.** `confirm` prunes the others, `reject` bans the tracklet.
7. **Modes and stop.** All transitions in §5; budget and `max_gap` stops.
8. **Baseline.** `use_st=False` → one probe over all cameras, done in one round.
9. **Purity.** Same input twice → identical output; input state unchanged; `SearchState` survives a JSON round trip.
10. **Efficiency.** On a simulated 25-camera building the rounds find the person with far fewer camera-minutes than batch search. Print both numbers.

## 9. Order of work

1. contracts, `site.py`, loading tests → 2. `timemodel.py` → 3. simulator → 4. `update` + gate (tests 1, 4) → 5. `expand` + `step` loop on the simulator (tests 2, 3) → 6. dormant / re-entry (test 5) → 7. predict, modes, stop (test 7) → 8. anchors, baseline, purity, efficiency (tests 6, 8, 9, 10).

Run `pytest` after each stage, keep commits small. Put every tunable (`B, K, p_det, merge_gap_s, max_gap_s, amb_delta, max_retries, new_hyp_floor, camera-minute budget, max_speed`) in `params.yaml`, never in code.

## 10. Done when

All tests are green; the 8-camera ring + chord scenario runs end to end on the simulator; a 20-line README explains how to run the simulator and shows an example input and output JSON; `QUESTIONS.md` lists every assumption you made.
