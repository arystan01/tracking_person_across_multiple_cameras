# Dead-letter inspection and replay

1. Run `javes-main-dlq list` with production database settings from the secret manager.
2. Diagnose the terminal `error_code`; fix the dependency before replay. Do not replay non-idempotent external operations without their receipt.
3. Replay one record with `javes-main-dlq replay job|outbox|routing ID`.
4. Watch queue depth, retries and duplicate-result metrics. The command resets ownership and extends the attempt ceiling by one; it does not erase history.
5. Never bulk-edit `FAILED` rows. Escalate repeated terminal failure instead of unbounded replay.
