"""Compact Telegram navigation for daily, weekly and monthly schedules."""
from __future__ import annotations

import calendar
import html
import json
from datetime import date as Date, datetime, timedelta

import bot as core


def month_start(value: str) -> str:
    return Date.fromisoformat(value).replace(day=1).isoformat()


def shift_month(value: str, step: int) -> str:
    day = Date.fromisoformat(value)
    number = day.year * 12 + day.month - 1 + step
    year, month = divmod(number, 12)
    return Date(year, month + 1, 1).isoformat()


def adjacent_day(value: str, step: int) -> str:
    day = Date.fromisoformat(value) + timedelta(days=step)
    while day.weekday() == 6:
        day += timedelta(days=step)
    return day.isoformat()


def month_entries(snapshot: dict, value: str, highlights: dict | None = None) -> list[dict]:
    """Use the whole selected month, including parallel lessons and removed entries."""
    first = month_start(value)
    next_month = shift_month(first, 1)
    changed = (highlights or {}).get("changed") or {}
    result = []
    for removed, items in ((False, snapshot.values()),
                           (True, (highlights or {}).get("removed") or [])):
        for item in items:
            day = core.lesson_date(item)
            if not first <= day < next_month:
                continue
            subject, kind = core.subject_and_type(item)
            result.append({"date": day, "start": item["датаНачала"][11:16],
                           "end": item["датаОкончания"][11:16], "subject": subject,
                           "type": kind, "teacher": item.get("преподаватель", ""),
                           "place": item.get("аудитория", ""),
                           "subgroup": item.get("номерПодгруппы", 0),
                           "change": "УДАЛЕНО" if removed else changed.get(item["код"], ""),
                           "removed": removed})
    return sorted(result, key=lambda item: (item["date"], item["start"], item["subject"], item["place"]))


