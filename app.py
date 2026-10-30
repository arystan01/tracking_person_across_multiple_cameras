"""Local, backend-independent operator prototype. Run: streamlit run app.py."""
import base64
import io
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from PIL import Image, ImageOps, UnidentifiedImageError
from search_dashboard.adapters import DemoProvider, ImportProvider, JsonExport
from search_dashboard.domain import CAMERAS, timestamp
from search_dashboard.service import CaseManager, ranked_candidates, route
from search_dashboard.storage import SQLiteStore
from search_dashboard.views import site_map

ROOT = Path(__file__).parent
TZ = ZoneInfo('Asia/Seoul')
st.set_page_config(page_title='TRACE · Search operations', page_icon='◎', layout='wide')
st.markdown('''<style>
.stApp {background:#0b1420} .block-container {max-width:1440px;padding-top:3rem}
[data-testid="stSidebar"] {background:#101e2c}
[data-testid="stMetric"] {background:#132536;border:1px solid #294153;border-radius:12px;padding:14px}
h1 {letter-spacing:-1px} [data-testid="stCaptionContainer"] {color:#a9bdcf}
.stButton button {border-radius:8px} </style>''', unsafe_allow_html=True)


def refresh(message):
    st.session_state['flash'] = message
    st.rerun()


def act(fn, message):
    try:
        fn()
        refresh(message)
    except ValueError as exc:
        st.error(str(exc))


if not st.session_state.get('operator'):
    a, center, b = st.columns([1, 1.4, 1])
    with center:
        st.caption('TRACE / SEARCH OPERATIONS')
        st.title('Operator access')
        st.write('Sign in to open a search or retrieve a case by its ID.')
        st.info('Local interface demo · use operator / trace-demo. This is a simulated login, not production authentication.')
        with st.form('login'):
            username = st.text_input('Operator ID')
            password = st.text_input('Password', type='password')
            submit = st.form_submit_button('Sign in', type='primary', width='stretch')
        if submit:
            if username == 'operator' and password == 'trace-demo':
                st.session_state['operator'] = username
                st.session_state['page'] = 'Find case'
                st.rerun()
            else:
                st.error('Operator ID or password is incorrect.')
        st.caption('Access is provisioned by an administrator. No public registration or MFA in this prototype.')
    st.stop()

operator = st.session_state['operator']
store = SQLiteStore(os.environ.get('TRACE_DB_PATH', str(ROOT / 'data' / 'trace.sqlite3')))
manager = CaseManager(store)
manager.cleanup()
with st.sidebar:
    st.markdown('## ◎ TRACE')
    st.caption('SEARCH OPERATIONS')
    st.divider()
    page = st.radio('Workspace', ['Find case', 'New case', 'Current case'], key='page')
    st.divider()
    st.caption(f'Signed in as {operator}\n\nLocal prototype · simulated analysis\n\nAsia/Seoul · UTC+09:00')
    if st.button('Sign out', width='stretch'):
        st.session_state.clear()
        st.rerun()
if 'flash' in st.session_state:
    st.success(st.session_state.pop('flash'))


def open_case(case_id):
    st.session_state.pop('reviewing', None)
    st.session_state['case_id'] = case_id
    st.session_state['page'] = 'Current case'


def demo_case():
    now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
    case = manager.create('Atrium search · demo', operator, 'A', now.isoformat(),
        (now-timedelta(minutes=15)).isoformat(), (now+timedelta(minutes=30)).isoformat())
    open_case(case.id)


if page == 'Find case':
    st.caption('CASE ACCESS')
    st.title('Find the case you need.')
    st.write('Enter the exact case ID to retrieve its search or saved results.')
    with st.form('lookup'):
        lookup = st.text_input('Case ID', placeholder='e.g. 84c26a3f01bd')
        submitted = st.form_submit_button('Find case', type='primary')
    if submitted:
        try:
            found = store.get(lookup.strip().lower())
            st.session_state['case_id'] = found.id
            st.success(f'Found: {found.title}')
            st.button('Open case', on_click=open_case, args=(found.id,))
        except ValueError:
            st.error('No case found with that ID. Check the ID and try again.')
    st.divider()
    st.caption('First visit? Explore the full workflow with a fictional case.')
    st.button('Try a complete demo case', on_click=demo_case)
    st.stop()

