"""Multi-seed simulator benchmark producing a stable CSV report."""
from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

from routing.demo import run_demo
from routing.sim.config import SimParams


def _variant_scenario(scenario: str) -> str:
    if scenario in {"ring8_chord", "grid25", "mall_two_floors"}:
        return "normal"
    if scenario == "reentry":
        return "reentry"
    raise ValueError(f"unsupported scenario {scenario}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["ring8_chord", "grid25", "mall_two_floors", "reentry"], default="ring8_chord")
    parser.add_argument("--variant", choices=["clean", "mismatch"], default="clean")
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    rows: list[dict[str, object]] = []
    for seed in range(arguments.seeds):
        if arguments.scenario == "grid25":
            n_cameras = 25
            path = [f"cam_{index}" for index in range(7)]
            chords = [(2, 10), (10, 18)]
        elif arguments.scenario == "mall_two_floors":
            n_cameras = 12
            path = ["cam_0", "cam_1", "cam_2", "cam_8"]
            chords = [(2, 8)]
        else:
            n_cameras = 8
            path = ["cam_0", "cam_1", "cam_2", "cam_6"]
            chords = [(2, 6)]
        summary = run_demo(
            scenario=_variant_scenario(arguments.scenario),
            seed=seed,
            late_factor=1.0 if arguments.variant == "clean" else 1.35,
            outage=set() if arguments.variant == "clean" else {"cam_1"},
            n_cameras=n_cameras,
            path_override=path,
            chords=chords,
            two_floors=arguments.scenario == "mall_two_floors",
            sim_params=SimParams(
                seed=seed,
                p_det=1.0 if arguments.variant == "clean" else 0.72,
                appearance_noise_std=0.04 if arguments.variant == "clean" else 0.16,
                false_positive_rate=0.0 if arguments.variant == "clean" else 0.05,
            ),
        )
        rows.append(
            {
                "scenario": arguments.scenario,
                "variant": arguments.variant,
                "seed": seed,
                "found": int(summary.found_last_camera),
                "rounds": summary.rounds,
                "smart_camera_minutes": round(summary.camera_minutes, 6),
                "batch_camera_minutes": round(summary.batch_camera_minutes, 6),
            }
        )
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    with arguments.out.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    found_rate = sum(int(row["found"]) for row in rows) / len(rows)
    smart = [float(row["smart_camera_minutes"]) for row in rows]
    ordered = sorted(smart)
    p90 = ordered[min(int(0.9 * len(ordered)), len(ordered) - 1)]
    print(
        f"found_rate={found_rate:.3f} smart_camera_minutes "
        f"mean={statistics.fmean(smart):.3f} median={statistics.median(smart):.3f} p90={p90:.3f}"
    )


if __name__ == "__main__":
    main()
