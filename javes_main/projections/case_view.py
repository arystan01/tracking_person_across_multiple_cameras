from __future__ import annotations

from datetime import datetime

from javes_contracts import ClipLifecycleState, Hypothesis

from javes_main.domain.models import (
    CaseRecord,
    CaseView,
    JobRecord,
    OutboxRecord,
    ProbeResultRecord,
    RouteMapView,
    RoutingTriggerRecord,
)


def _published_live(case: CaseRecord, now: datetime | None) -> dict | None:
    live_feed = case.live_feed
    if live_feed is None or (
        live_feed.expires_at is not None
        and now is not None
        and live_feed.expires_at <= now
    ):
        return None
    return {
        "camera_id": live_feed.camera_id,
        "tracklet_id": live_feed.tracklet_id,
        "bbox": list(live_feed.bbox),
        "observed_at": live_feed.observed_at.isoformat(),
        "age_s": (
            max((now - live_feed.observed_at).total_seconds(), 0.0)
            if now is not None
            else None
        ),
        "stream_kind": live_feed.stream_kind,
        "stream_url": f"/v1/cases/{case.request.case_id}/live",
        "expires_at": live_feed.expires_at.isoformat() if live_feed.expires_at else None,
    }


def _candidate_views(case: CaseRecord) -> list[dict]:
    values: list[dict] = []
    for candidate_id, raw in case.candidates.items():
        candidate = dict(raw)
        tracklet_id = str(candidate.get("tracklet_id", ""))
        tracklet = case.tracklets.get(tracklet_id)
        if tracklet is not None:
            candidate.update(
                {
                    "camera_id": tracklet.get("camera_id"),
                    "t_start": tracklet.get("t_start"),
                    "t_end": tracklet.get("t_end"),
                    "bbox_first": tracklet.get("bbox_first"),
                    "bbox_last": tracklet.get("bbox_last"),
                    "thumbnail_refs": list(tracklet.get("crop_paths") or []),
                }
            )
        clip_id = case.candidate_to_clip.get(candidate_id)
        clip = case.clips.get(clip_id or "")
        candidate["clip_id"] = clip_id
        candidate["review_state"] = (
            clip.state.value
            if clip is not None and clip.state is not None
            else "UNREVIEWED"
        )
        values.append(candidate)
    return sorted(
        values,
        key=lambda item: (
            -float(item.get("appearance_score", 0.0)),
            str(item.get("candidate_id", "")),
        ),
    )


