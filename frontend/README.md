# TRACE React dashboard

The default React dashboard follows the original Streamlit demo workflow. Its Python simulator uses the same search stages, candidate decisions, report generator, and local SQLite case store as Streamlit.

## Run the demo locally

Use Node.js 22.12+ or 20.19+ and a Python environment with the repository's root requirements installed. Vite uses the root `.venv/bin/python` when present, or `python3` otherwise. Set `TRACE_DEMO_PYTHON` to use another interpreter.

```sh
cd frontend
npm ci
npm run dev -- --open
```

Open the URL printed by Vite and sign in with **operator / trace-demo**. Vite starts the local Python simulator when the first request arrives. Use **New case** to select, rotate, and crop a reference photo, pick a named camera, and enter the last-known date and time. **Find case** opens a previously saved case by ID and also offers **Create demo case**. In **Current case**, use the Candidates, Route, and Case details tabs to review results. Case details includes the existing PDF report, close, and delete actions.

## Controls-connected workspace

Open the Vite URL with `?controls` appended to open the separate Controls-connected workspace. Start Main/Controls and select **Connect to Controls** with your backend-issued account. The frontend calls `/v1` on its own origin and Vite forwards it to `http://127.0.0.1:8080` during development and preview. Set a different URL when you start Vite if needed.

```sh
TRACE_CONTROLS_URL=http://127.0.0.1:8080 npm run dev
```

For backend startup steps, see the [JAVES-AI README](https://github.com/arystan01/tracking_person_across_multiple_cameras/tree/develop#configuration-and-start-order). The connected **New case** form uses the same file picker, preview, rotation, crop, and KST date/time fields as the demo. Configure a site, its deployed version, and the camera labels before starting Vite.

For a local check, install the backend’s `[test,infra]` dependencies in a Python environment and start the loopback fixture with a `develop` checkout. Add `--check` to verify case seeding without opening a port.

```sh
TRACE_CONTROLS_CHECKOUT=/path/to/backend-develop \
TRACE_CONTROLS_PYTHON=/path/to/backend-venv/bin/python sh ./start-local-controls.command
```

The fixture uses test workers and a fixed timeline. Sign in with **operator / trace-local-demo** and open **api-case**. Use these site and camera values for the fixture.

```sh
VITE_TRACE_SITE_ID=api-site VITE_TRACE_SITE_VERSION=v1 \
VITE_TRACE_CAMERAS='{"A":"Camera A","B":"Camera B"}' npm run dev
```

Set the same variables at build time for deployment. The camera object maps backend camera IDs to display labels.

### Reference upload contract

When the operator selects **Create case**, React generates the case and target IDs, applies the preview's rotation and crop, and uploads the resulting JPEG. The frontend expects an authenticated `POST /v1/cases/{case_id}/reference-image` that accepts a multipart `file` field and an `Idempotency-Key` header. The case ID identifies a draft at this point. Controls stages the file for the authenticated operator and returns JSON containing `reference_image_ref`. The frontend passes the full returned reference unchanged in the existing `POST /v1/cases` request and opens the returned case.

Use a Controls server running the team’s `develop` branch for reference uploads. The default demo URL runs the interaction locally. The current case API lacks fields for a title and notes, which the simulator supports.

## Build and deployment

```sh
npm run build
```

Use `npm run preview` to preview the demo from the built files. For a Controls deployment, serve `dist/` over HTTPS and forward `/v1/` on the same origin to Controls. The Vite proxy is for development and local preview. The frontend keeps the Controls bearer token in memory for the current page session. Controls checks permissions and owns case state. Case views and live JPEG snapshots refresh about every five seconds while monitoring a case. The search stages follow Controls. Candidate clips, the final merged export, and SVG route maps use authenticated case resources. The latest published frame, cameras on watch, and covered or unobserved periods depend on the running workers. With archived footage, the case time must overlap the archive timeline.

The local simulator exercises catch-up, simulated live tracking, and waiting. It enters the final summary when the operator closes the case. It uses sample clips for review and produces a PDF report.

## Visual references

The interface draws on [Camlytics](https://dribbble.com/shots/26907784-Camlytics-Live-Monitoring-Dashboard-for-CCTV-Management) for its dark camera wall, [Daily UI 22](https://dribbble.com/shots/25185363-Daily-UI-22-Search) for its map and camera navigation, and [Velize](https://dribbble.com/shots/27500052-Velize-Smart-City-Command-Center-Dashboard-UI-UX-Design) for its operations layout. In the default React view, the camera wall previews four [NVIDIA synthetic warehouse recordings in the team repository](https://github.com/arystan01/tracking_person_across_multiple_cameras/tree/8b27b7843c342fbe964027c8b77c58928e6fc308/data/nvidia_mtmc/mtmc_4cam/videos). The videos load from GitHub when selected. The demo candidate dialog plays a bundled three-second excerpt (source time 00:07–00:10) from the corresponding recording. Cameras A–D map to Cam001–004. A pre-rendered box follows the pink-shirted demo candidate in each excerpt. Candidate times, appearance scores, and review decisions in the demo are simulated. In the separate Controls workspace, the demo stills depict fictional locations generated for TRACE.
