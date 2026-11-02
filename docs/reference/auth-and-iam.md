# Internal authentication and IAM

Status: implemented for the Main Infra attachment-ready contour.

## Product rule

JAVES-AI is a closed internal system. There is no self-registration, invitation
link, public signup, social login, or account-creation screen for ordinary
Dashboard users.

The first `ROOT` account is created from Main Infra by an administrator:

```bash
export JAVES_BOOTSTRAP_PASSWORD='replace-with-a-secret-from-your-secret-store'
javes-main-bootstrap-root \
  --database-url postgresql+psycopg://... \
  --username root
unset JAVES_BOOTSTRAP_PASSWORD
```

Bootstrap succeeds only when the user table is empty. Afterwards, ROOT creates
and manages users through the authenticated Main Infra admin API. A production
operations CLI or admin console may call that API, but Dashboard must not expose
a public signup flow.

## Login and sessions

Dashboard sends `username + password` to `POST /v1/auth/login`. Main Infra
returns an opaque bearer token and expiry time. Only the SHA-256 digest of the
token is stored. Passwords are hashed with Argon2id and are never returned or
stored in audit payloads.

- Sessions expire after the configured TTL (eight hours by default).
- Logout revokes the current session.
- Role change, account disable and password reset revoke all user sessions.
- Five consecutive failures temporarily lock the account by default.
- A newly created human user receives a temporary password and may call only
  the password-change flow until it has been changed.
- Passwords are currently required to contain at least 12 characters.

MFA is intentionally not part of this MVP. It is a later security-hardening
feature and should be added through the auth boundary, not simulated by
Dashboard code.

## Roles and effective permissions

| Role | Effective access |
|---|---|
| `ROOT` | Every permission, including user creation, role assignment, enable/disable, password reset and case deletion |
| `ADMIN` | Read/create/review/close/export/delete cases, read case events and global audit; cannot manage IAM users |
| `OPERATOR` | Read/create/review/close/export cases and their event log; cannot delete cases or manage IAM |
| `VIEWER` | Read case views, maps and clips only |
| `SERVICE` | Submit worker results only; intended for a real Perception adapter |

The checked-in map is `ROLE_PERMISSIONS` in
`javes_main/domain/models.py`. Main Infra enforces it server-side; hiding a
button in Dashboard is usability, not authorization.

## IAM operations

All IAM mutations except login/logout require an authenticated ROOT session and
an `idempotency-key` header:

```text
GET  /v1/admin/users
POST /v1/admin/users
PUT  /v1/admin/users/{user_id}/role
POST /v1/admin/users/{user_id}/disable
POST /v1/admin/users/{user_id}/enable
POST /v1/admin/users/{user_id}/reset-password
GET  /v1/admin/audit
```

Safety rules:

- a user cannot change their own role or disable their own account;
- the last active ROOT cannot be demoted or disabled;
- usernames are case-insensitive and immutable;
- accounts are disabled rather than physically deleted, preserving audit
  attribution;
- password reset installs a temporary password and revokes existing sessions;
- SERVICE accounts do not require an interactive first-login password change.

## Dashboard responsibilities

Dashboard implements:

1. login form;
2. secure in-memory/session handling of the bearer token;
3. password-change screen when `must_change_password=true`;
4. logout;
5. controls filtered by the returned permissions;
6. correct handling of `401`, `403` and RFC7807 problem responses.

Dashboard must not:

- offer self-registration;
- decide authorization locally;
- send an `operator_id` different from the authenticated `user_id`;
- store passwords or worker secrets;
- expose the internal worker-result endpoint;
- infer that a hidden button makes an operation secure.

If the team later wants a ROOT-only IAM page, it remains a client of these Main
Infra endpoints and does not create accounts directly in the database.

## Worker authentication

The current worker-result endpoint accepts either a fixed deployment secret or
a `SERVICE` bearer account. The fixed token exists for simple initial
integration. Production deployment should prefer a rotated secret mechanism,
mTLS or a queue identity appropriate to the chosen transport. Worker identity
does not grant case-management permissions.

## Audit and later hardening

Login success/failure, logout, root bootstrap, user creation, role changes,
enable/disable, password changes and resets are append-only IAM audit events.
Case and IAM audit events keep actor, timestamp, action and details forever;
case DELETE/TTL and technical-log rotation never rewrite or remove them.
Future security work may add MFA, password-breach screening, external SSO,
shorter-lived access tokens with refresh rotation, IP/device policy and a
formal security review. None of those changes require modifying Routing or the
worker contracts.
