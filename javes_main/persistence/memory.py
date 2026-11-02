from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime
from threading import RLock

from javes_main.domain.models import (
    AuditEvent,
    AuthSession,
    CaseDataRecord,
    CaseRecord,
    JobRecord,
    JobState,
    OutboxRecord,
    OutboxState,
    ProbeResultRecord,
    RoutingTriggerRecord,
    RoutingTriggerState,
    UserAccount,
)


class RevisionConflict(RuntimeError):
    pass


class InMemoryRepository:
    def __init__(self) -> None:
        self._cases: dict[str, CaseRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._job_dedupe: dict[str, str] = {}
        self._events: dict[str, list[AuditEvent]] = {}
        self._idempotency: dict[str, tuple[str, dict]] = {}
        self._users: dict[str, UserAccount] = {}
        self._usernames: dict[str, str] = {}
        self._sessions: dict[str, AuthSession] = {}
        self._outbox: dict[str, OutboxRecord] = {}
        self._outbox_dedupe: dict[str, str] = {}
        self._probe_results: dict[str, ProbeResultRecord] = {}
        self._case_data: dict[tuple[str, str, str], CaseDataRecord] = {}
        self._routing_triggers: dict[str, RoutingTriggerRecord] = {}
        self._routing_trigger_dedupe: dict[str, str] = {}
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            snapshots = {
                "cases": copy.deepcopy(self._cases),
                "jobs": copy.deepcopy(self._jobs),
                "job_dedupe": copy.deepcopy(self._job_dedupe),
                "events": copy.deepcopy(self._events),
                "idempotency": copy.deepcopy(self._idempotency),
                "users": copy.deepcopy(self._users),
                "usernames": copy.deepcopy(self._usernames),
                "sessions": copy.deepcopy(self._sessions),
                "outbox": copy.deepcopy(self._outbox),
                "outbox_dedupe": copy.deepcopy(self._outbox_dedupe),
                "probe_results": copy.deepcopy(self._probe_results),
                "case_data": copy.deepcopy(self._case_data),
                "routing_triggers": copy.deepcopy(self._routing_triggers),
                "routing_trigger_dedupe": copy.deepcopy(self._routing_trigger_dedupe),
            }
            try:
                yield self
            except Exception:
                self._cases = snapshots["cases"]
                self._jobs = snapshots["jobs"]
                self._job_dedupe = snapshots["job_dedupe"]
                self._events = snapshots["events"]
                self._idempotency = snapshots["idempotency"]
                self._users = snapshots["users"]
                self._usernames = snapshots["usernames"]
                self._sessions = snapshots["sessions"]
                self._outbox = snapshots["outbox"]
                self._outbox_dedupe = snapshots["outbox_dedupe"]
                self._probe_results = snapshots["probe_results"]
                self._case_data = snapshots["case_data"]
                self._routing_triggers = snapshots["routing_triggers"]
                self._routing_trigger_dedupe = snapshots["routing_trigger_dedupe"]
                raise

    def add(self, case: CaseRecord) -> None:
        with self._lock:
            case_id = case.request.case_id
            if case_id in self._cases:
                raise ValueError(f"case {case_id!r} already exists")
            self._cases[case_id] = case.model_copy(deep=True)

    def get(self, case_id: str) -> CaseRecord:
        with self._lock:
            try:
                return self._cases[case_id].model_copy(deep=True)
            except KeyError as exc:
                raise KeyError(f"unknown case {case_id!r}") from exc

    def list_cases(self, *, lifecycles: set[str] | None = None) -> list[CaseRecord]:
        with self._lock:
            values = [
                item.model_copy(deep=True)
                for item in self._cases.values()
                if lifecycles is None or item.lifecycle.value in lifecycles
            ]
        return sorted(values, key=lambda item: (item.created_at, item.request.case_id))

    def save(self, case: CaseRecord, expected_revision: int) -> CaseRecord:
        with self._lock:
            case_id = case.request.case_id
            current = self._cases.get(case_id)
            if current is None:
                raise KeyError(f"unknown case {case_id!r}")
            if current.revision != expected_revision:
                raise RevisionConflict(
                    f"case {case_id!r} revision is {current.revision}, expected {expected_revision}"
                )
            saved = case.model_copy(update={"revision": expected_revision + 1}, deep=True)
            self._cases[case_id] = saved
            return saved.model_copy(deep=True)

    def add_job(self, job: JobRecord) -> None:
        with self._lock:
            previous_id = self._job_dedupe.get(job.dedupe_key)
            if previous_id is not None:
                previous = self._jobs[previous_id]
                if previous.payload != job.payload:
                    raise ValueError("job dedupe key reused with different payload")
                return
            previous = self._jobs.get(job.job_id)
            if previous is not None and previous != job:
                raise ValueError("job id reused with different payload")
            self._jobs[job.job_id] = job.model_copy(deep=True)
            self._job_dedupe[job.dedupe_key] = job.job_id

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._jobs[job_id].model_copy(deep=True)
            except KeyError as exc:
                raise KeyError(f"unknown job {job_id!r}") from exc

    def save_job(self, job: JobRecord) -> None:
        with self._lock:
            if job.job_id not in self._jobs:
                raise KeyError(f"unknown job {job.job_id!r}")
            self._jobs[job.job_id] = job.model_copy(deep=True)

    def list_jobs(
        self, case_id: str, *, states: set[str] | None = None
    ) -> list[JobRecord]:
        with self._lock:
            values = [
                job.model_copy(deep=True)
                for job in self._jobs.values()
                if job.case_id == case_id
                and (states is None or job.state.value in states)
            ]
        return sorted(values, key=lambda item: (item.created_at, item.job_id))

    def claim_jobs(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
        case_id: str | None = None,
    ) -> list[JobRecord]:
        if limit < 1:
            return []
        with self._lock:
            pending = sorted(
                (
                    job
                    for job in self._jobs.values()
                    if job.state == JobState.PENDING
                    and (case_id is None or job.case_id == case_id)
                ),
                key=lambda item: (item.created_at, item.job_id),
            )[:limit]
            claimed: list[JobRecord] = []
            for job in pending:
                job.state = JobState.LEASED
                job.attempt += 1
                job.lease_owner = worker_id
                job.lease_expires_at = lease_expires_at
                job.updated_at = now
                claimed.append(job.model_copy(deep=True))
            return claimed

    def reclaim_expired_jobs(self, *, now: datetime) -> list[JobRecord]:
        with self._lock:
            reclaimed: list[JobRecord] = []
            for job in sorted(
                self._jobs.values(), key=lambda item: (item.created_at, item.job_id)
            ):
                if (
                    job.state != JobState.LEASED
                    or job.lease_expires_at is None
                    or job.lease_expires_at > now
                ):
                    continue
                job.state = (
                    JobState.UNAVAILABLE
                    if job.attempt >= job.max_attempts
                    else JobState.PENDING
                )
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
                job.last_error = "lease expired without a result"
                reclaimed.append(job.model_copy(deep=True))
            return reclaimed

    def append_event(self, event: AuditEvent) -> None:
        with self._lock:
            events = self._events.setdefault(event.case_id, [])
            if any(item.event_id == event.event_id for item in events):
                return
            events.append(event.model_copy(deep=True))
            events.sort(key=lambda item: (item.occurred_at, item.event_id))

    def list_events(self, case_id: str) -> list[AuditEvent]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._events.get(case_id, [])]

    def remember_idempotency(
        self, key: str, fingerprint: str, response: dict
    ) -> None:
        with self._lock:
            previous = self._idempotency.get(key)
            if previous is not None and previous[0] != fingerprint:
                raise ValueError("idempotency key reused with different payload")
            if previous is not None:
                return
            self._idempotency[key] = (fingerprint, dict(response))

    def get_idempotency(self, key: str) -> tuple[str, dict] | None:
        with self._lock:
            previous = self._idempotency.get(key)
            return None if previous is None else (previous[0], dict(previous[1]))

    def lock_idempotency(self, key: str) -> None:
        # transaction() already owns the repository-wide re-entrant lock.
        # Keeping this explicit mirrors the PostgreSQL repository contract.
        return None

    def add_user(self, user: UserAccount) -> None:
        username = user.username.casefold()
        with self._lock:
            if user.user_id in self._users or username in self._usernames:
                raise ValueError("user id or username already exists")
            self._users[user.user_id] = user.model_copy(deep=True)
            self._usernames[username] = user.user_id

    def get_user(self, user_id: str) -> UserAccount:
        with self._lock:
            try:
                return self._users[user_id].model_copy(deep=True)
            except KeyError as exc:
                raise KeyError(f"unknown user {user_id!r}") from exc

    def get_user_by_username(self, username: str) -> UserAccount:
        with self._lock:
            user_id = self._usernames.get(username.casefold())
            if user_id is None:
                raise KeyError("unknown username")
            return self._users[user_id].model_copy(deep=True)

    def save_user(self, user: UserAccount) -> None:
        with self._lock:
            previous = self._users.get(user.user_id)
            if previous is None:
                raise KeyError(f"unknown user {user.user_id!r}")
            if previous.username.casefold() != user.username.casefold():
                raise ValueError("username cannot be changed")
            self._users[user.user_id] = user.model_copy(deep=True)

    def list_users(self) -> list[UserAccount]:
        with self._lock:
            return [
                user.model_copy(deep=True)
                for user in sorted(self._users.values(), key=lambda item: item.username)
            ]

    def add_session(self, session: AuthSession) -> None:
        with self._lock:
            if session.session_id in self._sessions:
                raise ValueError("session already exists")
            if any(item.token_hash == session.token_hash for item in self._sessions.values()):
                raise ValueError("session token already exists")
            self._sessions[session.session_id] = session.model_copy(deep=True)

    def get_session_by_token_hash(self, token_hash: str) -> AuthSession | None:
        with self._lock:
            for session in self._sessions.values():
                if session.token_hash == token_hash:
                    return session.model_copy(deep=True)
            return None

    def save_session(self, session: AuthSession) -> None:
        with self._lock:
            if session.session_id not in self._sessions:
                raise KeyError(f"unknown session {session.session_id!r}")
            self._sessions[session.session_id] = session.model_copy(deep=True)

    def revoke_user_sessions(self, user_id: str, *, now: datetime) -> int:
        changed = 0
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if session.user_id == user_id and session.revoked_at is None:
                    self._sessions[session_id] = session.model_copy(
                        update={"revoked_at": now}
                    )
                    changed += 1
        return changed

    def add_outbox(self, message: OutboxRecord) -> None:
        with self._lock:
            previous_id = self._outbox_dedupe.get(message.dedupe_key)
            if previous_id is not None:
                previous = self._outbox[previous_id]
                if previous.topic != message.topic or previous.payload != message.payload:
                    raise ValueError("outbox dedupe key reused with different payload")
                return
            if message.message_id in self._outbox:
                raise ValueError("outbox message id already exists")
            self._outbox[message.message_id] = message.model_copy(deep=True)
            self._outbox_dedupe[message.dedupe_key] = message.message_id

    def get_outbox(self, message_id: str) -> OutboxRecord:
        with self._lock:
            try:
                return self._outbox[message_id].model_copy(deep=True)
            except KeyError as exc:
                raise KeyError(f"unknown outbox message {message_id!r}") from exc

    def list_outbox(
        self, *, case_id: str | None = None, states: set[str] | None = None
    ) -> list[OutboxRecord]:
        with self._lock:
            values = [
                item.model_copy(deep=True)
                for item in self._outbox.values()
                if (case_id is None or item.case_id == case_id)
                and (states is None or item.state.value in states)
            ]
        return sorted(values, key=lambda item: (item.created_at, item.message_id))

    def save_outbox(self, message: OutboxRecord) -> None:
        with self._lock:
            if message.message_id not in self._outbox:
                raise KeyError(f"unknown outbox message {message.message_id!r}")
            self._outbox[message.message_id] = message.model_copy(deep=True)

    def claim_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> list[OutboxRecord]:
        if limit < 1:
            return []
        with self._lock:
            pending = sorted(
                (
                    item
                    for item in self._outbox.values()
                    if item.state == OutboxState.PENDING and item.available_at <= now
                ),
                key=lambda item: (item.available_at, item.created_at, item.message_id),
            )[:limit]
            claimed: list[OutboxRecord] = []
            for item in pending:
                item.state = OutboxState.LEASED
                item.attempt += 1
                item.lease_owner = worker_id
                item.lease_expires_at = lease_expires_at
                item.updated_at = now
                claimed.append(item.model_copy(deep=True))
            return claimed

    def reclaim_expired_outbox(self, *, now: datetime) -> list[OutboxRecord]:
        with self._lock:
            reclaimed: list[OutboxRecord] = []
            for item in sorted(
                self._outbox.values(), key=lambda value: (value.created_at, value.message_id)
            ):
                if (
                    item.state != OutboxState.LEASED
                    or item.lease_expires_at is None
                    or item.lease_expires_at > now
                ):
                    continue
                item.state = (
                    OutboxState.FAILED
                    if item.attempt >= item.max_attempts
                    else OutboxState.PENDING
                )
                item.lease_owner = None
                item.lease_expires_at = None
                item.updated_at = now
                item.last_error = "outbox lease expired before acknowledgement"
                reclaimed.append(item.model_copy(deep=True))
            return reclaimed

    def add_probe_result(self, record: ProbeResultRecord) -> bool:
        with self._lock:
            if record.fingerprint in self._probe_results:
                return False
            self._probe_results[record.fingerprint] = record.model_copy(deep=True)
            return True

    def list_probe_results(self, case_id: str) -> list[ProbeResultRecord]:
        with self._lock:
            values = [
                item.model_copy(deep=True)
                for item in self._probe_results.values()
                if item.case_id == case_id
            ]
        return sorted(values, key=lambda item: (item.accepted_at, item.fingerprint))

    def add_case_data(self, record: CaseDataRecord) -> None:
        key = (record.case_id, record.kind.value, record.record_id)
        with self._lock:
            previous = self._case_data.get(key)
            if previous is not None:
                if previous != record:
                    raise ValueError("case data record id reused with different payload")
                return
            self._case_data[key] = record.model_copy(deep=True)

    def list_case_data(
        self, case_id: str, *, kinds: set[str] | None = None
    ) -> list[CaseDataRecord]:
        with self._lock:
            values = [
                item.model_copy(deep=True)
                for item in self._case_data.values()
                if item.case_id == case_id
                and (kinds is None or item.kind.value in kinds)
            ]
        return sorted(
            values, key=lambda item: (item.occurred_at, item.kind.value, item.record_id)
        )

    def add_routing_trigger(self, trigger: RoutingTriggerRecord) -> None:
        with self._lock:
            previous_id = self._routing_trigger_dedupe.get(trigger.dedupe_key)
            if previous_id is not None:
                previous = self._routing_triggers[previous_id]
                if (
                    previous.probe_results != trigger.probe_results
                    or previous.anchors != trigger.anchors
                    or previous.trigger_type != trigger.trigger_type
                ):
                    raise ValueError("routing trigger dedupe key reused")
                return
            if trigger.trigger_id in self._routing_triggers:
                raise ValueError("routing trigger id already exists")
            self._routing_triggers[trigger.trigger_id] = trigger.model_copy(deep=True)
            self._routing_trigger_dedupe[trigger.dedupe_key] = trigger.trigger_id

    def get_routing_trigger(self, trigger_id: str) -> RoutingTriggerRecord:
        with self._lock:
            try:
                return self._routing_triggers[trigger_id].model_copy(deep=True)
            except KeyError as exc:
                raise KeyError(f"unknown routing trigger {trigger_id!r}") from exc

    def save_routing_trigger(self, trigger: RoutingTriggerRecord) -> None:
        with self._lock:
            if trigger.trigger_id not in self._routing_triggers:
                raise KeyError(f"unknown routing trigger {trigger.trigger_id!r}")
            self._routing_triggers[trigger.trigger_id] = trigger.model_copy(deep=True)

    def list_routing_triggers(
        self, *, case_id: str | None = None, states: set[str] | None = None
    ) -> list[RoutingTriggerRecord]:
        with self._lock:
            values = [
                item.model_copy(deep=True)
                for item in self._routing_triggers.values()
                if (case_id is None or item.case_id == case_id)
                and (states is None or item.state.value in states)
            ]
        return sorted(values, key=lambda item: (item.created_at, item.trigger_id))

    def claim_routing_triggers(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> list[RoutingTriggerRecord]:
        if limit < 1:
            return []
        with self._lock:
            pending = sorted(
                (
                    item
                    for item in self._routing_triggers.values()
                    if item.state == RoutingTriggerState.PENDING
                ),
                key=lambda item: (item.created_at, item.trigger_id),
            )[:limit]
            claimed: list[RoutingTriggerRecord] = []
            for item in pending:
                item.state = RoutingTriggerState.LEASED
                item.attempt += 1
                item.lease_owner = worker_id
                item.lease_expires_at = lease_expires_at
                item.updated_at = now
                claimed.append(item.model_copy(deep=True))
            return claimed

    def reclaim_expired_routing_triggers(
        self, *, now: datetime
    ) -> list[RoutingTriggerRecord]:
        with self._lock:
            reclaimed: list[RoutingTriggerRecord] = []
            for item in sorted(
                self._routing_triggers.values(),
                key=lambda value: (value.created_at, value.trigger_id),
            ):
                if (
                    item.state != RoutingTriggerState.LEASED
                    or item.lease_expires_at is None
                    or item.lease_expires_at > now
                ):
                    continue
                item.state = (
                    RoutingTriggerState.FAILED
                    if item.attempt >= item.max_attempts
                    else RoutingTriggerState.PENDING
                )
                item.lease_owner = None
                item.lease_expires_at = None
                item.updated_at = now
                item.last_error = "routing trigger lease expired"
                reclaimed.append(item.model_copy(deep=True))
            return reclaimed

    def purge_case_content(
        self, case_id: str, *, preserve_outbox_message_id: str | None = None
    ) -> None:
        with self._lock:
            # Audit events are append-only security records. Case retention
            # removes operational and media content, never the event identity,
            # actor, timestamp, action, or details.
            job_ids = [key for key, value in self._jobs.items() if value.case_id == case_id]
            for job_id in job_ids:
                job = self._jobs.pop(job_id)
                self._job_dedupe.pop(job.dedupe_key, None)
            self._probe_results = {
                key: value
                for key, value in self._probe_results.items()
                if value.case_id != case_id
            }
            self._case_data = {
                key: value
                for key, value in self._case_data.items()
                if value.case_id != case_id
            }
            trigger_ids = [
                key
                for key, value in self._routing_triggers.items()
                if value.case_id == case_id
            ]
            for trigger_id in trigger_ids:
                trigger = self._routing_triggers.pop(trigger_id)
                self._routing_trigger_dedupe.pop(trigger.dedupe_key, None)
            for message_id, message in list(self._outbox.items()):
                if message.case_id == case_id and message_id != preserve_outbox_message_id:
                    next_state = (
                        OutboxState.CANCELLED
                        if message.state in {OutboxState.PENDING, OutboxState.LEASED}
                        else message.state
                    )
                    self._outbox[message_id] = message.model_copy(
                        update={
                            "payload": {"redacted": True},
                            "state": next_state,
                            "lease_owner": None,
                            "lease_expires_at": None,
                            "last_error": None,
                        }
                    )

    def redact_case_data(self, case_id: str, *, kinds: set[str]) -> None:
        """Keep immutable record identity/timing while removing temp payloads."""

        with self._lock:
            for key, record in list(self._case_data.items()):
                if record.case_id == case_id and record.kind.value in kinds:
                    self._case_data[key] = record.model_copy(
                        update={"payload": {"redacted": True}}, deep=True
                    )

    def purge_search_content(
        self, case_id: str, *, preserve_outbox_message_id: str | None = None
    ) -> None:
        """Remove search-only payloads after a successful CLOSE_CASE."""

        temporary_kinds = {
            "ROUTING_STATE",
            "ROUTING_RUN",
            "PROBE_REQUEST",
            "TRACKLET",
            "CANDIDATE",
        }
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.case_id != case_id:
                    continue
                redacted_probe = job.payload.probe.model_copy(
                    update={
                        "camera": "__deleted__",
                        "t_from": job.created_at,
                        "t_to": job.created_at,
                        "reason": "REDACTED",
                        "hypothesis_ids": [],
                    },
                    deep=True,
                )
                self._jobs[job_id] = job.model_copy(
                    update={
                        "payload": job.payload.model_copy(
                            update={
                                "target_gallery_ref": "__deleted__",
                                "probe": redacted_probe,
                            },
                            deep=True,
                        )
                    },
                    deep=True,
                )
            for key, value in list(self._probe_results.items()):
                if value.case_id == case_id:
                    self._probe_results[key] = value.model_copy(
                        update={
                            "result": value.result.model_copy(
                                update={"tracklets": [], "candidates": []}, deep=True
                            )
                        },
                        deep=True,
                    )
            for trigger_id, trigger in list(self._routing_triggers.items()):
                if trigger.case_id == case_id:
                    self._routing_triggers[trigger_id] = trigger.model_copy(
                        update={
                            "probe_results": [],
                            "anchors": trigger.anchors.__class__(),
                        },
                        deep=True,
                    )
            for key, record in list(self._case_data.items()):
                temporary_artifact = (
                    record.kind.value == "ARTIFACT"
                    and (
                        record.payload.get("kind") == "HOLD_CLIP"
                        or record.payload.get("state") == "RELEASED"
                    )
                )
                if record.case_id == case_id and (
                    record.kind.value in temporary_kinds or temporary_artifact
                ):
                    self._case_data[key] = record.model_copy(
                        update={"payload": {"redacted": True}}, deep=True
                    )
            for message_id, message in list(self._outbox.items()):
                if message.case_id == case_id and message_id != preserve_outbox_message_id:
                    self._outbox[message_id] = message.model_copy(
                        update={"payload": {"redacted": True}}, deep=True
                    )
            for key, (fingerprint, response) in list(self._idempotency.items()):
                if response.get("case_id") == case_id:
                    self._idempotency[key] = (
                        fingerprint,
                        {"case_id": case_id, "redacted": True},
                    )