def build_case_view(
    case: CaseRecord,
    *,
    jobs: list[JobRecord] | None = None,
    outbox: list[OutboxRecord] | None = None,
    routing_triggers: list[RoutingTriggerRecord] | None = None,
    probe_results: list[ProbeResultRecord] | None = None,
    now: datetime | None = None,
) -> CaseView:
    jobs = jobs or []
    outbox = outbox or []
    routing_triggers = routing_triggers or []
    probe_results = probe_results or []
    if case.lifecycle.value in {"DELETE_REQUESTED", "DELETED"}:
        return CaseView(
            case_id=case.request.case_id,
            revision=case.revision,
            route_revision=(case.latest_route.revision if case.latest_route else 0),
            lifecycle=case.lifecycle,
        )
    retained_view = case.lifecycle.value in {"CLOSED", "EXPORTING", "SAVED"}

    def visible_clips() -> list[dict]:
        return [
            item.model_dump(mode="json")
            for item in sorted(case.clips.values(), key=lambda value: value.clip_id)
            if not retained_view or item.state == ClipLifecycleState.CONFIRMED
        ]
    processed_by_probe: dict[str, float] = {}
    for record in probe_results:
        duration = float(record.result.processed_duration_s or 0.0)
        processed_by_probe[record.result.probe_id] = max(
            processed_by_probe.get(record.result.probe_id, 0.0), duration
        )
    progress = {
        "jobs_total": len(jobs),
        "jobs_by_state": {
            state: sum(item.state.value == state for item in jobs)
            for state in sorted({item.state.value for item in jobs})
        },
        "processed_camera_seconds": sum(processed_by_probe.values()),
        "routing_triggers_by_state": {
            state: sum(item.state.value == state for item in routing_triggers)
            for state in sorted({item.state.value for item in routing_triggers})
        },
    }
    errors = [
        {
            "source": "job",
            "id": item.job_id,
            "state": item.state.value,
            "error": item.last_error,
        }
        for item in jobs
        if item.state.value in {"FAILED", "TIMEOUT", "UNAVAILABLE"}
    ] + [
        {
            "source": "outbox",
            "id": item.message_id,
            "state": item.state.value,
            "error": item.last_error,
        }
        for item in outbox
        if item.state.value == "FAILED"
    ] + [
        {
            "source": "routing",
            "id": item.trigger_id,
            "state": item.state.value,
            "error": item.last_error,
        }
        for item in routing_triggers
        if item.state.value == "FAILED"
    ]
    errors.extend(
        {
            "source": "clip",
            "id": item.clip_id,
            "state": item.status.value,
            "error": item.error_code,
        }
        for item in sorted(case.clips.values(), key=lambda value: value.clip_id)
        if item.status.value in {"FAILED", "UNAVAILABLE"}
    )
    if case.export_ref is not None and case.export_ref.get("status") in {
        "FAILED",
        "UNAVAILABLE",
    }:
        errors.append(
            {
                "source": "export",
                "id": case.export_ref.get("export_request_id"),
                "state": case.export_ref.get("status"),
                "error": case.export_ref.get("error_code"),
            }
        )
    route = case.latest_route
    if route is None:
        return CaseView(
            case_id=case.request.case_id,
            revision=case.revision,
            route_revision=0,
            lifecycle=case.lifecycle,
            candidates=[] if retained_view else _candidate_views(case),
            clips=visible_clips(),
            export=(dict(case.export_ref) if case.export_ref is not None else None),
            search_progress=progress,
            errors=errors,
            live=None if retained_view else _published_live(case, now),
        )

    step = route.step_result
    best_id = step.best.id if isinstance(step.best, Hypothesis) else step.best
    best = next((item for item in step.hypotheses if item.id == best_id), None)
    alternatives = [
        item.model_dump(mode="json")
        for item in sorted(step.hypotheses, key=lambda value: (-value.score, value.id))
        if item.id != best_id
    ]
    maps = [
        RouteMapView(
            floor_id=item.floor_id,
            mime_type=item.mime_type,
            url=(
                f"/v1/cases/{case.request.case_id}/routes/"
                f"{route.revision}/{item.floor_id}.svg"
            ),
            alt_text=item.alt_text,
            width=item.width,
            height=item.height,
        )
        for item in route.rendered.maps
    ]
    candidates = [] if retained_view else _candidate_views(case)
    live = None if retained_view else _published_live(case, now)
    if live is None and step.mode.value == "LIVE_TRACKING" and best is not None and best.visits:
        visit = best.visits[-1]
        live = {
            "camera_id": visit.camera,
            "tracklet_ids": list(visit.tracklet_ids),
            "t_start": visit.t_in.isoformat(),
            "t_end": "OPEN" if visit.open else visit.t_out.isoformat(),
            "stream_url": None,
        }
    watch = [
        {
            "probe_id": probe.probe_id,
            "camera_id": probe.camera,
            "t_from": probe.t_from.isoformat(),
            "t_to": "OPEN",
            "reason": probe.reason,
        }
        for probe in step.probes
        if probe.t_to is None
    ]
    return CaseView(
        case_id=case.request.case_id,
        revision=case.revision,
        route_revision=route.revision,
        lifecycle=case.lifecycle,
        mode=step.mode.value,
        done=step.done,
        stop_reason=step.stop_reason.value if step.stop_reason is not None else None,
        best_hypothesis=(best.model_dump(mode="json") if best is not None else None),
        alternatives=alternatives,
        candidates=[dict(item) for item in candidates],
        estimate=step.estimate.model_dump(mode="json") if step.estimate is not None else None,
        rendered_route_maps=maps,
        clips=visible_clips(),
        export=(dict(case.export_ref) if case.export_ref is not None else None),
        search_progress=progress,
        live=live,
        watch=[] if retained_view else watch,
        unobserved_windows=list(step.state.unobserved_windows),
        errors=errors,
    )
