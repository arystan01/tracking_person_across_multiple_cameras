"""SQLAlchemy repository implementing the current Case Manager port.

The repository stores versioned aggregate JSON and indexed lifecycle columns.
More read models may be normalized later without exposing ORM models to the
application layer.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Iterator

from sqlalchemy import JSON, DateTime, Index, Integer, String, create_engine, delete, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

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

from .memory import RevisionConflict


class Base(DeclarativeBase):
    pass


class CaseRow(Base):
    __tablename__ = "cases"

    case_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    lifecycle: Mapped[str] = mapped_column(String(40), index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobRow(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index("ix_jobs_case_state", JobRow.case_id, JobRow.state)


class EventRow(Base):
    __tablename__ = "case_events"

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class IdempotencyRow(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)


class UserRow(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"

    session_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class OutboxRow(Base):
    __tablename__ = "outbox"

    message_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    dedupe_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index("ix_outbox_state_available", OutboxRow.state, OutboxRow.available_at)


class ProbeResultRow(Base):
    __tablename__ = "probe_results"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class CaseDataRow(Base):
    __tablename__ = "case_data"

    case_id: Mapped[str] = mapped_column(String(160), primary_key=True, index=True)
    kind: Mapped[str] = mapped_column(String(60), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


Index("ix_case_data_case_kind", CaseDataRow.case_id, CaseDataRow.kind)


class RoutingTriggerRow(Base):
    __tablename__ = "routing_triggers"

    trigger_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    trigger_type: Mapped[str] = mapped_column(String(60), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    dedupe_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._active_session: ContextVar[Session | None] = ContextVar(
            f"javes_sql_session_{id(self)}", default=None
        )

    @contextmanager
    def transaction(self) -> Iterator[SqlAlchemyRepository]:
        active = self._active_session.get()
        if active is not None:
            yield self
            return
        with self.sessions.begin() as session:
            token = self._active_session.set(session)
            try:
                yield self
            finally:
                self._active_session.reset(token)

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        active = self._active_session.get()
        if active is not None:
            yield active
            return
        with self.sessions() as session:
            yield session

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        active = self._active_session.get()
        if active is not None:
            yield active
            return
        with self.sessions.begin() as session:
            yield session

    @classmethod
    def from_url(cls, url: str, **engine_kwargs) -> SqlAlchemyRepository:
        return cls(create_engine(url, **engine_kwargs))

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def add(self, case: CaseRecord) -> None:
        row = CaseRow(
            case_id=case.request.case_id,
            lifecycle=case.lifecycle.value,
            revision=case.revision,
            payload=case.model_dump(mode="json"),
            created_at=case.created_at,
            updated_at=case.updated_at,
        )
        with self._write_session() as session:
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError(f"case {case.request.case_id!r} already exists") from exc

    def get(self, case_id: str) -> CaseRecord:
        with self._read_session() as session:
            row = session.get(CaseRow, case_id)
            if row is None:
                raise KeyError(f"unknown case {case_id!r}")
            return CaseRecord.model_validate(row.payload)

    def list_cases(self, *, lifecycles: set[str] | None = None) -> list[CaseRecord]:
        statement = select(CaseRow)
        if lifecycles is not None:
            statement = statement.where(CaseRow.lifecycle.in_(sorted(lifecycles)))
        statement = statement.order_by(CaseRow.created_at, CaseRow.case_id)
        with self._read_session() as session:
            return [
                CaseRecord.model_validate(row.payload)
                for row in session.scalars(statement).all()
            ]

    def save(self, case: CaseRecord, expected_revision: int) -> CaseRecord:
        saved = case.model_copy(update={"revision": expected_revision + 1}, deep=True)
        statement = (
            update(CaseRow)
            .where(
                CaseRow.case_id == case.request.case_id,
                CaseRow.revision == expected_revision,
            )
            .values(
                lifecycle=saved.lifecycle.value,
                revision=saved.revision,
                payload=saved.model_dump(mode="json"),
                updated_at=saved.updated_at,
            )
        )
        with self._write_session() as session:
            result = session.execute(statement)
            if result.rowcount != 1:
                exists = session.get(CaseRow, case.request.case_id)
                if exists is None:
                    raise KeyError(f"unknown case {case.request.case_id!r}")
                raise RevisionConflict(
                    f"case {case.request.case_id!r} revision is {exists.revision}, "
                    f"expected {expected_revision}"
                )
        return saved

    def add_job(self, job: JobRecord) -> None:
        with self._write_session() as session:
            previous = session.scalar(
                select(JobRow).where(JobRow.dedupe_key == job.dedupe_key)
            )
            if previous is not None:
                if JobRecord.model_validate(previous.payload).payload != job.payload:
                    raise ValueError("job dedupe key reused with different payload")
                return
            session.add(
                JobRow(
                    job_id=job.job_id,
                    case_id=job.case_id,
                    kind=job.kind,
                    state=job.state.value,
                    attempt=job.attempt,
                    max_attempts=job.max_attempts,
                    lease_owner=job.lease_owner,
                    lease_expires_at=job.lease_expires_at,
                    dedupe_key=job.dedupe_key,
                    payload=job.model_dump(mode="json"),
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError("job id or dedupe key already exists") from exc

    def get_job(self, job_id: str) -> JobRecord:
        with self._read_session() as session:
            row = session.get(JobRow, job_id)
            if row is None:
                raise KeyError(f"unknown job {job_id!r}")
            return JobRecord.model_validate(row.payload)

    def save_job(self, job: JobRecord) -> None:
        with self._write_session() as session:
            row = session.get(JobRow, job.job_id)
            if row is None:
                raise KeyError(f"unknown job {job.job_id!r}")
            row.state = job.state.value
            row.attempt = job.attempt
            row.max_attempts = job.max_attempts
            row.lease_owner = job.lease_owner
            row.lease_expires_at = job.lease_expires_at
            row.payload = job.model_dump(mode="json")
            row.updated_at = job.updated_at

    def list_jobs(
        self, case_id: str, *, states: set[str] | None = None
    ) -> list[JobRecord]:
        statement = select(JobRow).where(JobRow.case_id == case_id)
        if states is not None:
            statement = statement.where(JobRow.state.in_(sorted(states)))
        statement = statement.order_by(JobRow.created_at, JobRow.job_id)
        with self._read_session() as session:
            return [
                JobRecord.model_validate(row.payload)
                for row in session.scalars(statement).all()
            ]

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
        candidates = select(JobRow.job_id).where(JobRow.state == JobState.PENDING.value)
        if case_id is not None:
            candidates = candidates.where(JobRow.case_id == case_id)
        candidates = candidates.order_by(JobRow.created_at, JobRow.job_id).limit(limit)
        if self.engine.dialect.name == "postgresql":
            candidates = candidates.with_for_update(skip_locked=True)

        claimed: list[JobRecord] = []
        with self._write_session() as session:
            candidate_ids = list(session.scalars(candidates).all())
            for job_id in candidate_ids:
                statement = (
                    update(JobRow)
                    .where(
                        JobRow.job_id == job_id,
                        JobRow.state == JobState.PENDING.value,
                    )
                    .values(
                        state=JobState.LEASED.value,
                        attempt=JobRow.attempt + 1,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                )
                result = session.execute(statement)
                if result.rowcount != 1:
                    continue
                row = session.get(JobRow, job_id)
                assert row is not None
                record = JobRecord.model_validate(row.payload).model_copy(
                    update={
                        "state": JobState.LEASED,
                        "attempt": row.attempt,
                        "lease_owner": worker_id,
                        "lease_expires_at": lease_expires_at,
                        "updated_at": now,
                    }
                )
                row.payload = record.model_dump(mode="json")
                claimed.append(record)
        return claimed

    def reclaim_expired_jobs(self, *, now: datetime) -> list[JobRecord]:
        statement = (
            select(JobRow)
            .where(
                JobRow.state == JobState.LEASED.value,
                JobRow.lease_expires_at.is_not(None),
                JobRow.lease_expires_at <= now,
            )
            .order_by(JobRow.created_at, JobRow.job_id)
        )
        reclaimed: list[JobRecord] = []
        with self._write_session() as session:
            for row in session.scalars(statement).all():
                state = (
                    JobState.UNAVAILABLE
                    if row.attempt >= row.max_attempts
                    else JobState.PENDING
                )
                record = JobRecord.model_validate(row.payload).model_copy(
                    update={
                        "state": state,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "updated_at": now,
                        "last_error": "lease expired without a result",
                    }
                )
                row.state = state.value
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                row.payload = record.model_dump(mode="json")
                reclaimed.append(record)
        return reclaimed

    def append_event(self, event: AuditEvent) -> None:
        with self._write_session() as session:
            if session.get(EventRow, event.event_id) is not None:
                return
            session.add(
                EventRow(
                    event_id=event.event_id,
                    case_id=event.case_id,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    payload=event.model_dump(mode="json"),
                )
            )

    def list_events(self, case_id: str) -> list[AuditEvent]:
        statement = (
            select(EventRow)
            .where(EventRow.case_id == case_id)
            .order_by(EventRow.occurred_at, EventRow.event_id)
        )
        with self._read_session() as session:
            return [
                AuditEvent.model_validate(row.payload)
                for row in session.scalars(statement).all()
            ]

    def remember_idempotency(
        self, key: str, fingerprint: str, response: dict
    ) -> None:
        with self._write_session() as session:
            previous = session.get(IdempotencyRow, key)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise ValueError("idempotency key reused with different payload")
                return
            session.add(
                IdempotencyRow(
                    key=key,
                    fingerprint=fingerprint,
                    response=dict(response),
                )
            )

    def get_idempotency(self, key: str) -> tuple[str, dict] | None:
        with self._read_session() as session:
            row = session.get(IdempotencyRow, key)
            return None if row is None else (row.fingerprint, dict(row.response))

    def lock_idempotency(self, key: str) -> None:
        session = self._active_session.get()
        if session is None:
            raise RuntimeError("idempotency lock requires an active transaction")
        if self.engine.dialect.name == "postgresql":
            session.execute(
                text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )

    def add_user(self, user: UserAccount) -> None:
        row = UserRow(
            user_id=user.user_id,
            username=user.username.casefold(),
            role=user.role.value,
            active=user.active,
            payload=user.model_dump(mode="json"),
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
        with self._write_session() as session:
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError("user id or username already exists") from exc

    def get_user(self, user_id: str) -> UserAccount:
        with self._read_session() as session:
            row = session.get(UserRow, user_id)
            if row is None:
                raise KeyError(f"unknown user {user_id!r}")
            return UserAccount.model_validate(row.payload)

    def get_user_by_username(self, username: str) -> UserAccount:
        statement = select(UserRow).where(UserRow.username == username.casefold())
        with self._read_session() as session:
            row = session.scalar(statement)
            if row is None:
                raise KeyError("unknown username")
            return UserAccount.model_validate(row.payload)

    def save_user(self, user: UserAccount) -> None:
        with self._write_session() as session:
            row = session.get(UserRow, user.user_id)
            if row is None:
                raise KeyError(f"unknown user {user.user_id!r}")
            if row.username != user.username.casefold():
                raise ValueError("username cannot be changed")
            row.role = user.role.value
            row.active = user.active
            row.payload = user.model_dump(mode="json")
            row.updated_at = user.updated_at

    def list_users(self) -> list[UserAccount]:
        statement = select(UserRow).order_by(UserRow.username)
        with self._read_session() as session:
            return [
                UserAccount.model_validate(row.payload)
                for row in session.scalars(statement).all()
            ]

    def add_session(self, auth_session: AuthSession) -> None:
        row = AuthSessionRow(
            session_id=auth_session.session_id,
            token_hash=auth_session.token_hash,
            user_id=auth_session.user_id,
            expires_at=auth_session.expires_at,
            revoked_at=auth_session.revoked_at,
            payload=auth_session.model_dump(mode="json"),
        )
        with self._write_session() as session:
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError("session already exists") from exc

    def get_session_by_token_hash(self, token_hash: str) -> AuthSession | None:
        statement = select(AuthSessionRow).where(AuthSessionRow.token_hash == token_hash)
        with self._read_session() as session:
            row = session.scalar(statement)
            return None if row is None else AuthSession.model_validate(row.payload)

    def save_session(self, auth_session: AuthSession) -> None:
        with self._write_session() as session:
            row = session.get(AuthSessionRow, auth_session.session_id)
            if row is None:
                raise KeyError(f"unknown session {auth_session.session_id!r}")
            row.revoked_at = auth_session.revoked_at
            row.expires_at = auth_session.expires_at
            row.payload = auth_session.model_dump(mode="json")

    def revoke_user_sessions(self, user_id: str, *, now: datetime) -> int:
        statement = select(AuthSessionRow).where(
            AuthSessionRow.user_id == user_id,
            AuthSessionRow.revoked_at.is_(None),
        )
        count = 0
        with self._write_session() as session:
            for row in session.scalars(statement).all():
                auth_session = AuthSession.model_validate(row.payload).model_copy(
                    update={"revoked_at": now}
                )
                row.revoked_at = now
                row.payload = auth_session.model_dump(mode="json")
                count += 1
        return count

    def add_outbox(self, message: OutboxRecord) -> None:
        with self._write_session() as session:
            previous = session.scalar(
                select(OutboxRow).where(OutboxRow.dedupe_key == message.dedupe_key)
            )
            if previous is not None:
                previous_record = OutboxRecord.model_validate(previous.payload)
                if (
                    previous_record.topic != message.topic
                    or previous_record.payload != message.payload
                ):
                    raise ValueError("outbox dedupe key reused with different payload")
                return
            session.add(
                OutboxRow(
                    message_id=message.message_id,
                    case_id=message.case_id,
                    topic=message.topic,
                    state=message.state.value,
                    attempt=message.attempt,
                    max_attempts=message.max_attempts,
                    dedupe_key=message.dedupe_key,
                    available_at=message.available_at,
                    lease_owner=message.lease_owner,
                    lease_expires_at=message.lease_expires_at,
                    payload=message.model_dump(mode="json"),
                    created_at=message.created_at,
                    updated_at=message.updated_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError("outbox message id or dedupe key already exists") from exc

    def get_outbox(self, message_id: str) -> OutboxRecord:
        with self._read_session() as session:
            row = session.get(OutboxRow, message_id)
            if row is None:
                raise KeyError(f"unknown outbox message {message_id!r}")
            return OutboxRecord.model_validate(row.payload)

    def list_outbox(
        self, *, case_id: str | None = None, states: set[str] | None = None
    ) -> list[OutboxRecord]:
        statement = select(OutboxRow)
        if case_id is not None:
            statement = statement.where(OutboxRow.case_id == case_id)
        if states is not None:
            statement = statement.where(OutboxRow.state.in_(sorted(states)))
        statement = statement.order_by(OutboxRow.created_at, OutboxRow.message_id)
        with self._read_session() as session:
            return [
                OutboxRecord.model_validate(row.payload)
                for row in session.scalars(statement).all()
            ]

    def save_outbox(self, message: OutboxRecord) -> None:
        with self._write_session() as session:
            row = session.get(OutboxRow, message.message_id)
            if row is None:
                raise KeyError(f"unknown outbox message {message.message_id!r}")
            row.state = message.state.value
            row.attempt = message.attempt
            row.max_attempts = message.max_attempts
            row.available_at = message.available_at
            row.lease_owner = message.lease_owner
            row.lease_expires_at = message.lease_expires_at
            row.updated_at = message.updated_at
            row.payload = message.model_dump(mode="json")

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
        candidates = (
            select(OutboxRow.message_id)
            .where(
                OutboxRow.state == OutboxState.PENDING.value,
                OutboxRow.available_at <= now,
            )
            .order_by(OutboxRow.available_at, OutboxRow.created_at, OutboxRow.message_id)
            .limit(limit)
        )
        if self.engine.dialect.name == "postgresql":
            candidates = candidates.with_for_update(skip_locked=True)
        claimed: list[OutboxRecord] = []
        with self._write_session() as session:
            for message_id in list(session.scalars(candidates).all()):
                result = session.execute(
                    update(OutboxRow)
                    .where(
                        OutboxRow.message_id == message_id,
                        OutboxRow.state == OutboxState.PENDING.value,
                    )
                    .values(
                        state=OutboxState.LEASED.value,
                        attempt=OutboxRow.attempt + 1,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    continue
                row = session.get(OutboxRow, message_id)
                assert row is not None
                record = OutboxRecord.model_validate(row.payload).model_copy(
                    update={
                        "state": OutboxState.LEASED,
                        "attempt": row.attempt,
                        "lease_owner": worker_id,
                        "lease_expires_at": lease_expires_at,
                        "updated_at": now,
                    }
                )
                row.payload = record.model_dump(mode="json")
                claimed.append(record)
        return claimed

    def reclaim_expired_outbox(self, *, now: datetime) -> list[OutboxRecord]:
        statement = (
            select(OutboxRow)
            .where(
                OutboxRow.state == OutboxState.LEASED.value,
                OutboxRow.lease_expires_at.is_not(None),
                OutboxRow.lease_expires_at <= now,
            )
            .order_by(OutboxRow.created_at, OutboxRow.message_id)
        )
        reclaimed: list[OutboxRecord] = []
        with self._write_session() as session:
            for row in session.scalars(statement).all():
                state = (
                    OutboxState.FAILED
                    if row.attempt >= row.max_attempts
                    else OutboxState.PENDING
                )
                record = OutboxRecord.model_validate(row.payload).model_copy(
                    update={
                        "state": state,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "updated_at": now,
                        "last_error": "outbox lease expired before acknowledgement",
                    }
                )
                row.state = state.value
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                row.payload = record.model_dump(mode="json")
                reclaimed.append(record)
        return reclaimed

    def add_probe_result(self, record: ProbeResultRecord) -> bool:
        with self._write_session() as session:
            if session.get(ProbeResultRow, record.fingerprint) is not None:
                return False
            try:
                with session.begin_nested():
                    session.add(
                        ProbeResultRow(
                            fingerprint=record.fingerprint,
                            job_id=record.job_id,
                            case_id=record.case_id,
                            accepted_at=record.accepted_at,
                            payload=record.model_dump(mode="json"),
                        )
                    )
                    session.flush()
            except IntegrityError:
                return False
            return True

    def list_probe_results(self, case_id: str) -> list[ProbeResultRecord]:
        statement = (
            select(ProbeResultRow)
            .where(ProbeResultRow.case_id == case_id)
            .order_by(ProbeResultRow.accepted_at, ProbeResultRow.fingerprint)
        )
        with self._read_session() as session:
            return [
                ProbeResultRecord.model_validate(row.payload)
                for row in session.scalars(statement).all()
            ]

    def add_case_data(self, record: CaseDataRecord) -> None:
        with self._write_session() as session:
            key = {
                "case_id": record.case_id,
                "kind": record.kind.value,
                "record_id": record.record_id,
            }
            previous = session.get(CaseDataRow, key)
            if previous is not None:
                if CaseDataRecord.model_validate(previous.payload) != record:
                    raise ValueError("case data record id reused with different payload")
                return
            session.add(
                CaseDataRow(
                    kind=record.kind.value,
                    record_id=record.record_id,
                    case_id=record.case_id,
                    occurred_at=record.occurred_at,
                    payload=record.model_dump(mode="json"),
                )
            )

    def list_case_data(
        self, case_id: str, *, kinds: set[str] | None = None
    ) -> list[CaseDataRecord]:
        statement = select(CaseDataRow).where(CaseDataRow.case_id == case_id)
        if kinds is not None:
            statement = statement.where(CaseDataRow.kind.in_(sorted(kinds)))
        statement = statement.order_by(
            CaseDataRow.occurred_at, CaseDataRow.kind, CaseDataRow.record_id
        )
        with self._read_session() as session:
            return [
                CaseDataRecord.model_validate(row.payload)
                for row in session.scalars(statement).all()
            ]

    def add_routing_trigger(self, trigger: RoutingTriggerRecord) -> None:
        with self._write_session() as session:
            previous = session.scalar(
                select(RoutingTriggerRow).where(
                    RoutingTriggerRow.dedupe_key == trigger.dedupe_key
                )
            )
            if previous is not None:
                existing = RoutingTriggerRecord.model_validate(previous.payload)
                if (
                    existing.probe_results != trigger.probe_results
                    or existing.anchors != trigger.anchors
                    or existing.trigger_type != trigger.trigger_type
                ):
                    raise ValueError("routing trigger dedupe key reused")
                return
            session.add(
                RoutingTriggerRow(
                    trigger_id=trigger.trigger_id,
                    case_id=trigger.case_id,
                    trigger_type=trigger.trigger_type,
                    state=trigger.state.value,
                    attempt=trigger.attempt,
                    max_attempts=trigger.max_attempts,
                    dedupe_key=trigger.dedupe_key,
                    lease_owner=trigger.lease_owner,
                    lease_expires_at=trigger.lease_expires_at,
                    payload=trigger.model_dump(mode="json"),
                    created_at=trigger.created_at,
                    updated_at=trigger.updated_at,
                )
            )

    def get_routing_trigger(self, trigger_id: str) -> RoutingTriggerRecord:
        with self._read_session() as session:
            row = session.get(RoutingTriggerRow, trigger_id)
            if row is None:
                raise KeyError(f"unknown routing trigger {trigger_id!r}")
            return RoutingTriggerRecord.model_validate(row.payload)

    def save_routing_trigger(self, trigger: RoutingTriggerRecord) -> None:
        with self._write_session() as session:
            row = session.get(RoutingTriggerRow, trigger.trigger_id)
            if row is None:
                raise KeyError(f"unknown routing trigger {trigger.trigger_id!r}")
            row.state = trigger.state.value
            row.attempt = trigger.attempt
            row.max_attempts = trigger.max_attempts
            row.lease_owner = trigger.lease_owner
            row.lease_expires_at = trigger.lease_expires_at
            row.updated_at = trigger.updated_at
            row.payload = trigger.model_dump(mode="json")

    def list_routing_triggers(
        self, *, case_id: str | None = None, states: set[str] | None = None
    ) -> list[RoutingTriggerRecord]:
        statement = select(RoutingTriggerRow)
        if case_id is not None:
            statement = statement.where(RoutingTriggerRow.case_id == case_id)
        if states is not None:
            statement = statement.where(RoutingTriggerRow.state.in_(sorted(states)))
        statement = statement.order_by(RoutingTriggerRow.created_at, RoutingTriggerRow.trigger_id)
        with self._read_session() as session:
            return [
                RoutingTriggerRecord.model_validate(row.payload)
                for row in session.scalars(statement).all()
            ]

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
        candidates = (
            select(RoutingTriggerRow.trigger_id)
            .where(RoutingTriggerRow.state == RoutingTriggerState.PENDING.value)
            .order_by(RoutingTriggerRow.created_at, RoutingTriggerRow.trigger_id)
            .limit(limit)
        )
        if self.engine.dialect.name == "postgresql":
            candidates = candidates.with_for_update(skip_locked=True)
        claimed: list[RoutingTriggerRecord] = []
        with self._write_session() as session:
            for trigger_id in list(session.scalars(candidates).all()):
                result = session.execute(
                    update(RoutingTriggerRow)
                    .where(
                        RoutingTriggerRow.trigger_id == trigger_id,
                        RoutingTriggerRow.state == RoutingTriggerState.PENDING.value,
                    )
                    .values(
                        state=RoutingTriggerState.LEASED.value,
                        attempt=RoutingTriggerRow.attempt + 1,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    continue
                row = session.get(RoutingTriggerRow, trigger_id)
                assert row is not None
                record = RoutingTriggerRecord.model_validate(row.payload).model_copy(
                    update={
                        "state": RoutingTriggerState.LEASED,
                        "attempt": row.attempt,
                        "lease_owner": worker_id,
                        "lease_expires_at": lease_expires_at,
                        "updated_at": now,
                    }
                )
                row.payload = record.model_dump(mode="json")
                claimed.append(record)
        return claimed

    def reclaim_expired_routing_triggers(
        self, *, now: datetime
    ) -> list[RoutingTriggerRecord]:
        statement = (
            select(RoutingTriggerRow)
            .where(
                RoutingTriggerRow.state == RoutingTriggerState.LEASED.value,
                RoutingTriggerRow.lease_expires_at.is_not(None),
                RoutingTriggerRow.lease_expires_at <= now,
            )
            .order_by(RoutingTriggerRow.created_at, RoutingTriggerRow.trigger_id)
        )
        reclaimed: list[RoutingTriggerRecord] = []
        with self._write_session() as session:
            for row in session.scalars(statement).all():
                state = (
                    RoutingTriggerState.FAILED
                    if row.attempt >= row.max_attempts
                    else RoutingTriggerState.PENDING
                )
                record = RoutingTriggerRecord.model_validate(row.payload).model_copy(
                    update={
                        "state": state,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "updated_at": now,
                        "last_error": "routing trigger lease expired",
                    }
                )
                row.state = state.value
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                row.payload = record.model_dump(mode="json")
                reclaimed.append(record)
        return reclaimed

    def purge_case_content(
        self, case_id: str, *, preserve_outbox_message_id: str | None = None
    ) -> None:
        with self._write_session() as session:
            # EventRow is an append-only security log and is deliberately not
            # touched by case/content retention.
            session.execute(delete(JobRow).where(JobRow.case_id == case_id))
            session.execute(delete(ProbeResultRow).where(ProbeResultRow.case_id == case_id))
            session.execute(delete(CaseDataRow).where(CaseDataRow.case_id == case_id))
            session.execute(
                delete(RoutingTriggerRow).where(RoutingTriggerRow.case_id == case_id)
            )
            statement = select(OutboxRow).where(OutboxRow.case_id == case_id)
            if preserve_outbox_message_id is not None:
                statement = statement.where(
                    OutboxRow.message_id != preserve_outbox_message_id
                )
            for row in session.scalars(statement).all():
                current = OutboxRecord.model_validate(row.payload)
                next_state = (
                    OutboxState.CANCELLED
                    if current.state in {OutboxState.PENDING, OutboxState.LEASED}
                    else current.state
                )
                message = current.model_copy(
                    update={
                        "payload": {"redacted": True},
                        "state": next_state,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "last_error": None,
                    }
                )
                row.state = next_state.value
                row.lease_owner = None
                row.lease_expires_at = None
                row.payload = message.model_dump(mode="json")

    def redact_case_data(self, case_id: str, *, kinds: set[str]) -> None:
        if not kinds:
            return
        with self._write_session() as session:
            rows = session.scalars(
                select(CaseDataRow).where(
                    CaseDataRow.case_id == case_id,
                    CaseDataRow.kind.in_(sorted(kinds)),
                )
            ).all()
            for row in rows:
                record = CaseDataRecord.model_validate(row.payload).model_copy(
                    update={"payload": {"redacted": True}}, deep=True
                )
                row.payload = record.model_dump(mode="json")

    def purge_search_content(
        self, case_id: str, *, preserve_outbox_message_id: str | None = None
    ) -> None:
        temporary_kinds = {
            "ROUTING_STATE",
            "ROUTING_RUN",
            "PROBE_REQUEST",
            "TRACKLET",
            "CANDIDATE",
        }
        with self._write_session() as session:
            for row in session.scalars(
                select(JobRow).where(JobRow.case_id == case_id)
            ).all():
                job = JobRecord.model_validate(row.payload)
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
                job = job.model_copy(
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
                row.payload = job.model_dump(mode="json")
            for row in session.scalars(
                select(ProbeResultRow).where(ProbeResultRow.case_id == case_id)
            ).all():
                record = ProbeResultRecord.model_validate(row.payload)
                record = record.model_copy(
                    update={
                        "result": record.result.model_copy(
                            update={"tracklets": [], "candidates": []}, deep=True
                        )
                    },
                    deep=True,
                )
                row.payload = record.model_dump(mode="json")
            for row in session.scalars(
                select(RoutingTriggerRow).where(RoutingTriggerRow.case_id == case_id)
            ).all():
                current = RoutingTriggerRecord.model_validate(row.payload)
                trigger = current.model_copy(
                    update={
                        "probe_results": [],
                        "anchors": current.anchors.__class__(),
                    },
                    deep=True,
                )
                row.payload = trigger.model_dump(mode="json")
            for row in session.scalars(
                select(CaseDataRow).where(CaseDataRow.case_id == case_id)
            ).all():
                record = CaseDataRecord.model_validate(row.payload)
                temporary_artifact = (
                    record.kind.value == "ARTIFACT"
                    and (
                        record.payload.get("kind") == "HOLD_CLIP"
                        or record.payload.get("state") == "RELEASED"
                    )
                )
                if record.kind.value in temporary_kinds or temporary_artifact:
                    row.payload = record.model_copy(
                        update={"payload": {"redacted": True}}, deep=True
                    ).model_dump(mode="json")
            statement = select(OutboxRow).where(OutboxRow.case_id == case_id)
            if preserve_outbox_message_id is not None:
                statement = statement.where(
                    OutboxRow.message_id != preserve_outbox_message_id
                )
            for row in session.scalars(statement).all():
                message = OutboxRecord.model_validate(row.payload).model_copy(
                    update={"payload": {"redacted": True}}, deep=True
                )
                row.payload = message.model_dump(mode="json")
            for row in session.scalars(select(IdempotencyRow)).all():
                if row.response.get("case_id") == case_id:
                    row.response = {"case_id": case_id, "redacted": True}
