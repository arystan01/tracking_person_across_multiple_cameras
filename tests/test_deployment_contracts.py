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
    default = next(
        item for item in documents if item["metadata"]["name"] == "default-deny"
    )
    assert default["spec"]["podSelector"] == {}
    assert set(default["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    main = next(
        item for item in documents if item["metadata"]["name"] == "main-boundaries"
    )
    assert main["spec"]["ingress"]
    assert main["spec"]["egress"]


def test_alerts_slos_and_runbooks_are_connected() -> None:
    alerts = yaml.safe_load((ROOT / "ops/alerts.yaml").read_text(encoding="utf-8"))
    slo = yaml.safe_load((ROOT / "ops/slo.yaml").read_text(encoding="utf-8"))
    names = {item["name"] for item in alerts["alerts"]}
    assert {
        "queue_stalled",
        "expired_leases",
        "deletion_failure",
        "archive_unavailable",
    } <= names
    assert slo["scope"] == "nvidia-12cam"
    assert slo["objectives"]["controls_api_p95_s"]["target"] == 2.0
    assert slo["objectives"]["routing_step_p95_s"]["target"] == 2.0
    assert slo["objectives"]["deletion_completion_p95_s"]["target"] == 60.0
    routing = next(
        item for item in alerts["alerts"] if item["name"] == "routing_latency"
    )
    assert routing["expression"] == "routing_trigger_latency.p95_s > 2"
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
    assert "COPY javes_telemetry javes_telemetry" in dockerfile
    assert "COPY javes_archive javes_archive" in dockerfile
    assert "AS perception-runtime" in dockerfile
    assert "AS clip-runtime" in dockerfile
    assert "apt-get install --no-install-recommends -y ffmpeg" in dockerfile


def test_staging_compose_is_api_first_private_and_persistent() -> None:
    compose = yaml.safe_load(
        (ROOT / "deploy/staging/compose.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert {
        "postgres",
        "migrate",
        "main-api",
        "scheduler",
        "routing",
        "site-config",
        "perception-worker",
        "clip-worker",
        "retention-worker",
        "archive",
        "otel-collector",
        "observability-backend",
    } <= set(services)
    assert compose["networks"]["javes-internal"]["internal"] is True
    assert {"postgres-data", "artifacts", "site-config", "worker-state"} <= set(
        compose["volumes"]
    )
    exposed = {name for name, service in services.items() if service.get("ports")}
    assert exposed == {"main-api", "reverse-proxy"}
    assert (
        services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    )
    assert (
        services["main-api"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert services["perception-worker"]["build"]["target"] == "perception-runtime"
    assert services["clip-worker"]["build"]["target"] == "clip-runtime"
    assert "/nvidia" in " ".join(services["clip-worker"]["command"])
    assert any(str(volume).endswith(":/nvidia:ro") for volume in services["clip-worker"]["volumes"])


def test_staging_templates_have_no_real_secrets_or_fake_services() -> None:
    example = (ROOT / "deploy/staging/.env.example").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/staging/compose.yaml").read_text(encoding="utf-8")
    assert "replace-with-random" in example
    assert "development-only" not in compose
    assert "javes_fakes" not in compose
    assert "fake" not in compose.lower()
    assert "JAVES_CLIP_WORKER_TOKEN" in compose
    assert "clip-worker:" in compose


def test_collector_exports_all_three_signals_to_local_backend() -> None:
    collector = yaml.safe_load(
        (ROOT / "deploy/otel/collector.yaml").read_text(encoding="utf-8")
    )
    assert set(collector["service"]["pipelines"]) == {"traces", "metrics", "logs"}
    for pipeline in collector["service"]["pipelines"].values():
        assert "otlp" in pipeline["receivers"]
        assert "otlphttp/backend" in pipeline["exporters"]


def test_ci_has_postgres_contention_and_security_gates() -> None:
    tests = yaml.safe_load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    )
    security = yaml.safe_load(
        (ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8")
    )
    assert "postgres" in tests["jobs"]
    assert tests["jobs"]["postgres"]["services"]["postgres"]["image"] == "postgres:16"
    assert {"dependency-and-sbom", "container"} <= set(security["jobs"])