if page == 'New case':
    st.caption('CASE INTAKE')
    st.title('Start with a last known sighting.')
    left, right = st.columns([1, 1.2])
    image_bytes = None
    with left:
        st.subheader('Reference photo')
        uploaded = st.file_uploader('Upload reference image', type=['jpg', 'jpeg', 'png'])
        if uploaded:
            try:
                if uploaded.size > 5_000_000:
                    raise ValueError('Use an image smaller than 5 MB.')
                im = ImageOps.exif_transpose(Image.open(uploaded)).convert('RGB')
                rotation = st.select_slider('Rotate clockwise', options=[0, 90, 180, 270], value=0)
                im = im.rotate(-rotation, expand=True)
                x = st.slider('Horizontal crop (%)', 0, 100, (0, 100))
                y = st.slider('Vertical crop (%)', 0, 100, (0, 100))
                if x[0] >= x[1] or y[0] >= y[1]:
                    raise ValueError('The crop must have a positive width and height.')
                w, h = im.size
                box = (int(w*x[0]/100), int(h*y[0]/100), int(w*x[1]/100), int(h*y[1]/100))
                if box[2] <= box[0] or box[3] <= box[1]:
                    raise ValueError('Crop is too small.')
                im = im.crop(box)
                im.thumbnail((1400, 1400))
                out = io.BytesIO(); im.save(out, format='JPEG')
                image_bytes = out.getvalue()
                st.image(image_bytes, caption='Reference used for this case', width=280)
            except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                st.error(str(exc))
        else:
            st.info('Upload a recent body/clothing reference, or enable the photo-free demo below.')
    with right:
        with st.form('intake'):
            title = st.text_input('Case title')
            camera = st.selectbox('Last-known zone / camera', list(CAMERAS), format_func=lambda c:f'{c} · {CAMERAS[c]["name"]}')
            day = st.date_input('Search date', datetime.now(TZ).date())
            known = st.time_input('Last-known time', time(14))
            c1, c2 = st.columns(2)
            start = c1.time_input('Window start', time(13,45))
            end = c2.time_input('Window end', time(14,30))
            overnight = st.checkbox('Window ends the following day')
            timeout = st.number_input('Reappearance timeout (seconds)', min_value=10, max_value=86400, value=300)
            st.caption('Used by the manual timeout simulation; it is not a frame count.')
            notes = st.text_area('Notes')
            demo = st.checkbox('Photo-free demo')
            submitted = st.form_submit_button('Open case', type='primary')
        if submitted:
            try:
                if image_bytes is None and not demo:
                    raise ValueError('Upload a valid reference image or select Photo-free demo.')
                case = manager.create(title, operator, camera, datetime.combine(day, known, TZ).isoformat(),
                    datetime.combine(day, start, TZ).isoformat(), datetime.combine(day+timedelta(days=int(overnight)), end, TZ).isoformat(), image_bytes, notes)
                case.timeout_seconds = int(timeout)
                store.save(case, operator, 'TIMEOUT_CONFIGURED')
                st.session_state['case_id'] = case.id
                st.success(f'Case created. Keep this ID: {case.id}')
                st.button('Continue to search', on_click=open_case, args=(case.id,))
            except ValueError as exc:
                st.error(str(exc))
    st.stop()

if not st.session_state.get('case_id'):
    st.title('No case is open')
    st.info('Find a case by ID or create a new case.')
    st.stop()
try:
    case = store.get(st.session_state['case_id'])
except ValueError:
    st.info('This case was deleted or expired. Find or create another case.')
    st.stop()
active = not case.saved and case.status != 'CLOSED'
st.caption(f'CASE {case.id.upper()} · {"SAVED / VIEW ONLY" if case.saved else "ACTIVE WORKSPACE"}')
st.title(case.title)
st.code(case.id, language=None)
st.caption('Copy this ID to reopen the case later. No personal search history is displayed.')

if case.saved:
    st.success('Saved case · results are view-only')
else:
    stages = ['Starting search', 'In camera coverage', 'Outside coverage', 'Conclusion on the past']
    stage = {'SEARCHING':0, 'IN_COVERAGE':1, 'OUT_OF_COVERAGE':2, 'CLOSED':3}[case.status]
    for i,col in enumerate(st.columns(4)):
        with col:
            (st.success if i == stage else st.caption)(f'{i+1}. {stages[i]}')

