"""Streamlit client for a replaceable control API. Run: streamlit run app.py."""
import base64
import io
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import streamlit as st
from PIL import Image, ImageOps, UnidentifiedImageError
from search_dashboard.report import PdfExport
from search_dashboard.backend import ControlAPI, DemoControlAPI
from search_dashboard.domain import CAMERAS, timestamp
from search_dashboard.service import can_review, status_label, STOP_LABELS, updated_snapshot
from search_dashboard.storage import SQLiteStore
from search_dashboard.views import site_map

ROOT = Path(__file__).parent
TZ = ZoneInfo("Asia/Seoul")
st.set_page_config(page_title="TRACE", layout="wide")
st.markdown("""<style>
.stApp {background:#ffffff;color:#243447}
.block-container {max-width:1380px;padding-top:3.5rem}
[data-testid="stSidebar"] {background:#f3f6fa}
[data-testid="stMetric"] {border:1px solid #dfe6ee;border-radius:6px;padding:12px}
h1 {font-size:2rem} h2 {font-size:1.5rem}
</style>""", unsafe_allow_html=True)

def display_time(value):
    if not value:
        return "Not available"
    try:
        return timestamp(value).astimezone(TZ).strftime("%d %b %Y, %H:%M:%S KST")
    except ValueError:
        return "Not available"

def camera_name(camera_id):
    if not camera_id:
        return "Camera not specified"
    name = CAMERAS.get(camera_id, {}).get("name")
    return f"Camera {camera_id} · {name}" if name else f"Camera {camera_id}"

def refresh():
    st.rerun()

