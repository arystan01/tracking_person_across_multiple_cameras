"""Canonical HTTP-shaped requests emitted by a fake Dashboard."""

from __future__ import annotations

from datetime import datetime

from .contracts import (
    CaseCommandBody,
    CreateCaseBody,
    FakeHttpRequest,
    ReviewDecisionBody,
)


def _headers(
    request_id: str,
    access_token: str = "fake-dashboard-session",
) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "idempotency-key": request_id,
        "authorization": f"Bearer {access_token}",
    }


class FakeDashboardRequests:
    @staticmethod
    def login(
        request_id: str,
        username: str = "operator-demo",
        password: str = "fake-dashboard-password",
    ) -> FakeHttpRequest:
        return FakeHttpRequest(
            request_id=request_id,
            method="POST",
            path="/v1/auth/login",
            headers={"content-type": "application/json"},
            body={"username": username, "password": password},
        )

    @staticmethod
    def create_case(request_id: str, body: CreateCaseBody) -> FakeHttpRequest:
        return FakeHttpRequest(
            request_id=request_id,
            method="POST",
            path="/v1/cases",
            headers=_headers(request_id),
            body=body.model_dump(mode="json"),
        )

    @staticmethod
    def get_case(
        request_id: str,
        case_id: str,
        operator_id: str = "operator-demo",
    ) -> FakeHttpRequest:
        return FakeHttpRequest(
            request_id=request_id,
            method="GET",
            path=f"/v1/cases/{case_id}/view",
            headers=_headers(request_id),
        )

    @staticmethod
    def decision(
        request_id: str, case_id: str, body: ReviewDecisionBody
    ) -> FakeHttpRequest:
        return FakeHttpRequest(
            request_id=request_id,
            method="POST",
            path=f"/v1/cases/{case_id}/decisions",
            headers=_headers(request_id),
            body=body.model_dump(mode="json"),
        )

    @staticmethod
    def command(
        request_id: str, case_id: str, body: CaseCommandBody
    ) -> FakeHttpRequest:
        action = body.action.lower()
        return FakeHttpRequest(
            request_id=request_id,
            method="POST",
            path=f"/v1/cases/{case_id}/commands/{action}",
            headers=_headers(request_id),
            body=body.model_dump(mode="json"),
        )

    @classmethod
    def standard_scenario(
        cls,
        *,
        case: CreateCaseBody,
        candidate_id: str,
        tracklet_id: str,
        operator_id: str,
        now: datetime,
    ) -> list[FakeHttpRequest]:
        rejected = ReviewDecisionBody(
            decision_id="decision-000",
            candidate_id=f"{candidate_id}-rejected",
            tracklet_id=f"{tracklet_id}-rejected",
            decision="REJECT",
            operator_id=operator_id,
            occurred_at=now,
        )
        confirmed = ReviewDecisionBody(
            decision_id="decision-001",
            candidate_id=candidate_id,
            tracklet_id=tracklet_id,
            decision="CONFIRM",
            operator_id=operator_id,
            occurred_at=now,
        )
        commands = [
            CaseCommandBody(
                command_id=f"command-{action.lower()}",
                action=action,
                operator_id=operator_id,
                occurred_at=now,
            )
            for action in ("CLOSE", "EXPORT", "DELETE")
        ]
        return [
            cls.login("request-login"),
            cls.create_case("request-create", case),
            cls.get_case("request-view", case.case_id, operator_id),
            cls.decision("request-reject", case.case_id, rejected),
            cls.decision("request-confirm", case.case_id, confirmed),
            *(cls.command(f"request-{item.action.lower()}", case.case_id, item) for item in commands),
        ]
