"""Streamlit screens backed by the versioned Controls API."""

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from search_dashboard.controls import ControlsClient, ControlsError, case_path

KST = ZoneInfo("Asia/Seoul")


def show_error(error):
    if error.status == 401:
        st.session_state.clear()
        st.session_state["controls_notice"] = str(error)
        st.rerun()
    st.error(str(error))


def mutation(client, path, fields, *, stamped=True, secret=False):
    """Retry the same payload and key after a timeout or an ambiguous response."""
    signature = sha256(json.dumps([path, fields], sort_keys=True).encode()).hexdigest()
    pending = st.session_state.setdefault("controls_pending", {})
    key, issued_at = pending.setdefault(signature, (uuid4().hex, datetime.now(timezone.utc).isoformat()))
    payload = dict(fields)
    if path == "/v1/cases":
        payload["window"] = {**payload["window"], "end": issued_at}
    if stamped:
        payload.update(occurred_at=issued_at)
        payload["decision_id" if path.endswith("/decisions") else "command_id"] = key
    try:
        response = client.request("POST", path, payload=payload, key=key)
    except ControlsError as exc:
        if secret or (exc.status is not None and 400 <= exc.status < 500):
            pending.pop(signature, None)
        raise
    pending.pop(signature, None)
    return response


def sign_in(client):
    st.title("TRACE")
    st.subheader("Sign in")
    if notice := st.session_state.pop("controls_notice", None):
        st.warning(notice)
    with st.form("controls_login", clear_on_submit=True):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        try:
            session = client.request("POST", "/v1/auth/login", payload={
                "username": username.strip(), "password": password,
            })
        except ControlsError as exc:
            st.error(str(exc))
        else:
            st.session_state["controls_session"] = session
            st.rerun()


def change_password(client):
    st.subheader("Change your temporary password")
    with st.form("controls_password", clear_on_submit=True):
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        submitted = st.form_submit_button("Change password")
    if submitted:
        try:
            mutation(client, "/v1/auth/change-password", {
                "current_password": current, "new_password": new,
            }, stamped=False, secret=True)
        except ControlsError as exc:
            show_error(exc)
        else:
            st.session_state.clear()
            st.session_state["controls_notice"] = "Password changed. Sign in again."
            st.rerun()


def find_case(client):
    st.title("Find case")
    with st.form("controls_lookup"):
        case_id = st.text_input("Case ID").strip()
        submitted = st.form_submit_button("Open case")
    if submitted and case_id:
        st.session_state["controls_case"] = case_id
        st.rerun()
    try:
        cases = client.request("GET", "/v1/cases")
    except ControlsError as exc:
        show_error(exc)
        return
    if cases:
        st.dataframe([
            {"Case ID": item["case_id"], "Target ID": item["target_id"],
             "Status": item["lifecycle"], "Mode": item["mode"], "Site": item["site_id"]}
            for item in cases
        ], hide_index=True)
    else:
        st.info("No cases are available.")


def new_case(client, user):
    st.title("New case")
    st.caption("Use the case ID and reference image key supplied by the backend operator.")
    now = datetime.now(KST)
    known = now - timedelta(minutes=15)
    with st.form("controls_new_case"):
        case_id = st.text_input("Case ID").strip()
        target_id = st.text_input("Target ID").strip()
        reference = st.text_input("Staged reference image key").strip()
        site = st.text_input("Site ID").strip()
        version = st.text_input("Site version").strip()
        camera = st.text_input("Last known camera ID").strip()
        day = st.date_input("Last known date (KST)", known.date())
        time = st.time_input("Last known time (KST)", known.time())
        submitted = st.form_submit_button("Create case", type="primary")
    if not submitted:
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]+", case_id) or not all((target_id, site, version, camera)):
        st.error("Enter a case ID using letters, digits, hyphens or underscores, and fill in the other fields.")
        return
    prefix = f"cases/{case_id}/"
    if (
        not reference.startswith(prefix) or not reference[len(prefix):]
        or ".." in PurePosixPath(reference).parts or "\\" in reference
    ):
        st.error(f"Enter a staged reference image key under {prefix}")
        return
    start = datetime.combine(day, time, tzinfo=KST)
    if start > now:
        st.error("Last known time must be in the past.")
        return
    try:
        view = mutation(client, "/v1/cases", {
            "case_id": case_id, "target_id": target_id, "operator_id": user["user_id"],
            "site_id": site, "site_version": version,
            "reference_image_ref": {"object_key": reference},
            "last_known": {"camera_id": camera, "time": start.isoformat()},
            "window": {"start": start.isoformat()},
        }, stamped=False)
    except ControlsError as exc:
        show_error(exc)
    else:
        st.session_state["controls_case"] = view["case_id"]
        st.rerun()