if not st.session_state.get("operator"):
    _, column, _ = st.columns([1, 1.4, 1])
    with column:
        st.title("TRACE")
        st.subheader("Sign in")
        st.caption("Demo account: operator / trace-demo")
        with st.form("login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if username == "operator" and password == "trace-demo":
                st.session_state.update(operator=username, page="Find case")
                refresh()
            else:
                st.error("Incorrect username or password.")
        st.caption("Demo authentication. A production account service is not connected.")
    st.stop()

operator = st.session_state["operator"]
store = SQLiteStore(os.environ.get("TRACE_DB_PATH", str(ROOT / "data" / "trace.sqlite3")))
# Replace this factory with the team's ControlAPI client when transport is agreed.
api: ControlAPI = DemoControlAPI(store)

with st.sidebar:
    st.header("TRACE")
    page = st.radio("Navigation", ["Find case", "New case", "Current case"], key="page")
    st.divider()
    st.caption(f"Signed in as {operator}")
    st.caption("Demo version · updates automatically")
    if st.button("Sign out"):
        st.session_state.clear()
        refresh()

def open_case(case_id):
    st.session_state["case_id"] = case_id
    st.session_state["page"] = "Current case"

def create_demo():
    known = datetime.now(TZ) - timedelta(minutes=15)
    case = api.create_case("Demo case", operator, "A", known.isoformat())
    open_case(case.id)

if page == "Find case":
    st.title("Find case")
    with st.form("lookup"):
        lookup = st.text_input("Case ID")
        submitted = st.form_submit_button("Find case", type="primary")
    if submitted:
        try:
            found = api.get_case_view(lookup.strip().lower())
            st.success(f"{found.title} — {status_label(found)}")
            st.button("Open case", on_click=open_case, args=(found.id,))
        except ValueError as exc:
            st.error(str(exc))
    st.button("Create demo case", on_click=create_demo)
    st.caption("The demo automatically progresses through preparation, recordings, live tracking, and waiting. It stays open until you close it. Its observations and videos are simulated.")
    st.stop()

if page == "New case":
    st.title("New case")
    left, right = st.columns([1, 1.3])
    image_bytes = None
    with left:
        uploaded = st.file_uploader("Reference photo", type=["jpg", "jpeg", "png"])
        if uploaded:
            try:
                if uploaded.size > 5_000_000:
                    raise ValueError("Use an image smaller than 5 MB.")
                im = ImageOps.exif_transpose(Image.open(uploaded)).convert("RGB")
                rotation = st.select_slider("Rotation", options=[0, 90, 180, 270], value=0)
                im = im.rotate(-rotation, expand=True)
                x = st.slider("Horizontal crop (%)", 0, 100, (0, 100))
                y = st.slider("Vertical crop (%)", 0, 100, (0, 100))
                w, h = im.size
                bounds = (int(w*x[0]/100), int(h*y[0]/100), int(w*x[1]/100), int(h*y[1]/100))
                if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                    raise ValueError("Select a crop with positive width and height.")
                im = im.crop(bounds)
                im.thumbnail((1400, 1400))
                output = io.BytesIO()
                im.save(output, format="JPEG")
                image_bytes = output.getvalue()
                st.image(image_bytes, width=250)
            except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                st.error(str(exc))
    with right:
        with st.form("intake"):
            title = st.text_input("Case title")
            camera = st.selectbox("Last-known camera", list(CAMERAS), format_func=lambda c: f"{c} · {CAMERAS[c]['name']}")
            now = datetime.now(TZ)
            default_known = now-timedelta(minutes=15)
            day = st.date_input("Last-known date", default_known.date())
            known = st.time_input("Last-known time", default_known.time().replace(second=0, microsecond=0))
            st.caption("Asia/Seoul. If the time is approximate, enter an earlier estimate.")
            notes = st.text_area("Notes")
            no_photo = st.checkbox("Continue without a photo (demo)")
            submitted = st.form_submit_button("Create case", type="primary")
        if submitted:
            try:
                if image_bytes is None and not no_photo:
                    raise ValueError("Upload a reference photo or enable the demo option.")
                case = api.create_case(title, operator, camera, datetime.combine(day, known, TZ).isoformat(), image_bytes, notes)
                st.success("Case created.")
                st.code(case.id, language=None)
                st.button("Open case", on_click=open_case, args=(case.id,))
            except ValueError as exc:
                st.error(str(exc))
    st.stop()

if not st.session_state.get("case_id"):
    st.title("Current case")
    st.info("Find or create a case to continue.")
    st.stop()

def show_media(url):
    if url and urlparse(url).scheme in {"http", "https"}:
        st.video(url)
    else:
        st.caption("Video is not available yet.")

@st.fragment(run_every="2s")
def case_panel():
    try:
        fetched = api.get_case_view(st.session_state["case_id"])
    except (ValueError, OSError) as exc:
        st.error(f"Unable to load case: {exc}")
        return
    cache_key = f"view_{fetched.id}"
    previous = st.session_state.get(cache_key)
    # Refresh the whole snapshot on either revision. An unrelated revision does
    # not settle a review: that candidate must explicitly report APPLIED.
    st.session_state[cache_key] = updated_snapshot(previous, fetched)
    case = st.session_state[cache_key]
    st.title(case.title)
    st.caption(f"Case ID: {case.id}")
    if case.source == "demo":
        st.caption("Demo case with simulated sightings and video.")
    label = status_label(case)
    if case.lifecycle == "FAILED":
        st.error(label)
    elif case.done:
        st.success(label)
        st.write(STOP_LABELS.get(case.stop_reason, "Search ended. Stopping reason not provided."))
    else:
        st.info(label)
        if case.mode == "WAITING":
            st.caption("Routing is waiting for another sighting. The search has not ended.")
    ready = [c for c in case.candidates if c.decision == "UNREVIEWED" and c.review_state != "PENDING"]
    pending = [c for c in case.candidates if c.review_state == "PENDING"]
    confirmed = [c for c in case.candidates if c.decision == "CONFIRMED"]
    for col, name, value in zip(st.columns(4), ["Candidates", "Unreviewed", "Pending updates", "Confirmed"], [len(case.candidates), len(ready), len(pending), len(confirmed)]):
        col.metric(name, value)
    if pending:
        st.info("Decision submitted. Updating results; the search continues.")
    if case.errors or case.unobserved_windows:
        st.warning("Some recordings or search tasks need attention. See Coverage issues below.")

    results, route_tab, details = st.tabs(["Candidates", "Route", "Case details"], key=f"tabs_{case.id}", on_change="rerun")
    with results:
        show_rejected = st.checkbox("Show rejected candidates", key=f"rejected_{case.id}")
        if not case.candidates:
            st.info("No candidates received yet." if not case.done else "No candidates found.")
        for c in case.candidates:  # Preserve backend order and scores.
            if c.decision == "REJECTED" and not show_rejected:
                continue
            with st.container(border=True):
                st.subheader(f"Camera {c.camera_id} · {CAMERAS.get(c.camera_id, {}).get('name', c.camera_id)}")
                st.caption(f"{c.id} · {timestamp(c.observed_at).astimezone(TZ):%d %b %Y %H:%M:%S} KST")
                st.write(f"Appearance score: {c.appearance_score:.2f}")
                if c.observation_type == "LONG_GAP":
                    st.warning("Long gap. Confirmation is required before this observation can enter the main route.")
                if c.explanation:
                    st.write(c.explanation)
                with st.expander("Video", expanded=False, key=f"video_{case.id}_{c.id}", on_change="rerun"):
                    demo_clip = ROOT / "assets" / f"camera-{c.camera_id}.mp4"
                    if case.source == "demo" and c.camera_id in CAMERAS and demo_clip.exists():
                        st.video(str(demo_clip))
                        st.caption("Illustrative animation, not CCTV footage.")
                    else:
                        show_media(c.clip_url)
                if c.review_state == "PENDING":
                    st.info(f"{c.requested_decision.title()} submitted · updating results")
                elif c.decision != "UNREVIEWED":
                    st.write(f"Decision: **{c.decision.title()}** · Clip: {c.clip_state.title()}")
                else:
                    a, b, _ = st.columns([1, 1, 3])
                    for column, action in ((a, "CONFIRM"), (b, "REJECT")):
                        if column.button(action.title(), key=f"{action}_{case.id}_{c.id}", disabled=not can_review(case)):
                            try:
                                api.submit_decision(case.id, c.id, action, operator)
                                st.rerun()
                            except ValueError as exc:
                                st.error(str(exc))
        st.caption("Decisions cannot be changed. Scores are comparison values, not identity probabilities.")
    with route_tab:
        st.image(site_map(case).decode(), width="stretch")
        st.caption("Camera locations are schematic; lines show the reported route.")
        if case.route:
            st.dataframe([{
                "Location": camera_name(point.get("camera_id")),
                "Time": display_time(point.get("observed_at")),
                "Sighting": {"REPORTED": "Reported by staff", "CONFIRMED": "Confirmed"}.get(point.get("source"), "Search result"),
            } for point in case.route], hide_index=True, width="stretch")
            observed = [p for p in case.route if p.get("source") == "CONFIRMED"]
            if observed:
                last = max(observed, key=lambda p: timestamp(p["observed_at"]))
                st.write(f"Last confirmed sighting: Camera {last['camera_id']} · {display_time(last['observed_at'])}")
            else:
                st.caption("No confirmed sightings yet.")
        else:
            st.info("No route available yet.")
        if case.merged_clip_url:
            st.subheader("Clip sequence")
            show_media(case.merged_clip_url)
    with details:
        if case.reference_b64:
            st.image(base64.b64decode(case.reference_b64), width=190, caption="Reference photo")
        st.subheader("Case information")
        st.write(f"**Case ID:** {case.id}")
        st.write(f"**Title:** {case.title}")
        st.write(f"**Last-known location:** {camera_name(case.last_known['camera_id'])}")
        st.write(f"**Last seen:** {display_time(case.last_known['time'])}")
        st.write(f"**Created:** {display_time(case.created_at)}")
        if case.notes:
            st.subheader("Notes")
            st.write(case.notes)
        with st.expander("Coverage issues", expanded=bool(case.errors or case.unobserved_windows)):
            explanations = {
                "CAMERA_UNAVAILABLE": "This camera was temporarily unavailable. Some footage may be missing.",
                "LEGACY_SNAPSHOT": "This case comes from an earlier prototype. Its original records are available for reference.",
            }
            for error in case.errors:
                message = explanations.get(error.get("code"), "A search task reported a problem. Some results may be incomplete.")
                if error.get("camera_id"):
                    message = f"{camera_name(error['camera_id'])}: {message}"
                st.write(message)
            if case.unobserved_windows:
                st.write("**Periods without search coverage**")
                st.dataframe([{
                    "Camera": camera_name(item.get("camera_id")),
                    "From": display_time(item.get("start")),
                    "To": display_time(item.get("end")),
                } for item in case.unobserved_windows], hide_index=True, width="stretch")
            if not case.errors and not case.unobserved_windows:
                st.caption("No coverage issues reported.")
        st.download_button("Download report", PdfExport().encode(case), file_name=f"{case.id}.pdf", mime="application/pdf")
        if case.lifecycle in {"PREPARING", "SEARCHING"} and not case.done:
            with st.expander("Close search"):
                st.caption("Ends the active search. Existing results remain available by case ID.")
                ack = st.checkbox("Close this search", key=f"close_{case.id}")
                if st.button("Close search", disabled=not ack or bool(pending)):
                    try:
                        api.close_case(case.id, operator)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
        st.caption("Results are stored automatically. Saving a report does not end the search.")
        if case.done or case.lifecycle == "FAILED":
            with st.expander("Delete case"):
                delete_ack = st.checkbox("Permanently delete this case", key=f"delete_{case.id}")
                if st.button("Delete case", disabled=not delete_ack):
                    try:
                        api.delete_case(case.id, operator)
                        st.session_state.pop("case_id", None)
                        st.session_state.pop(cache_key, None)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
case_panel()
