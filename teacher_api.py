"""Public DSTU teacher directory, timetable and official photo links.

Verified against edu.donstu.ru on 2026-11-17: search results use objectID
for the public user profile and additionalObjectID for Rasp?idTeacher.
Photo URLs follow the official website's UserInfo/Photo endpoint.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import bot as core


def _positive_id(value) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,11}", str(value)):
        raise ValueError("Неверный идентификатор преподавателя")
    return int(value)


def name_parts(name: str) -> list[str]:
    """Normalize public names and common job-title prefixes, preserving initials."""
    value = unicodedata.normalize("NFKC", str(name or "")).casefold().replace("ё", "е")
    value = re.sub(r"^(?:(?:ст\s*\.?\s*)?(?:преподаватель|преп|доцент|доц|профессор|проф)(?:\s*\.\s*|\s+))+", "", value)
    return re.findall(r"[а-яa-z]+(?:-[а-яa-z]+)*", value)


def names_match(left: str, right: str) -> bool:
    """Compatible surname and given names/initials; never fuzzy surname matching."""
    a, b = name_parts(left), name_parts(right)
    if not a or not b or a[0] != b[0]:
        return False
    for x, y in zip(a[1:], b[1:]):
        if x != y and not ((len(x) == 1 or len(y) == 1) and x[0] == y[0]):
            return False
    # A short public name is legitimate, but missing requested initials are not.
    return len(a) == 1 or len(b) == 1 or len(a) == len(b)


def avatar_url(user_id) -> str | None:
    """Construct a URL on a fixed official origin; never accept upstream URLs."""
    try:
        value = _positive_id(user_id)
    except ValueError:
        return None
    return f"https://edu.donstu.ru/api/UserInfo/Photo?userID={value}&miniature=true"


class TeacherAPI:
    """Cached public API. cache_path is an optional SQLite file, not a directory.

    Constructor does not perform network requests. Schedules are cached for five
    minutes and name resolutions for one hour. An outage preserves a previous
    accepted schedule with last_fetch_failed=True and its original fetched_at.
    Caller must authorize teacher IDs against a user's group before calling.
    """

    def __init__(self, cache_path: Path | str | None = None, *, requester=None,
                 clock=None, schedule_ttl: int = 300, search_ttl: int = 3600):
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.requester = requester or core.request_json
        self.clock = clock or time.time
        self.schedule_ttl = schedule_ttl
        self.search_ttl = search_ttl
        self._memory: OrderedDict[str, dict] = OrderedDict()
        self._teachers: dict[int, dict] = {}
        self._failures: dict[str, float] = {}
        self._request_locks: OrderedDict[str, threading.Lock] = OrderedDict()
        self._lock = threading.RLock()
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS teacher_cache (key TEXT PRIMARY KEY, saved_at REAL NOT NULL, value TEXT NOT NULL)")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.cache_path), timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _cached(self, key: str) -> dict | None:
        with self._lock:
            record = self._memory.get(key)
            if record is not None:
                self._memory.move_to_end(key)
                return copy.deepcopy(record)
            if self.cache_path:
                with self._connect() as connection:
                    row = connection.execute("SELECT saved_at, value FROM teacher_cache WHERE key=?", (key,)).fetchone()
                if row:
                    try:
                        record = {"saved_at": row[0], "data": json.loads(row[1])}
                    except (TypeError, ValueError):
                        return None
                    self._memory[key] = record
                    self._trim_memory()
                    return copy.deepcopy(record)
        return None

    def _trim_memory(self):
        while len(self._memory) > 128:
            self._memory.popitem(last=False)

    def _store(self, key: str, data) -> None:
        record = {"saved_at": self.clock(), "data": copy.deepcopy(data)}
        with self._lock:
            self._memory[key] = record
            self._memory.move_to_end(key)
            self._trim_memory()
            if self.cache_path:
                with self._connect() as connection:
                    connection.execute("INSERT INTO teacher_cache(key,saved_at,value) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET saved_at=excluded.saved_at,value=excluded.value",
                                       (key, record["saved_at"], json.dumps(data, ensure_ascii=False)))
                    connection.execute("DELETE FROM teacher_cache WHERE key NOT IN (SELECT key FROM teacher_cache ORDER BY saved_at DESC LIMIT 256)")

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")

    def _request_lock(self, key: str):
        with self._lock:
            lock = self._request_locks.get(key)
            if lock is None:
                lock = self._request_locks[key] = threading.Lock()
            self._request_locks.move_to_end(key)
            while len(self._request_locks) > 256:
                self._request_locks.popitem(last=False)
            return lock

    def _remember(self, candidates: list[dict]) -> list[dict]:
        with self._lock:
            for candidate in candidates:
                self._teachers[candidate["id"]] = dict(candidate)
            if len(self._teachers) > 512:
                self._teachers = {candidate["id"]: dict(candidate) for candidate in candidates}
        return copy.deepcopy(candidates)

    def resolve(self, name: str) -> list[dict]:
        """Return every compatible teacher; initials can legitimately be ambiguous."""
        parts = name_parts(name)
        if not parts or len(parts[0]) < 2 or len(name) > 180:
            return []
        key = "resolve:" + " ".join(parts)
        previous = self._cached(key)
        if previous and self.clock() - previous["saved_at"] < self.search_ttl:
            return self._remember(previous["data"])
        try:
            response = self.requester(f"{core.BASE_URL}/search", {"searchQuery": parts[0]}, timeout=15)
            data = response.get("data")
            if response.get("state") != 1 or not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise RuntimeError("ДГТУ вернул неверный список преподавателей")
            result = []
            seen = set()
            for raw in data["results"]:
                if not isinstance(raw, dict) or raw.get("type") != "Преподаватель":
                    continue
                candidate_name = raw.get("name")
                if not isinstance(candidate_name, str) or not names_match(name, candidate_name):
                    continue
                try:
                    teacher_id = _positive_id(raw.get("additionalObjectID"))
                    user_id = _positive_id(raw.get("objectID"))
                except ValueError:
                    continue
                if teacher_id not in seen:
                    result.append({"id": teacher_id, "user_id": user_id, "name": candidate_name,
                                   "avatar_url": avatar_url(user_id)})
                    seen.add(teacher_id)
            self._store(key, result)
            return self._remember(result)
        except RuntimeError:
            if previous:
                return self._remember(previous["data"])
            raise

    def _validate_expected(self, payload: dict, expected_name: str) -> None:
        if expected_name and not names_match(expected_name, payload["teacher"]["name"]):
            raise RuntimeError("ДГТУ вернул расписание другого преподавателя")

    def _fetch_schedule(self, teacher_id: int, expected_name: str) -> dict:
        response = self.requester(f"{core.BASE_URL}/Rasp", {"idTeacher": teacher_id}, timeout=20)
        data = response.get("data")
        if response.get("state") != 1 or not isinstance(data, dict) or not isinstance(data.get("rasp"), list):
            raise RuntimeError("ДГТУ вернул неверное расписание преподавателя")
        info = data.get("info")
        source = info.get("prepod") if isinstance(info, dict) else None
        if not isinstance(source, dict):
            raise RuntimeError("В ответе ДГТУ не указан преподаватель")
        reported_name = source.get("name")
        if not isinstance(reported_name, str) or not name_parts(reported_name):
            raise RuntimeError("В ответе ДГТУ не указан преподаватель")
        with self._lock:
            known = dict(self._teachers.get(teacher_id) or {})
        try:
            user_id = _positive_id(source.get("userID"))
        except ValueError:
            user_id = None
        if known:
            if not names_match(known["name"], reported_name) or (user_id and known["user_id"] != user_id):
                raise RuntimeError("ДГТУ вернул расписание другого преподавателя")
            user_id = user_id or known["user_id"]
        person = {"id": teacher_id, "user_id": user_id,
                  "name": known.get("name") or reported_name, "avatar_url": avatar_url(user_id)}
        result = {"teacher": person, "snapshot": {}, "fetched_at": self._now(),
                  "last_fetch_attempt_at": self._now(), "last_fetch_failed": False, "has_snapshot": True}
        self._validate_expected(result, expected_name)
        for raw in data["rasp"]:
            if not isinstance(raw, dict):
                raise RuntimeError("Неверная запись в расписании преподавателя")
            raw_teacher = raw.get("кодПреподавателя")
            if raw_teacher not in (None, 0, "0", "") and str(raw_teacher) != str(teacher_id):
                raise RuntimeError("В расписании найдена запись другого преподавателя")
            item = core.normalize_lesson(raw)
            if item["код"] in result["snapshot"]:
                raise RuntimeError("Повторяющаяся запись расписания преподавателя")
            item.update({"group_name": str(raw.get("группа") or ""), "teacher_id": teacher_id,
                         "user_id": user_id})
            result["snapshot"][item["код"]] = item
        return result

    def schedule(self, teacher_id: int, expected_name: str = "") -> dict:
        teacher_id = _positive_id(teacher_id)
        with self._request_lock(f"schedule:{teacher_id}"):
            return self._schedule(teacher_id, expected_name)

    def _schedule(self, teacher_id: int, expected_name: str) -> dict:
        key = f"schedule:{teacher_id}"
        previous = self._cached(key)
        failed_at = self._failures.get(key)
        if not previous and failed_at is not None and self.clock() - failed_at < 30:
            raise RuntimeError("Расписание преподавателя временно недоступно; повторите позже")
        if previous:
            self._validate_expected(previous["data"], expected_name)
            if self.clock() - previous["saved_at"] < self.schedule_ttl:
                return previous["data"]
            if failed_at is not None and self.clock() - failed_at < 30:
                fallback = previous["data"]
                fallback["last_fetch_failed"] = True
                fallback["last_fetch_attempt_at"] = datetime.fromtimestamp(failed_at, timezone.utc).isoformat(timespec="seconds")
                return fallback
        try:
            result = self._fetch_schedule(teacher_id, expected_name)
            # An unexpectedly empty response must not erase a working timetable.
            if previous and previous["data"].get("snapshot") and not result["snapshot"]:
                now = self._now()[:10]
                if any(core.lesson_date(item) >= now for item in previous["data"]["snapshot"].values()):
                    raise RuntimeError("ДГТУ вернул неожиданно пустое расписание преподавателя")
            self._store(key, result)
            self._failures.pop(key, None)
            return copy.deepcopy(result)
        except RuntimeError:
            self._failures[key] = self.clock()
            if len(self._failures) > 256:
                self._failures = {key: self._failures[key]}
            if previous:
                fallback = previous["data"]
                fallback["last_fetch_failed"] = True
                fallback["last_fetch_attempt_at"] = self._now()
                return fallback
            raise
