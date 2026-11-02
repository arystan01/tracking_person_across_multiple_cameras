"""Seeded fake Perception used by tests, demos and benchmarks."""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

from routing.contracts import Candidate, ProbeRequest, ProbeResult, ProbeStatus, Tracklet
from routing.sim.walker import GroundTruthVisit


class FakePerception:
    def __init__(
        self,
        ground_truth: list[GroundTruthVisit],
        p_det: float = 0.85,
        noise_std: float = 0.1,
        appearance_mean: float = 0.8,
        false_positive_rate: float = 0.0,
        seed: int = 42,
        camera_p_det: dict[str, float] | None = None,
        unavailable_cameras: set[str] | None = None,
    ):
        self.ground_truth = list(ground_truth)
        self.p_det = p_det
        self.noise_std = noise_std
        self.appearance_mean = appearance_mean
        self.false_positive_rate = false_positive_rate
        self.seed = seed
        self.camera_p_det = camera_p_det or {}
        self.unavailable_cameras = unavailable_cameras or set()

    def _rng(self, *parts: object) -> random.Random:
        text = "\x1f".join([str(self.seed), *(str(part) for part in parts)])
        value = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        return random.Random(value)

    @staticmethod
    def _tracklet_id(visit: GroundTruthVisit) -> str:
        text = f"{visit.camera}\x1f{visit.t_in.isoformat()}\x1f{visit.t_out.isoformat()}"
        return f"trk_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"

    def answer_probe(self, probe: ProbeRequest, now: datetime | None = None) -> ProbeResult:
        if probe.camera in self.unavailable_cameras:
            return ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.UNAVAILABLE,
                coverage_ratio=0.0,
                processed_duration_s=0.0,
                n_tracklets_total=0,
            )
        end = probe.t_to or now or (probe.t_from + timedelta(hours=24))
        effective_p_det = self.camera_p_det.get(probe.camera, self.p_det)
        tracklets: list[Tracklet] = []
        candidates: list[Candidate] = []
        total = 0
        for ground_truth in self.ground_truth:
            if ground_truth.camera != probe.camera:
                continue
            if ground_truth.t_in > end or ground_truth.t_out < probe.t_from:
                continue
            total += 1
            rng = self._rng(
                probe.probe_id,
                ground_truth.camera,
                ground_truth.t_in.isoformat(),
                now.isoformat() if probe.t_to is None and now is not None else "bounded",
            )
            if rng.random() >= effective_p_det:
                continue
            tracklet_id = self._tracklet_id(ground_truth)
            t_in = max(ground_truth.t_in, probe.t_from)
            t_out = min(ground_truth.t_out, end)
            is_open = probe.t_to is None and now is not None and now < ground_truth.t_out
            tracklets.append(
                Tracklet(
                    id=tracklet_id,
                    probe_id=probe.probe_id,
                    camera=probe.camera,
                    t_in=t_in,
                    t_out=t_out,
                    bbox_first=(0.0, 0.0, 10.0, 10.0),
                    bbox_last=(10.0, 10.0, 20.0, 20.0),
                    open=is_open,
                    clipped_in=t_in > ground_truth.t_in,
                    clipped_out=t_out < ground_truth.t_out,
                )
            )
            score = min(max(rng.gauss(self.appearance_mean, self.noise_std), 0.01), 1.0)
            candidates.append(
                Candidate(
                    id=f"cand_{tracklet_id[4:]}",
                    tracklet_id=tracklet_id,
                    score=score,
                    calibration_version="sim-v1",
                )
            )

        fp_rng = self._rng(probe.probe_id, "false-positive")
        if self.false_positive_rate > 0 and fp_rng.random() < self.false_positive_rate:
            total += 1
            span = max((end - probe.t_from).total_seconds(), 0.1)
            fp_time = probe.t_from + timedelta(seconds=fp_rng.uniform(0, span))
            tracklet_id = f"fp_{hashlib.sha256(probe.probe_id.encode()).hexdigest()[:12]}"
            tracklets.append(
                Tracklet(
                    id=tracklet_id,
                    probe_id=probe.probe_id,
                    camera=probe.camera,
                    t_in=fp_time,
                    t_out=min(fp_time + timedelta(seconds=2), end),
                    bbox_first=(0.0, 0.0, 10.0, 10.0),
                    bbox_last=(10.0, 10.0, 20.0, 20.0),
                )
            )
            candidates.append(
                Candidate(
                    id=f"cand_{tracklet_id}",
                    tracklet_id=tracklet_id,
                    score=min(max(fp_rng.gauss(0.4, 0.2), 0.01), 1.0),
                    calibration_version="sim-v1",
                )
            )
        duration = (
            0.0
            if probe.t_to is None and now is None
            else max((end - probe.t_from).total_seconds(), 0.0)
        )
        return ProbeResult(
            probe_id=probe.probe_id,
            status=ProbeStatus.DONE,
            tracklets=tracklets,
            candidates=candidates,
            coverage_ratio=1.0,
            processed_duration_s=duration,
            n_tracklets_total=total,
        )

    def process_probes(
        self, probes: list[ProbeRequest], now: datetime | None = None
    ) -> list[ProbeResult]:
        return [self.answer_probe(probe, now=now) for probe in probes]
