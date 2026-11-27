# Clip worker

The worker implements `CLIP_CUT`, `CLIP_STATE`, `CLIP_EXPORT`, and internal
`CLIP_PURGE` over Main's
existing leased HTTP worker API. It imports public `javes_contracts` and
`javes_workers`, never Main's database or application internals.

## Run

Python 3.11+, FFmpeg with libx264, and ffprobe are required. On macOS use
`brew install ffmpeg`; on Ubuntu use `sudo apt-get install ffmpeg`.
Install the project with `python -m pip install -e '.[test,infra]'`.

```bash
javes-clip-worker --main-url http://127.0.0.1:8080 \
  --token "$CLIP_TOKEN" --worker-id clip-1 \
  --artifact-root "$JAVES_ARTIFACT_ROOT" --archive-root "$NVIDIA_ROOT" \
  --state-db clip-state.sqlite \
  --archive-url "$CCTV_ARCHIVE_URL" --archive-token "$CCTV_ARCHIVE_TOKEN"
```

`--once` processes at most one task. The archive token can alternatively come
from `JAVES_ARCHIVE_SERVICE_TOKEN`. Use a stable worker ID and persistent SQLite
database outside the served artifact root. Main, Clip, and Retention must share
the configured private artifact filesystem.

## Archive input

The worker posts `VideoSegmentRequest` to the read-only
`/v1/video-segments` archive endpoint, as Perception does. Camera, request,
requested timestamps and (when supplied) archive identity must match. The
resolved media can begin before `covered_from`, so extraction seeks by
`source_offset_s + command.t_from - covered_from`.
Padding is already included in `t_from`/`t_to` by Main and is not added again.
Main's default context is two seconds before and after the sighting, bounded
by the case window. An existing immutable cut is not recut when this default
changes. Exports concatenate the exact confirmed input clips, including any
context or overlapping camera time; they do not load complete camera recordings
or perform another person-detection pass to remove empty frames.

The archive must return a complete finite window. A missing or partial window
returns `UNAVAILABLE` without a fake output. A changed source identity or
inconsistent response fails validation. Supported sources are HTTP(S), file
URIs returned by the trusted archive, or a confined shared object key. HTTP
media downloads do not inherit the archive API bearer token or follow
redirects. Signed/authenticated media URLs must work without that API token.
RTSP and stitching several archive segments are not supported by this adapter.

## Outputs and lifecycle

- Cuts re-encode into H.264/yuv420p MP4 for accurate seeking. Output is silent:
  this adapter intentionally drops source audio.
- New clips start in `HOLD`. Review moves them to `CONFIRMED` or `RELEASED`.
  The lifecycle is reversible through revisioned commands. Restoring `HOLD`
  verifies that the retained bytes still exist and match their checksum.
- Exports accept only ready, confirmed clips from the same case. They preserve
  `ordered_clip_ids` exactly. Missing, corrupt, or unconfirmed inputs return a
  failed export with the affected IDs. A new export request ID is needed after
  inputs change. Export normalizes video to the first clip's resolution,
  letterboxes different aspect ratios, and preserves the first clip's FPS.
- Complete files publish atomically under `cases/{case_id}/clips/` or
  `cases/{case_id}/exports/`. MIME sidecars use Main/Retention's `.mime` format.
  Manifests contain the actual byte count, SHA-256, stable file creation time,
  and `review_hold`, `confirmed_evidence`, `case_14d`, or `ephemeral` class,
  plus a monotonic policy revision.
- The worker never deletes CCTV recordings, audit logs, or case artifacts.
  Main authorizes review/export/delete; Retention owns physical cleanup.

## Replay and leases

Command fingerprints, clip records, and result receipts persist in SQLite.
An exact request replay returns its original result. Reusing an ID with a
different payload fails. HTTP task results and manifests persist before
callback, so acknowledgement loss does not recut footage or repeat a state
transition. Workers use one leased task at a time and renew the lease every
third of its duration, including during FFmpeg work. Replacement lease tokens
are used for callbacks.

Run one polling process per state database for predictable task ordering.
SQLite serializes command execution across instances using the same database.
Deployments with independent databases must not share the same Clip queue:
state/export tasks need the clip metadata produced by cuts. After Retention
confirms physical deletion, Main sends `CLIP_PURGE`; the worker removes
case-scoped metadata and receipts and keeps only an anonymised purge tombstone.
Legal hold prevents Main from reaching this step.

## Verify

```bash
python -m pytest -q tests/test_clip_worker.py
python -m pytest -q -m 'not real_cv'
```

Tests generate short actual videos and compare decoded frames/duration/order.
They also check persistent replay, lost callback recovery, exact archive
correlation, path confinement, checksums, MIME compatibility, and Main API
acceptance. Media tests skip explicitly when FFmpeg/ffprobe are absent.
No large generated media or private local paths are committed.
