from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_network_policy_is_default_deny_with_explicit_main_boundaries() -> None:
    documents = list(
        yaml.safe_load_all(
            (ROOT / "deploy/kubernetes/network-policy.yaml").read_text(encoding="utf-8")
        )
    )
    default = next(item for item in documents if item["metadata"]["name"] == "default-deny")
    assert default["spec"]["podSelector"] == {}
    assert set(default["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    main = next(item for item in documents if item["metadata"]["name"] == "main-boundaries")
    assert main["spec"]["ingress"]
    assert main["spec"]["egress"]


def test_alerts_slos_and_runbooks_are_connected() -> None:
    alerts = yaml.safe_load((ROOT / "ops/alerts.yaml").read_text(encoding="utf-8"))
    slo = yaml.safe_load((ROOT / "ops/slo.yaml").read_text(encoding="utf-8"))
    names = {item["name"] for item in alerts["alerts"]}
    assert {"queue_stalled", "expired_leases", "deletion_failure", "archive_unavailable"} <= names
    assert slo["scope"] == "nvidia-12cam"
    for alert in alerts["alerts"]:
        assert (ROOT / alert["runbook"]).is_file()


def test_container_and_proxy_contract_run_unprivileged_with_tls() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    proxy = (ROOT / "deploy/reverse-proxy/nginx.conf").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "ssl_protocols TLSv1.2 TLSv1.3" in proxy
    assert "X-Forwarded-Proto https" in proxy
    assert "Strict-Transport-Security" in proxy


def test_ci_has_postgres_contention_and_security_gates() -> None:
    tests = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    security = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8"))
    assert "postgres" in tests["jobs"]
    assert tests["jobs"]["postgres"]["services"]["postgres"]["image"] == "postgres:16"
    assert {"dependency-and-sbom", "container"} <= set(security["jobs"])
