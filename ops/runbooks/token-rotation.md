# Service-token rotation

1. Generate a new random token in the secret manager; never commit or print it.
2. Configure it as the current component token and retain the old value in the corresponding `_PREVIOUS` variable.
3. Restart Main, then update and restart that worker. Confirm claims and callbacks with the new token.
4. Remove `_PREVIOUS`, restart Main, and verify the retired token receives HTTP 401.
5. Rotate `WORKER_LEASE_SECRET` only after existing leases expire or during a coordinated worker drain.
