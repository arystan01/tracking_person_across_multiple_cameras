# Incident response

1. Record UTC start time, affected case IDs, release and correlation IDs. Never paste bearer tokens, lease tokens, CCTV credentials or object keys into the ticket.
2. Check `/health/ready` and `/internal/v1/metrics`; identify queue, lease, Routing, deletion or archive symptoms.
3. Stop only the failing consumer. At-least-once tasks remain durable; do not edit database rows by hand.
4. Follow the DLQ, restore or rollback runbook as applicable. Preserve the security audit and database backup.
5. Verify a case by `x-request-id`/`traceparent`, worker task ID and route revision. Confirm no duplicate artifact or deletion effect.
6. Close only after metrics recover, one NVIDIA case completes, and all temporary credentials are revoked.
