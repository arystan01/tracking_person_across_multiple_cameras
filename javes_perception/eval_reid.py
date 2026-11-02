from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from .models import OsnetEmbedder, pick_device


def parse_name(path: Path) -> tuple[int, int]:
    pid, rest = path.name.split("_", 1)
    return int(pid), int(rest[1])


def load_split(folder: Path) -> tuple[list[Path], np.ndarray, np.ndarray]:
    paths = sorted(p for p in folder.glob("*.jpg") if not p.name.startswith("-1"))
    ids = np.array([parse_name(p) for p in paths])
    return paths, ids[:, 0], ids[:, 1]


def embed(embedder, paths: list[Path], batch: int = 128) -> np.ndarray:
    out = []
    for i in range(0, len(paths), batch):
        out.append(embedder([cv2.imread(str(p)) for p in paths[i : i + batch]]))
    return np.concatenate(out)


def evaluate(qf, qpid, qcam, gf, gpid, gcam) -> dict:
    sims = qf @ gf.T
    rng = np.random.default_rng(0)
    ranks1, aps, pos, neg = [], [], [], []
    for i in range(len(qf)):
        keep = ~((gpid == qpid[i]) & (gcam == qcam[i]))
        s = sims[i][keep]
        match = gpid[keep] == qpid[i]
        if not match.any():
            continue
        order = np.argsort(-s)
        hits = match[order]
        ranks1.append(hits[0])
        cum = np.cumsum(hits)
        aps.append(float((cum[hits] / (np.flatnonzero(hits) + 1)).mean()))
        pos.extend(s[match].tolist())
        others = s[~match]
        neg.extend(rng.choice(others, size=min(50, len(others)), replace=False).tolist())
    return {
        "rank1": round(float(np.mean(ranks1)), 4),
        "mAP": round(float(np.mean(aps)), 4),
        "pos": pos,
        "neg": neg,
    }


def tracklet_queries(feats, pids, cams):
    groups = defaultdict(list)
    for i, key in enumerate(zip(pids, cams)):
        groups[key].append(i)
    keys = [k for k, idx in groups.items() if len(idx) >= 2 and k[0] > 0]
    mean = np.stack([feats[groups[k]].mean(axis=0) for k in keys])
    mean /= np.linalg.norm(mean, axis=1, keepdims=True)
    return mean, np.array([k[0] for k in keys]), np.array([k[1] for k in keys])


def fit_calibration(pos: list[float], neg: list[float]) -> tuple[float, float]:
    x = np.array(pos + neg)
    y = np.array([1.0] * len(pos) + [0.0] * len(neg))
    w = np.where(y == 1, 0.5 / len(pos), 0.5 / len(neg))
    a, b = 10.0, -6.0
    for _ in range(20000):
        p = 1 / (1 + np.exp(-(a * x + b)))
        a -= 5.0 * float(np.sum(w * (p - y) * x))
        b -= 5.0 * float(np.sum(w * (p - y)))
    return round(a, 3), round(b, 3)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Re-ID accuracy on Market-1501")
    parser.add_argument("--data", required=True, help="Market-1501 folder")
    parser.add_argument("--model", default="osnet_x1_0")
    parser.add_argument("--weights", required=True)
    parser.add_argument("--no-flip", action="store_true")
    parser.add_argument("--device", default="auto")
    args = parser.parse_args(argv)

    data = Path(args.data)
    embedder = OsnetEmbedder(args.model, args.weights, pick_device(args.device), flip=not args.no_flip)
    qp, qpid, qcam = load_split(data / "query")
    gp, gpid, gcam = load_split(data / "bounding_box_test")
    qf, gf = embed(embedder, qp), embed(embedder, gp)

    single = evaluate(qf, qpid, qcam, gf, gpid, gcam)
    tf, tpid, tcam = tracklet_queries(gf, gpid, gcam)
    tracklet = evaluate(tf, tpid, tcam, gf, gpid, gcam)
    a, b = fit_calibration(single["pos"], single["neg"])
    print(json.dumps({
        "model": args.model,
        "flip": not args.no_flip,
        "single_image": {"rank1": single["rank1"], "mAP": single["mAP"]},
        "tracklet_mean": {"rank1": tracklet["rank1"], "mAP": tracklet["mAP"]},
        "same_person_cos_median": round(float(np.median(single["pos"])), 3),
        "other_person_cos_p99": round(float(np.quantile(single["neg"], 0.99)), 3),
        "other_person_cos_median": round(float(np.median(single["neg"])), 3),
        "calibration": {"a": a, "b": b},
    }, indent=2))


if __name__ == "__main__":
    main()
