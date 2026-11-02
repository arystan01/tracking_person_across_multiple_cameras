# Routing demo

## Quick start

```bash
python -m pip install -e '.[test]'
python -m routing.demo --scenario normal --seed 42 --out demo_out
```

The command prints a JSON summary and creates `round-NNN.json`, `round-NNN-<floor>.svg`, and `index.html`. Open the HTML file and step through the Routing-produced images.

## Five-minute walkthrough

1. Run `normal`; show how only graph neighbours are probed and compare `camera_minutes` with `batch_camera_minutes`.
2. Open the viewer; red is the best route/current camera, orange dashed lines are alternatives or approximate geometry, green cameras have sightings, grey cameras were checked empty.
3. Run `late`; the route remains possible after `t_max` and receives a likelihood penalty instead of being hard-rejected.
4. Run `outage`; failed coverage is reported as unobserved rather than as a miss.
5. Run `reentry`; inspect `LONG_GAP` in round JSON and the dashed gap on the SVG.

## Scenarios

- `normal` / `ring8_chord`: 0 → 1 → 2 → 6, using the chord.
- `late`: transit-time model mismatch.
- `reentry`: exit through OUTSIDE and later sighting.
- `outage`: unavailable camera.
- `operator`: rejects the first returned tracklet.
- `--no-st`: all-camera, appearance-only batch round.

The simulator is deliberately small. It is suitable for deterministic integration tests, not for claiming production recall. Use the multi-seed benchmark and real annotated CCTV shadow runs before setting operational thresholds.

The demo uses fake Perception and does not exercise the real Clip worker. Their
required payloads and integration acceptance criteria are documented in
[`perception-clip.md`](perception-clip.md); real
worker compatibility remains unverified.

## Main Infra fake-stack demo

After installing the `infra` extra, run:

```bash
javes-main-demo
```

This second demo goes through the actual Controls ASGI handlers: ROOT login,
ROOT-created operator, forced password change, case creation, durable fake
Perception jobs, Routing and SVG, candidate confirmation, Clip export,
ROOT-authorised deletion and Retention redaction. It prints a compact JSON
summary and operational metrics. It uses no real video or external service.
