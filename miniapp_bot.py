"""Optional web entry point; existing Telegram/VK flows remain independent."""
from __future__ import annotations

import copy
import os
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import bot as core
from history_store import HistoryStore
from miniapp_server import (APIError, CommandQueue, LimitedHTTPServer, MiniAppService,
                            make_handler, schedules, validate_command)
from teacher_api import TeacherAPI
from watch_groups import MAX_WATCHED_GROUPS


class MiniAppMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        base = self.state_file.parent
        self.history = HistoryStore(Path(os.getenv("HISTORY_FILE", str(base / "schedule.sqlite3"))))
        self.miniapp_queue = CommandQueue(base / "miniapp_commands.sqlite3")
        self.miniapp_queue.interrupted()
        self._processing_miniapp = False
        self._miniapp_server = None
        self._miniapp_thread = None
        self._miniapp_ready = False
        self.miniapp_url = os.getenv("MINIAPP_URL", "").strip().rstrip("/")
        if self.miniapp_url:
            parsed = urlsplit(self.miniapp_url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise RuntimeError("MINIAPP_URL должен быть HTTPS-адресом без логина, пароля и параметров")
            if parsed.path not in ("", "/"):
                raise RuntimeError("Для Mini App нужен корень отдельного домена, например https://rasp.example.ru")
        # Bootstrap old JSON snapshots once. The timestamp is when observation
        # began, not a fictional date of addition on the university website.
        self.index_saved_schedules()

    def index_saved_schedules(self):
        newest = {}
        for owner in self.state.get("profiles", {}).values():
            for profile in schedules(owner, include_disabled=True):
                if not profile.get("group_id") or not profile.get("group_name") or profile.get("snapshot") is None:
                    continue
                key = int(profile["group_id"])
                if key not in newest or (profile.get("last_fetch_at") or "") > (newest[key].get("last_fetch_at") or ""):
                    newest[key] = profile
        now = datetime.now(core.MOSCOW).isoformat(timespec="microseconds")
        for profile in newest.values():
            if not self.history.has_group(profile["group_id"]):
                self.history.observe(profile["group_id"], profile["group_name"], profile["snapshot"], now)

    def process_profile_snapshot(self, profile, snapshot, now):
        old = profile.get("snapshot")
        if old and not snapshot and any(core.lesson_date(item) >= now.date().isoformat() for item in old.values()):
            raise RuntimeError("API неожиданно вернул пустое расписание; старые данные сохранены")
        self.history.observe(profile["group_id"], profile["group_name"], snapshot,
                             now.isoformat(timespec="microseconds"), baseline=old)
        return super().process_profile_snapshot(profile, snapshot, now)

    def apply_user_input(self, profile, value, today):
        before = {id(p): p.get("snapshot") for p in schedules(profile, include_disabled=True)}
        result = super().apply_user_input(profile, value, today)
        for source in schedules(profile, include_disabled=True):
            previous = before.get(id(source))
            if source.get("snapshot") is not None and source.get("snapshot") is not previous:
                self.history.observe(source["group_id"], source["group_name"], source["snapshot"],
                                     datetime.now(core.MOSCOW).isoformat(timespec="microseconds"), baseline=previous)
        self.index_saved_schedules()
        return result

    def settings_view(self, profile):
        message, markup = super().settings_view(profile)
        if self.miniapp_url and self._miniapp_ready and profile.get("channel") == "telegram":
            markup["inline_keyboard"].insert(0, [{"text": "Открыть Mini App", "web_app": {"url": self.miniapp_url}}])
        return message, markup

    def start_miniapp(self):
        if not self.miniapp_url:
            return
        if not self.telegram_token:
            core.LOG.warning("Mini App требует TELEGRAM_BOT_TOKEN; бот продолжает работу без web-интерфейса")
            return
        try:
            service = MiniAppService(self.state_file, self.history, self.miniapp_queue, self.telegram_token,
                                     TeacherAPI(cache_path=self.state_file.parent / "teacher_cache.sqlite3"))
            address = os.getenv("MINIAPP_HOST", "127.0.0.1")
            port = int(os.getenv("MINIAPP_PORT", "8080"))
            self._miniapp_server = LimitedHTTPServer((address, port), make_handler(service, Path(__file__).parent / "miniapp"))
            self._miniapp_thread = threading.Thread(target=self._miniapp_server.serve_forever,
                                                    name="miniapp-http", daemon=True)
            self._miniapp_thread.start()
            self._miniapp_ready = True
            core.LOG.info("Mini App слушает %s:%d", address, port)
        except Exception as exc:
            core.LOG.warning("Не удалось запустить Mini App (%s); чат-бот продолжает работать", type(exc).__name__)
            return
        try:
            # One menu entry adds no new reply-keyboard row and leaves all bot
            # commands/buttons available to people who prefer the chat.
            menu = {"type": "web_app", "text": "Расписание", "web_app": {"url": self.miniapp_url}}
            if self.state.get("miniapp_menu_url") != self.miniapp_url:
                import json
                response = core.request_json(f"https://api.telegram.org/bot{self.telegram_token}/setChatMenuButton",
                                             {"menu_button": json.dumps(menu, ensure_ascii=False)}, method="POST")
                if response.get("ok"):
                    self.state["miniapp_menu_url"] = self.miniapp_url
                    core.save_state(self.state_file, self.state)
                else:
                    core.LOG.warning("Telegram не установил кнопку Mini App; используйте Настройки в боте")
        except Exception as exc:
            core.LOG.warning("Не удалось настроить меню Mini App (%s); используйте кнопку в настройках", type(exc).__name__)

    def stop_miniapp(self):
        self._miniapp_ready = False
        if self._miniapp_server:
            self._miniapp_server.shutdown()
            self._miniapp_server.server_close()
            self._miniapp_server = None

    def process_miniapp_commands(self):
        if self._processing_miniapp:
            return
        self._processing_miniapp = True
        try:
            # A slow upstream job must not drain a whole burst before Telegram
            # gets another polling opportunity.
            for _ in range(1):
                job = self.miniapp_queue.next()
                if not job:
                    break
                try:
                    owner = self.profile_for_chat(job["chat_id"])
                    if not owner or owner.get("onboarding"):
                        raise APIError(403, "onboarding", "Сначала выберите группу в боте.")
                    validate_command(job["kind"], job["payload"], owner)
                    self.apply_miniapp_command(owner, job["kind"], job["payload"])
                    core.save_state(self.state_file, self.state)
                    self.miniapp_queue.finish(job["id"])
                except APIError as exc:
                    self.miniapp_queue.finish(job["id"], exc.message)
                except Exception as exc:
                    core.LOG.warning("Действие Mini App не выполнено: %s", type(exc).__name__)
                    self.miniapp_queue.finish(job["id"], "Не удалось выполнить действие. Проверьте расписание и попробуйте ещё раз.")
        finally:
            self._processing_miniapp = False

    def apply_miniapp_command(self, owner, kind, data):
        now = datetime.now(core.MOSCOW)
        if kind == "settings":
            owner.update(copy.deepcopy(data))
            if not owner.get("multigroup_enabled"):
                owner["selected_group_id"] = owner["group_id"]
                for child in owner.get("watched_groups", {}).values():
                    child["cards"] = {}
                self._prune_reports(owner)
            else:
                self.needs_refresh = True
            if not owner.get("send_schedule_on_change"):
                owner["pending"] = [p for p in owner["pending"] if p.get("kind") != "change_card"]
            return
        if kind == "refresh":
            requested = str(data.get("group_id", "all"))
            targets = {str(p["group_id"]) for p in self.monitored_schedules(owner)
                       if requested == "all" or requested == str(p["group_id"])}
            for peers in self._refresh_targets().values():
                if str(peers[0]["group_id"]) in targets and not self.recently_checked(peers, now):
                    self.refresh_profiles(peers, now)
            self.flush_group_reports()
            return
        if kind != "groups":
            raise APIError(400, "action", "Неизвестное действие.")
        if data["action"] == "remove":
            key = str(data["group_id"])
            owner["watched_groups"].pop(key, None)
            if str(owner.get("selected_group_id")) == key:
                owner["selected_group_id"] = owner["group_id"]
            self._prune_reports(owner)
            return
        name = data["name"].strip()
        if data["action"] == "add":
            if not owner.get("multigroup_enabled"):
                raise APIError(400, "groups_disabled", "Сначала включите отслеживание нескольких групп.")
            if len(owner.get("watched_groups", {})) >= MAX_WATCHED_GROUPS:
                raise APIError(400, "group_limit", f"Можно добавить до {MAX_WATCHED_GROUPS} дополнительных групп.")
            if core.compact_name(name) in {core.compact_name(p["group_name"]) for p in schedules(owner, include_disabled=True)}:
                raise APIError(400, "duplicate", "Эта группа уже подключена.")
        try:
            group_id = core.find_group_id(name)
            snapshot = core.fetch_schedule(group_id, name)
        except Exception:
            raise APIError(503, "group_unavailable", "Группа не найдена или ДГТУ сейчас недоступен.") from None
        stamp = now.isoformat(timespec="microseconds")
        self.history.observe(group_id, name, snapshot, stamp)
        if data["action"] == "add":
            child = self.new_profile("telegram", owner["chat_id"])
            child.update({"group_id": group_id, "group_name": name, "snapshot": snapshot,
                          "owner_chat_id": owner["chat_id"], "group_checked_on": now.date().isoformat(),
                          "last_fetch_at": stamp, "last_fetch_attempt_at": stamp, "last_fetch_failed": False})
            owner.setdefault("watched_groups", {})[str(group_id)] = child
        else:
            owner.update({"group_id": group_id, "group_name": name, "snapshot": snapshot,
                          "selected_group_id": group_id, "last_fetch_at": stamp, "last_fetch_attempt_at": stamp,
                          "last_fetch_failed": False, "group_checked_on": now.date().isoformat(),
                          "pending": [], "cards": {}, "change_batches": {}, "pending_group_changes": {},
                          "daily_queued": now.date().isoformat(), "weekly_queued": "", "week_cursor": "",
                          "awaiting": "", "onboarding": False, "welcome_card_pending": False})
            owner.get("watched_groups", {}).pop(str(group_id), None)

    def cycle(self):
        self.process_miniapp_commands()
        return super().cycle()

    def telegram_commands(self, today):
        self.process_miniapp_commands()
        return super().telegram_commands(today)

    def queue_due(self, now):
        self.process_miniapp_commands()
        return super().queue_due(now)
