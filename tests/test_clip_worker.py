"""Actual FFmpeg outputs plus archive and Main HTTP boundary checks."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from javes_clip.archive import HttpClipArchive
from javes_clip.media import FFmpegMedia, MediaError
from javes_clip.runner import ClipTaskRunner
from javes_clip.storage import ClipStore, case_prefix, checksum
from javes_clip.worker import ClipWorker
from javes_contracts import CutClipCommand, ClipStateCommand, ExportClipsCommand, WorkerTaskEnvelope, WorkerHeartbeatResponse
from javes_contracts.acceptance import check_cut_result, check_clip_state_result, check_export_result

BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)
pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='requires FFmpeg and ffprobe')


@pytest.fixture
def source(tmp_path):
    path = tmp_path/'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x240:rate=25:duration=8', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path


def command(name='cut-1', *, start=2, end=4, case='case-1'):
    return CutClipCommand(clip_request_id=name, case_id=case, candidate_id='candidate-'+name,
                          tracklet_id='track-'+name, camera_id='A', t_from=BASE+timedelta(seconds=start),
                          t_to=BASE+timedelta(seconds=end), source_ref={'archive_id': 'recording-A'})


def archive(source, *, availability='AVAILABLE', response_change=None):
    def handler(request):
        assert request.method == 'POST'
        assert request.url.path == '/v1/video-segments'
        body = json.loads(request.content)
        response = dict(request_id=body['request_id'], camera_id=body['camera_id'], availability=availability,
                        requested_from=body['t_from'], requested_to=body['t_to'],
                        covered_from=BASE.isoformat(), covered_to=(BASE+timedelta(seconds=8)).isoformat(),
                        coverage_ratio=1.0, source_ref={'archive_id': 'recording-A', 'uri': source.as_uri()})
        if availability == 'UNAVAILABLE':
            response.update(coverage_ratio=0, source_ref=None)
        if availability == 'PARTIAL':
            response.update(coverage_ratio=0.5, covered_to=(BASE+timedelta(seconds=3)).isoformat())
        if response_change:
            response.update(response_change)
        return httpx.Response(200, json=response)
    return HttpClipArchive('http://archive', 'test-secret', artifact_root=source.parent,
                           client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def worker(tmp_path, source):
    store = ClipStore(tmp_path/'state.sqlite', tmp_path/'artifacts')
    w = ClipWorker(store, archive(source))
    yield w
    store.close()


def pixels(path, seconds):
    return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-ss', str(seconds), '-frames:v', '1',
                           '-pix_fmt', 'gray', '-f', 'rawvideo', '-'], check=True, capture_output=True).stdout


def test_cut_content_checksum_mime_and_persistent_replay(worker, source, tmp_path):
    before = checksum(source)
    cmd = command()
    record = worker.cut(cmd)
    check_cut_result(cmd, record)
    path = worker.store.path(record.artifact_ref.object_key)
    assert record.duration_s == pytest.approx(2, abs=.05)
    original, cut = pixels(source, 2), pixels(path, 0)
    assert len(original) == len(cut) == 320*240
    assert sum(abs(a-b) for a,b in zip(original,cut))/len(cut) < 4
    assert checksum(source) == before
    manifest = worker.manifest(record)[0]
    assert manifest.sha256 == checksum(path)
    assert manifest.size_bytes == path.stat().st_size
    assert manifest.retention_class == 'review_hold'
    from javes_main.adapters.artifacts import FileArtifactStore
    data, mime = FileArtifactStore(worker.store.root).get(record.artifact_ref.object_key)
    assert mime == 'video/mp4' and hashlib.sha256(data).hexdigest() == manifest.sha256
    reopened = ClipStore(tmp_path/'state.sqlite', worker.store.root)
    try:
        replay = ClipWorker(reopened, None).cut(cmd)
        assert replay == record
        assert worker.manifest(replay) == [manifest]
    finally:
        reopened.close()
    with pytest.raises(ValueError, match='different payload'):
        worker.cut(cmd.model_copy(update={'t_to': cmd.t_to+timedelta(seconds=1)}))


def test_state_rules_replay_case_isolation_and_release(worker):
    original = worker.cut(command())
    cmd = ClipStateCommand(command_id='confirm', case_id='case-1', clip_id=original.clip_id,
                           action='CONFIRM', occurred_at=BASE)
    confirmed = worker.change_state(cmd)
    check_clip_state_result(cmd, confirmed, previous=original)
    assert worker.change_state(cmd) == confirmed
    assert worker.manifest(confirmed)[0].retention_class == 'case_14d'
    with pytest.raises(ValueError, match='different payload'):
        worker.change_state(cmd.model_copy(update={'action':'RELEASE'}))
    with pytest.raises(ValueError, match='reversed'):
        worker.change_state(cmd.model_copy(update={'command_id':'undo', 'action':'RELEASE'}))
    with pytest.raises(ValueError, match='unknown clip'):
        worker.change_state(cmd.model_copy(update={'command_id':'other', 'case_id':'case-2'}))
    second = worker.cut(command('cut-2', start=4, end=6))
    released = worker.change_state(cmd.model_copy(update={'command_id':'release', 'clip_id':second.clip_id, 'action':'RELEASE'}))
    assert released.state == 'RELEASED'
    assert worker.manifest(released)[0].retention_class == 'ephemeral'
    assert worker.store.path(released.artifact_ref.object_key).exists()  # Retention owns deletion.


def test_export_requires_confirmed_clips_and_preserves_order(worker):
    clips = [worker.cut(command('cut-'+str(i), start=i*2, end=i*2+2)) for i in range(2)]
    missing = ExportClipsCommand(export_request_id='early', case_id='case-1', ordered_clip_ids=[c.clip_id for c in clips])
    assert worker.export(missing).missing_clip_ids == missing.ordered_clip_ids
    for i, clip in enumerate(clips):
        worker.change_state(ClipStateCommand(command_id='confirm-'+str(i), case_id='case-1',
                            clip_id=clip.clip_id, action='CONFIRM', occurred_at=BASE))
    cmd = missing.model_copy(update={'export_request_id':'ready', 'ordered_clip_ids':list(reversed(missing.ordered_clip_ids))})
    exported = worker.export(cmd)
    check_export_result(cmd, exported)
    assert exported.status == 'READY'
    assert exported.duration_s == pytest.approx(4, abs=.1)
    path = worker.store.path(exported.artifact_ref.object_key)
    for offset, clip in [(0, clips[1]), (2, clips[0])]:
        a, b = pixels(path, offset), pixels(worker.store.path(clip.artifact_ref.object_key), 0)
        assert sum(abs(x-y) for x,y in zip(a,b))/len(a) < 5
    assert worker.export(cmd) == exported
    assert worker.manifest(exported)[0].kind == 'export'
    with pytest.raises(ValueError, match='different payload'):
        worker.export(cmd.model_copy(update={'ordered_clip_ids':missing.ordered_clip_ids}))
    cross = worker.export(cmd.model_copy(update={'export_request_id':'cross', 'case_id':'case-2'}))
    assert cross.status == 'FAILED'


@pytest.mark.parametrize('availability', ['UNAVAILABLE','PARTIAL'])
def test_missing_or_partial_source_never_reports_success(worker, source, availability):
    worker.archive = archive(source, availability=availability)
    result = worker.cut(command())
    assert result.status == 'UNAVAILABLE'
    assert result.artifact_ref is None and worker.manifest(result) == []


@pytest.mark.parametrize('change', [{'camera_id':'wrong'}, {'source_ref':{'archive_id':'wrong','uri':'file:///missing'}},
                                    {'requested_from':(BASE+timedelta(seconds=7)).isoformat()}])
def test_archive_correlation_checks(worker, source, change):
    worker.archive = archive(source, response_change=change)
    with pytest.raises(ValueError):
        worker.cut(command())


@pytest.mark.parametrize('case_id', ['../escape','a/b','.','..','a\\b',''])
def test_unsafe_case_ids_rejected(case_id):
    with pytest.raises(ValueError):
        case_prefix(case_id)


def test_invalid_duration_and_path_escape(worker, source):
    with pytest.raises(ValueError):
        worker.cut(command(start=2,end=2))
    for key in ['../x','/tmp/a','cases/../../x','a\\b']:
        with pytest.raises(ValueError):
            worker.store.path(key)
    with pytest.raises(MediaError):
        worker.media.cut(source, source.parent/'bad.mp4', 7, 3)
    assert not list(worker.store.root.glob('.clip-*'))


class Client:
    def __init__(self, task):
        self.task = task
        self.results, self.failures = [], []
        self.heartbeats = 0
        self.fail_callback_once = False

    def claim(self, **kwargs):
        assert kwargs['limit'] == 1
        return [self.task]

    def heartbeat(self, task, **kwargs):
        self.heartbeats += 1
        return WorkerHeartbeatResponse(task_id=task.task_id, lease_token='renewed-'+str(self.heartbeats),
                                       lease_expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))

    def complete(self, task, result):
        assert result.lease_token == 'renewed-'+str(self.heartbeats)
        self.results.append(result)
        if self.fail_callback_once:
            self.fail_callback_once = False
            raise httpx.ConnectError('lost response')

    def fail(self, task, **kwargs):
        self.failures.append(kwargs)


def task(cmd):
    return WorkerTaskEnvelope(task_id='task-1', case_id=cmd.case_id, component='clip', operation='CLIP_CUT',
                              attempt=1, lease_token='initial', lease_expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),
                              payload=cmd.model_dump(mode='json'))


def test_runner_callback_replay_renewed_token_and_payload_conflict(worker):
    client = Client(task(command()))
    runner = ClipTaskRunner(worker, client)
    client.fail_callback_once = True
    assert runner.run_once() == 1
    assert client.failures[0]['retryable']
    assert runner.run_once() == 1
    assert client.results[0].result == client.results[1].result
    assert client.results[0].artifacts == client.results[1].artifacts
    client.task = task(command('different'))
    runner.run_once()
    assert not client.failures[-1]['retryable']


def test_long_operation_renews_lease_before_callback(worker):
    import time
    original = worker.media.cut
    def slow_cut(*args):
        time.sleep(2)
        return original(*args)
    worker.media.cut = slow_cut
    client = Client(task(command()))
    ClipTaskRunner(worker, client).run_once(lease_seconds=5)
    assert client.heartbeats >= 2
    assert len(client.results) == 1 and not client.failures


def test_export_normalizes_mixed_resolutions(worker, source, tmp_path):
    first = worker.cut(command())
    small = tmp_path/'small.mp4'
    subprocess.run(['ffmpeg', '-v','error','-i',str(source),'-vf','scale=160:120','-r','10',str(small)],check=True)
    worker.archive = archive(small)
    second = worker.cut(command('small',start=4,end=6))
    for i,c in enumerate([first,second]):
        worker.change_state(ClipStateCommand(command_id='mixed-'+str(i),case_id='case-1',clip_id=c.clip_id,
                                             action='CONFIRM',occurred_at=BASE))
    result = worker.export(ExportClipsCommand(export_request_id='mixed',case_id='case-1',
                                             ordered_clip_ids=[first.clip_id,second.clip_id]))
    duration,width,height=worker.media.probe(worker.store.path(result.artifact_ref.object_key))
    assert (width,height)==(320,240)
    assert duration == pytest.approx(4,abs=.1)


def test_real_main_http_accepts_clip_cut_and_state(worker):
    from fastapi.testclient import TestClient
    from javes_main.api import create_app
    from javes_main.adapters.artifacts import FileArtifactStore
    from javes_main.domain import OutboxTopic
    from javes_workers import HttpWorkerClient
    from tests.test_main_infra import system, create_body
    app_system = system()
    app_system.manager.artifacts = FileArtifactStore(worker.store.root)
    app_system.manager.create_case(create_body(), idempotency_key='clip-http-create')
    cmd = command()
    app_system.manager._enqueue('case-1', OutboxTopic.CLIP_CUT, 'cut-test', {'command':cmd.model_dump(mode='json')})
    api = create_app(app_system.manager, auth=app_system.auth, worker_tokens={'clip':'clip-secret'}, worker_lease_secret='lease-secret')
    with TestClient(api) as http:
        client = HttpWorkerClient('http://testserver','clip','clip-secret','clip-test',client=http)
        runner = ClipTaskRunner(worker,client)
        assert runner.run_once() == 1
        case = app_system.repository.get('case-1')
        record = next(iter(case.clips.values()))
        assert record.status == 'READY'
        state = ClipStateCommand(command_id='confirm-api',case_id='case-1',clip_id=record.clip_id,action='CONFIRM',occurred_at=BASE)
        app_system.manager._enqueue('case-1',OutboxTopic.CLIP_STATE,'state-test',{'command':state.model_dump(mode='json')})
        assert runner.run_once() == 1
        assert app_system.repository.get('case-1').clips[record.clip_id].state == 'CONFIRMED'
