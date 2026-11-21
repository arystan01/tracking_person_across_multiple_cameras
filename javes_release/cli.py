"""CLI for the deterministic NVIDIA release sign-off report."""

from __future__ import annotations

import argparse
import html
import json
import subprocess
from pathlib import Path

from .acceptance import (
    AcceptancePolicy,
    LegacyNvidiaReleaseEvidence,
    NvidiaReleaseEvidence,
    NvidiaReleaseReport,
    evaluate_nvidia_release,
    legacy_failure_report,
    verify_evidence_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate an immutable JAVES-AI NVIDIA release evidence bundle"
    )
    parser.add_argument("evidence", type=Path, help="release evidence JSON")
    parser.add_argument(
        "--bundle-root",
        type=Path,
        required=True,
        help="root containing every bundle-relative evidence path",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        help=(
            "optional policy override JSON; the built-in NVIDIA policy uses "
            "2 s p95 for Controls/Routing and 60 s deletion SLA"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="directory for report.json and index.html",
    )
    parser.add_argument(
        "--verify-git-commit",
        action="store_true",
        help="require evidence.git_commit to equal the evaluated checkout HEAD",
    )
    parser.add_argument(
        "--checkout-root",
        type=Path,
        default=Path.cwd(),
        help="Git checkout used with --verify-git-commit",
    )
    return parser


def _html_report(report: NvidiaReleaseReport) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.check_id)}</td>"
        f"<td>{html.escape(item.category)}</td>"
        f"<td>{'PASS' if item.passed else 'BLOCKED'}</td>"
        f"<td>{html.escape(item.detail)}</td>"
        "</tr>"
        for item in report.checks
    )
    return f"""<!doctype html><html><head><meta charset='utf-8'>
<title>JAVES-AI NVIDIA release sign-off</title>
<style>body{{font:14px system-ui;max-width:1400px;margin:auto;padding:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #bbb;padding:7px;text-align:left}}.PASS{{color:#067647}}.BLOCKED{{color:#b42318}}</style></head>
<body><h1>NVIDIA release sign-off</h1>
<p>Status: <strong class='{report.status}'>{report.status}</strong></p>
<p>Release: {html.escape(report.release_version)} · commit {html.escape(report.git_commit)}</p>
<table><thead><tr><th>Check</th><th>Category</th><th>Status</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def run(
    evidence_path: Path,
    bundle_root: Path,
    output: Path,
    policy_path: Path | None = None,
    *,
    verify_git_commit: bool = False,
    checkout_root: Path | None = None,
) -> NvidiaReleaseReport:
    raw = json.loads(evidence_path.read_text(encoding="utf-8"))
    policy = (
        AcceptancePolicy.model_validate_json(policy_path.read_text(encoding="utf-8"))
        if policy_path is not None
        else AcceptancePolicy()
    )
    if raw.get("schema_version") == "javes-nvidia-release-evidence.v1":
        report = legacy_failure_report(
            LegacyNvidiaReleaseEvidence.model_validate(raw), policy
        )
    else:
        evidence = NvidiaReleaseEvidence.model_validate(raw)
        checkout_commit = None
        if verify_git_commit:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=(checkout_root or Path.cwd()),
                check=False,
                capture_output=True,
                text=True,
            )
            checkout_commit = result.stdout.strip() if result.returncode == 0 else ""
        verification = verify_evidence_bundle(
            evidence,
            bundle_root,
            evidence_manifest_path=evidence_path,
            checkout_commit=checkout_commit,
        )
        report = evaluate_nvidia_release(evidence, policy, verification)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output / "index.html").write_text(_html_report(report), encoding="utf-8")
    return report


def main() -> None:
    args = build_parser().parse_args()
    report = run(
        args.evidence,
        args.bundle_root,
        args.output,
        args.policy,
        verify_git_commit=args.verify_git_commit,
        checkout_root=args.checkout_root,
    )
    print(
        json.dumps(
            {
                "status": report.status,
                "failed_checks": [item.check_id for item in report.failures],
                "report": "report.json",
            },
            sort_keys=True,
        )
    )
    raise SystemExit(0 if report.status == "PASS" else 1)


if __name__ == "__main__":
    main()
