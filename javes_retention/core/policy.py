"""Explicit retention policy; no deletion decision is hidden in an adapter."""

from __future__ import annotations

from dataclasses import dataclass, field

from javes_contracts import RetentionClass, RetentionCommand, RetentionResource


@dataclass(frozen=True)
class RetentionPolicy:
    """Deletion policy used by the worker.

    ``expires_at=None`` means that a resource inherits the case-level TTL.  A
    ``TTL_EXPIRED`` command is emitted only when that case deadline is reached,
    so inherited resources are due at that point.  Explicit future expiries are
    preserved until a later command.

    Audit data and legal holds are never physically deleted by this policy.
    Main Infra keeps the complete append-only audit event (actor, timestamp,
    action and details) after case content is deleted.
    """

    version: str = "retention-policy.v1"
    always_preserve_classes: frozenset[RetentionClass] = field(
        default_factory=lambda: frozenset({RetentionClass.AUDIT})
    )

    def should_delete(
        self, resource: RetentionResource, command: RetentionCommand
    ) -> bool:
        if resource.deleted:
            return False
        if resource.legal_hold:
            return False
        if resource.retention_class in self.always_preserve_classes:
            return False
        if command.action == "CLOSE_CASE":
            return False
        if command.action == "DELETE_CASE":
            return True
        if command.action == "TTL_EXPIRED":
            return (
                resource.expires_at is None
                or resource.expires_at <= command.occurred_at
            )
        raise ValueError(f"unknown retention action {command.action!r}")

    def should_delete_after_case_marker(
        self,
        resource: RetentionResource,
        *,
        closed: bool,
        deleted_or_expired: bool,
    ) -> bool:
        """Apply an existing case tombstone to a late registration."""

        if resource.legal_hold:
            return False
        if resource.retention_class in self.always_preserve_classes:
            return False
        if deleted_or_expired:
            return True
        return False

    @staticmethod
    def allows_class_transition(
        previous: RetentionClass, incoming: RetentionClass
    ) -> bool:
        """Reject stale at-least-once registrations without a wire revision.

        The v1 resource contract has no monotonic revision.  Its valid lifecycle
        is nevertheless one-way: HOLD may become confirmed/released, and a
        superseded case result may become ephemeral.  Terminal registrations
        must not be resurrected by an older outbox delivery.
        """

        if previous == incoming:
            return True
        return (
            previous == RetentionClass.REVIEW_HOLD
            and incoming
            in {RetentionClass.CONFIRMED_EVIDENCE, RetentionClass.EPHEMERAL}
        ) or (
            previous == RetentionClass.CASE_RESULT
            and incoming == RetentionClass.EPHEMERAL
        )
