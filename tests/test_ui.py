import os
import tempfile
import unittest
from pathlib import Path
from streamlit.testing.v1 import AppTest
from search_dashboard.storage import SQLiteStore
from search_dashboard.service import CaseManager

class InterfaceTest(unittest.TestCase):
    def test_login_lookup_review_save_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            old=os.environ.get('TRACE_DB_PATH'); os.environ['TRACE_DB_PATH']=str(Path(temp)/'ui.db')
            try:
                app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run()
                self.assertFalse(app.exception)
                app.text_input[0].set_value('operator');app.text_input[1].set_value('wrong')
                app.button[0].click().run(); self.assertTrue(app.error)
                app.text_input[1].set_value('trace-demo');app.button[0].click().run()
                self.assertFalse(app.exception)
                next(b for b in app.button if b.label=='Try a complete demo case').click().run()
                next(b for b in app.button if b.label=='Start search').click().run()
                self.assertFalse(app.exception);self.assertEqual(app.metric[0].value,'5')
                next(b for b in app.button if b.key=='review_demo-B').click().run()
                self.assertFalse(app.exception)
                next(b for b in app.button if b.label=='Apply decision').click().run()
                self.assertFalse(app.exception);self.assertEqual(app.metric[2].value,'1')
                store=SQLiteStore(os.environ['TRACE_DB_PATH']); manager=CaseManager(store);case=store.list()[0]
                for c in case.candidates:
                    if c.decision=='UNREVIEWED':manager.review(case.id,c.id,'REJECTED','operator')
                app.run()
                next(b for b in app.button if b.label=='Save case').click().run()
                self.assertFalse(app.exception);self.assertTrue(store.get(case.id).saved)
                self.assertFalse(any(b.label=='Review clip' for b in app.button))
                app.sidebar.radio[0].set_value('Find case').run()
                app.text_input[0].set_value('missing');next(b for b in app.button if b.label=='Find case').click().run()
                self.assertTrue(app.error)
                app.text_input[0].set_value(case.id);next(b for b in app.button if b.label=='Find case').click().run()
                next(b for b in app.button if b.label=='Open case').click().run()
                self.assertFalse(app.exception)
                next(c for c in app.checkbox if c.label.startswith('Permanently')).check().run()
                next(b for b in app.button if b.label=='Delete case').click().run()
                self.assertEqual(store.list(),[]);self.assertFalse(app.exception)
                app.sidebar.radio[0].set_value('New case').run();self.assertFalse(app.exception)
            finally:
                if old is None:os.environ.pop('TRACE_DB_PATH',None)
                else:os.environ['TRACE_DB_PATH']=old
