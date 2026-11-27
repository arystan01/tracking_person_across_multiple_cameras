# Backup and restore rehearsal

1. Create an encrypted destination outside the shared artifact mount. Run `javes-main-backup backup PATH` with `JAVES_DATABASE_URL` supplied by the secret manager.
2. Copy the backup and adjacent `.manifest.json`; verify restricted permissions and off-host retention.
3. Restore only into an isolated database first: `javes-main-backup restore PATH`.
4. Run `javes-main-migrate current`, `/health/ready`, case/audit counts and one read-only Controls query.
5. For production recovery, stop writers, take a final backup, restore, upgrade to the deployed schema, then start one scheduler before scaling out.
6. RPO is 24 hours and RTO is 4 hours for the NVIDIA prototype. Record measured values after every rehearsal.