if not case.round and active:
    st.subheader('Search selected recordings')
    source = st.radio('Source', ['Simulated search', 'Import candidate results'], horizontal=True)
    upload = st.file_uploader('Candidate JSON or CSV', type=['json','csv']) if source.startswith('Import') else None
    st.caption('Demo results are fabricated and independent of the reference photo. Progress below represents local prototype steps.')
    if st.button('Start search', type='primary', disabled=source.startswith('Import') and upload is None):
        progress = st.progress(0, text='Preparing case')
        try:
            progress.progress(30, text='Loading candidate batch')
            provider = DemoProvider() if source == 'Simulated search' else ImportProvider(upload.getvalue(), upload.name.rsplit('.',1)[-1])
            manager.search(case.id, provider, operator)
            progress.progress(100, text='Candidate batch ready for review')
            refresh('Search completed. Review each candidate before saving.')
        except ValueError as exc:
            st.error(str(exc))
    st.stop()

confirmed = [c for c in case.candidates if c.decision == 'CONFIRMED']
unresolved = [c for c in case.candidates if c.decision in {'UNREVIEWED','RECALCULATE'}]
for col, label, value in zip(st.columns(4), ['Candidates','Awaiting decision','Confirmed','Case state'], [len(case.candidates),len(unresolved),len(confirmed),'Saved' if case.saved else case.status.replace('_',' ').title()]):
    col.metric(label,value)
if not case.saved:
    st.progress((len(case.candidates)-len(unresolved))/max(1,len(case.candidates)), text='Candidate review progress')


def play_candidate(candidate):
    path = ROOT / 'assets' / f'camera-{candidate.camera_id}.mp4'
    if case.source == 'demo' and path.exists():
        st.video(str(path))
        st.caption('Illustrative animation · simulated camera clip, not CCTV or model output.')
    else:
        st.info('No playable clip supplied by the backend for this candidate.')


def dismiss_review():
    st.session_state.pop('reviewing', None)


@st.dialog('Verify candidate', width='large', on_dismiss=dismiss_review)
def review_dialog(candidate_id):
    current = store.get(case.id)
    c = next(c for c in current.candidates if c.id == candidate_id)
    st.write(f'**Camera {c.camera_id} · {CAMERAS[c.camera_id]["name"]}**')
    st.caption(c.observed_at)
    play_candidate(c)
    choice = st.radio('What did you observe?', ['Target present; location correct', 'Target absent — remove from results', 'Target present; identification or route needs recalculation'], key=f'decision_{case.id}_{c.id}')
    if st.button('Apply decision', type='primary'):
        decision = {'Target present; location correct':'CONFIRMED','Target absent — remove from results':'REJECTED','Target present; identification or route needs recalculation':'RECALCULATE'}[choice]
        st.session_state.pop('reviewing', None)
        act(lambda: manager.review(case.id,c.id,decision,operator), 'Decision recorded.')


review_tab, route_tab, result_tab = st.tabs(['Candidate review' if not case.saved else 'Confirmed sightings', 'Route & coverage', 'Case results'])
with review_tab:
    main, context = st.columns([2.2,1])
    with context:
        st.subheader('Search context')
        if case.reference_b64:
            st.image(base64.b64decode(case.reference_b64), width=230)
        st.write(f'Last-known zone: **{case.camera_id} · {CAMERAS[case.camera_id]["name"]}**')
        st.caption(f'{case.start} → {case.end}')
        st.caption('Scores are appearance comparisons, not probabilities of identity.')
    with main:
        show_removed = st.checkbox('Show removed candidates', value=False) if not case.saved else False
        for row in ranked_candidates(case):
            c = row['candidate']
            if (case.saved and c.decision != 'CONFIRMED') or (c.decision == 'REJECTED' and not show_removed):
                continue
            with st.container(border=True):
                st.markdown(f'**Camera {c.camera_id} · {CAMERAS[c.camera_id]["name"]}**')
                st.caption(f'{c.id} · {timestamp(c.observed_at).astimezone(TZ):%H:%M:%S} · {c.decision}')
                st.write(f'Appearance **{c.appearance_score:.2f}** · {row["reason"]}')
                if not row['feasible']: st.warning('Travel-time conflict with the last-known sighting.')
                if c.metadata.get('recalculation'): st.info(c.metadata['recalculation'])
                if active:
                    if st.button('Review clip', key=f'review_{c.id}'):
                        st.session_state['reviewing'] = c.id
                else:
                    play_candidate(c)
        if any(c.decision == 'RECALCULATE' for c in case.candidates):
            st.warning('Recalculation requested. The demo retry returns the same observations for review; no model is connected.')
            if st.button('Run demo recalculation', disabled=not active or case.source != 'demo'):
                act(lambda: manager.recalculate(case.id,operator), 'Demo retry completed. Review the returned observations.')
        if not confirmed and case.saved: st.info('Saved with no confirmed target sightings.')
