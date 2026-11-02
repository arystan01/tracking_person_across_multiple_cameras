# Simulator benchmark results

Command (200 deterministic seeds per row):

```bash
python -m routing.sim.bench --scenario ring8_chord --variant clean --seeds 200 --out bench/ring8_clean.csv
python -m routing.sim.bench --scenario ring8_chord --variant mismatch --seeds 200 --out bench/ring8_mismatch.csv
```

| Variant | Found | Smart camera-min mean / median / p90 | Batch camera-min mean / median / p90 | Rounds mean / median / p90 |
|---|---:|---:|---:|---:|
| clean | 99.5% | 22.545 / 18.816 / 40.082 | 39.547 / 39.343 / 44.734 | 14.575 / 14 / 21 |
| mismatch | 43.0% | 13.617 / 14.500 / 14.500 | 47.789 / 47.514 / 54.791 | 20.725 / 30 / 30 |

The batch baseline is every camera over the complete case window `[window_start, window_end]`; the exact window varies with the seeded walk and its camera-minutes are recorded per row.

The clean result meets the simulator target of at least 95% found while using fewer mean camera-minutes than batch. The mismatch case deliberately combines lower `p_det`, higher score noise, false positives, slower transit, and an outage on `cam_1`. Its 43% found rate is a failure signal, not a routing-efficiency win: lower smart camera-minutes partly come from searches that do not recover the target. This variant needs calibration and outage/hidden-path hardening before production use.
