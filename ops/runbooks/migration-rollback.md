# Migration and release rollback

1. Back up and verify restore before `javes-main-migrate upgrade`.
2. Deploy migrations with application writers stopped or in the documented compatible version range.
3. Check Alembic revision, readiness, queue claims and a Controls read/write smoke test.
4. If rollback is required, stop writers, restore the pre-migration backup, deploy the prior image and run its `upgrade head`. Use `downgrade` only in rehearsal or when the revision explicitly documents safe data preservation.
5. Never point a prior binary at a newer schema without a compatibility result.
