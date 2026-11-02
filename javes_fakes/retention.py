"""Policy-visible in-memory Retention worker fake."""

from __future__ import annotations

import hashlib

from .contracts import (
    RetentionClass,
    RetentionCommand,
    RetentionResource,
    RetentionResult,
)


def _tombstone(command: RetentionCommand) -> str:
    text = f"{command.case_id}\x1f{command.command_id}\x1f{command.action}"
    return f"audit_{hashlib.sha256(text.encode()).hexdigest()[:16]}"


class FakeRetentionWorker:
    """Applies an explicit safe default policy without touching real files.

    CLOSE_CASE removes EPHEMERAL and REVIEW_HOLD resources. DELETE_CASE removes
    all non-audit resources except legal holds. TTL_EXPIRED removes due (or
    case-TTL inherited) resources except legal holds. The returned tombstone
    represents the deletion audit event and is not itself a retained case
    payload.
    """

    def __init__(self, resources: list[RetentionResource] | None = None) -> None:
        self.resources: dict[str, RetentionResource] = {}
        self._commands: dict[str, tuple[str, RetentionResult]] = {}
        self.deleted_cases: set[str] = set()
        self.expired_cases: set[str] = set()
        self.closed_cases: set[str] = set()
        for resource in resources or []:
            self.register(resource)

    def register(self, resource: RetentionResource) -> None:
        previous = self.resources.get(resource.resource_id)
        if previous is not None and previous.case_id != resource.case_id:
            raise ValueError("resource_id cannot move between cases")
        if previous is not None and previous.retention_class != resource.retention_class:
            allowed = (
                previous.retention_class == RetentionClass.REVIEW_HOLD
                and resource.retention_class
                in {RetentionClass.CONFIRMED_EVIDENCE, RetentionClass.EPHEMERAL}
            ) or (
                previous.retention_class == RetentionClass.CASE_RESULT
                and resource.retention_class == RetentionClass.EPHEMERAL
            )
            if not allowed:
                return
        value = resource.model_copy(deep=True)
        if (
            resource.case_id in self.deleted_cases | self.expired_cases
            and not resource.legal_hold
            and resource.retention_class != RetentionClass.AUDIT
        ):
            value.deleted = True
        elif (
            resource.case_id in self.closed_cases
            and resource.retention_class
            in {RetentionClass.EPHEMERAL, RetentionClass.REVIEW_HOLD}
            and not resource.legal_hold
        ):
            value.deleted = True
        self.resources[resource.resource_id] = value

    def process(self, command: RetentionCommand) -> RetentionResult:
        fingerprint = command.model_dump_json()
        previous = self._commands.get(command.command_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("same retention command_id used with different payload")
            return previous[1].model_copy(deep=True)

        deleted: list[str] = []
        preserved: list[str] = []
        if command.action == "DELETE_CASE":
            self.deleted_cases.add(command.case_id)
        elif command.action == "TTL_EXPIRED":
            self.expired_cases.add(command.case_id)
        elif command.action == "CLOSE_CASE":
            self.closed_cases.add(command.case_id)
        candidates = sorted(
            (
                resource
                for resource in self.resources.values()
                if resource.case_id == command.case_id and not resource.deleted
            ),
            key=lambda item: item.resource_id,
        )
        for resource in candidates:
            should_delete = False
            if (
                not resource.legal_hold
                and resource.retention_class != RetentionClass.AUDIT
            ):
                if command.action == "CLOSE_CASE":
                    should_delete = resource.retention_class in {
                        RetentionClass.EPHEMERAL,
                        RetentionClass.REVIEW_HOLD,
                    }
                elif command.action == "DELETE_CASE":
                    should_delete = True
                elif command.action == "TTL_EXPIRED":
                    should_delete = (
                        resource.expires_at is None
                        or resource.expires_at <= command.occurred_at
                    )
            if should_delete:
                self.resources[resource.resource_id] = resource.model_copy(
                    update={"deleted": True}, deep=True
                )
                deleted.append(resource.resource_id)
            else:
                preserved.append(resource.resource_id)

        result = RetentionResult(
            command_id=command.command_id,
            case_id=command.case_id,
            action=command.action,
            deleted_resource_ids=deleted,
            preserved_resource_ids=preserved,
            audit_tombstone_id=_tombstone(command),
        )
        self._commands[command.command_id] = (fingerprint, result)
        return result.model_copy(deep=True)
