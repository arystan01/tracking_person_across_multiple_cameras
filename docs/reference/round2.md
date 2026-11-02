# ROUND2.md — Routing worker: review fixes and real-world extensions

> Historical review preserved verbatim. Current `DEMO.md` and `VERIFY.md` live
> in `docs/reference/`; the current architecture and roadmap live in
> `docs/architecture.md` and `docs/roadmap.md`.

Read `AGENTS.md` first, then this file. **Where they conflict, this file wins.** It is the result of a review of your first implementation (summary, README, QUESTIONS.md, params.yaml; the code itself was not reviewed). Some items below say "verify": do not assume they are broken, but do not claim they work without evidence.

## 0. How to work

- **The demo is tomorrow.** Priority order: Phase 0 first (demo kit), then Phase A, then B1, B2, B3, B5, B9, then the rest of Phase B, then Phase D, then Phase C. `DEMO.md` must describe the real current state at every moment: update it at the end of each item. If time runs out, an honest "TODO" in `DEMO.md` is better than a missing feature claimed as done.
- Work in the phases below (priority order above). Run `pytest` after every item and keep commits small.
- Never write "fully implemented" without evidence. Final report = `VERIFY.md` (every spec item with `file:line` or "missing") + test counts + the benchmark table from Phase D.
- Contracts 3, 6, 7 are shared with other people (Perception: Arystan, Case store/API: Sihyun, Dashboard: Jungwoo). Do not change them alone. Implement new fields as **optional with backward-compatible defaults**, and write every proposed change into `PROPOSALS.md` (field, why, default). Routing must work if the other side has not implemented the proposal yet.
- Keep every tunable in `params.yaml` and every site-specific number in `site.yaml`. Do not hard-code.
- Do not tune the simulator to make Routing pass (see Phase D).

## Phase 0 — Demo kit (do this FIRST)

Viktor presents his part (Routing) tomorrow. He needs to know exactly what exists, what does not, how to run it and how to play with it. In the first hour, before any refactor, produce the following. Everything must come from real runs of the code; nothing hard-coded or mocked up for show.

**0.1 `DEMO.md`.** Create it. If a similar file already exists, re-verify every statement against the code and rewrite it. Sections:
1. **What this is** (5 lines): what Routing does and where it sits (Perception → Routing ⇄ Case manager → Dashboard).
2. **Status table**: feature | `DONE` / `PARTIAL` / `TODO` | evidence (file and test name) | notes. Cover every item of AGENTS.md §5 and every item of this file. `DONE` only with a passing test. State clearly what is real (Routing code) and what is simulated or stubbed (Perception, video, Case store, API, dashboard).
3. **Quick start** from a clean checkout: Python version, install command, run tests, run the demo. Paste the real expected output snippets and how long each takes.
4. **How to feel it**: hands-on experiments, each with the exact command and what to look for in the output:
   - normal run on `ring8_chord` (watch the rounds, probes, hypotheses);
   - `--no-st` (baseline) and the camera-minutes comparison;
   - `--late-factor 3` (person arrives three times later than typical);
   - `--scenario reentry` (leaves through one entrance, returns through another);
   - a camera outage (`--outage B`);
   - an operator `confirm` / `reject` (`--reject-tracklet ID`; the revoke case stays `TODO` until C6);
   - drawing the route on the plan or graph (0.3).
