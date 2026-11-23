"""Same-origin Mini App API. Telegram identity is verified on every API request.

The HTTP thread reads atomic state files; mutations enter a durable queue and are
applied by the bot's own thread. It never writes another thread's profile JSON.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import bot as core
from watch_groups import notification_subject

LOG = logging.getLogger("rasp_bot.miniapp")
PERSONAL_KEYS = ("group_id", "group_name", "daily_time", "week_layout", "seasonal_theme",
                 "display_format", "alert_types", "muted_subjects", "multigroup_enabled",
                 "send_schedule_on_change", "miniapp_card_style")


class APIError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def validate_init_data(value: str, token: str, *, now=None, max_age=86400) -> int:
    """Bot-token validation described at core.telegram.org/bots/webapps.

    Neither initDataUnsafe nor a caller-supplied chat ID is an identity.
    """
    try:
        if not token or not value or len(value) > 16384:
            raise ValueError
        pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError
        received = fields.pop("hash")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", received):
            raise ValueError
        check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received.lower()):
            raise ValueError
        stamp = int(fields["auth_date"])
        age = (time.time() if now is None else now) - stamp
        if not -30 <= age <= max_age:
            raise ValueError
        user = json.loads(fields["user"])
        if not isinstance(user, dict):
            raise ValueError
        user_id = user.get("id")
        if type(user_id) is not int or user_id <= 0 or user.get("is_bot"):
            raise ValueError
        return user_id
    except (ValueError, TypeError, KeyError, OverflowError):
        raise APIError(401, "telegram_auth", "Откройте Mini App заново из Telegram.") from None


class CommandQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                created_at REAL NOT NULL, finished_at REAL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, chat_id, kind, payload):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM commands WHERE chat_id=? AND status IN ('queued','running')",
                               (chat_id,)).fetchone()[0]
            if count >= 5:
                raise APIError(429, "queue_full", "Дождитесь завершения предыдущего действия.")
            uid = secrets.token_hex(16)
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,NULL)",
                       (uid, chat_id, kind, json.dumps(payload, ensure_ascii=False), "queued", None, time.time()))
            # Completed jobs are operational receipts, not lesson history.
            db.execute("DELETE FROM commands WHERE finished_at < ?", (time.time() - 7 * 86400,))
        return {"job_id": uid, "status": "queued"}

    def next(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM commands WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE commands SET status='running' WHERE id=?", (row["id"],))
        return {**dict(row), "payload": json.loads(row["payload"])}

    def finish(self, uid, error=None):
        with self.connect() as db:
            db.execute("UPDATE commands SET status=?,error=?,finished_at=? WHERE id=?",
                       ("failed" if error else "done", error, time.time(), uid))

    def interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE commands SET status='failed',error=?,finished_at=? WHERE status='running'",
                       ("Бот перезапустился. Проверьте настройки и повторите действие при необходимости.", time.time()))

    def get(self, uid, chat_id):
        with self.connect() as db:
            row = db.execute("SELECT id,status,error FROM commands WHERE id=? AND chat_id=?", (uid, chat_id)).fetchone()
        if row is None:
            raise APIError(404, "not_found", "Действие не найдено.")
        return dict(row)


def schedules(owner, *, include_disabled=False):
    result = [owner]
    if include_disabled or owner.get("multigroup_enabled"):
        seen = {str(owner["group_id"])}
        for child in owner.get("watched_groups", {}).values():
            key = str(child.get("group_id"))
            if key not in seen:
                result.append(child)
                seen.add(key)
    return result


def group_info(owner):
    return [{"id": int(p["group_id"]), "name": p["group_name"], "primary": i == 0,
             "enabled": i == 0 or bool(owner.get("multigroup_enabled")),
             "last_fetch_at": p.get("last_fetch_at") or None,
             "last_fetch_failed": bool(p.get("last_fetch_failed")),
             "has_snapshot": p.get("snapshot") is not None}
            for i, p in enumerate(schedules(owner, include_disabled=True))]


def lesson_dto(record):
    raw = record["lesson"]
    subject, kind = core.subject_and_type(raw)
    event = record.get("last_event") or {}
    status = "removed" if record.get("deleted") else ""
    # A historic addition should not remain a permanent 'new' badge.
    try:
        observed = datetime.fromisoformat(event.get("detected_at", ""))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=core.MOSCOW)
        recent = 0 <= (datetime.now(core.MOSCOW) - observed).total_seconds() <= 48 * 3600
    except (ValueError, TypeError):
        recent = False
    if not status and recent:
        if event.get("kind") in ("added", "restored"):
            status = "added"
        elif event.get("kind") == "changed":
            status = "room" if set(event.get("types", [])) == {"room"} else "changed"
    return {"id": record["id"], "group_id": record["group_id"], "group_name": record["group_name"],
            "date": core.lesson_date(raw), "start": raw["датаНачала"][11:16],
            "end": raw.get("датаОкончания", "")[11:16], "subject": subject,
            "type": {"Лекция": "lec", "Практика": "prac", "Лабораторная": "lab"}.get(kind, "other"),
            "teacher": str(raw.get("преподаватель") or ""), "room": str(raw.get("аудитория") or ""),
            "subgroup": raw.get("номерПодгруппы") or "", "deleted": bool(record.get("deleted")),
            "military": notification_subject(raw)[0] == "военная кафедра", "status": status,
            "history_count": record.get("history_count", 0), "observed_at": record.get("observed_at")}


def event_dto(event, record):
    result = {key: event.get(key) for key in ("id", "detected_at", "kind", "types")}
    result.update({"lesson_id": record["id"], "group_id": record["group_id"], "group_name": record["group_name"]})
    for key in ("before", "after"):
        raw = event.get(key)
        result[key] = lesson_dto({**record, "last_event": event, "lesson": raw,
                                 "deleted": key == "before" and event["kind"] == "removed"}) if raw else None
    return result


class MiniAppService:
    def __init__(self, state_file, history, queue, token, teachers, *, auth_max_age=86400):
        self.state_file, self.history, self.queue = Path(state_file), history, queue
        self.token, self.teachers, self.auth_max_age = token, teachers, auth_max_age

    def authenticate(self, authorization):
        if not authorization.startswith("tma "):
            raise APIError(401, "telegram_auth", "Откройте приложение кнопкой в Telegram-боте.")
        chat_id = validate_init_data(authorization[4:], self.token, max_age=self.auth_max_age)
        state = core.load_state(self.state_file)
        owner = state.get("profiles", {}).get(core.MultiBot.profile_key(chat_id))
        if (not owner or owner.get("channel") != "telegram" or owner.get("chat_id") != chat_id
                or owner.get("onboarding") or not owner.get("group_id") or not owner.get("group_name")):
            raise APIError(403, "onboarding", "Сначала нажмите /start и выберите группу в боте.")
        return chat_id, owner

    @staticmethod
    def bounds(params):
        today = datetime.now(core.MOSCOW).date()
        try:
            start = date.fromisoformat(params.get("from", today.isoformat()))
            end = date.fromisoformat(params.get("to", start.isoformat()))
            if end < start or (end - start).days > 62:
                raise ValueError
        except (ValueError, TypeError):
            raise APIError(400, "date_range", "Выберите период не длиннее 63 дней.") from None
        return start.isoformat(), end.isoformat()

    def allowed_lesson(self, uid, owner):
        record = self.history.lesson(uid)
        if record is None or str(record["group_id"]) not in {str(p["group_id"]) for p in schedules(owner)}:
            raise APIError(404, "not_found", "Пара не найдена в ваших отслеживаемых группах.")
        return record

    def dispatch(self, method, path, params, body, chat_id, owner):
        if method == "GET" and path == "/api/bootstrap":
            subjects = dict(owner.get("muted_subjects") or {})
            for profile in schedules(owner):
                for item in (profile.get("snapshot") or {}).values():
                    key, name = notification_subject(item)
                    subjects[key] = name
            profile = {key: owner.get(key) for key in PERSONAL_KEYS}
            profile.update({"display_format": owner.get("display_format") or "image",
                            "alert_types": sorted(core.profile_alert_types(owner)),
                            "muted_subjects": owner.get("muted_subjects") or {},
                            "miniapp_card_style": (owner.get("miniapp_card_style")
                                                   if owner.get("miniapp_card_style") in ("stripe", "fill")
                                                   else "stripe")})
            return {"today": datetime.now(core.MOSCOW).date().isoformat(), "timezone": "Europe/Moscow",
                    "profile": profile, "groups": group_info(owner),
                    "subjects": [{"key": key, "name": name, "enabled": key not in (owner.get("muted_subjects") or {})}
                                 for key, name in sorted(subjects.items())],
                    "alert_types": [{"key": key, "label": label} for key, label in core.ALERT_LABELS.items()]}
        if method == "GET" and path == "/api/schedule":
            start, end = self.bounds(params)
            group = params.get("group_id", str(owner["group_id"]))
            sources = [p for p in schedules(owner) if group == "all" or str(p["group_id"]) == group]
            if not sources:
                raise APIError(403, "group_access", "Эта группа не отслеживается вашим профилем.")
            lessons = [lesson_dto(item) for p in sources for item in self.history.schedule(int(p["group_id"]), start, end)]
            return {"lessons": sorted(lessons, key=lambda x: (x["date"], x["start"], x["group_name"], x["id"])),
                    "groups": group_info(owner), "slots": [{"start": a, "end": b} for a, b in core.PAIR_SLOTS],
                    "freshness": [{"group_id": p["group_id"], "last_fetch_at": p.get("last_fetch_at") or None,
                                   "last_fetch_failed": bool(p.get("last_fetch_failed")),
                                   "has_snapshot": p.get("snapshot") is not None} for p in sources]}
        if method == "GET" and re.fullmatch(r"/api/lessons/[a-zA-Z0-9_-]{1,64}", path):
            record = self.allowed_lesson(path.rsplit("/", 1)[1], owner)
            return {"lesson": lesson_dto(record), "history": [event_dto(event, record) for event in self.history.history(record["id"])]}
        if method == "GET" and path == "/api/changes":
            try:
                limit = min(100, max(1, int(params.get("limit", 50))))
                offset = max(0, int(params.get("offset", 0)))
                if offset > 100000:
                    raise ValueError
            except ValueError:
                raise APIError(400, "pagination", "Неверная страница истории.") from None
            events = self.history.events([int(p["group_id"]) for p in schedules(owner)], limit=limit + 1, offset=offset, include_baseline=False)
            result = []
            for event in events[:limit]:
                if event.get("kind") == "baseline":
                    continue
                record = self.history.lesson(event["lesson_id"])
                if record:
                    result.append(event_dto(event, record))
            return {"events": result, "next_offset": offset + limit if len(events) > limit else None}
        if method == "GET" and path == "/api/teacher":
            record = self.allowed_lesson(params.get("lesson_id", ""), owner)
            start, end = self.bounds(params)
            name = str(record["lesson"].get("преподаватель") or "")
            candidates = self.teachers.resolve(name) if name else []
            requested = params.get("teacher_id")
            chosen = next((x for x in candidates if str(x["id"]) == requested), None) if requested else (candidates[0] if len(candidates) == 1 else None)
            if requested and chosen is None:
                raise APIError(403, "teacher_access", "Выберите преподавателя этой пары.")
            if not chosen:
                return {"teacher": None, "candidates": candidates, "lessons": [],
                        "freshness": {"has_snapshot": False, "last_fetch_failed": False, "last_fetch_at": None},
                        "message": "Выберите преподавателя." if candidates else "Преподаватель не найден в справочнике ДГТУ."}
            result = self.teachers.schedule(chosen["id"], expected_name=chosen["name"])
            lessons = []
            for code, raw in result["snapshot"].items():
                if start <= core.lesson_date(raw) <= end:
                    lessons.append(lesson_dto({"id": "teacher-" + str(code), "group_id": 0,
                                              "group_name": raw.get("group_name") or raw.get("группа") or "",
                                              "lesson": raw, "deleted": False, "observed_at": result.get("fetched_at")}))
                    lessons[-1]["history_available"] = False
            return {"teacher": result["teacher"], "candidates": candidates,
                    "lessons": sorted(lessons, key=lambda x: (x["date"], x["start"], x["group_name"])),
                    "freshness": {"last_fetch_at": result.get("fetched_at"), "has_snapshot": True,
                                  "last_fetch_failed": bool(result.get("last_fetch_failed"))}}
        if method == "GET" and re.fullmatch(r"/api/jobs/[a-f0-9]{32}", path):
            return self.queue.get(path.rsplit("/", 1)[1], chat_id)
        if method == "POST" and path in ("/api/settings", "/api/groups", "/api/refresh"):
            if not isinstance(body, dict):
                raise APIError(400, "body", "Ожидался JSON-объект.")
            validate_command(path.rsplit("/", 1)[1], body, owner)
            return self.queue.submit(chat_id, path.rsplit("/", 1)[1], body)
        raise APIError(404, "not_found", "Страница не найдена.")


def validate_command(kind, data, owner):
    def invalid():
        raise APIError(400, "settings", "Проверьте значения настроек.")
    if kind == "settings":
        allowed = set(PERSONAL_KEYS) - {"group_id", "group_name"}
        if not data or set(data) - allowed:
            invalid()
        for key, value in data.items():
            if key in ("seasonal_theme", "multigroup_enabled", "send_schedule_on_change") and type(value) is not bool:
                invalid()
            if key == "daily_time" and (not isinstance(value, str) or not core.valid_time(value)):
                invalid()
            if key == "week_layout" and value not in ("horizontal", "vertical", "list"):
                invalid()
            if key == "display_format" and value not in ("text", "image"):
                invalid()
            if key == "miniapp_card_style" and (not isinstance(value, str) or value not in ("stripe", "fill")):
                invalid()
            if key == "alert_types" and (not isinstance(value, list) or any(not isinstance(x, str) or x not in core.ALERT_LABELS for x in value)):
                invalid()
            if key == "muted_subjects":
                known = dict(owner.get("muted_subjects") or {})
                for p in schedules(owner, include_disabled=True):
                    for raw in (p.get("snapshot") or {}).values():
                        k, name = notification_subject(raw)
                        known[k] = name
                if not isinstance(value, dict) or any(k not in known or name != known[k] for k, name in value.items()):
                    invalid()
    elif kind == "groups":
        if data.get("action") not in ("add", "remove", "primary") or set(data) - {"action", "name", "group_id"}:
            invalid()
        if data["action"] in ("add", "primary"):
            if not isinstance(data.get("name"), str) or not 1 <= len(data["name"].strip()) <= 40:
                invalid()
        elif str(data.get("group_id")) not in {str(p["group_id"]) for p in schedules(owner, include_disabled=True)[1:]}:
            invalid()
    elif kind == "refresh":
        if set(data) - {"group_id"} or str(data.get("group_id", "all")) not in {"all", *(str(p["group_id"]) for p in schedules(owner))}:
            invalid()


class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_handler(service, static_root):
    static_root = Path(static_root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "RaspMiniApp"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            # Query strings/initData never enter access logs.
            pass

        def send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://edu.donstu.ru; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org")
            self.end_headers()
            self.wfile.write(body)

        def json(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def handle_request(self, method):
            try:
                url = urlsplit(self.path)
                static = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                          "/app.js": ("app.js", "text/javascript"), "/app.css": ("app.css", "text/css")}
                if method == "GET" and url.path in static:
                    filename, mime = static[url.path]
                    self.send(200, (static_root / filename).read_bytes(), mime + "; charset=utf-8")
                    return
                if method == "GET" and url.path == "/healthz":
                    self.json(200, {"ok": True})
                    return
                if not url.path.startswith("/api/"):
                    raise APIError(404, "not_found", "Страница не найдена.")
                chat_id, owner = service.authenticate(self.headers.get("Authorization", ""))
                pairs = parse_qsl(url.query, max_num_fields=16)
                if len(dict(pairs)) != len(pairs):
                    raise APIError(400, "query", "Повторяющийся параметр запроса.")
                body = None
                if method == "POST":
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 32768 or self.headers.get_content_type() != "application/json":
                        raise APIError(400, "body", "Отправьте JSON размером до 32 КБ.")
                    body = json.loads(self.rfile.read(length))
                result = service.dispatch(method, url.path, dict(pairs), body, chat_id, owner)
                self.json(202 if method == "POST" else 200, result)
            except APIError as exc:
                self.json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            except (ValueError, UnicodeDecodeError):
                self.json(400, {"error": {"code": "request", "message": "Неверный запрос."}})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception as exc:
                LOG.warning("Ошибка Mini App: %s", type(exc).__name__)
                self.json(503, {"error": {"code": "unavailable", "message": "Источник данных временно недоступен. Попробуйте ещё раз."}})

    return Handler
