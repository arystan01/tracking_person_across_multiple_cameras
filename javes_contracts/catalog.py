"""Owned endpoint/DTO inventory for the boundary bundle."""

from __future__ import annotations


ENDPOINTS = [
    # Dashboard -> Controls.
    ("Controls", "Dashboard", "POST", "/v1/auth/login", "LoginRequest", "LoginResponse"),
    ("Controls", "Dashboard", "GET", "/v1/auth/me", None, "ActorView"),
    ("Controls", "Dashboard", "POST", "/v1/cases", "CreateCaseBody", "CaseView"),
    ("Controls", "Dashboard", "GET", "/v1/cases", None, "CaseSummary[]"),
    ("Controls", "Dashboard", "GET", "/v1/cases/{case_id}/view", None, "CaseView"),
    ("Controls", "Dashboard", "POST", "/v1/cases/{case_id}/decisions", "ReviewDecisionBody", "CaseView"),
    ("Controls", "Dashboard", "POST", "/v1/cases/{case_id}/commands/{action}", "CaseCommandBody", "CaseView"),
    ("Controls", "Dashboard", "GET", "/v1/cases/{case_id}/live", None, "binary"),
    ("Controls", "Dashboard", "GET", "/v1/cases/{case_id}/routes/{revision}/{floor_id}.svg", None, "image/svg+xml"),
    # Workers -> Main.
    ("Main", "Workers", "POST", "/internal/v1/workers/{component}/tasks/claim", "WorkerClaimRequest", "WorkerClaimResponse"),
    ("Main", "Workers", "POST", "/internal/v1/workers/{component}/tasks/{task_id}/heartbeat", "WorkerHeartbeatRequest", "WorkerHeartbeatResponse"),
    ("Main", "Workers", "POST", "/internal/v1/workers/{component}/tasks/{task_id}/result", "WorkerTaskResultRequest", "WorkerTaskAck"),
    ("Main", "Workers", "POST", "/internal/v1/workers/{component}/tasks/{task_id}/failure", "WorkerTaskFailureRequest", "WorkerTaskAck"),
    ("Main", "Perception", "POST", "/internal/v1/cases/{case_id}/live", "LiveFeedUpdate", "CaseView"),
    # Main -> Routing and Site Config.
    ("Routing", "Main", "POST", "/internal/v1/routing/init", "RoutingInitRequest", "RoutingInitResponse"),
    ("Routing", "Main", "POST", "/internal/v1/routing/step", "RoutingStepRequest", "RoutingStepResponse"),
    ("SiteConfig", "Main/Routing", "GET", "/internal/v1/sites/{site_id}/versions/{version}", None, "SiteVersionResponse"),
    ("SiteConfig", "Main/Routing", "GET", "/internal/v1/sites/{site_id}/versions/{version}/assets/{asset_id}", None, "binary"),
    ("SiteConfig", "Routing", "GET", "/internal/v1/routing-params/{version}", None, "RoutingParamsResponse"),
    # Workers -> NVIDIA archive registry/API.
    ("Archive", "Perception/Clip", "POST", "/v1/video-segments", "VideoSegmentRequest", "VideoSegmentResult"),
    ("Archive", "Operators", "GET", "/internal/v1/archive/inspect", None, "archive-inspection.v1"),
]


MODEL_OWNERS = {
    "Controls": [
        "ActorView", "CaseCommandBody", "CaseSummary", "CaseView",
        "CreateCaseBody", "LoginRequest", "LoginResponse", "ProblemDetails",
        "ReviewDecisionBody", "RouteMapView",
    ],
    "Main": [
        "ArtifactManifest", "WorkerClaimRequest", "WorkerClaimResponse",
        "WorkerHeartbeatRequest", "WorkerHeartbeatResponse", "WorkerTaskAck",
        "WorkerTaskEnvelope", "WorkerTaskFailureRequest", "WorkerTaskResultRequest",
    ],
    "Routing": [
        "ProbeRequest", "RoutingInitRequest", "RoutingInitResponse",
        "RoutingStepRequest", "RoutingStepResponse", "SiteGraph",
    ],
    "Perception": [
        "Candidate", "LiveFeedUpdate", "PerceptionProbeJob", "ProbeResult",
        "TargetGalleryCommand", "TargetGalleryResult", "Tracklet",
    ],
    "Retention": ["RetentionCommand", "RetentionResource", "RetentionResult"],
    "SiteConfig": ["RoutingParamsResponse", "SiteVersionResponse"],
    "Archive": ["VideoSegmentRequest", "VideoSegmentResult"],
    "Clip": [
        "ClipRecord", "ClipStateCommand", "CutClipCommand",
        "ExportClipsCommand", "ExportClipsResult",
    ],
}


def inventory_document() -> dict:
    endpoints = [
        {
            "owner": owner,
            "consumer": consumer,
            "method": method,
            "path": path,
            "request": request,
            "response": response,
        }
        for owner, consumer, method, path, request, response in ENDPOINTS
    ]
    return {
        "boundary_version": "javes-boundary.v1alpha1",
        "models": {owner: sorted(models) for owner, models in sorted(MODEL_OWNERS.items())},
        "endpoints": sorted(endpoints, key=lambda item: (item["path"], item["method"])),
    }
