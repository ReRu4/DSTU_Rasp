"""Optional monitoring of several groups with one report per subscriber."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

import bot as core


MAX_WATCHED_GROUPS = 10
SUBJECTS_PER_PAGE = 6
PERSONAL_FIELDS = ("daily_time", "week_layout", "seasonal_theme", "alert_types", "display_format",
                   "send_schedule_on_change", "muted_subjects")


def notification_subject(item):
    """Lesson types share a switch; military timetable labels form one family."""
    name = " ".join(unicodedata.normalize("NFKC", core.subject_and_type(item)[0]).split())
    key = name.casefold().replace("ё", "е")
    if re.match(r"^военная кафедра\b", key):
        return "военная кафедра", "Военная кафедра"
    return key, name


def subject_token(key):
    # Full subject names can exceed Telegram's 64-byte callback_data limit.
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class WatchGroupsMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for owner in self.state["profiles"].values():
            self._watch_defaults(owner)
        core.save_state(self.state_file, self.state)

    @staticmethod
    def _watch_defaults(profile):
        profile.setdefault("multigroup_enabled", False)
        profile.setdefault("watched_groups", {})
        profile.setdefault("send_schedule_on_change", False)
        profile.setdefault("pending_group_changes", {})
        profile.setdefault("muted_subjects", {})

    def new_profile(self, *args, **kwargs):
        profile = super().new_profile(*args, **kwargs)
        self._watch_defaults(profile)
        return profile

    def owner_profile(self, profile):
        if profile.get("owner_chat_id"):
            return self.profile_for_chat(profile["owner_chat_id"]) or profile
        return profile

    def monitored_schedules(self, owner):
        self._watch_defaults(owner)
        schedules = [owner]
        if owner["channel"] == "telegram" and owner["multigroup_enabled"]:
            for child in owner["watched_groups"].values():
                if core.compact_name(child["group_name"]) == core.compact_name(owner["group_name"]):
                    continue
                for key in PERSONAL_FIELDS:
                    if key in owner:
                        child[key] = copy.deepcopy(owner[key])
                child["owner_chat_id"] = owner["chat_id"]
                child["chat_id"] = owner["chat_id"]
                schedules.append(child)
        return schedules

    def selected_schedule(self, owner):
        chosen = str(owner.get("selected_group_id", owner.get("group_id")))
        return next((item for item in self.monitored_schedules(owner)
                     if str(item["group_id"]) == chosen), owner)

    def settings_view(self, profile):
        message, markup = super().settings_view(profile)
        self._watch_defaults(profile)
        enabled = "вкл" if profile["multigroup_enabled"] else "выкл"
        cards = "да" if profile["send_schedule_on_change"] else "нет"
        message += (f"\n\nНесколько групп: {enabled}."
                    f"\nНовые карточки при изменениях: {cards}.")
        markup["inline_keyboard"].append([
            {"text": f"👥 Несколько групп · {enabled}", "callback_data": "watch:settings"}])
        markup["inline_keyboard"].append([
            {"text": f"🖼 Карточки при изменениях · {cards}", "callback_data": "settings:change_cards"}])
        muted = len(profile["muted_subjects"])
        markup["inline_keyboard"].append([
            {"text": f"🔔 Уведомления по предметам · отключено {muted}",
             "callback_data": "subjects:settings"}])
        return message, markup

    def notification_subjects(self, owner):
        """Keep muted subjects selectable even after they disappear from snapshots."""
        self._watch_defaults(owner)
        subjects = dict(owner["muted_subjects"])
        for profile in self.monitored_schedules(owner):
            for item in (profile.get("snapshot") or {}).values():
                key, name = notification_subject(item)
                subjects.setdefault(key, name)
        return sorted(subjects.items(), key=lambda item: item[0])

    def subject_change_allowed(self, owner, before, after):
        muted = owner.get("muted_subjects", {})
        return not any(notification_subject(item)[0] in muted
                       for item in (before, after) if item)

    def subject_settings_view(self, owner, page=0):
        subjects = self.notification_subjects(owner)
        pages = max(1, (len(subjects) + SUBJECTS_PER_PAGE - 1) // SUBJECTS_PER_PAGE)
        page = max(0, min(page, pages - 1))
        text = ("🔔 Уведомления по предметам\n\n"
                "Нажмите на предмет, чтобы включить или выключить оповещения о его изменениях.\n"
                "🔔 — включены · 🔕 — выключены\n\n"
                "Выбор действует для всех ваших отслеживаемых групп. Лекции, практики и лабораторные "
                "одного предмета переключаются вместе; все варианты военной кафедры — тоже. "
                "Предметы остаются в расписании, а отправленные карточки продолжают обновляться. "
                "Настройка действует для новых проверок расписания.")
        rows = []
        for index, (key, name) in enumerate(subjects[page * SUBJECTS_PER_PAGE:(page + 1) * SUBJECTS_PER_PAGE],
                                           start=page * SUBJECTS_PER_PAGE + 1):
            icon = "🔕" if key in owner["muted_subjects"] else "🔔"
            text += f"\n\n{index}. {name[:300]} · {'выключены' if icon == '🔕' else 'включены'}"
            label = name if len(name) <= 46 else name[:43] + "…"
            rows.append([{"text": f"{icon} {index}. {label}",
                          "callback_data": f"subjects:toggle:{subject_token(key)}:{page}"}])
        if not subjects:
            text += "\n\nПредметы появятся здесь после загрузки расписания."
        if pages > 1:
            text += f"\n\nСтраница {page + 1} из {pages}"
            arrows = []
            if page:
                arrows.append({"text": "◀️ Назад", "callback_data": f"subjects:page:{page - 1}"})
            if page + 1 < pages:
                arrows.append({"text": "Далее ▶️", "callback_data": f"subjects:page:{page + 1}"})
            rows.append(arrows)
        rows.append([{"text": "⬅️ К настройкам", "callback_data": "settings:back"}])
        return text, {"inline_keyboard": rows}

    def show_subject_settings(self, owner, message_id=None, page=0):
        text, markup = self.subject_settings_view(owner, page)
        params = {"chat_id": owner["chat_id"], "text": text,
                  "reply_markup": json.dumps(markup, ensure_ascii=False)}
        if message_id:
            params["message_id"] = message_id
        response = core.request_json(
            f"https://api.telegram.org/bot{self.telegram_token}/{'editMessageText' if message_id else 'sendMessage'}",
            params, method="POST")
        if not response.get("ok") and "message is not modified" not in str(response.get("description", "")):
            if message_id:
                return self.show_subject_settings(owner, page=page)
            raise RuntimeError("Не удалось показать настройки предметов")

    def show_watch_settings(self, owner, message_id=None):
        self._watch_defaults(owner)
        enabled = owner["multigroup_enabled"]
        text = ("👥 Несколько групп\n\n"
                f"Основная группа: {owner['group_name']}\n"
                f"Отслеживание: {'включено' if enabled else 'выключено'}\n\n"
                "Изменения выбранных групп приходят общим отчётом. "
                "Ежедневное расписание приходит только для основной группы. "
                "Остальные можно открыть через выбор группы под расписанием.\n\n"
                f"Можно добавить до {MAX_WATCHED_GROUPS} дополнительных групп.")
        rows = [[{"text": "Выключить" if enabled else "Включить",
                  "callback_data": "watch:off" if enabled else "watch:on"}]]
        if enabled:
            rows.append([{"text": "➕ Добавить группу", "callback_data": "watch:add"}])
        for key, child in owner["watched_groups"].items():
            rows.append([{"text": f"✖ Удалить {child['group_name']}",
                          "callback_data": f"watch:remove:{key}"}])
        rows.append([{"text": "⬅️ К настройкам", "callback_data": "settings:back"}])
        params = {"chat_id": owner["chat_id"], "text": text,
                  "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False)}
        if message_id:
            params["message_id"] = message_id
        response = core.request_json(
            f"https://api.telegram.org/bot{self.telegram_token}/{'editMessageText' if message_id else 'sendMessage'}",
            params, method="POST")
        if not response.get("ok") and "message is not modified" not in str(response.get("description", "")):
            if message_id:
                return self.show_watch_settings(owner)
            raise RuntimeError("Не удалось показать настройки групп")

    def handle_profile_callback(self, profile, callback):
        action = str(callback.get("data") or "")
        message_id = (callback.get("message") or {}).get("message_id")
        if action.startswith("subjects:"):
            owner = self.owner_profile(profile)
            self._watch_defaults(owner)
            page = 0
            page_match = re.fullmatch(r"subjects:page:(\d{1,5})", action)
            toggle_match = re.fullmatch(r"subjects:toggle:([a-f0-9]{16}):(\d{1,5})", action)
            if page_match:
                page = int(page_match[1])
            elif toggle_match:
                page = int(toggle_match[2])
                subject = next(((key, name) for key, name in self.notification_subjects(owner)
                                if subject_token(key) == toggle_match[1]), None)
                if subject is None:
                    self.answer_callback(callback["id"], "Список предметов обновился")
                    self.show_subject_settings(owner, message_id, page)
                    return
                key, name = subject
                if key in owner["muted_subjects"]:
                    del owner["muted_subjects"][key]
                else:
                    owner["muted_subjects"][key] = name
                self.monitored_schedules(owner)
                core.save_state(self.state_file, self.state)
            elif action != "subjects:settings":
                self.answer_callback(callback["id"], "Кнопка устарела")
                return
            self.answer_callback(callback["id"])
            self.show_subject_settings(owner, message_id, page)
            return
        if action == "settings:change_cards":
            self.answer_callback(callback["id"])
            profile["send_schedule_on_change"] = not profile.get("send_schedule_on_change", False)
            if not profile["send_schedule_on_change"]:
                profile["pending"] = [item for item in profile["pending"]
                                      if item.get("kind") != "change_card"]
            core.save_state(self.state_file, self.state)
            self.show_settings(profile, message_id)
            return
        if not action.startswith("watch:"):
            return super().handle_profile_callback(profile, callback)
        self.answer_callback(callback["id"])
        self._watch_defaults(profile)
        if action == "watch:on":
            profile["multigroup_enabled"] = True
            # Existing child snapshots are baselines: changes since pausing are reported.
            self.needs_refresh = True
        elif action == "watch:off":
            profile["multigroup_enabled"] = False
            profile["selected_group_id"] = profile["group_id"]
            profile["awaiting"] = ""
            for child in profile["watched_groups"].values():
                child["cards"] = {}
            self._prune_reports(profile)
        elif action.startswith("watch:remove:"):
            key = action.split(":", 2)[2]
            profile["watched_groups"].pop(key, None)
            if str(profile.get("selected_group_id")) == key:
                profile["selected_group_id"] = profile["group_id"]
            self._prune_reports(profile)
        elif action == "watch:add":
            if not profile["multigroup_enabled"]:
                self.show_watch_settings(profile, message_id)
                return
            if len(profile["watched_groups"]) >= MAX_WATCHED_GROUPS:
                self.send_profile_message(profile, f"Можно добавить до {MAX_WATCHED_GROUPS} групп. Сначала удалите ненужную.")
                return
            profile["awaiting"] = "watch_group"
            core.save_state(self.state_file, self.state)
            self.send_profile_message(profile, "Напишите название дополнительной группы, например ПИ31.",
                                      {"inline_keyboard": [[{"text": "Отмена", "callback_data": "watch:cancel"}]]})
            return
        elif action == "watch:cancel":
            profile["awaiting"] = ""
        elif action != "watch:settings":
            return
        core.save_state(self.state_file, self.state)
        self.show_watch_settings(profile, message_id)

    def apply_user_input(self, profile, value, today):
        if profile.get("awaiting") != "watch_group":
            old_group = core.compact_name(profile.get("group_name", ""))
            result = super().apply_user_input(profile, value, today)
            if old_group != core.compact_name(profile.get("group_name", "")):
                profile["selected_group_id"] = profile["group_id"]
                profile.get("watched_groups", {}).pop(str(profile["group_id"]), None)
                profile["pending_group_changes"] = {}
                core.save_state(self.state_file, self.state)
            return result
        self._watch_defaults(profile)
        if not profile["multigroup_enabled"]:
            profile["awaiting"] = ""
            self.show_watch_settings(profile)
            return
        if not 1 <= len(value) <= 40:
            self.send_profile_message(profile, "Введите короткое название группы, например ПИ31.")
            return
        names = {core.compact_name(item["group_name"]) for item in self.monitored_schedules(profile)}
        if core.compact_name(value) in names:
            self.send_profile_message(profile, "Эта группа уже подключена. Напишите другую или нажмите «Отмена».")
            return
        if len(profile["watched_groups"]) >= MAX_WATCHED_GROUPS:
            self.send_profile_message(profile, f"Достигнут лимит: {MAX_WATCHED_GROUPS} дополнительных групп.")
            return
        try:
            group_id = core.find_group_id(value)
            if group_id == profile["group_id"] or str(group_id) in profile["watched_groups"]:
                self.send_profile_message(profile, "Эта группа уже подключена.")
                return
            snapshot = core.fetch_schedule(group_id, value)
        except Exception as exc:
            core.LOG.warning("Не удалось добавить группу %s: %s", value, exc)
            self.send_profile_message(profile, "Группа не найдена или ДГТУ недоступен. Проверьте название и попробуйте снова.")
            return
        now = datetime.now(core.MOSCOW).isoformat(timespec="seconds")
        child = self.new_profile("telegram", profile["chat_id"])
        child.update({"group_name": value, "group_id": group_id, "owner_chat_id": profile["chat_id"],
                      "snapshot": snapshot, "group_checked_on": today, "last_fetch_at": now,
                      "last_fetch_attempt_at": now, "last_fetch_failed": False,
                      "keyboard_version": profile.get("keyboard_version", 3)})
        profile["watched_groups"][str(group_id)] = child
        profile["awaiting"] = ""
        core.save_state(self.state_file, self.state)
        self.send_profile_message(profile, f"Группа {value} добавлена. Буду сообщать о новых изменениях общим отчётом.")
        self.show_watch_settings(profile)

    def _prune_reports(self, owner):
        """Do not deliver paused/removed groups from previously queued report chunks."""
        allowed = {str(item["group_id"]) for item in self.monitored_schedules(owner)}
        owner["pending_group_changes"] = {key: value for key, value in owner["pending_group_changes"].items()
                                          if key in allowed}
        for batch in owner["change_batches"].values():
            if "groups" in batch:
                batch["groups"] = [group for group in batch["groups"] if str(group["group_id"]) in allowed]
        rebuilt, emitted = [], set()
        for item in owner["pending"]:
            if (item.get("kind") in ("change_card", "refresh") and item.get("source_group_id") is not None
                    and str(item["source_group_id"]) not in allowed):
                continue
            batch_id = item.get("watch_batch_id")
            if not batch_id:
                rebuilt.append(item)
                continue
            if batch_id in emitted:
                continue
            emitted.add(batch_id)
            batch = owner["change_batches"].get(batch_id, {})
            if batch.get("groups"):
                rebuilt.extend(self._report_items(batch_id, batch))
        owner["pending"] = rebuilt

    def process_profile_snapshot(self, profile, snapshot, now):
        today = now.date().isoformat()
        old = profile.get("snapshot")
        if old and not snapshot and any(core.lesson_date(item) >= today for item in old.values()):
            raise RuntimeError("API неожиданно вернул пустое расписание; старые данные сохранены")
        owner = self.owner_profile(profile)
        self._watch_defaults(owner)
        if old is not None:
            alerts = core.profile_alert_types(owner)
            change_filter = lambda before, after: self.subject_change_allowed(owner, before, after)
            message = core.changes_message(old, snapshot, today, profile["group_name"], alerts,
                                           change_filter=change_filter)
            if message:
                # Include today's changes in the same saved week marker structure.
                since = (now.date() - timedelta(days=1)).isoformat()
                weeks = core.future_change_weeks(old, snapshot, since, alerts, change_filter=change_filter)
                owner["pending_group_changes"][str(profile["group_id"])] = {
                    "group_id": profile["group_id"], "group_name": profile["group_name"],
                    "text": message, "weeks": weeks,
                    "snapshot": {key: item for key, item in snapshot.items()
                                 if core.week_start(core.lesson_date(item)) in weeks},
                    "fetched_at": now.isoformat(timespec="seconds")}
            changed_dates = {core.lesson_date(item) for before, after in core.schedule_changes(old, snapshot)
                             for item in (before, after) if item and core.lesson_date(item) >= today}
            refreshes = {(view, date) for day in changed_dates
                         for view, date in (("day", day), ("week", core.week_start(day)),
                                            ("month", day[:7] + "-01"))
                         if f"{view}:{date}" in profile["cards"]}
            for view, date in sorted(refreshes):
                if not any(item.get("kind") == "refresh" and item.get("view") == view
                           and item.get("date") == date
                           and str(item.get("source_group_id", owner["group_id"])) == str(profile["group_id"])
                           for item in owner["pending"]):
                    owner["pending"].append({"kind": "refresh", "format": "card", "view": view,
                                              "date": date, "source_group_id": profile["group_id"]})
        profile["snapshot"] = snapshot
        profile.update({"last_fetch_at": now.isoformat(timespec="seconds"),
                        "last_fetch_attempt_at": now.isoformat(timespec="seconds"),
                        "last_fetch_failed": False})

    @staticmethod
    def _report_items(batch_id, batch):
        groups = batch["groups"]
        title = "🔔 Общий отчёт об изменениях" if len(groups) > 1 else "🔔 Изменения в расписании"
        sections = []
        for group in groups:
            # Each group is visibly separated, even when the long report is split by Telegram limits.
            body = group["text"].split("\n\n", 1)[-1]
            sections.append(f"🎓 {group['group_name']}\n\n{body}")
        parts = core.split_message(title + "\n\n" + "\n\n──────────\n\n".join(sections))
        return [{"kind": "combined_change", "text": part, "watch_batch_id": batch_id,
                 "batch_id": batch_id if index == len(parts)-1 else ""}
                for index, part in enumerate(parts)]

    def flush_group_reports(self):
        for owner in self.active_profiles():
            self._watch_defaults(owner)
            events = owner["pending_group_changes"]
            if not events:
                continue
            allowed = {str(item["group_id"]) for item in self.monitored_schedules(owner)}
            groups = [event for key, event in events.items() if key in allowed]
            owner["pending_group_changes"] = {}
            if not groups:
                continue
            batch_id = secrets.token_hex(4)
            batch = {"groups": groups}
            owner["change_batches"][batch_id] = batch
            while len(owner["change_batches"]) > 20:
                owner["change_batches"].pop(next(iter(owner["change_batches"])))
            owner["pending"].extend(self._report_items(batch_id, batch))
            if owner.get("send_schedule_on_change", False):
                for group in groups:
                    for week in sorted(group["weeks"]):
                        owner["pending"].append({"kind": "change_card", "source_group_id": group["group_id"],
                                                  "batch_id": batch_id, "date": week})

    def _send_group_change_card(self, owner, group, week):
        source = {**owner, "snapshot": group["snapshot"], "group_name": group["group_name"],
                  "group_id": group["group_id"], "last_fetch_at": group.get("fetched_at"),
                  "last_fetch_failed": False}
        caption = (f"Изменения {group['group_name']} · неделя с {datetime.fromisoformat(week):%d.%m.%Y}"
                   f"\n\n{core.profile_freshness(source)}")
        try:
            png = self.render_profile_card(source, "week", week, group["weeks"][week])
            if owner["channel"] == "telegram":
                self.send_telegram_card(owner["chat_id"], png, caption)
            else:
                self.send_vk_card(png, caption)
        except Exception as exc:
            core.LOG.warning("Не удалось отправить карточку изменений группы %s: %s", group["group_name"], exc)
            for text in core.split_message(caption + "\n\n" + core.week_message(group["snapshot"], week, group["group_name"])):
                self.send_profile_message(owner, text)

    def show_profile_change_batch(self, owner, batch_id):
        batch = owner["change_batches"].get(batch_id)
        if not batch or "groups" not in batch:
            return super().show_profile_change_batch(owner, batch_id)
        if not batch["groups"]:
            self.send_profile_message(owner, "Эти группы больше не отслеживаются. Откройте актуальное расписание.")
            return
        for group in batch["groups"]:
            for week in sorted(group["weeks"]):
                self._send_group_change_card(owner, group, week)

    def drain(self, today):
        for owner in self.active_profiles():
            pending = owner["pending"]
            while pending:
                item = pending[0]
                view, date = item.get("view"), item.get("date", "")
                expired = ((item["kind"] == "daily"
                            and datetime.fromisoformat(date or today).weekday() == 6)
                           or (item["kind"] in ("daily", "refresh") and view == "day" and date < today)
                           or (item["kind"] in ("weekly", "refresh") and view == "week"
                               and (datetime.fromisoformat(date) + timedelta(days=5)).date().isoformat() < today)
                           or (item["kind"] == "refresh" and view == "month" and date[:7] < today[:7]))
                if expired:
                    pending.pop(0)
                    core.save_state(self.state_file, self.state)
                    continue
                try:
                    if item["kind"] == "change_card":
                        batch = owner["change_batches"].get(item.get("batch_id"), {})
                        group = next((g for g in batch.get("groups", [])
                                      if str(g["group_id"]) == str(item.get("source_group_id"))), None)
                        if owner.get("send_schedule_on_change") and group and date in group["weeks"]:
                            self._send_group_change_card(owner, group, date)
                    elif item.get("format") == "card":
                        source = next((profile for profile in self.monitored_schedules(owner)
                                       if str(profile["group_id"]) == str(item.get("source_group_id", owner["group_id"]))), None)
                        if source is not None:
                            self.send_profile_card(source, view or "day", date, replace=item["kind"] == "refresh")
                    elif owner["channel"] == "telegram" and item.get("batch_id"):
                        markup = {"inline_keyboard": [[{"text": "🗓 Показать расписание",
                                                       "callback_data": f"changes:{item['batch_id']}"}]]}
                        self.send_profile_message(owner, item["text"], markup)
                    else:
                        self.send_profile_message(owner, item["text"])
                except Exception as exc:
                    core.LOG.error("Не удалось отправить сообщение подписчику %s: %s",
                                   owner.get("chat_id") or owner["channel"], exc)
                    break
                if item["kind"] == "release":
                    owner["last_release_notified"] = item["release_version"]
                pending.pop(0)
                core.save_state(self.state_file, self.state)

    def _refresh_targets(self):
        groups = defaultdict(list)
        for owner in self.active_profiles():
            if owner.get("onboarding") or not owner.get("group_name") or not owner.get("group_id"):
                continue
            for profile in self.monitored_schedules(owner):
                groups[core.compact_name(profile["group_name"])].append(profile)
        return groups

    def handle_refresh_callback(self, owner, callback):
        action = str(callback["data"])
        message = callback.get("message") or {}
        source = None
        if action != "refresh:settings":
            match = re.fullmatch(r"refresh:(?:(\d+):)?(day|week|month):(\d{4}-\d{2}-\d{2})", action)
            if not match:
                self.answer_callback(callback["id"], "Кнопка устарела")
                return
            group_id, view, date = match.groups()
            if group_id:
                source = next((item for item in self.monitored_schedules(owner)
                               if str(item["group_id"]) == group_id), None)
                if source is None:
                    self.answer_callback(callback["id"], "Группа больше не отслеживается")
                    return
                owner["selected_group_id"] = source["group_id"]
            try:
                datetime.fromisoformat(date)
            except ValueError:
                self.answer_callback(callback["id"], "Кнопка устарела")
                return
        targets = self.monitored_schedules(owner) if action == "refresh:settings" else [source or self.selected_schedule(owner)]
        groups = self._refresh_targets()
        now = datetime.now(core.MOSCOW)
        keys = {core.compact_name(item["group_name"]) for item in targets}
        requested = [groups[key] for key in keys if groups.get(key)
                     and not self.recently_checked(groups[key], now)]
        self.answer_callback(callback["id"], "Проверяю расписание…" if requested else
                             "Повторная проверка доступна через 30 секунд после предыдущей.")
        for peers in requested:
            self.refresh_profiles(peers, now)
        self.flush_group_reports()
        core.save_state(self.state_file, self.state)
        if action == "refresh:settings":
            self.show_settings(owner, message.get("message_id"))
        elif hasattr(self, "show_schedule"):
            self.show_schedule(owner, view, date, message_id=message.get("message_id"),
                               message_kind="photo" if message.get("photo") else "text")
        else:
            self.send_profile_text_view(owner, view, date, message.get("message_id"))

    def cycle(self):
        now = datetime.now(core.MOSCOW)
        self.telegram_commands(now.date().isoformat())
        now = datetime.now(core.MOSCOW)
        for profiles in self._refresh_targets().values():
            needs_initial = any(item.get("snapshot") is None or item.get("welcome_card_pending") for item in profiles)
            if needs_initial or not self.recently_checked(profiles, now):
                self.refresh_profiles(profiles, now)
        self.flush_group_reports()
        self.queue_due(now)
        core.save_state(self.state_file, self.state)
        self.ensure_telegram_keyboards()
        self.drain(now.date().isoformat())
