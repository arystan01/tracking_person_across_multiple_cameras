"""Cross-service boundary conventions for the v1alpha1 handoff."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProblemPolicy:
    slug: str
    code: str
    default_status: int
    retryable: bool
    consumer_action: str


PROBLEM_POLICIES: dict[str, ProblemPolicy] = {
    policy.slug: policy
    for policy in (
        ProblemPolicy("not-found", "NOT_FOUND", 404, False, "stop"),
        ProblemPolicy(
            "revision-conflict", "REVISION_CONFLICT", 409, True, "reload-and-retry"
        ),
        ProblemPolicy("worker-lease", "WORKER_LEASE_INVALID", 409, True, "reclaim"),
        ProblemPolicy("conflict", "CONFLICT", 409, False, "operator-or-new-command"),
        ProblemPolicy("authentication", "AUTHENTICATION_REQUIRED", 401, False, "login"),
        ProblemPolicy("authorization", "FORBIDDEN", 403, False, "stop"),
        ProblemPolicy("validation", "VALIDATION_FAILED", 422, False, "fix-request"),
        ProblemPolicy("timeout", "UPSTREAM_TIMEOUT", 504, True, "retry-with-backoff"),
        ProblemPolicy(
            "service-unavailable", "SERVICE_UNAVAILABLE", 503, True, "retry-with-backoff"
        ),
        ProblemPolicy("rate-limit", "RATE_LIMITED", 429, True, "retry-after"),
        ProblemPolicy("internal", "INTERNAL_ERROR", 500, True, "retry-with-backoff"),
        ProblemPolicy("http", "HTTP_ERROR", 400, False, "inspect-status"),
    )
}


def classify_problem(slug: str, status: int | None = None) -> ProblemPolicy:
    """Return the stable retry classification for an HTTP problem."""

    policy = PROBLEM_POLICIES.get(slug)
    if policy is not None:
        return policy
    effective_status = status or 500
    return ProblemPolicy(
        slug=slug,
        code="UNKNOWN_PROBLEM",
        default_status=effective_status,
        retryable=effective_status >= 500 or effective_status in {408, 429},
        consumer_action="retry-with-backoff" if effective_status >= 500 else "stop",
    )


def conventions_document() -> dict:
    return {
        "boundary_version": "javes-boundary.v1alpha1",
        "release_status": "BLOCKED",
        "release_blockers": [
            "dashboard-contract-tests",
            "clip-worker-contract-tests",
            "operator-decision-revoke-semantics",
            "legal-hold-authority",
        ],
        "timestamps": {
            "wire_format": "RFC3339",
            "timezone": "UTC",
            "naive_values": "REJECTED",
            "open_window": "OPEN only where explicitly declared by schema",
        },
        "delivery": {
            "worker_semantics": "AT_LEAST_ONCE",
            "callback_semantics": "IDEMPOTENT_BY_TASK_ID_AND_FINGERPRINT",
            "expired_lease": "REJECT_AND_RECLAIM",
            "deadline": "absolute UTC; workers must not start expired work",
            "retry": "only retryable failures; exponential backoff with bounded attempts",
            "cancellation": (
                "heartbeat cancel_requested=true is terminal for new work; a worker may "
                "return a cancellation acknowledgement but must not publish later artifacts"
            ),
        },
        "id_ownership": {
            "case_id": "Controls client; made immutable by Main",
            "decision_id": "Dashboard/Controls client",
            "command_id": "calling component",
            "task_id": "Main",
            "lease_token": "Main; opaque and secret",
            "probe_id": "Routing",
            "tracklet_id": "Perception",
            "candidate_id": "Perception",
            "route_revision": "Main after Routing CAS",
            "resource_id": "artifact producer or Main registration boundary",
        },
        "problems": {
            slug: asdict(policy) for slug, policy in sorted(PROBLEM_POLICIES.items())
        },
    }
