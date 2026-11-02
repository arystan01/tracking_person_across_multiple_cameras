"""Retention worker implementing durable, resumable physical deletion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from javes_contracts import RetentionCommand, RetentionResource, RetentionResult

from .policy import RetentionPolicy
from .repository import SQLiteRetentionRepository, StoredResource
from .storage import DeletionPending, DeletionRouter


def _system_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _tombstone(command: RetentionCommand) -> str:
    text = f"{command.case_id}\x1f{command.command_id}\x1f{command.action}"
    return f"audit_{hashlib.sha256(text.encode()).hexdigest()[:20]}"


class RetentionDeletionError(RuntimeError):
    """The command is durable but at least one physical delete is unfinished."""


class RetentionWorker:
    """Production Retention core behind the existing Main Infra port.

    The worker first journals a command and its resource snapshot, then deletes
    each storage locator.  Successful items remain durable if another backend
    fails, and a retry resumes only unfinished work.  Missing objects count as
    successful idempotent deletion; unsupported locations fail closed.
    """

    def __init__(
        self,
        repository: SQLiteRetentionRepository,
        deletion_router: DeletionRouter,
        *,
        policy: RetentionPolicy | None = None,
        clock: Callable[[], datetime] = _system_now,
    ) -> None:
        self.repository = repository
        self.deletion_router = deletion_router
        self.policy = policy or RetentionPolicy()
        self.clock = clock
        self._lock = threading.RLock()

    def close(self) -> None:
        self.repository.close()

    def __enter__(self) -> RetentionWorker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retention clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _same_location(first: RetentionResource, second: RetentionResource) -> bool:
        if first.artifact_ref is None or second.artifact_ref is None:
            return True
        return first.artifact_ref == second.artifact_ref

    def register(self, resource: RetentionResource) -> None:
        """Upsert inventory and immediately enforce any existing tombstone."""

        if resource.artifact_ref is not None and resource.artifact_ref.archive_id:
            raise ValueError(
                "archive_id is an external read-only CCTV source reference; "
                "it cannot be registered as a Retention-owned artifact"
            )

        with self._lock:
            now = self._now()
            incoming = resource.model_copy(deep=True, update={"deleted": False})
            with self.repository.transaction():
                previous = self.repository.get_resource(incoming.resource_id)
                if previous is not None:
                    if previous.resource.case_id != incoming.case_id:
                        raise ValueError("resource_id cannot move between cases")
                    if not self._same_location(previous.resource, incoming):
                        raise ValueError(
                            "resource_id cannot be re-registered with another artifact location"
                        )
                    if not self.policy.allows_class_transition(
                        previous.resource.retention_class,
                        incoming.retention_class,
                    ):
                        # An older at-least-once registration arrived after a
                        # terminal lifecycle update. Keep the newer policy
                        # state; retrying the stale message is unnecessary.
                        return
                    # Allow metadata-only registration to be enriched once a stable
                    # artifact reference becomes known.
                    if incoming.artifact_ref is None:
                        incoming = incoming.model_copy(
                            update={"artifact_ref": previous.resource.artifact_ref}
                        )
                marker = self.repository.get_case_marker(incoming.case_id)
                must_delete = self.policy.should_delete_after_case_marker(
                    incoming,
                    closed=marker.closed_at is not None,
                    deleted_or_expired=(
                        marker.deleted_at is not None
                        or marker.ttl_expired_at is not None
                    ),
                )
                if previous is not None and previous.state == "DELETED" and not must_delete:
                    # A later legal-hold/class update cannot restore bytes that
                    # were already erased.
                    self.repository.save_resource(
                        incoming,
                        state="DELETED",
                        now=now,
                        registered_at=previous.registered_at,
                        deleted_at=previous.deleted_at,
                        attempt=previous.attempt,
                        proof=previous.proof,
                    )
                    return
                self.repository.save_resource(
                    incoming,
                    state="ACTIVE",
                    now=now,
                    registered_at=previous.registered_at if previous else now,
                    attempt=previous.attempt if previous else 0,
                    proof=previous.proof if previous else [],
                )
            # Re-registering an already-deleted resource may mean that an
            # upstream retry wrote the bytes again.  Enforce the tombstone a
            # second time instead of trusting the old DELETED flag.
            if must_delete:
                try:
                    proof = self._delete_resource(incoming.resource_id)
                    self._redact_deleted_resource(incoming.resource_id, proof)
                except Exception as exc:
                    raise RetentionDeletionError(
                        f"late resource {incoming.resource_id!r} cleanup failed: {exc}"
                    ) from exc

    def _delete_resource(self, resource_id: str) -> list[dict[str, object]]:
        stored = self.repository.get_resource(resource_id)
        if stored is None:
            raise KeyError(f"unknown retention resource {resource_id!r}")
        now = self._now()
        attempt = stored.attempt + 1
        self.repository.save_resource(
            stored.resource,
            state="DELETING",
            now=now,
            registered_at=stored.registered_at,
            attempt=attempt,
            proof=stored.proof,
        )
        try:
            receipts = self.deletion_router.delete(stored.resource)
            proof = [receipt.to_dict() for receipt in receipts]
        except Exception as exc:
            failure_proof = stored.proof
            if isinstance(exc, DeletionPending):
                failure_proof = [exc.receipt.to_dict()]
            self.repository.save_resource(
                stored.resource,
                state="DELETE_FAILED",
                now=self._now(),
                registered_at=stored.registered_at,
                attempt=attempt,
                last_error=f"{type(exc).__name__}: {exc}",
                proof=failure_proof,
            )
            raise
        self.repository.save_resource(
            stored.resource,
            state="DELETED",
            now=self._now(),
            registered_at=stored.registered_at,
            deleted_at=self._now(),
            attempt=attempt,
            proof=proof,
        )
        return proof

    @staticmethod
    def _redacted_proof(proof: list[dict[str, object]]) -> list[dict[str, object]]:
        sanitized: list[dict[str, object]] = []
        for receipt in proof:
            value = dict(receipt)
            locator = value.get("locator")
            if isinstance(locator, dict):
                locator_value = str(locator.get("value", ""))
                value["locator"] = {
                    "kind": locator.get("kind"),
                    "value": "sha256:"
                    + hashlib.sha256(locator_value.encode("utf-8")).hexdigest(),
                }
            value["detail"] = None
            sanitized.append(value)
        return sanitized

    def _redact_completed_deletions(self, command_id: str) -> None:
        """Keep proof semantics but remove artifact paths from durable history."""

        for item in self.repository.list_command_items(command_id):
            if item.disposition != "DELETE" or item.state != "DONE":
                continue
            proof = self._redact_deleted_resource(item.resource_id, item.proof)
            self.repository.update_command_item(
                command_id,
                item.resource_id,
                state="DONE",
                proof=proof,
            )

    def _redact_deleted_resource(
        self, resource_id: str, proof: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        stored = self.repository.get_resource(resource_id)
        if stored is None:
            return self._redacted_proof(proof)
        sanitized = self._redacted_proof(proof or stored.proof)
        redacted = stored.resource.model_copy(
            update={"kind": "DELETED", "artifact_ref": None, "deleted": True}
        )
        self.repository.save_resource(
            redacted,
            state="DELETED",
            now=self._now(),
            registered_at=stored.registered_at,
            deleted_at=stored.deleted_at,
            attempt=stored.attempt,
            proof=sanitized,
        )
        return sanitized

    def process(self, command: RetentionCommand) -> RetentionResult:
        """Apply one idempotent command and return final deletion proof."""

        with self._lock:
            fingerprint = _fingerprint(command)
            previous = self.repository.get_command(command.command_id)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise ValueError(
                        "same retention command_id used with different payload"
                    )
                if previous.state == "DONE" and previous.result is not None:
                    return previous.result.model_copy(deep=True)

            now = self._now()
            if previous is None:
                # Marker first: any late registration racing after this point
                # sees the case state and cleans itself immediately.
                self.repository.apply_case_marker(command)
                targets: list[str] = []
                preserved: list[str] = []
                for item in self.repository.list_resources(command.case_id):
                    if item.state == "DELETED":
                        continue
                    candidate = item.resource.model_copy(update={"deleted": False})
                    if self.policy.should_delete(candidate, command):
                        targets.append(candidate.resource_id)
                    else:
                        preserved.append(candidate.resource_id)
                try:
                    self.repository.begin_command(
                        command,
                        fingerprint,
                        targets=targets,
                        preserved=preserved,
                        now=now,
                    )
                except sqlite3.IntegrityError:
                    # Another process won the duplicate-delivery race.
                    concurrent = self.repository.get_command(command.command_id)
                    if concurrent is None or concurrent.fingerprint != fingerprint:
                        raise ValueError(
                            "same retention command_id used with different payload"
                        )
                    if concurrent.state == "DONE" and concurrent.result is not None:
                        return concurrent.result.model_copy(deep=True)
                    self.repository.resume_command(command.command_id, now=now)
            else:
                self.repository.resume_command(command.command_id, now=now)

            for item in self.repository.list_command_items(command.command_id):
                if item.disposition != "DELETE" or item.state == "DONE":
                    continue
                try:
                    stored = self.repository.get_resource(item.resource_id)
                    if stored is None:
                        raise KeyError(
                            f"retention inventory lost resource {item.resource_id!r}"
                        )
                    if stored.state == "DELETED":
                        proof = stored.proof
                    else:
                        proof = self._delete_resource(item.resource_id)
                    self.repository.update_command_item(
                        command.command_id,
                        item.resource_id,
                        state="DONE",
                        proof=proof,
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    self.repository.update_command_item(
                        command.command_id,
                        item.resource_id,
                        state="FAILED",
                        last_error=error,
                    )
                    self.repository.fail_command(
                        command.command_id, now=self._now(), error=error
                    )
                    raise RetentionDeletionError(
                        f"retention command {command.command_id!r} failed on "
                        f"resource {item.resource_id!r}: {exc}"
                    ) from exc

            items = self.repository.list_command_items(command.command_id)
            unfinished = [
                item.resource_id
                for item in items
                if item.disposition == "DELETE" and item.state != "DONE"
            ]
            if unfinished:
                raise RetentionDeletionError(
                    f"retention command has unfinished resources: {unfinished}"
                )
            deleted = sorted(
                item.resource_id
                for item in items
                if item.disposition == "DELETE" and item.state == "DONE"
            )
            preserved = sorted(
                item.resource_id
                for item in items
                if item.disposition == "PRESERVE"
            )
            result = RetentionResult(
                command_id=command.command_id,
                case_id=command.case_id,
                action=command.action,
                deleted_resource_ids=deleted,
                preserved_resource_ids=preserved,
                audit_tombstone_id=_tombstone(command),
            )
            self._redact_completed_deletions(command.command_id)
            items = self.repository.list_command_items(command.command_id)
            proof_payload = [
                {
                    "resource_id": item.resource_id,
                    "disposition": item.disposition,
                    "state": item.state,
                    "proof": item.proof,
                }
                for item in items
            ]
            self.repository.complete_command(
                result,
                now=self._now(),
                policy_version=self.policy.version,
                proof_digest=_fingerprint(proof_payload),
            )
            return result.model_copy(deep=True)

    def get_resource(self, resource_id: str) -> StoredResource | None:
        return self.repository.get_resource(resource_id)

    def list_resources(self, case_id: str | None = None) -> list[StoredResource]:
        return self.repository.list_resources(case_id)

    def get_tombstone(self, tombstone_id: str) -> dict[str, object] | None:
        return self.repository.get_tombstone(tombstone_id)

    def health_snapshot(self) -> dict[str, object]:
        """Small vendor-neutral snapshot for health/metrics adapters."""

        now = self._now()
        resources = self.repository.list_resources()
        states = Counter(item.state for item in resources)
        failures = [item for item in resources if item.state == "DELETE_FAILED"]
        return {
            "policy_version": self.policy.version,
            "resources_by_state": dict(sorted(states.items())),
            "legal_holds": sum(item.resource.legal_hold for item in resources),
            "failed_deletions": len(failures),
            "oldest_failure_age_s": (
                max((now - min(item.updated_at for item in failures)).total_seconds(), 0.0)
                if failures
                else 0.0
            ),
            "tombstones": len(self.repository.list_tombstones()),
        }

    def sweep_expired(self, now: datetime | None = None) -> list[RetentionResult]:
        """Delete explicit resource TTLs that outlive the case-level deadline.

        This maintenance entry point is safe to run repeatedly.  Command IDs
        are derived from the exact due-resource set, so a retry is idempotent
        and a later set of expiries becomes a new command.
        """

        cutoff = now or self._now()
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("retention sweep time must be timezone-aware")
        cutoff = cutoff.astimezone(timezone.utc)
        due_by_case: dict[str, list[tuple[str, str]]] = {}
        for item in self.repository.list_resources():
            value = item.resource
            marker = self.repository.get_case_marker(value.case_id)
            if (
                item.state != "DELETED"
                and (marker.deleted_at is not None or marker.ttl_expired_at is not None)
                and not value.legal_hold
                and value.retention_class not in self.policy.always_preserve_classes
                and value.expires_at is not None
                and value.expires_at <= cutoff
            ):
                due_by_case.setdefault(value.case_id, []).append(
                    (value.resource_id, value.expires_at.isoformat())
                )
        results: list[RetentionResult] = []
        for case_id, resources in sorted(due_by_case.items()):
            digest = hashlib.sha256(
                _canonical([case_id, sorted(resources)]).encode("utf-8")
            ).hexdigest()[:20]
            results.append(
                self.process(
                    RetentionCommand(
                        command_id=f"retention_ttl_{digest}",
                        case_id=case_id,
                        action="TTL_EXPIRED",
                        actor_id="retention-worker",
                        occurred_at=cutoff,
                    )
                )
            )
        return results