with route_tab:
    st.image(site_map(case).decode(), width='stretch')
    st.caption('Schematic camera zones; connecting lines are inferred movement, not exact positions.')
    st.dataframe(route(case), hide_index=True, width='stretch')
    if any(p['travel_conflict'] for p in route(case)): st.warning('Confirmed sightings contain incompatible travel times.')
    if confirmed:
        last = max(confirmed,key=lambda c:timestamp(c.observed_at))
        st.info(f'Last observed: Camera {last.camera_id} · {CAMERAS[last.camera_id]["name"]} · {last.observed_at}')
    else: st.info('No confirmed sighting. Only the reported starting location is known.')
    if active:
        st.caption('Prototype controls: manually simulate coverage changes and elapsed timeout.')
        a,b,c = st.columns(3)
        if a.button('Mark in coverage / re-entry', disabled=not confirmed or case.status=='IN_COVERAGE'):
            act(lambda:manager.transition(case.id,'IN_COVERAGE',operator), 'Re-entry recorded (simulated).')
        if b.button('Mark outside coverage', disabled=case.status=='OUT_OF_COVERAGE'):
            act(lambda:manager.transition(case.id,'OUT_OF_COVERAGE',operator), 'Outside coverage. Current position is unknown.')
        if c.button(f'Simulate {case.timeout_seconds}s timeout', disabled=case.status!='OUT_OF_COVERAGE'):
            st.session_state[f'timed_out_{case.id}'] = True
        if st.session_state.get(f'timed_out_{case.id}'):
            st.info('Simulated timeout reached: conclude the observed search, review remaining candidates, then save results. The missing-person case is not automatically closed.')
with result_tab:
    st.subheader('Confirmed clip sequence')
    if confirmed and case.source == 'demo':
        import subprocess
        import tempfile
        import shutil
        ordered = sorted(confirmed,key=lambda c:timestamp(c.observed_at))
        ffmpeg = shutil.which('ffmpeg')
        if ffmpeg:
            with tempfile.TemporaryDirectory() as temp:
                listing = Path(temp)/'clips.txt'
                listing.write_text('\n'.join("file '"+str(ROOT/'assets'/f'camera-{c.camera_id}.mp4')+"'" for c in ordered))
                output = Path(temp)/'sequence.mp4'
                result = subprocess.run([ffmpeg,'-v','error','-f','concat','-safe','0','-i',str(listing),'-c','copy','-y',str(output)],capture_output=True)
                if result.returncode == 0:
                    video = output.read_bytes()
                    st.video(video)
                    st.download_button('Download illustrative clip sequence',video,f'{case.id}-demo.mp4','video/mp4')
                else: st.warning('Demo sequence could not be assembled.')
        else:
            for c in ordered: play_candidate(c)
        st.caption('Sequence assembled from illustrative animations. Real footage preservation requires the backend.')
    elif confirmed:
        st.info('Confirmed observations saved as metadata. Clip merging awaits backend media.')
    else: st.info('Confirm a sighting to preview the clip sequence, or save a no-match result after reviewing all candidates.')
    st.download_button('Download case report',JsonExport().encode(case,route(case)),f'{case.id}.json','application/json')
    if active:
        st.divider()
        st.write('Save locks the reviewed results. You can reopen them by case ID.')
        if st.button('Save case',type='primary',disabled=bool(unresolved)):
            act(lambda:manager.save_result(case.id,operator),'Case saved. Results are now view-only.')
        if unresolved: st.caption('Review every candidate and resolve recalculation requests before saving.')
        st.caption('Leave this case open to hold your progress; decisions are stored automatically.')
    with st.expander('Delete case'):
        ack = st.checkbox('Permanently delete this case and its stored data',key=f'delete_{case.id}')
        if st.button('Delete case',disabled=not ack):
            store.delete(case.id,operator)
            st.session_state.pop('case_id',None)
            refresh('Case deleted.')

if active and st.session_state.get('reviewing') in {c.id for c in case.candidates}:
    review_dialog(st.session_state['reviewing'])
