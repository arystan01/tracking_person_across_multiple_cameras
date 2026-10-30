import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from search_dashboard.adapters import DemoProvider, ImportProvider, JsonExport, CsvExport
from search_dashboard.service import CaseManager, ranked_candidates, route
from search_dashboard.storage import SQLiteStore

class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp.name) / "cases.db")
        self.manager = CaseManager(self.store)
        self.now = datetime(2026, 10, 9, 5, tzinfo=timezone.utc)
        self.case = self.manager.create("Test", "operator", "A", self.now.isoformat(),
            (self.now-timedelta(minutes=15)).isoformat(), (self.now+timedelta(minutes=30)).isoformat(), b"test-reference", "sensitive notes")
    def tearDown(self):
        self.temp.cleanup()
    def search(self):
        return self.manager.search(self.case.id, DemoProvider(), "operator")
    def test_demo_penalizes_impossible_high_similarity(self):
        case = self.search()
        ranked = ranked_candidates(case)
        self.assertEqual(ranked[-1]['candidate'].id, 'demo-distractor')
        self.assertFalse(ranked[-1]['feasible'])
        self.assertEqual(len(case.candidates), 5)
    def test_review_route_and_rejection(self):
        self.search()
        self.manager.review(self.case.id, "demo-B", "CONFIRMED", "operator")
        case = self.store.get(self.case.id)
        self.assertEqual(route(case)[-1]['camera_id'], 'B')
        self.manager.review(case.id, 'demo-B', 'REJECTED', 'operator')
        case = self.store.get(case.id)
        self.assertEqual(len(route(case)), 1)
        self.assertEqual(next(c for c in case.candidates if c.id == 'demo-B').clip_state, 'RELEASED')
        self.manager.review(case.id, 'demo-B', 'UNREVIEWED', 'operator')
        self.assertEqual(next(c for c in self.store.get(case.id).candidates if c.id == 'demo-B').clip_state, 'HOLD')
    def test_closure_cleanup_expiry_and_lock(self):
        self.search()
        self.manager.review(self.case.id, 'demo-B', 'CONFIRMED', 'operator')
        self.manager.transition(self.case.id, 'CLOSED', 'operator', 1, self.now)
        closed = self.store.get(self.case.id)
        self.assertIsNone(closed.reference_b64)
        self.assertEqual(closed.notes, '')
        self.assertEqual(len(closed.candidates), 1)
        self.assertEqual(closed.candidates[0].metadata, {})
        with self.assertRaises(ValueError): self.manager.review(closed.id, 'demo-B', 'REJECTED', 'operator')
        with self.assertRaises(ValueError): self.manager.search(closed.id, DemoProvider(), 'operator')
        self.assertEqual(self.manager.cleanup(self.now), 0)
        self.assertEqual(self.manager.cleanup(self.now+timedelta(hours=1)), 1)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.audit(closed.id)[0]['action'], 'DELETED')
    def test_atomic_bad_import(self):
        rows = [{'id':'ok','camera_id':'B','observed_at':self.now.isoformat(),'appearance_score':0.8},
                {'id':'bad','camera_id':'Q','observed_at':self.now.isoformat(),'appearance_score':0.9}]
        with self.assertRaisesRegex(ValueError, 'Row 2'):
            self.manager.search(self.case.id, ImportProvider(json.dumps(rows).encode(),'json'),'operator')
        self.assertEqual(self.store.get(self.case.id).round, 0)
        self.assertEqual(self.store.get(self.case.id).candidates, [])
    def test_custom_csv_mapping_and_exports(self):
        data = f'track,cam,time,score,extra\ntrack1,B,{self.now.isoformat()},0.9,hello\n'.encode()
        mapping = dict(id='track',camera_id='cam',observed_at='time',appearance_score='score')
        case = self.manager.search(self.case.id, ImportProvider(data, 'csv', mapping), 'operator')
        self.assertEqual(case.candidates[0].metadata, {'extra':'hello'})
        report = json.loads(JsonExport().encode(case,route(case)))
        self.assertEqual(report['schema_version'],'1.0')
        self.assertNotIn('reference_b64',report['case'])
        self.assertNotIn('metadata',report['case']['candidates'][0])
        self.assertIn(b'track1,B',CsvExport().encode(case,route(case)))
    def test_rejects_invalid_scores_duplicates_timezones_and_windows(self):
        base = dict(id='a',camera_id='A',observed_at=self.now.isoformat(),appearance_score=.9)
        for updates in [dict(appearance_score=float('nan')), dict(appearance_score=1.1),dict(observed_at='2026-10-09T14:00:00'),dict(observed_at='2020-01-01T00:00:00Z')]:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                ImportProvider(json.dumps([base|updates]).encode(),'json').search(self.case)
        with self.assertRaises(ValueError): ImportProvider(json.dumps([base,base]).encode(),'json').search(self.case)
    def test_coverage_requires_confirmation_and_supports_reentry(self):
        with self.assertRaises(ValueError): self.manager.transition(self.case.id,'IN_COVERAGE','operator')
        self.search()
        self.manager.review(self.case.id,'demo-B','CONFIRMED','operator')
        for status in ['IN_COVERAGE','OUT_OF_COVERAGE','IN_COVERAGE']:
            self.manager.transition(self.case.id,status,'operator')
            self.assertEqual(self.store.get(self.case.id).status,status)
    def test_undo_last_confirmation_clears_in_coverage(self):
        self.search()
        self.manager.review(self.case.id, "demo-B", "CONFIRMED", "operator")
        self.manager.transition(self.case.id, "IN_COVERAGE", "operator")
        self.manager.review(self.case.id, "demo-B", "UNREVIEWED", "operator")
        self.assertEqual(self.store.get(self.case.id).status, "SEARCHING")

    def test_route_flags_conflicting_confirmations(self):
        self.search()
        self.manager.review(self.case.id,'demo-distractor','CONFIRMED','operator')
        self.assertTrue(route(self.store.get(self.case.id))[-1]['travel_conflict'])
    def test_persistence_and_case_isolation(self):
        self.search()
        second = self.manager.create('Other','other','B',self.case.last_known_at,self.case.start,self.case.end)
        reopened = SQLiteStore(self.store.path)
        self.assertEqual(len(reopened.get(self.case.id).candidates),5)
        self.assertEqual(reopened.get(second.id).candidates,[])
        self.store.delete(second.id,'other')
        self.assertEqual(len(self.store.list()),1)
    def test_search_batch_cannot_overwrite_decisions(self):
        self.search()
        with self.assertRaises(ValueError): self.search()

    def test_recalculation_and_saved_lock(self):
        self.search()
        with self.assertRaises(ValueError): self.manager.save_result(self.case.id, 'operator')
        self.manager.review(self.case.id, 'demo-B', 'RECALCULATE', 'operator')
        self.manager.recalculate(self.case.id, 'operator')
        updated=self.store.get(self.case.id)
        self.assertEqual(next(c for c in updated.candidates if c.id=='demo-B').decision,'UNREVIEWED')
        for c in updated.candidates:
            self.manager.review(updated.id,c.id,'CONFIRMED' if c.id=='demo-B' else 'REJECTED','operator')
        self.manager.save_result(updated.id,'operator')
        self.assertTrue(self.store.get(updated.id).saved)
        for fn in [lambda:self.manager.review(updated.id,'demo-B','REJECTED','operator'),
                   lambda:self.manager.transition(updated.id,'OUT_OF_COVERAGE','operator'),
                   lambda:self.manager.recalculate(updated.id,'operator')]:
            with self.assertRaises(ValueError): fn()

if __name__ == '__main__': unittest.main()