5. **How to read the output**: legend for modes, probes, hypotheses, scores, estimate, flags (`LONG_GAP`, `GATE_OVERRIDE`, `via: UNKNOWN`).
6. **Known limitations and failure cases**, honestly: copy from `VERIFY.md` and `bench/RESULTS.md`; what is untested; what depends on other people (Arystan's score calibration, Sihyun's API, Jungwoo's dashboard).
7. **Contracts snapshot**: pointers to `examples/*.json` and the field list of contracts 2, 8, 9.
8. **A 5-minute demo script**: what to run, in which order, what to point at, what to say at each step. Then **at least 8 likely questions with honest answers** (why rounds and not batch search; why several hypotheses; how late arrival is handled; what if the graph is wrong; what is simulated; how success is measured; privacy; real-time).
9. **Troubleshooting**: the five most likely failures and their fixes.

**0.2 `python -m routing.demo`.** One command that runs a scripted scenario on the simulator and prints a readable round-by-round trace. Options: `--scenario`, `--seed`, `--no-st`, `--late-factor`, `--outage CAM`, `--reject-tracklet ID`, `--out demo_out/`. Per round print: round number, mode, probes issued (camera, window, reason), results (found / empty / failed), best hypothesis with score, alternatives, estimate. At the end print a summary: found or not, last-seen error, camera-minutes smart vs batch. Plain text by default, deterministic for a given seed.

**0.3 Visual output.** The same command writes `demo_out/` with one image per round (matplotlib, optional dependency with a plain-text fallback): graph or plan with nodes coloured by state (found / checked empty / not looked at), best path solid, alternatives dashed, current estimate marked. Add `demo_out/index.html`, a static page that steps through the rounds, so it can be shown on screen. If B9 is done and a plan image exists, draw on the plan using `map_xy` and `map_path`; otherwise draw the abstract graph. Simple and reliable beats pretty.

**0.4 Smoke test.** `tests/test_demo_smoke.py` runs the demo for two scenarios and asserts exit code 0 and that the expected files exist.

## Phase A — Verify (no code changes)

Run:

```
grep -rnE "LONG_GAP|unobserved|clipped|via_alternatives|PAST_SUMMARY|reject_reason|TIMEOUT|FAILED|valid_until|open" routing/
```

Create `VERIFY.md`: one row per item, evidence `file:line` or `MISSING`.
Items: `LONG_GAP` flag on sightings after a gap; FAILED/TIMEOUT → retry ≤ `max_retries`, then window `unobserved` (never counted as a miss); `open=true` tracklets (`t_out = now`); `clipped_*` (skip lateness penalty, keep hard gate); `via_alternatives`; probed-window cache; `reject_reason` on `rejected[]`; `estimate.valid_until`; mode `PAST_SUMMARY` and all mode transitions (including WAITING → LIVE_TRACKING); stop reasons `ALL_ENDED`, `BUDGET_SPENT`, `MAX_GAP`; ambiguity probes; OPEN watch probes ordered by entrance `p`; map fields (`map`, `floor`, `map_xy`, `map_path`) surviving `site.py` → `SiteGraph` → JSON unchanged (AGENTS.md only said "pass through"; check that nothing is dropped).
Anything `MISSING` becomes work in Phase B.

## Phase B — Fix defects (in this order)

**B1. Score formula (most important).** AGENTS.md §5 said "Σ log(a)". That is wrong: every found visit lowers the score, so a hypothesis that ignores a strong sighting beats the one that uses it.
- New visit term: `log(a / (1 − a))` (log-odds), `a` clipped to `[score_clip_min, 1 − score_clip_min]`. Example: a = 0.72 → +0.94; a = 0.5 → 0; a = 0.3 → negative.
- Time term stays `log time_lik` (≤ 0). Ignoring a tracklet costs 0.
- **Recompute every hypothesis score from its evidence (visits + probe coverage) on each step. Do not accumulate increments.** This is required for B5 and C6.
- Tests: (a) a strong sighting consistent with the graph raises the score of the hypothesis that uses it; (b) a weak one (a < 0.5) does not; (c) a = 1.0 does not produce infinity.

**B2. Hard gate and speeds.** `max_speed = 2.0 m/s` would reject a running child (3–4 m/s). A false hard reject is far more expensive than a soft one.
- Rename `max_speed` → `gate_speed` (default 4.0 m/s, keep reading `max_speed` as a deprecated alias). `t_min` default = `d_m / gate_speed`.
- If `t_typ` is omitted: `d_m / walk_speed` (1.3 m/s). If `t_max` is omitted: `t_typ × tmax_over_ttyp` (2.5).
- Gate rejects only if `Δt < t_min − clock_tolerance_s` (default 3 s; NVR clocks drift).
- Case profile: `CaseRequest.profile` ∈ ADULT | CHILD | ELDERLY (default ADULT) scales `t_min`, `t_typ`, `t_max` by `profiles[profile]` from params (starting guesses, see below).
- `rejected[]` keeps the top `rejected_top_n` (5) gated candidates with `appearance`, `reject_reason`, `gate_margin_s`. An operator `confirm` on a rejected candidate creates a hypothesis flagged `GATE_OVERRIDE` (same mechanism as C2 leak edges), never silently ignored.
- Tests: running child (4 m/s) over a 40 m edge is accepted with profile CHILD; `Δt` inside the clock tolerance is not rejected; override works.

**B3. Bounding-box format.** The page's contract 6 says `bbox = [x, y, w, h]` in pixels; you assumed `(x1, y1, x2, y2)`.
- Use **x, y, w, h** (per contract 6) until Arystan agrees otherwise. Isolate the format in one function at the contract boundary (`bbox_to_xyxy`), used everywhere else. Add a `PROPOSALS.md` line asking Arystan to confirm.
- Tests: polygon hit test with a known xywh box on both sides of a polygon edge.

**B4. Determinism across processes.** Python `set`s serialised as lists change order between processes (hash randomisation); one-process purity tests miss it.
- No iteration over sets or unordered dicts in decision paths. Serialise ids as **sorted** lists.
- Explicit tie-breaks everywhere (`(-score, id)` for top-B, `(-priority, camera, t_from)` for top-K).
- Generate ids from state counters or content, never `uuid4`/time.
- Test: run `step()` on the same input in two subprocesses with different `PYTHONHASHSEED`; the output JSON must be byte-identical.

**B5. Arrival-time mass instead of "fully covered window" (fixes the missing tail and the "arrived N + X minutes later" case).** This replaces the miss rule of AGENTS.md §5.
- For a hypothesis expecting arrival on camera X, let `F` = probability mass of the arrival-time distribution (lognormal of the edge, after profile scaling) inside windows already covered by DONE probes (capped at `now`), weighted by each probe's `coverage_ratio` (C3; default 1.0).
- Miss penalty = `log(1 − p_det(X) · F)`. Full coverage → `log(1 − p_det)`; unprobed tail → no penalty. A late arrival therefore removes the provisional miss automatically (scores are recomputed, B1).
- Expand: keep issuing lower-priority **tail probes** for the remaining mass up to `tail_factor × t_typ` (3.0), priority = hypothesis weight × remaining mass, until remaining mass < `tail_eps` (0.05). After that the hypothesis becomes DORMANT, not dead, and joins the watch logic.
- **Pass-through branch:** a (near-)full miss at X creates the branch "passed X unseen", so probes continue on X's neighbours with the window shifted by X's transit time. Without this, a person who skips one camera is lost.
- Tests: corridor edge, person arrives at 3 × `t_typ` → found by a tail probe, score lower than on-time arrival; person bypasses camera X (p_det = 0 there) → found on the next camera with a penalty.

**B6. Calling cadence.** `step()` is pure and takes `now`, so time-based transitions (DORMANT, `max_gap`, `valid_until`) happen only when it is called.
- Document in README: Case manager calls `step()` on every ProbeResult **and** every `heartbeat_s` (5 s) with an empty result list. `step()` with empty results must be cheap (< 50 ms on 25 cameras) and idempotent for the same `now`.
- Tests: an empty call at a later `now` triggers DORMANT, `MAX_GAP` stop and `valid_until` expiry; two calls with the same `now` give identical output.

**B7. Contract drift and README.** Align field names with the architecture page:
- Contract 2: `{probes[{probe_id, camera_id, t_from, t_to | "OPEN", priority, reason, hypothesis_ids[]}], mode, done, stop_reason}`. At the JSON boundary use the string `"OPEN"` (not `null`).
- Contract 9: `hypotheses[] {id, visits[], gap, score, status}`, `best`, `ambiguities[]`, `rejected[]`, `estimate {zones[{zone, p}], valid_until}`, `mode`. StepResult must include all of them.
- Replace the hand-written JSON in README with files in `examples/` generated by `examples/dump_examples.py` (really runs `step()` on the simulator). Add a test that loads every `examples/*.json` into the pydantic models.

**B8. Visit merging and candidates in crowds.** Open question 1 in QUESTIONS.md.
- Merge two tracklets of one camera only if both are above the candidate floor, the gap ≤ `merge_gap_s`, and the end of the first and the start of the second are close (distance between box centres ≤ `merge_dist_frac` × frame diagonal, default 0.25).
- Visit score = mean of the two highest tracklet scores (not max: max of noisy scores is biased up).
- Several different candidates in one probe are separate branches, not one merged visit. Keep the top `cands_per_probe` (3). A tracklet is used at most once per hypothesis.

**B9. Map data and Routing-owned route rendering.** Routing owns both the logical route and its drawing on the building plan. The Dashboard only displays the finished per-floor SVG, switches floors, and refreshes on `route_revision`.
- `site.yaml`: `map: {floors: [{id: 1F, image: maps/1F.png, size_px: [W, H], m_per_px: 0.05}]}` (`m_per_px` optional). Every camera, group and blind node gets `floor` and `map_xy` (pixels on that floor's image). Edges get optional `map_path: [[x, y], ...]` (pixels on the edge's floor, from start to end). Vertical edges (elevator, escalator, stairs between floors) have no path.
- Validate on load: floor ids exist; image files exist (warning if not); `map_xy` and `map_path` points lie inside `size_px`; vertical edges connect different floors.
- `SiteGraph` keeps every map field and serialises it back unchanged. Never drop or rename them.
- Add pure `render_route_maps(step_result, graph, floor_assets)` returning a standalone SVG per floor. Exact `map_path` is solid; an edge without it is an approximate dashed line. Blind/OUTSIDE/UNKNOWN/LONG_GAP/GATE_OVERRIDE and vertical transitions are dashed/marked. Alternatives, camera states, current camera, and estimates are rendered by Routing. Raster plans are embedded as data URIs; scripts and external URLs are forbidden.
- Live mode: Routing marks the current camera in the SVG. No pixel-to-map projection of the person's bounding box is needed or expected.
- Tests: a two-floor site loads; `SiteGraph` round trip keeps `floor`, `map_xy`, `map_path`; exact and approximate routes render correctly; vertical/gap/alternatives are dashed; out-of-bounds points fail; SVG is deterministic, valid XML, and safe.

## Phase D — Honest benchmark (do this before Phase C)

The first benchmark ("8.1 vs 125.0 camera-minutes") is circular: the simulator and Routing share one time model, the window is probably 5 min, one seed.

- Keep the simulator's generative model **independent of Routing's parameters**: `sim/` must not import `routing.timemodel` or read Routing's `params.yaml`; use its own `sim_params.yaml`.
- Realism knobs in `sim/`: per-edge time mismatch (true times = `site.yaml` × U(0.6, 1.4)), hidden edges (walker uses a door not in the graph with probability `p_hidden`), true `p_det` lower than the assumed one, Poisson false candidates per probe, appearance score degrading after long gaps, camera outages (windows with no detections and low coverage), clock skew ±3 s, overlapping-FOV groups (C1), crowd density, child speed profile.
- Runnable CLI, not only pytest: `python -m routing.sim.bench --scenario ring8_chord --seeds 200 --out bench/results.csv`. Scenarios: `ring8_chord`, `grid25`, `mall_two_floors` (elevator), `overlap_groups`, each with a clean and a `mismatch` variant.
- Report per scenario, as mean / median / p90 over seeds: found within time limit (rate), time-to-find, last-seen error (graph hops), camera-minutes (smart), camera-minutes (batch). State the batch baseline explicitly: **all cameras over [last_known, last_known + W]**, with W printed.
- Also report estimated **wall-clock**, not only camera-minutes: `wall_smart = Σ over rounds (max over that round's parallel probes of probe_overhead_s + probe_minutes·60 / decode_speed_x)`, `wall_batch = total_minutes·60 / decode_speed_x`. Params in `sim_params.yaml` (`probe_overhead_s` 2, `decode_speed_x` 20 as placeholders; Sihyun and Arystan replace them after measuring).
- The efficiency test asserts on aggregates over ≥ 30 seeds in the `mismatch` variant, initial targets: found rate ≥ 0.8, camera-minutes ≤ 0.5 × batch. Adjust targets after the first honest run; do not adjust the simulator.
- Write `bench/RESULTS.md` with the table and one paragraph on where Routing fails.

## Phase C — Real-world extensions

**C1. Overlapping fields of view (camera groups).** AGENTS.md validation `0 < t_min` and the rule "nobody in two places at once" break for cameras that see the same place.
- `site.yaml`: `groups: [{id, cameras: [...], zone}]`. Edges may start or end at a group. Inside a group no edge is needed. `t_min ≥ 0` is allowed only for edges that touch a group; everywhere else keep `t_min > 0`.
- Visits of cameras in one group merge into one **area visit** (`t_in` = min, `t_out` = max). Score = `max + (1 − group_corr) · second` of the per-camera log-odds (`group_corr` = 0.7: the two cameras' errors are correlated, so never sum). Exit edge from the camera whose region the last sighting hit.
- Probing: one primary camera per group (best `p_det`); probe the secondary only if the primary is empty with low coverage or the case is ambiguous. Budget counts every camera probed. If both are empty, miss = `log(1 − p1·F) + (1 − group_corr) · log(1 − p2·F)`.
- Output: one visit per area visit (shared `visit_id`); no duplicate candidates for the dashboard.
- Tests: two overlapping cameras see the person 3 s apart with overlapping intervals → one visit, no gate rejection, one hypothesis, score < sum of both; a group edge with `t_min = 0` loads; a non-group edge with `t_min = 0` is rejected by validation.

**C2. Incomplete graph (leak edges).** Staff doors and fire exits are never all in the graph; without them a real route is gated out.
- From every node there is an implicit edge to `UNKNOWN` with log prior `log(leak_prior)` (0.02) and dwell `unknown_dwell` (median 600 s, σ 1.2). A sighting reached through it is a visit with `via: UNKNOWN`, flagged `LONG_GAP`, needs operator confirmation, never auto-joins the main route.
- A gated candidate with appearance ≥ 0.8 automatically spawns such a low-weight hypothesis so it is visible to the operator (this is also the B2 override path).
- Tests: person reappears on a camera with no edge from the last one → appears as `via: UNKNOWN` hypothesis, not silently dropped; real route still wins when it exists.

**C3. Camera did not actually see (coverage, outages, archive).** An empty probe on a broken, covered or dark camera is not a miss.
- Proposed optional fields for `ProbeResult` (write to `PROPOSALS.md`, default values keep v1 behaviour): `coverage_ratio` (0–1, fraction of the window actually decoded; default 1.0), `n_tracklets_total` (everyone tracked in the window; default `null`), and a new `status: UNAVAILABLE` (outside archive retention).
- Use: if `coverage_ratio < min_coverage` (0.5) the window is `unobserved`, not a miss; otherwise it scales `F` (B5). If `n_tracklets_total == 0` for a window longer than `quiet_window_s` (120) on a camera marked `busy: true`, halve the coverage. `UNAVAILABLE` → `unobserved`; do not issue probes before `camera.available_from` when given.
- `site.yaml` per camera: `p_det` (default from params), `busy`, `available_from`.
- Tests: empty probe with coverage 0.2 does not lower any hypothesis; empty probe with coverage 1.0 does; UNAVAILABLE is not re-probed.

**C4. Elevators, escalators, rooms.** Blind nodes get `kind` (`room` | `elevator` | `escalator`), `borders`, `dwell {median_s, sigma}` and `exits {camera: p}`. An elevator is a blind node with exits on several floors and a heavy tail; an escalator has a narrow dwell. Test: person enters an elevator on floor 1 and appears on floor 3 after a heavy-tailed delay; probes go to all exits ordered by `p`.

**C5. Crowds and look-alikes.**
- Per-camera `appearance_floor` (higher on busy cameras).
- Visit term becomes `log(a/(1 − a)) − crowd_penalty_k · log(1 + n)`, `n` = candidates above floor in the same probe window (0.3 default). Do not trust a high score on a crowded camera.
- If the best hypothesis has ≥ `confirm_after_n` (2) unconfirmed visits and ambiguity is high, return `needs_review: [tracklet_ids]` so the dashboard asks the operator early (a confirm collapses the beam).
- On `confirm`, return `suggest_gallery_add: [tracklet_ids]`; the Case store forwards them to Perception (contract 5, source `confirmed_tracklet`). Routing does not touch embeddings.

**C6. Operator undo and manual sightings.** A wrong `confirm` currently destroys the true route.
- `SearchState.evidence` keeps a bounded log: probe results (ids, camera, window, status, coverage, tracklet and candidate metadata, **no images or embeddings**), decisions, manual sightings. Deleted with the case.
- `Anchors.revoke: [tracklet_ids]` cancels a confirm or reject; `step()` then **rebuilds** all hypotheses by replaying the evidence log (possible because `step` is pure and scores are recomputed, B1). Provide `rebuild(case, graph, params, evidence, now) -> StepResult`.
- `Anchors.manual_sightings: [{node | camera | zone, time, confidence, source}]`: a witness or guard saw the person; acts like `last_known` with time uncertainty `manual_time_sigma_s` (120 s). Treated as a visit without a tracklet; shown in the route with its source.
- Tests: confirm a wrong tracklet → beam collapses; revoke → the original best hypothesis is back, identical to a run that never had the confirm; a manual sighting starts or updates a hypothesis.

## Params additions (`params.yaml`)

```yaml
# round 2 (defaults are starting guesses; tune on the honest benchmark)
score_clip_min: 0.01          # a clipped to [clip, 1 - clip]
gate_speed: 4.0               # m/s, hard gate; max_speed kept as deprecated alias
walk_speed: 1.3               # m/s, derives t_typ when omitted
tmax_over_ttyp: 2.5           # derives t_max when omitted
clock_tolerance_s: 3.0
rejected_top_n: 5
tail_factor: 3.0
tail_eps: 0.05
heartbeat_s: 5
cands_per_probe: 3
merge_dist_frac: 0.25
leak_prior: 0.02
unknown_dwell: {median_s: 600, sigma: 1.2}
group_corr: 0.7
min_coverage: 0.5
quiet_window_s: 120
crowd_penalty_k: 0.3
confirm_after_n: 2
manual_time_sigma_s: 120
profiles:
  ADULT:   {tmin_k: 1.0, ttyp_k: 1.0, tmax_k: 1.0}
  CHILD:   {tmin_k: 0.6, ttyp_k: 1.0, tmax_k: 1.6}
  ELDERLY: {tmin_k: 1.0, ttyp_k: 1.5, tmax_k: 2.0}
```

## `site.yaml` additions

```yaml
map:                                                   # B9
  floors:
    - {id: 1F, image: maps/1F.png, size_px: [1600, 900], m_per_px: 0.05}
    - {id: 2F, image: maps/2F.png, size_px: [1600, 900]}
cameras:
  A: {zone: 1F-NW, floor: 1F, map_xy: [120, 340],       # plus existing regions, clock_offset_s
      p_det: 0.85, busy: true, appearance_floor: 0.35,
      available_from: "2026-11-05T13:00:00+09:00"}
edges:
  - {id: A-B-1, from: A, to: B, d_m: 25, t_min: 60, t_typ: 110, t_max: 180,   # plus existing fields
     map_path: [[120, 340], [210, 300], [300, 220]]}   # optional; absent = straight line, drawn dashed
groups:
  - {id: G_atrium, cameras: [B, C], zone: 1F-atrium}   # overlapping fields of view
blind_nodes:
  - {id: elev_1, kind: elevator, borders: [A, E],
     dwell: {median_s: 90, sigma: 0.8}, exits: {A: 0.5, E: 0.5}}
```

## `PROPOSALS.md` must contain (for other people, not for you to apply)

1. Bbox format: `x, y, w, h` (Arystan to confirm).
2. `ProbeResult`: optional `coverage_ratio`, `n_tracklets_total`, status `UNAVAILABLE` (Arystan, Sihyun).
3. `t_to` is `"OPEN"` on the wire, not `null` (Sihyun).
4. `step()` heartbeat every 5 s with empty results (Sihyun, Case manager).
5. `CaseRequest.profile`; `Anchors.revoke` and `Anchors.manual_sightings`; `StepResult.needs_review`, `suggest_gallery_add` (Sihyun, Jungwoo).
6. Dashboard should show the top `rejected[]` candidates with reasons and let the operator override (Jungwoo).
7. Map: Case Manager passes plan bytes to Routing, stores or serves `RenderedRouteBundle`, and `CaseView` carries ready SVG URLs/inline payloads. Dashboard only displays them and switches floors; it never draws route geometry (Sihyun, Jungwoo).

## Definition of done

- `DEMO.md` exists, is accurate for the current code (status table with evidence, quick start, "how to feel it", 5-minute demo script, honest limitations) and `python -m routing.demo` produces the trace and `demo_out/` from a clean checkout.
- All old tests still pass; new tests exist for B1–B9 and C1–C6 as listed.
- `VERIFY.md` has no `MISSING` rows left unexplained.
- `bench/RESULTS.md` has the table over ≥ 200 seeds per scenario (clean and mismatch), camera-minutes and estimated wall-clock, and says honestly where Routing fails.
- `README.md` documents the calling cadence (B6) and points to `examples/*.json` generated by running the code.
- `QUESTIONS.md` and `PROPOSALS.md` are up to date.