def media(client, view, path):
    maps = view.get("rendered_route_maps", [])
    if maps:
        floors = {item["floor_id"]: item for item in maps}
        floor = st.selectbox("Floor", list(floors), key=f"floor_{view['case_id']}")
        route = floors[floor]
        try:
            url = route["url"]
            if not url.startswith(f"{path}/routes/"):
                raise ControlsError("Controls returned an invalid route image URL.")
            svg = client.request("GET", url, media_type="image/svg+xml")
            encoded = base64.b64encode(svg).decode("ascii")
            st.image(f"data:image/svg+xml;base64,{encoded}", caption=route["alt_text"])
        except ControlsError as exc:
            show_error(exc)
    else:
        st.info("A route map is not available yet.")

    if view.get("live") and view["live"].get("stream_url"):
        try:
            frame = client.request("GET", f"{path}/live", media_type="image/jpeg")
            st.image(frame, caption=f"Live camera {view['live']['camera_id']}")
        except ControlsError as exc:
            if exc.status == 404:
                st.info("The live frame is no longer available.")
            else:
                show_error(exc)


def can(user, permission):
    return "*" in user["permissions"] or permission in user["permissions"]


def review_candidates(client, view, path, user):
    st.subheader("Candidates")
    candidates = view.get("candidates", [])
    if not candidates:
        st.caption("No candidates reported.")
    for item in candidates:
        candidate_id = item["candidate_id"]
        with st.expander(f"{item.get('camera_id') or 'Camera unavailable'} · {candidate_id}"):
            st.write({
                "Start": item.get("t_start"), "End": item.get("t_end"),
                "Appearance score": item.get("appearance_score"),
                "Review state": item.get("review_state"),
            })
            if (
                can(user, "case.review") and view["lifecycle"] in {"SEARCHING", "CLOSED"}
                and item.get("review_state") in {"HOLD", "UNREVIEWED"}
            ):
                for column, decision in zip(st.columns(2), ("CONFIRM", "REJECT")):
                    if column.button(decision.title(), key=f"{path}:{candidate_id}:{decision}"):
                        try:
                            mutation(client, f"{path}/decisions", {
                                "candidate_id": candidate_id, "tracklet_id": item["tracklet_id"],
                                "operator_id": user["user_id"], "decision": decision,
                            })
                        except ControlsError as exc:
                            show_error(exc)
                        else:
                            st.rerun()


def case_actions(client, view, path, user):
    lifecycle = view["lifecycle"]
    actions = []
    if can(user, "case.close") and lifecycle in {"PREPARING", "SEARCHING"}:
        actions.append(("CLOSE", "Close search"))
    if can(user, "case.export") and lifecycle in {"CLOSED", "SAVED"}:
        actions.append(("EXPORT", "Request clip export"))
    if can(user, "case.delete") and lifecycle in {"CLOSED", "SAVED", "FAILED"}:
        if st.checkbox("Confirm case deletion", key=f"confirm_{path}"):
            actions.append(("DELETE", "Delete case"))
    for action, label in actions:
        if st.button(label, key=f"{path}:{action}"):
            try:
                mutation(client, f"{path}/commands/{action.lower()}", {
                    "action": action, "operator_id": user["user_id"],
                })
            except ControlsError as exc:
                show_error(exc)
            else:
                st.rerun()


@st.fragment(run_every="5s")
def case_view(client, case_id, user):
    path = case_path(case_id)
    try:
        view = client.request("GET", f"{path}/view")
    except ControlsError as exc:
        show_error(exc)
        return
    st.title(f"Case {view['case_id']}")
    st.write(" · ".join(filter(None, (view["lifecycle"], view.get("mode"), view.get("stop_reason")))))
    media(client, view, path)
    review_candidates(client, view, path, user)
    for field, label in (
        ("estimate", "Location estimate"), ("alternatives", "Alternative routes"),
        ("search_progress", "Search progress"), ("unobserved_windows", "Unobserved windows"),
        ("errors", "Errors"),
    ):
        if value := view.get(field):
            with st.expander(label):
                st.write(value)
    if clips := view.get("clips"):
        with st.expander("Clips"):
            st.dataframe([
                {key: clip.get(key) for key in ("clip_id", "camera_id", "status", "state", "t_start", "t_end")}
                for clip in clips
            ], hide_index=True)
    if export := view.get("export"):
        st.write(f"Clip export · {export.get('status', 'Requested')}")
    case_actions(client, view, path, user)


def run(base_url):
    session = st.session_state.get("controls_session")
    try:
        client = ControlsClient(base_url, session["access_token"] if session else None)
    except ControlsError as exc:
        st.error(str(exc))
        return
    if not session:
        sign_in(client)
        return

    try:
        user = client.request("GET", "/v1/auth/me")
    except ControlsError as exc:
        show_error(exc)
        st.button("Retry")
        return
    with st.sidebar:
        st.header("TRACE")
        st.caption(f"Signed in as {user['username']}")
        if st.button("Sign out"):
            try:
                client.request("POST", "/v1/auth/logout")
            except ControlsError as exc:
                show_error(exc)
            else:
                st.session_state.clear()
                st.rerun()
    if user["must_change_password"]:
        change_password(client)
        return

    with st.sidebar:
        find = st.button("Find case")
        new = can(user, "case.create") and st.button("New case")
    if find or new:
        st.session_state.pop("controls_case", None)
        st.session_state["controls_page"] = "new" if new else "find"
    if selected := st.session_state.get("controls_case"):
        case_view(client, selected, user)
    elif st.session_state.get("controls_page") == "new" and can(user, "case.create"):
        new_case(client, user)
    else:
        find_case(client)
