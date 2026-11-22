#!/bin/sh
set -eu

frontend_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
checkout=${TRACE_CONTROLS_CHECKOUT:-"$frontend_dir/../tracking_person_across_multiple_cameras"}
python=${TRACE_CONTROLS_PYTHON:-"$frontend_dir/../.venv/bin/python"}

if [ ! -f "$checkout/tests/test_reference_upload.py" ]; then
  printf '%s\n' 'Set TRACE_CONTROLS_CHECKOUT to a backend develop checkout with reference-photo upload.' >&2
  exit 1
fi

cd "$checkout"
exec "$python" - "$@" <<'PY'
import sys

import uvicorn
from fastapi.testclient import TestClient

from javes_main.api import create_app
from tests.test_controls_api import body, headers, make_system, reference_jpeg

system = make_system()
app = create_app(
    system.manager, auth=system.auth, scheduler=system.scheduler, run_fake_jobs_inline=True,
)

with TestClient(app) as client:
    def sign_in(username, password):
        return client.post("/v1/auth/login", json={
            "username": username, "password": password,
        }).raise_for_status().json()["access_token"]

    root = sign_in("root", "root-password-for-tests")
    operator = client.post("/v1/admin/users", headers=headers("local-user", root), json={
        "username": "operator", "temporary_password": "temporary-password-1", "role": "OPERATOR",
    }).raise_for_status().json()
    temporary = sign_in("operator", "temporary-password-1")
    client.post("/v1/auth/change-password", headers=headers("local-password", temporary), json={
        "current_password": "temporary-password-1", "new_password": "trace-local-demo",
    }).raise_for_status()
    token = sign_in("operator", "trace-local-demo")
    request = body(operator["user_id"]).model_dump(mode="json")
    upload = client.post(
        "/v1/cases/api-case/reference-image", headers=headers("local-reference", token),
        files={"file": ("reference.jpg", reference_jpeg(), "image/jpeg")},
    ).raise_for_status().json()
    request["reference_image_ref"] = upload["reference_image_ref"]
    client.post("/v1/cases", headers=headers("local-case", token), json=request).raise_for_status()
    client.get("/v1/cases/api-case/view", headers=headers("local-read", token)).raise_for_status()

print("Loopback Controls fixture with test workers. Sign in as operator / trace-local-demo and open api-case.", flush=True)
if "--check" not in sys.argv:
    uvicorn.run(app, host="127.0.0.1", port=8080, ws="none")
PY
