"""Local persistent store. Replace behind this interface for a REST backend."""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from .domain import Case

class SQLiteStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
                    at TEXT NOT NULL, operator TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        # Avoid keeping deleted image data on reusable SQLite pages.
        db.execute("PRAGMA secure_delete=ON")
        return db

    def save(self, case, operator, action, detail=""):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cases VALUES (?, ?)", (case.id, json.dumps(case.to_dict())))
            db.execute("INSERT INTO audit(case_id,at,operator,action,detail) VALUES (?,?,?,?,?)",
                       (case.id, datetime.now(timezone.utc).isoformat(), operator, action, detail))

    def get(self, case_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("Case no longer exists.")
        return Case.from_dict(json.loads(row[0]))

    def list(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM cases ORDER BY rowid DESC").fetchall()
        return [Case.from_dict(json.loads(row[0])) for row in rows]

    def audit(self, case_id):
        with self.connect() as db:
            rows = db.execute("SELECT at,operator,action,detail FROM audit WHERE case_id=? ORDER BY seq DESC", (case_id,)).fetchall()
        return [dict(zip(["at", "operator", "action", "detail"], row)) for row in rows]

    def delete(self, case_id, operator):
        with self.connect() as db:
            db.execute("DELETE FROM cases WHERE id=?", (case_id,))
            db.execute("DELETE FROM audit WHERE case_id=?", (case_id,))
            db.execute("INSERT INTO audit(case_id,at,operator,action,detail) VALUES (?,?,?,?,?)",
                       (case_id, datetime.now(timezone.utc).isoformat(), operator, "DELETED", "Case payload and prior audit removed; deletion receipt retained."))
