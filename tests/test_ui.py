import os
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from streamlit.testing.v1 import AppTest
from search_dashboard.storage import SQLiteStore

class InterfaceTest(unittest.TestCase):
    def test_new_contract_flow(self):
        with tempfile.TemporaryDirectory() as temp:
            old=os.environ.get('TRACE_DB_PATH');os.environ['TRACE_DB_PATH']=str(Path(temp)/'ui.db')
            try:
                app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
                app.text_input[0].set_value('operator');app.text_input[1].set_value('wrong')
                app.button[0].click().run();self.assertTrue(app.error)
                app.text_input[1].set_value('trace-demo');app.button[0].click().run()
                app.sidebar.radio[0].set_value('New case').run()
                self.assertFalse(app.exception)
                self.assertEqual([t.label for t in app.time_input],['Last-known time'])
                self.assertFalse(any('following' in c.label.lower() for c in app.checkbox))
                app.sidebar.radio[0].set_value('Find case').run()
                next(b for b in app.button if b.label=='Create demo case').click().run()
                self.assertFalse(app.exception)
                store=SQLiteStore(os.environ['TRACE_DB_PATH']);c=store.list()[0]
                c.created_at=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
                store.save(c,'test','ADVANCE_DEMO_CLOCK')
                app.run();self.assertFalse(app.exception)
                self.assertEqual(app.metric[0].value,'3')
                self.assertFalse(any(e.label=='Backend status' for e in app.expander))
                self.assertEqual(len(app.json),0)
                self.assertTrue(any('**Created:**' in m.value and 'KST' in m.value for m in app.markdown))
                self.assertFalse(any('recalculation' in b.label.lower() or b.label in ['Start search','Save case','Mark outside coverage'] for b in app.button))
                next(b for b in app.button if b.key==f'CONFIRM_{c.id}_demo-B').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(app.metric[2].value,'1')
                pending=store.get(c.id)
                target=next(x for x in pending.candidates if x.id=='demo-B')
                target.metadata['apply_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
                store.save(pending,'test','ADVANCE_DEMO_JOB')
                app.run();self.assertFalse(app.exception)
                self.assertEqual(app.metric[2].value,'0');self.assertEqual(app.metric[3].value,'1')
                self.assertFalse(any(b.key==f'REJECT_{c.id}_demo-B' for b in app.button))
                next(x for x in app.checkbox if x.label=='Close this search').check().run()
                next(b for b in app.button if b.label=='Close search').click().run()
                self.assertFalse(app.exception)
                self.assertTrue(store.get(c.id).done)
                self.assertTrue(any('Closed by operator' in m.value for m in app.markdown))
            finally:
                if old is None:os.environ.pop('TRACE_DB_PATH',None)
                else:os.environ['TRACE_DB_PATH']=old
