"""Persistent, notification-independent lesson history for the Mini App.

The first accepted snapshot starts observation; it is not evidence that a lesson
was added to the university website at that moment. Source IDs are hints only:
content matches survive the ID churn seen in the DSTU API. Ambiguous matches are
kept as separate lessons rather than attributing another lesson's history.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import bot as core


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _signature(item):
    return _json([item.get(field) for field in core.FIELDS])


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.isoformat()


def _snapshot(value):
    if not isinstance(value, dict):
        raise ValueError("snapshot must be a dictionary")
    result = {}
    for key, raw in value.items():
        # Keep optional API metadata, while applying the bot's field defaults.
        item = {**raw, **core.normalize_lesson(raw)}
        if str(key) != item["код"]:
            raise ValueError("snapshot key does not match lesson code")
        datetime.fromisoformat(item["датаНачала"])
        datetime.fromisoformat(item["датаОкончания"])
        result[item["код"]] = json.loads(_json(item))
    return result


class HistoryStore:
    """A small SQLite journal; every operation owns its database connection.

    ``observe`` is a single immediate transaction, including initial migration
    from an optional ``baseline``. Repeated observations are idempotent, and an
    observation older than the last accepted timestamp is ignored. ``schedule``
    uses inclusive ISO-date bounds. ``events`` and ``history`` are newest first.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schedule_groups (
                    group_id INTEGER PRIMARY KEY,
                    group_name TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_lessons (
                    id TEXT PRIMARY KEY,
                    group_id INTEGER NOT NULL REFERENCES schedule_groups(group_id),
                    source_code TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    lesson_date TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS schedule_lesson_dates
                    ON schedule_lessons(group_id, lesson_date, deleted);
                CREATE TABLE IF NOT EXISTS schedule_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id TEXT NOT NULL REFERENCES schedule_lessons(id),
                    group_id INTEGER NOT NULL REFERENCES schedule_groups(group_id),
                    detected_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    types TEXT NOT NULL,
                    before_lesson TEXT,
                    after_lesson TEXT
                );
                CREATE INDEX IF NOT EXISTS schedule_event_lessons
                    ON schedule_events(lesson_id, id);
                CREATE INDEX IF NOT EXISTS schedule_event_groups
                    ON schedule_events(group_id, id);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _event(db, uid, group_id, observed_at, kind, before, after):
        types = sorted(core.change_types(before, after)) if kind != "baseline" else []
        if kind == "restored":
            types = sorted(set(types) | {"added"})
        db.execute("""INSERT INTO schedule_events
            (lesson_id,group_id,detected_at,kind,types,before_lesson,after_lesson)
            VALUES (?,?,?,?,?,?,?)""", (uid, group_id, observed_at, kind, _json(types),
                                       _json(before) if before is not None else None,
                                       _json(after) if after is not None else None))

    @classmethod
    def _insert(cls, db, group_id, item, observed_at, kind):
        uid = uuid4().hex
        db.execute("""INSERT INTO schedule_lessons
            (id,group_id,source_code,lesson,lesson_date,deleted,observed_at)
            VALUES (?,?,?,?,?,0,?)""", (uid, group_id, item["код"], _json(item),
                                        core.lesson_date(item), observed_at))
        cls._event(db, uid, group_id, observed_at, kind, None, item)
        return uid

    @staticmethod
    def _update(db, uid, item, observed_at, deleted=False):
        db.execute("""UPDATE schedule_lessons SET source_code=?,lesson=?,lesson_date=?,
            deleted=?,observed_at=? WHERE id=?""", (item["код"], _json(item),
                                                    core.lesson_date(item), int(deleted),
                                                    observed_at, uid))

    @staticmethod
    def _restoration(item, tombstones):
        """Choose a previously deleted lesson only when it is unambiguous."""
        exact = [row for row in tombstones if _signature(row["item"]) == _signature(item)]
        same_code = [row for row in exact if row["source_code"] == item["код"]]
        if len(same_code) == 1:
            return same_code[0]
        if len(exact) == 1:
            return exact[0]
        # A removed lesson may return with an edited room or time. Restrict the
        # fallback to the same day/course/teacher/subgroup, and require one match.
        anchors = ("дисциплина", "преподаватель", "номерПодгруппы")
        candidates = [row for row in tombstones
                      if core.lesson_date(row["item"]) == core.lesson_date(item)
                      and all(row["item"].get(field) == item.get(field) for field in anchors)]
        by_code = [row for row in candidates if row["source_code"] == item["код"]]
        if len(by_code) == 1:
            return by_code[0]
        return candidates[0] if len(candidates) == 1 else None

    def observe(self, group_id, group_name, snapshot, observed_at, baseline=None):
        """Journal one validated, accepted upstream snapshot, atomically.

        Supply ``baseline`` only to seed an existing installation's old snapshot
        during its first observation. All timestamps still mean observation time.
        Returns nothing; reads expose the committed state.
        """
        group_id = int(group_id)
        group_name = str(group_name)
        observed_at = _timestamp(observed_at)
        current = _snapshot(snapshot)
        initial = _snapshot(baseline) if baseline is not None else current
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            group = db.execute("SELECT * FROM schedule_groups WHERE group_id=?",
                               (group_id,)).fetchone()
            if group and datetime.fromisoformat(observed_at) < datetime.fromisoformat(group["observed_at"]):
                return
            if not group:
                db.execute("INSERT INTO schedule_groups VALUES (?,?,?)",
                           (group_id, group_name, observed_at))
                for item in initial.values():
                    self._insert(db, group_id, item, observed_at, "baseline")
            else:
                db.execute("UPDATE schedule_groups SET group_name=?,observed_at=? WHERE group_id=?",
                           (group_name, observed_at, group_id))

            rows = [dict(row) for row in db.execute(
                "SELECT * FROM schedule_lessons WHERE group_id=?", (group_id,))]
            for row in rows:
                row["item"] = json.loads(row["lesson"])
            active = {row["source_code"]: row for row in rows if not row["deleted"]}
            # Only tombstones that existed before this reconciliation are eligible
            # for restoration. An ambiguous removal/addition in this same poll is
            # not evidence that a lesson was restored.
            tombstones = [row for row in rows if row["deleted"]]
            before_left = {code: row["item"] for code, row in active.items()}
            after_left = dict(current)
            # Reserve exact source+content matches before another indistinguishable
            # old row can consume them during the content-only reconciliation.
            for code in sorted(before_left.keys() & after_left.keys()):
                if _signature(before_left[code]) == _signature(after_left[code]):
                    self._update(db, active[code]["id"], after_left.pop(code), observed_at)
                    before_left.pop(code)
            content = defaultdict(list)
            for code, item in sorted(after_left.items()):
                content[_signature(item)].append(code)
            for code, item in sorted(before_left.copy().items()):
                matches = content.get(_signature(item))
                if not matches:
                    continue
                # Prefer an unchanged source ID for identical parallel lessons.
                new_code = code if code in matches else matches[0]
                matches.remove(new_code)
                self._update(db, active[code]["id"], after_left.pop(new_code), observed_at)
                before_left.pop(code)

            for before, after in core.schedule_changes(before_left, after_left):
                if before is not None:
                    uid = active[before["код"]]["id"]
                    self._update(db, uid, after or before, observed_at, deleted=after is None)
                    self._event(db, uid, group_id, observed_at,
                                "changed" if after is not None else "removed", before, after)
                else:
                    restored = self._restoration(after, tombstones)
                    if restored:
                        tombstones.remove(restored)
                        uid = restored["id"]
                        self._update(db, uid, after, observed_at)
                        self._event(db, uid, group_id, observed_at, "restored", restored["item"], after)
                    else:
                        self._insert(db, group_id, after, observed_at, "added")

    @staticmethod
    def _decode_event(row):
        event = dict(row)
        event["types"] = json.loads(event["types"])
        before, after = event.pop("before_lesson"), event.pop("after_lesson")
        event["before"] = json.loads(before) if before is not None else None
        event["after"] = json.loads(after) if after is not None else None
        return event

    @classmethod
    def _decode_lesson(cls, db, row):
        record = dict(row)
        record["lesson"] = json.loads(record["lesson"])
        record["deleted"] = bool(record["deleted"])
        record.pop("source_code", None)
        record.pop("lesson_date", None)
        event = db.execute("SELECT * FROM schedule_events WHERE lesson_id=? ORDER BY id DESC LIMIT 1",
                           (record["id"],)).fetchone()
        record["last_event"] = cls._decode_event(event) if event else None
        record["history_count"] = db.execute("SELECT COUNT(*) FROM schedule_events WHERE lesson_id=?",
                                              (record["id"],)).fetchone()[0]
        return record

    def schedule(self, group_id, start, end, include_removed=True):
        start, end = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
        if end < start:
            raise ValueError("end precedes start")
        with self._connect() as db:
            rows = db.execute("""SELECT l.*,g.group_name FROM schedule_lessons l
                JOIN schedule_groups g USING(group_id)
                WHERE l.group_id=? AND l.lesson_date BETWEEN ? AND ?"""
                              + ("" if include_removed else " AND l.deleted=0"),
                              (int(group_id), start, end)).fetchall()
            records = [self._decode_lesson(db, row) for row in rows]
        return sorted(records, key=lambda row: core.lesson_sort(row["lesson"]))

    def lesson(self, uid):
        with self._connect() as db:
            row = db.execute("""SELECT l.*,g.group_name FROM schedule_lessons l
                JOIN schedule_groups g USING(group_id) WHERE l.id=?""", (str(uid),)).fetchone()
            return self._decode_lesson(db, row) if row else None

    def has_group(self, group_id):
        """Whether an accepted snapshot has already initialized this group.

        Includes an empty initial snapshot: startup migration must not replay an
        older JSON snapshot over an independently committed journal.
        """
        with self._connect() as db:
            return db.execute("SELECT 1 FROM schedule_groups WHERE group_id=?",
                              (int(group_id),)).fetchone() is not None

    def history(self, uid):
        with self._connect() as db:
            return [self._decode_event(row) for row in db.execute(
                "SELECT * FROM schedule_events WHERE lesson_id=? ORDER BY id DESC", (str(uid),))]

    def event_count(self, group_ids, include_baseline=False):
        ids = sorted({int(group_id) for group_id in group_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM schedule_events WHERE group_id IN ({placeholders})"
                              + ("" if include_baseline else " AND kind != 'baseline'"), ids).fetchone()[0]

    def events(self, group_ids, limit=100, offset=0, include_baseline=True, before_id=None):
        ids = sorted({int(group_id) for group_id in group_ids})
        if not ids:
            return []
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        placeholders = ",".join("?" for _ in ids)
        boundary = int(before_id) if before_id is not None else None
        with self._connect() as db:
            return [self._decode_event(row) for row in db.execute(f"""SELECT e.*,g.group_name
                FROM schedule_events e JOIN schedule_groups g USING(group_id)
                WHERE e.group_id IN ({placeholders})"""
                + ("" if include_baseline else " AND e.kind != 'baseline'")
                + (" AND e.id < ?" if boundary is not None else "")
                + " ORDER BY e.id DESC LIMIT ? OFFSET ?",
                (*ids, *((boundary,) if boundary is not None else ()), limit, offset))]
