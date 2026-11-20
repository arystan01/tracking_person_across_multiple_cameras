import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from search_dashboard.backend import DemoControlAPI
from search_dashboard.storage import SQLiteStore
from search_dashboard.adapters import CaseViewAdapter, JsonExport
from search_dashboard.domain import CaseView
from search_dashboard.service import status_label, can_review, updated_snapshot

class ControlContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp.name) / 'test.db')
        self.start = datetime(2026, 11, 2, 5, tzinfo=timezone.utc)
        self.now = self.start
        self.api = DemoControlAPI(self.store, clock=lambda:self.now)
        self.case = self.api.create_case('Test', 'operator', 'A', (self.now-timedelta(minutes=15)).isoformat(), b'image')
    def tearDown(self): self.temp.cleanup()
    def advance(self, seconds):
        self.now = self.start + timedelta(seconds=seconds)
        return self.api.get_case_view(self.case.id)
    def test_server_sets_window_and_created_at(self):
        self.assertEqual(self.case.window['start'], self.case.last_known['time'])
        self.assertEqual(self.case.window['end'],self.case.created_at)
        self.assertEqual(self.case.created_at,self.now.isoformat())
    def test_future_sighting_invalid(self):
        with self.assertRaises(ValueError): self.api.create_case('Test','operator','A',(self.now+timedelta(minutes=1)).isoformat())
    def test_modes_switch_automatically(self):
        expected=[(0,'PREPARING','CATCHING_UP'),(3,'SEARCHING','CATCHING_UP'),(31,'SEARCHING','LIVE_TRACKING'),(61,'SEARCHING','WAITING'),(121,'SEARCHING','WAITING')]
        for sec,life,mode in expected:
            c=self.advance(sec)
            self.assertEqual((c.lifecycle,c.mode),(life,mode))
        self.assertFalse(c.done);self.assertIsNone(c.stop_reason)
    def test_long_gap_review_remains_available_after_a_day(self):
        case=self.advance(86400)
        self.assertTrue(can_review(case))
        self.assertFalse(case.done)
        candidate=next(c for c in case.candidates if c.observation_type=='LONG_GAP')
        self.api.submit_decision(case.id,candidate.id,'CONFIRM','operator')
        updated=self.advance(86404)
        self.assertFalse(updated.done)
        self.assertEqual(next(c for c in updated.candidates if c.id==candidate.id).decision,'CONFIRMED')
        self.assertTrue(any(p.get('candidate_id')==candidate.id for p in updated.route))

    def test_incremental_candidates_and_live_beyond_recording_window(self):
        self.assertEqual(len(self.advance(3).candidates),1)
        self.assertEqual(len(self.advance(9).candidates),2)
        case=self.advance(41)
        self.assertEqual(len(case.candidates),4)
        self.assertGreater(case.candidates[-1].observed_at,case.window['end'])
    def test_pending_search_continues_and_route_updates_later(self):
        c=self.advance(7)
        ack=self.api.submit_decision(c.id,'demo-A','CONFIRM','operator')
        self.assertEqual(ack.candidates[0].review_state,'PENDING')
        self.assertEqual(ack.route_revision,0)
        interim=self.advance(9)
        self.assertEqual(len(interim.candidates),2)
        self.assertGreater(interim.revision,ack.revision)
        self.assertEqual(interim.candidates[0].review_state,'PENDING')
        result=self.advance(11)
        self.assertEqual(result.candidates[0].decision,'CONFIRMED')
        self.assertEqual(result.candidates[0].review_state,'APPLIED')
        self.assertGreater(result.route_revision,ack.route_revision)
        self.assertEqual(result.candidates[0].clip_state,'CONFIRMED')
    def test_long_gap_excluded_until_confirmation(self):
        c=self.advance(15)
        gap=next(x for x in c.candidates if x.observation_type=='LONG_GAP')
        self.assertFalse(any(p.get('candidate_id')==gap.id for p in c.route))
        self.api.submit_decision(c.id,gap.id,'CONFIRM','operator')
        self.assertFalse(any(p.get('candidate_id')==gap.id for p in self.advance(16).route))
        self.assertTrue(any(p.get('candidate_id')==gap.id for p in self.advance(19).route))
    def test_reject_releases_clip(self):
        self.advance(3)
        self.api.submit_decision(self.case.id,'demo-A','REJECT','operator')
        c=self.advance(7)
        self.assertEqual(c.candidates[0].clip_state,'RELEASED')
        self.assertFalse(any(p.get('candidate_id')=='demo-A' for p in c.route))
    def test_conflicting_decisions_rejected_and_retry_idempotent(self):
        self.advance(3)
        ack=self.api.submit_decision(self.case.id,'demo-A','CONFIRM','operator')
        retry=self.api.submit_decision(self.case.id,'demo-A','CONFIRM','operator')
        self.assertEqual(ack.revision,retry.revision)
        with self.assertRaises(ValueError): self.api.submit_decision(self.case.id,'demo-A','REJECT','operator')
        self.advance(7)
        with self.assertRaises(ValueError): self.api.submit_decision(self.case.id,'demo-A','REJECT','operator')
        with self.assertRaises(ValueError): self.api.submit_decision(self.case.id,'demo-A','RECALCULATE','operator')
    def test_concurrent_contradicting_decisions(self):
        self.advance(3)
        def send(action):
            try: self.api.submit_decision(self.case.id,'demo-A',action,'operator');return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(send,['CONFIRM','REJECT']))
        self.assertEqual(sum(outcomes),1)
    def test_partial_errors_are_not_case_failure(self):
        c=self.advance(46)
        self.assertTrue(c.errors);self.assertTrue(c.unobserved_windows)
        self.assertEqual(c.lifecycle,'SEARCHING');self.assertFalse(c.done)
    def test_waiting_is_not_done(self):
        c=self.advance(61)
        self.assertFalse(c.done);self.assertTrue(can_review(c))
        self.assertEqual(status_label(c),'Waiting for sightings')
    def test_operator_close_is_not_blocked_by_unreviewed_candidates(self):
        self.advance(15)
        c=self.api.close_case(self.case.id,'operator')
        self.assertTrue(c.done);self.assertEqual(c.stop_reason,'OPERATOR_CLOSED')
        self.assertFalse(can_review(c))
        with self.assertRaises(ValueError):self.api.submit_decision(c.id,'demo-A','CONFIRM','operator')
    def test_pending_survives_client_recreation(self):
        self.advance(3)
        self.api.submit_decision(self.case.id,'demo-A','CONFIRM','operator')
        self.api=DemoControlAPI(SQLiteStore(self.store.path),clock=lambda:self.now)
        self.assertEqual(self.api.get_case_view(self.case.id).candidates[0].review_state,'PENDING')
        self.assertEqual(self.advance(7).candidates[0].review_state,'APPLIED')
    def test_terminal_modes_and_stop_reasons_decode(self):
        for reason in ['ALL_ENDED','MAX_GAP','BUDGET_SPENT','OPERATOR_CLOSED']:
            d=self.case.to_dict();d.update(lifecycle='CLOSED',mode='PAST_SUMMARY',done=True,stop_reason=reason)
            c=CaseViewAdapter().decode(d)
            self.assertEqual(status_label(c),'Search ended')
        d.update(lifecycle='FAILED',done=False,stop_reason=None)
        self.assertEqual(status_label(CaseViewAdapter().decode(d)),'Search failed')
    def test_inconsistent_done_rejected(self):
        d=self.case.to_dict();d['done']=True
        with self.assertRaises(ValueError):CaseViewAdapter().decode(d)
    def test_legacy_data_preserved_read_only(self):
        c=self.case
        legacy=dict(id='old',title='Old',operator='operator',camera_id='A',last_known_at=c.last_known['time'],start=c.window['start'],end=c.window['end'],created_at=c.created_at,candidates=[],timeout_seconds=300,saved=True,reference_b64='aW1hZ2U=')
        loaded=CaseView.from_dict(legacy)
        self.assertTrue(loaded.done);self.assertEqual(loaded.source,'legacy')
        self.assertEqual(loaded.reference_b64,'aW1hZ2U=')
        self.assertNotIn('timeout_seconds',loaded.to_dict())
    def test_export_excludes_private_demo_state(self):
        self.advance(3)
        c=self.api.submit_decision(self.case.id,'demo-A','CONFIRM','operator')
        report=json.loads(JsonExport().encode(c))['case_view']
        self.assertNotIn('reference_b64',report);self.assertNotIn('simulation',report)
        self.assertNotIn('metadata',report['candidates'][0])
    def test_snapshot_refresh_and_out_of_order_responses(self):
        old=self.case
        newer=CaseView.from_dict(old.to_dict());newer.route_revision+=1
        self.assertIs(updated_snapshot(old,newer),newer)
        self.assertIs(updated_snapshot(newer,old),newer)
        unchanged=CaseView.from_dict(newer.to_dict())
        self.assertIs(updated_snapshot(newer,unchanged),newer)

    def test_delete_requires_completed_case(self):
        with self.assertRaises(ValueError):self.api.delete_case(self.case.id,'operator')
        self.api.close_case(self.case.id,'operator')
        self.api.delete_case(self.case.id,'operator')
        self.assertEqual(self.store.list(),[])
        self.assertEqual(self.store.audit(self.case.id)[0]['action'],'DELETED')

    def test_unchanged_poll_does_not_increment_revision(self):
        c=self.advance(3)
        self.assertEqual(self.api.get_case_view(c.id).revision,c.revision)

if __name__=='__main__':unittest.main()
