"""SQLite persistence: enrollments, contacts, message log, idempotency, audit trail.

SQLite keeps the reference deployment dependency-free. Swap for Postgres by reimplementing
this class (the SQL is deliberately plain); nothing else touches the database.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .domain import ContactRecord, FPState

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT, email TEXT, fp_id TEXT,
  issue TEXT DEFAULT 'HVAC issue', dnd INTEGER DEFAULT 0, sms_consent INTEGER DEFAULT 0,
  fp_state TEXT DEFAULT 'lead', tags TEXT DEFAULT '[]', updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_contacts_fp ON contacts(fp_id);
CREATE INDEX IF NOT EXISTS ix_contacts_phone ON contacts(phone);

CREATE TABLE IF NOT EXISTS enrollments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id TEXT NOT NULL, sequence TEXT NOT NULL,
  step INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
  ctx TEXT DEFAULT '{}', started_at TEXT NOT NULL, last_step_at TEXT, ended_at TEXT, end_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_enroll_active
  ON enrollments(contact_id, sequence) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT UNIQUE, contact_id TEXT NOT NULL,
  direction TEXT NOT NULL, channel TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_contact ON messages(contact_id, at);

CREATE TABLE IF NOT EXISTS seen_events (key TEXT PRIMARY KEY, at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, contact_id TEXT, details TEXT
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._db.executescript(SCHEMA)

    def _x(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    def close(self) -> None:
        self._db.close()

    def ping(self) -> bool:
        return self._x("SELECT 1").fetchone()[0] == 1

    # ---------------------------------------------------------------- contacts
    def save_contact(self, c: ContactRecord, now: datetime) -> None:
        self._x(
            """INSERT INTO contacts(id,name,phone,email,fp_id,issue,dnd,sms_consent,fp_state,tags,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, phone=excluded.phone,
                 email=excluded.email, fp_id=excluded.fp_id, issue=excluded.issue, dnd=excluded.dnd,
                 sms_consent=excluded.sms_consent, fp_state=excluded.fp_state, tags=excluded.tags,
                 updated_at=excluded.updated_at""",
            (c.id, c.name, c.phone, c.email, c.fp_id, c.issue, int(c.dnd), int(c.sms_consent),
             c.fp_state.value, json.dumps(sorted(c.tags)), _iso(now)),
        )

    @staticmethod
    def _row_to_contact(r: sqlite3.Row) -> ContactRecord:
        return ContactRecord(
            id=r["id"], name=r["name"], phone=r["phone"], email=r["email"], fp_id=r["fp_id"],
            issue=r["issue"], dnd=bool(r["dnd"]), sms_consent=bool(r["sms_consent"]),
            fp_state=FPState(r["fp_state"]), tags=set(json.loads(r["tags"])),
        )

    def get_contact(self, contact_id: str) -> ContactRecord | None:
        r = self._x("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        return self._row_to_contact(r) if r else None

    def contact_by_fp(self, fp_id: str) -> ContactRecord | None:
        r = self._x("SELECT * FROM contacts WHERE fp_id=?", (fp_id,)).fetchone()
        return self._row_to_contact(r) if r else None

    def contact_by_phone(self, phone: str) -> ContactRecord | None:
        r = self._x("SELECT * FROM contacts WHERE phone=?", (phone,)).fetchone()
        return self._row_to_contact(r) if r else None

    def all_contacts(self) -> list[ContactRecord]:
        return [self._row_to_contact(r) for r in self._x("SELECT * FROM contacts")]

    # ------------------------------------------------------------- enrollments
    def enroll(self, contact_id: str, sequence: str, ctx: dict, now: datetime) -> int | None:
        """Create an active enrollment. Returns None if one is already active (idempotent)."""
        try:
            cur = self._x(
                "INSERT INTO enrollments(contact_id,sequence,ctx,started_at) VALUES(?,?,?,?)",
                (contact_id, sequence, json.dumps(ctx), _iso(now)),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def active_enrollments(self) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM enrollments WHERE status='active' ORDER BY id").fetchall()

    def is_enrolled(self, contact_id: str, sequence: str | None = None) -> bool:
        if sequence:
            q = "SELECT 1 FROM enrollments WHERE contact_id=? AND status='active' AND sequence=?"
            a = (contact_id, sequence)
        else:
            q, a = "SELECT 1 FROM enrollments WHERE contact_id=? AND status='active'", (contact_id,)
        return self._x(q, a).fetchone() is not None

    def advance(self, enrollment_id: int, now: datetime) -> None:
        self._x("UPDATE enrollments SET step=step+1, attempts=0, last_step_at=? WHERE id=?",
                (_iso(now), enrollment_id))

    def bump_attempt(self, enrollment_id: int) -> int:
        self._x("UPDATE enrollments SET attempts=attempts+1 WHERE id=?", (enrollment_id,))
        return self._x("SELECT attempts FROM enrollments WHERE id=?", (enrollment_id,)).fetchone()[0]

    def end_enrollment(self, enrollment_id: int, status: str, reason: str, now: datetime) -> None:
        self._x("UPDATE enrollments SET status=?, end_reason=?, ended_at=? WHERE id=?",
                (status, reason, _iso(now), enrollment_id))

    def cancel_enrollments(self, contact_id: str, reason: str, now: datetime,
                           sequences: Iterable[str] | None = None) -> int:
        rows = self._x("SELECT id, sequence FROM enrollments WHERE contact_id=? AND status='active'",
                       (contact_id,)).fetchall()
        wanted = set(sequences) if sequences is not None else None
        n = 0
        for r in rows:
            if wanted is None or r["sequence"] in wanted:
                self.end_enrollment(r["id"], "cancelled", reason, now)
                n += 1
        return n

    # ---------------------------------------------------------------- messages
    def record_message(self, idem_key: str | None, contact_id: str, direction: str, channel: str,
                       kind: str, text: str, now: datetime) -> bool:
        """False if idem_key was already recorded (duplicate send suppressed)."""
        try:
            self._x("INSERT INTO messages(idem_key,contact_id,direction,channel,kind,text,at) VALUES(?,?,?,?,?,?,?)",
                    (idem_key, contact_id, direction, channel, kind, text, _iso(now)))
            return True
        except sqlite3.IntegrityError:
            return False

    def marketing_sent_since(self, contact_id: str, since: datetime) -> int:
        return self._x(
            "SELECT COUNT(*) FROM messages WHERE contact_id=? AND direction='out' AND kind='marketing' AND at>=?",
            (contact_id, _iso(since))).fetchone()[0]

    def messages_for(self, contact_id: str) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM messages WHERE contact_id=? ORDER BY id", (contact_id,)).fetchall()

    def all_messages(self) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM messages ORDER BY id").fetchall()

    # ------------------------------------------------------ idempotency / audit
    def first_time(self, key: str, now: datetime) -> bool:
        """True exactly once per key (webhook de-duplication, campaign run guards)."""
        try:
            self._x("INSERT INTO seen_events(key, at) VALUES(?,?)", (key, _iso(now)))
            return True
        except sqlite3.IntegrityError:
            return False

    def audit(self, now: datetime, actor: str, action: str, contact_id: str | None = None,
              **details: object) -> None:
        self._x("INSERT INTO audit(at,actor,action,contact_id,details) VALUES(?,?,?,?,?)",
                (_iso(now), actor, action, contact_id, json.dumps(details, default=str)))

    def audit_rows(self, action: str | None = None) -> list[sqlite3.Row]:
        if action:
            return self._x("SELECT * FROM audit WHERE action=? ORDER BY id", (action,)).fetchall()
        return self._x("SELECT * FROM audit ORDER BY id").fetchall()