class NavigationMixin:
    """Telegram views; the scheduler always renders the profile it was given."""

    def _nav_owner(self, profile: dict) -> dict:
        return self.profile_for_chat(profile.get("chat_id")) or profile

    def _nav_schedules(self, owner: dict) -> list[dict]:
        getter = getattr(self, "monitored_schedules", None)
        return getter(owner) if getter else [owner]

    def _nav_selected(self, owner: dict) -> dict:
        getter = getattr(self, "selected_schedule", None)
        return getter(owner) if getter else owner

    def _nav_group(self, owner: dict, group_id: str | int) -> dict | None:
        return next((item for item in self._nav_schedules(owner)
                     if str(item.get("group_id")) == str(group_id)), None)

    def _nav_api(self, method: str, params: dict, files: dict | None = None) -> dict:
        kwargs = {"method": "POST"}
        if files:
            kwargs["files"] = files
        result = core.request_json(f"https://api.telegram.org/bot{self.telegram_token}/{method}",
                                   params, **kwargs)
        if not result.get("ok") and "message is not modified" not in str(result.get("description", "")):
            raise RuntimeError(f"Telegram: {result.get('description')}")
        return result

    @staticmethod
    def _nav_date(view: str, value: str) -> str:
        day = Date.fromisoformat(value)
        if not 2000 <= day.year <= 2100 or view not in ("day", "week", "month"):
            raise ValueError("Unsupported schedule date")
        return month_start(value) if view == "month" else core.week_start(value) if view == "week" else value

    def _nav_markup(self, owner: dict, schedule: dict, view: str, value: str) -> dict:
        group = schedule["group_id"]
        prefix = f"nav:show:{group}:{view}:"
        if view == "day":
            previous, following = adjacent_day(value, -1), adjacent_day(value, 1)
            middle = {"text": "Сегодня", "callback_data": f"nav:today:{group}"}
        else:
            previous = (shift_month(value, -1) if view == "month" else
                        (Date.fromisoformat(value) - timedelta(days=7)).isoformat())
            following = (shift_month(value, 1) if view == "month" else
                         (Date.fromisoformat(value) + timedelta(days=7)).isoformat())
            middle = {"text": "Выбрать день", "callback_data": f"nav:pick:{group}:{view}:{value}"}
        rows = [[{"text": "◀️", "callback_data": prefix + previous}, middle,
                 {"text": "▶️", "callback_data": prefix + following}]]
        if len(self._nav_schedules(owner)) > 1:
            rows.append([{"text": f"Группа · {schedule['group_name']}",
                          "callback_data": f"nav:groups:{view}:{value}"}])
        if schedule.get("last_fetch_failed") or schedule.get("snapshot") is None:
            rows.append([{"text": "🔄 Обновить", "callback_data": f"refresh:{group}:{view}:{value}"}])
        return {"inline_keyboard": rows}

    def _nav_picker(self, schedule: dict, view: str, value: str) -> dict:
        group = schedule["group_id"]
        day = Date.fromisoformat(value)
        if view == "week":
            days = [day + timedelta(days=offset) for offset in range(6)]
            rows = [[{"text": f"{core.WEEKDAY_SHORT[item.weekday()]} {item.day}",
                      "callback_data": f"nav:show:{group}:day:{item.isoformat()}"}
                     for item in days[index:index + 3]] for index in (0, 3)]
        else:
            rows = [[{"text": name, "callback_data": "nav:noop"} for name in core.WEEKDAY_SHORT[:6]]]
            for week in calendar.monthcalendar(day.year, day.month):
                if not any(week[:6]):
                    continue
                rows.append([{"text": str(number) if number else "·",
                              "callback_data": (f"nav:show:{group}:day:{day.replace(day=number).isoformat()}"
                                                if number else "nav:noop")}
                             for number in week[:6]])
        rows.append([{"text": "⬅️ К расписанию", "callback_data": f"nav:show:{group}:{view}:{value}"}])
        return {"inline_keyboard": rows}

    def _nav_track(self, owner: dict, schedule: dict, view: str, value: str,
                   message_id: int | None, kind: str, old_id: int | None = None) -> None:
        if message_id is None:
            return
        # An edited message now represents another day/group, so stale refresh jobs
        # must not restore the picture that used to occupy this message.
        candidates = [owner, *owner.get("watched_groups", {}).values()]
        for candidate in candidates:
            for key, remembered in list(candidate.get("cards", {}).items()):
                ids = remembered if isinstance(remembered, list) else [remembered]
                kept = [item for item in ids if item not in (message_id, old_id)]
                if kept:
                    candidate["cards"][key] = kept
                else:
                    candidate["cards"].pop(key, None)
        key = f"{view}:{value}"
        schedule.setdefault("cards", {}).setdefault(key, []).append(message_id)
        schedule["cards"][key] = schedule["cards"][key][-20:]
        records = owner.setdefault("schedule_messages", {})
        if old_id is not None:
            records.pop(str(old_id), None)
        records[str(message_id)] = {"group_id": schedule["group_id"], "view": view,
                                    "date": value, "kind": kind}
        while len(records) > 200:
            records.pop(next(iter(records)))

    def render_profile_card(self, profile: dict, view: str, date: str,
                            highlights: dict | None = None, layout_override: str | None = None) -> bytes:
        if view == "month":
            from month_card import render_month_card
            return render_month_card(profile["group_name"], month_start(date),
                                     month_entries(profile.get("snapshot") or {}, date, highlights),
                                     core.PAIR_SLOTS, core.card_theme(date, profile.get("seasonal_theme", True)))
        return super().render_profile_card(profile, view, date, highlights, layout_override)

    def _nav_text(self, schedule: dict, view: str, value: str) -> str:
        snapshot = schedule.get("snapshot")
        if snapshot is None:
            return html.escape(core.profile_freshness(schedule))
        if view == "month":
            day = Date.fromisoformat(value)
            days = calendar.monthrange(day.year, day.month)[1]
            lines = [f"<b>МЕСЯЦ {day:%m.%Y} · {html.escape(schedule['group_name'])}</b>"]
            for number in range(1, days + 1):
                current = day.replace(day=number)
                if current.weekday() == 6:
                    continue
                lessons = [item for item in snapshot.values() if core.lesson_date(item) == current.isoformat()]
                count = len({(item["датаНачала"], item["датаОкончания"]) for item in lessons})
                lines.append(f"{core.WEEKDAY_SHORT[current.weekday()]} {number:02d} · " +
                             (core.pairs_label(count) if lessons else "нет записей"))
            message = "\n".join(lines)
        else:
            formatter = core.text_week_view if view == "week" else core.text_day_view
            message, _ = formatter(snapshot, value, schedule["group_name"])
        return message + "\n\n" + html.escape(core.profile_freshness(schedule))

    def _nav_publish(self, owner: dict, schedule: dict, view: str, value: str,
                     message_id: int | None = None, message_kind: str | None = None,
                     format_override: str | None = None, layout_override: str | None = None,
                     track: bool = True, allow_new_on_edit_failure: bool = True) -> int | None:
        value = self._nav_date(view, value)
        mode = format_override or owner.get("display_format", "image")
        if message_id and message_kind and not format_override:
            mode = "image" if message_kind == "photo" else "text"
        if layout_override or view == "month":
            mode = "image"
        if schedule.get("snapshot") is None:
            mode = "text"
        markup = self._nav_markup(owner, schedule, view, value)
        params = {"chat_id": owner["chat_id"], "reply_markup": json.dumps(markup, ensure_ascii=False)}
        text = self._nav_text(schedule, view, value)
        if mode == "text" and len(text) > 4000:
            mode = "image"
        files = None
        kind = "photo" if mode == "image" else "text"
        if mode == "image":
            date_label = (f"на {Date.fromisoformat(value):%d.%m.%Y}" if view == "day" else
                          f"на неделю с {Date.fromisoformat(value):%d.%m.%Y}" if view == "week" else
                          f"на {Date.fromisoformat(value):%m.%Y}")
            caption = f"Расписание {schedule['group_name']} {date_label}"
            if layout_override:
                caption = "Пример · " + caption
            caption += "\n\n" + core.profile_freshness(schedule)
            png = self.render_profile_card(schedule, view, value, layout_override=layout_override)
            if message_id and message_kind != "text":
                method = "editMessageMedia"
                params.update({"message_id": message_id,
                               "media": json.dumps({"type": "photo", "media": "attach://card", "caption": caption},
                                                   ensure_ascii=False)})
                files = {"card": ("raspisanie.png", png, "image/png")}
            else:
                method = "sendPhoto"
                params["caption"] = caption
                files = {"photo": ("raspisanie.png", png, "image/png")}
        else:
            if message_id and message_kind == "photo":
                # Rare transition after an unavailable snapshot; preserve the photo
                # and replace its caption rather than accidentally creating duplicates.
                method = "editMessageCaption"
                params.update({"message_id": message_id, "caption": text[:1000], "parse_mode": "HTML"})
                kind = "photo"
            else:
                method = "editMessageText" if message_id else "sendMessage"
                params.update({"text": text, "parse_mode": "HTML"})
                if message_id:
                    params["message_id"] = message_id
        if message_id and method.startswith("send") and not allow_new_on_edit_failure:
            core.LOG.warning("Автоматическая замена сообщения требует нового сообщения; сохраняю прежнее.")
            return None
        try:
            result = self._nav_api(method, params, files)
        except RuntimeError as exc:
            if not method.startswith("edit"):
                raise
            core.LOG.warning("Не удалось заменить сообщение расписания: %s", exc)
            if not allow_new_on_edit_failure:
                return None
            sent_id = self._nav_publish(owner, schedule, view, value, format_override=mode,
                                        layout_override=layout_override, track=track)
            if track:
                self._nav_track(owner, schedule, view, value, sent_id, kind, old_id=message_id)
                core.save_state(self.state_file, self.state)
            return sent_id
        sent_id = (result.get("result") or {}).get("message_id") if isinstance(result.get("result"), dict) else None
        sent_id = sent_id or (message_id if method.startswith("edit") else None)
        if message_id and method.startswith("send") and sent_id:
            try:
                self._nav_api("deleteMessage", {"chat_id": owner["chat_id"], "message_id": message_id})
            except Exception as exc:
                core.LOG.warning("Не удалось убрать прежнее сообщение расписания: %s", exc)
        if track:
            self._nav_track(owner, schedule, view, value, sent_id, kind, old_id=message_id)
        owner["navigation"] = {"group_id": schedule["group_id"], "view": view, "date": value}
        core.save_state(self.state_file, self.state)
        return sent_id

    def show_schedule(self, profile: dict, view: str, date: str, message_id: int | None = None,
                      message_kind: str | None = None) -> None:
        owner = self._nav_owner(profile)
        schedule = self._nav_selected(owner)
        self._nav_publish(owner, schedule, view, date, message_id, message_kind)

    def send_profile_card(self, profile: dict, view: str, date: str, replace: bool = False,
                          layout_override: str | None = None, track: bool = True) -> None:
        if profile["channel"] != "telegram":
            return super().send_profile_card(profile, view, date, replace, layout_override, track)
        owner = self._nav_owner(profile)
        date = self._nav_date(view, date)
        remembered = profile.get("cards", {}).get(f"{view}:{date}", [])
        ids = list(remembered) if isinstance(remembered, list) else [remembered]
        if replace:
            for message_id in ids:
                record = owner.get("schedule_messages", {}).get(str(message_id), {})
                self._nav_publish(owner, profile, view, date, message_id, record.get("kind", "photo"),
                                  layout_override=layout_override, track=track, allow_new_on_edit_failure=False)
        else:
            self._nav_publish(owner, profile, view, date, layout_override=layout_override, track=track)

    def send_profile_text_view(self, profile: dict, view: str, date: str,
                               message_id: int | None = None) -> None:
        owner = self._nav_owner(profile)
        self._nav_publish(owner, self._nav_selected(owner), view, date, message_id,
                          "text" if message_id else None, format_override="text")

    def settings_view(self, profile: dict) -> tuple[str, dict]:
        message, markup = super().settings_view(profile)
        text = "текст" if profile.get("display_format", "image") == "text" else "картинка"
        markup["inline_keyboard"].append([{"text": f"🖼 Формат дня и недели · {text}",
                                            "callback_data": "settings:format"}])
        return message, markup

    def _nav_format_settings(self, profile: dict, message_id: int | None = None) -> None:
        current = profile.get("display_format", "image")
        rows = [[{"text": ("✅ " if current == mode else "") + label,
                  "callback_data": f"settings:format:{mode}"}]
                for mode, label in (("image", "Картинка"), ("text", "Текст"))]
        rows.append([{"text": "⬅️ К настройкам", "callback_data": "settings:back"}])
        params = {"chat_id": profile["chat_id"],
                  "text": "Формат дня и недели\n\nПрименяется к новым показам и ежедневной отправке. Месяц показывается картинкой.",
                  "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False)}
        if message_id:
            params["message_id"] = message_id
        self._nav_api("editMessageText" if message_id else "sendMessage", params)

    def handle_profile_callback(self, profile: dict, callback: dict) -> None:
        action = str(callback.get("data") or "")
        message = callback.get("message") or {}
        message_id = message.get("message_id")
        kind = "photo" if message.get("photo") else "text"
        if action == "settings:format" or action in ("settings:format:image", "settings:format:text"):
            self.answer_callback(callback["id"])
            if action != "settings:format":
                profile["display_format"] = action.rsplit(":", 1)[1]
                core.save_state(self.state_file, self.state)
            self._nav_format_settings(profile, message_id)
            return
        if action.startswith(("tw:", "td:")):
            _, value = action.split(":", 1)
            try:
                self._nav_date("week" if action.startswith("tw:") else "day", value)
            except ValueError:
                self.answer_callback(callback["id"], "Кнопка устарела")
                return
            self.answer_callback(callback["id"])
            self.send_profile_text_view(profile, "week" if action.startswith("tw:") else "day", value, message_id)
            return
        if action.startswith("refresh:"):
            self.handle_refresh_callback(profile, callback)
            return
        if not action.startswith("nav:"):
            return super().handle_profile_callback(profile, callback)
        if action == "nav:noop":
            self.answer_callback(callback["id"])
            return
        try:
            parts = action.split(":")
            mode = parts[1]
            if mode == "groups":
                _, _, view, value = parts
                value = self._nav_date(view, value)
                rows = [[{"text": item["group_name"],
                          "callback_data": f"nav:group:{item['group_id']}:{view}:{value}"}]
                        for item in self._nav_schedules(profile)]
                current = self._nav_selected(profile)
                rows.append([{"text": "⬅️ К расписанию",
                              "callback_data": f"nav:show:{current['group_id']}:{view}:{value}"}])
                self.answer_callback(callback["id"])
                self._nav_api("editMessageReplyMarkup", {"chat_id": profile["chat_id"], "message_id": message_id,
                                                          "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False)})
                return
            if mode == "today":
                _, _, group_id = parts
                view, value = "day", datetime.now(core.MOSCOW).date().isoformat()
            else:
                _, _, group_id, view, value = parts
            value = self._nav_date(view, value)
            schedule = self._nav_group(profile, group_id)
            if schedule is None or mode not in ("show", "pick", "group", "today"):
                raise ValueError("Group no longer monitored")
            profile["selected_group_id"] = schedule["group_id"]
            self.answer_callback(callback["id"])
            if mode == "pick":
                if view not in ("week", "month"):
                    raise ValueError("Invalid picker")
                self._nav_api("editMessageReplyMarkup", {"chat_id": profile["chat_id"], "message_id": message_id,
                                                          "reply_markup": json.dumps(self._nav_picker(schedule, view, value), ensure_ascii=False)})
            else:
                self._nav_publish(profile, schedule, view, value, message_id, kind)
        except (ValueError, IndexError, OverflowError):
            self.answer_callback(callback["id"], "Кнопка устарела. Откройте расписание заново.")
