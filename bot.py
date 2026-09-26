#!/usr/bin/env python3
"""Уведомления об изменениях расписания ДГТУ для одной группы."""

from __future__ import annotations

import json
import html
import logging
import os
import re
import secrets
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests


BASE_URL = "https://edu.donstu.ru/api"
MOSCOW = timezone(timedelta(hours=3))
LOG = logging.getLogger("rasp_bot")
STOP = False
HTTP = requests.Session()
HTTP.trust_env = False


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value.startswith(('"', "'")) and value.endswith(value[0]):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def proxy_for_url(url: str) -> str:
    host = urlsplit(url).hostname
    specific_key = {
        "api.telegram.org": "TELEGRAM_PROXY_URL",
        "edu.donstu.ru": "DONSTU_PROXY_URL",
        "api.vk.com": "VK_PROXY_URL",
    }.get(host, "")
    proxy = os.getenv(specific_key, "") if specific_key else ""
    key = specific_key if proxy else "PROXY_URL"
    proxy = proxy or os.getenv("PROXY_URL", "")
    if proxy:
        # Формат из панели прокси часто не содержит http://.
        if "://" not in proxy:
            proxy = "http://" + proxy
        try:
            parsed = urlsplit(proxy)
            valid = parsed.scheme in {"http", "https", "socks5", "socks5h"} and parsed.hostname and parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError(f"Неверный адрес прокси в {key}")
    return proxy


def request_json(url: str, params: dict | None = None, *, method: str = "GET", timeout: int = 25,
                 files: dict | None = None, proxy_override: str | None = None) -> dict:
    host = urlsplit(url).hostname
    proxy = proxy_override if proxy_override is not None else proxy_for_url(url)
    try:
        response = HTTP.request(
            method, url,
            params=params if method == "GET" else None,
            data=params if method == "POST" else None,
            files=files,
            headers={"Accept": "application/json", "User-Agent": "RaspBot/1.1 (personal schedule notifier)"},
            proxies={"http": proxy, "https": proxy} if proxy else {},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        # Исключение requests иногда содержит URL прокси с логином и паролем.
        raise RuntimeError(f"Ошибка сети при обращении к {host}: {type(exc).__name__}") from None
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} от {host}")
    body = response.content
    if len(body) > 2_000_000:
        raise RuntimeError("Слишком большой ответ API")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("API вернул не JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Неверный формат ответа API")
    return result


def compact_name(name: str) -> str:
    return re.sub(r"[^А-ЯA-Z0-9]", "", name.upper().replace("Ё", "Е"))


def find_group_id(name: str) -> int:
    # Поиск не понимает дефис, поэтому запрашиваем буквенную часть имени.
    query = re.match(r"[А-ЯA-Z]+", compact_name(name))
    response = request_json(f"{BASE_URL}/search", {"searchQuery": query.group() if query else name})
    results = (response.get("data") or {}).get("results") or []
    matches = [x for x in results if x.get("type") == "Группа" and compact_name(x.get("name", "")) == compact_name(name)]
    if len(matches) != 1:
        raise RuntimeError(f"Не удалось однозначно найти группу {name} через API поиска")
    return int(matches[0]["objectID"])


FIELDS = (
    "датаНачала", "датаОкончания", "дисциплина", "преподаватель", "аудитория",
    "номерПодгруппы", "замена", "ссылка", "тема",
)

ALERT_LABELS = {
    "added": "➕ Добавление", "removed": "➖ Удаление",
    "time": "🕒 Дата и время", "room": "📍 Аудитория",
    "teacher": "👤 Преподаватель", "subject": "📚 Предмет",
    "subgroup": "👥 Подгруппа", "replacement": "🔄 Замена",
    "other": "📝 Тема и ссылка",
}
DEFAULT_ALERT_TYPES = tuple(ALERT_LABELS)
FIELD_ALERT_TYPE = {
    "датаНачала": "time", "датаОкончания": "time",
    "дисциплина": "subject", "преподаватель": "teacher",
    "аудитория": "room", "номерПодгруппы": "subgroup",
    "замена": "replacement", "ссылка": "other", "тема": "other",
}


def normalize_lesson(raw: dict) -> dict:
    if not isinstance(raw, dict) or not raw.get("код") or not raw.get("датаНачала"):
        raise RuntimeError("В расписании найдено занятие без кода или даты")
    # Сравниваем только данные, значимые для студента. Служебные поля API меняются отдельно.
    item = {"код": str(raw["код"])}
    for field in FIELDS:
        item[field] = raw.get(field) or (0 if field == "номерПодгруппы" else "")
    item["замена"] = bool(raw.get("замена"))
    return item


def fetch_schedule(group_id: int, group_name: str) -> dict[str, dict]:
    response = request_json(f"{BASE_URL}/Rasp", {"idGroup": group_id})
    if response.get("state") != 1 or not isinstance(response.get("data"), dict):
        raise RuntimeError(f"API расписания вернул ошибку: {response.get('msg')}")
    data = response["data"]
    info = data.get("info") or {}
    group = info.get("group") or {}
    if compact_name(group.get("name", "")) != compact_name(group_name):
        raise RuntimeError(f"API вернул другую группу: {group.get('name')!r}")
    lessons = data.get("rasp")
    if not isinstance(lessons, list):
        raise RuntimeError("В ответе API нет списка занятий")
    result = {}
    for raw in lessons:
        item = normalize_lesson(raw)
        if item["код"] in result:
            raise RuntimeError(f"Повторяющийся код занятия: {item['код']}")
        result[item["код"]] = item
    return result


def lesson_date(item: dict) -> str:
    return item["датаНачала"][:10]


def lesson_sort(item: dict) -> tuple:
    return (item["датаНачала"], item.get("номерПодгруппы", 0), item["код"])


def schedule_changes(old: dict[str, dict], new: dict[str, dict]) -> list[tuple[dict | None, dict | None]]:
    """Сравнить занятия по содержимому, даже если ДГТУ поменял их коды."""
    before_left, after_left = dict(old), dict(new)
    after_by_content: dict[str, list[str]] = defaultdict(list)
    for code, item in sorted(after_left.items()):
        signature = json.dumps([item.get(field) for field in FIELDS], ensure_ascii=False,
                               sort_keys=True, default=str)
        after_by_content[signature].append(code)
    for code, item in sorted(before_left.items()):
        signature = json.dumps([item.get(field) for field in FIELDS], ensure_ascii=False,
                               sort_keys=True, default=str)
        matches = after_by_content.get(signature)
        if matches:
            before_left.pop(code)
            after_left.pop(matches.pop(0))

    changes: list[tuple[dict | None, dict | None]] = []
    def match_unique(key_for) -> None:
        before_groups: dict[tuple, list[str]] = defaultdict(list)
        after_groups: dict[tuple, list[str]] = defaultdict(list)
        for code, item in before_left.items():
            before_groups[key_for(item)].append(code)
        for code, item in after_left.items():
            after_groups[key_for(item)].append(code)
        for key in before_groups.keys() & after_groups.keys():
            if len(before_groups[key]) == len(after_groups[key]) == 1:
                changes.append((before_left.pop(before_groups[key][0]),
                                after_left.pop(after_groups[key][0])))

    # Сначала используем наиболее точные признаки, чтобы не смешивать
    # параллельные занятия по одному предмету в одном временном слоте.
    match_unique(lambda item: (item["датаНачала"], item["датаОкончания"],
                                   item["дисциплина"], item["преподаватель"],
                                   item["номерПодгруппы"], item["аудитория"]))
    match_unique(lambda item: (item["датаНачала"], item["датаОкончания"],
                                   item["дисциплина"], item["преподаватель"],
                                   item["номерПодгруппы"]))
    match_unique(lambda item: (item["датаНачала"], item["датаОкончания"],
                                   item["дисциплина"], item["аудитория"],
                                   item["номерПодгруппы"]))
    match_unique(lambda item: (item["датаНачала"], item["датаОкончания"],
                                   item["дисциплина"], item["преподаватель"],
                                   item["аудитория"]))
    # Затем связываем пару в том же слоте и перенос пары в пределах дня.
    match_unique(lambda item: (item["датаНачала"], item["датаОкончания"],
                                   item["дисциплина"], item["номерПодгруппы"]))
    match_unique(lambda item: (lesson_date(item), item["дисциплина"],
                                   item["преподаватель"], item["номерПодгруппы"]))
    # Если в том же слоте сменили предмет и код, покажем изменение предмета.
    match_unique(lambda item: (item["датаНачала"], item["датаОкончания"],
                                   item["преподаватель"], item["номерПодгруппы"]))
    # Сохранившийся код помогает связать правки, затронувшие несколько полей сразу.
    for code in sorted(before_left.keys() & after_left.keys()):
        changes.append((before_left.pop(code), after_left.pop(code)))
    changes.extend((item, None) for item in before_left.values())
    changes.extend((None, item) for item in after_left.values())
    return sorted(changes, key=lambda pair: lesson_sort(pair[1] or pair[0]))


def change_types(before: dict | None, after: dict | None) -> set[str]:
    if before is None:
        return {"added"}
    if after is None:
        return {"removed"}
    return {FIELD_ALERT_TYPE[field] for field in FIELDS
            if before.get(field) != after.get(field)}


def allowed_change(before: dict | None, after: dict | None,
                   alert_types: set[str] | None = None) -> bool:
    return bool(change_types(before, after) & (alert_types if alert_types is not None
                                               else set(DEFAULT_ALERT_TYPES)))


def profile_alert_types(profile: dict) -> set[str]:
    selected = profile.get("alert_types")
    return (set(DEFAULT_ALERT_TYPES) if selected is None else
            set(selected) & set(ALERT_LABELS))


def parse_alert_types(value: str) -> list[str]:
    chosen = {part.strip().lower() for part in value.split(",") if part.strip()}
    if not chosen or chosen == {"all"}:
        return list(DEFAULT_ALERT_TYPES)
    if chosen == {"none"}:
        return []
    unknown = chosen - set(ALERT_LABELS)
    if unknown:
        raise RuntimeError("Неизвестные виды уведомлений в VK_ALERT_TYPES: " + ", ".join(sorted(unknown)))
    return [name for name in DEFAULT_ALERT_TYPES if name in chosen]


LESSON_TYPES = {"лек": "Лекция", "пр": "Практика", "лаб": "Лабораторная"}
LESSON_TYPES_PLURAL = {"Лекция": "лекции", "Практика": "практики", "Лабораторная": "лабораторные"}
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
# Семь временных слотов, встречающихся в расписании ВКБ51, включая вечернюю пару.
PAIR_SLOTS = (("08:30", "10:05"), ("10:15", "11:50"), ("12:00", "13:35"),
              ("14:15", "15:50"), ("16:00", "17:35"), ("17:45", "19:20"),
              ("19:30", "21:05"))
PAIR_INDEX = {start: index for index, (start, _) in enumerate(PAIR_SLOTS)}
BUTTON_TODAY = "📅 Сегодня"
BUTTON_TOMORROW = "➡️ Завтра"
BUTTON_PREVIOUS_WEEK = "◀️ Неделя"
BUTTON_CURRENT_WEEK = "🗓 Эта неделя"
BUTTON_NEXT_WEEK = "Неделя ▶️"
BUTTON_TEXT = "📝 Текст"
BUTTON_SETTINGS = "⚙️ Настройки"


def telegram_keyboard() -> dict:
    return {
        "keyboard": [[{"text": BUTTON_TODAY}, {"text": BUTTON_TOMORROW}],
                     [{"text": BUTTON_PREVIOUS_WEEK}, {"text": BUTTON_NEXT_WEEK}],
                     [{"text": BUTTON_CURRENT_WEEK}, {"text": BUTTON_TEXT}],
                     [{"text": BUTTON_SETTINGS}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def week_start(date: str) -> str:
    day = datetime.fromisoformat(date).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def card_theme(date: str, seasonal: bool = True) -> str:
    """Осенняя палитра действует для карточек сентября–ноября."""
    return "autumn" if seasonal and datetime.fromisoformat(date).month in (9, 10, 11) else "classic"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"{name} должен быть true или false")


def valid_time(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value))


def slot_index(item: dict) -> int | None:
    return PAIR_INDEX.get(item["датаНачала"][11:16])


def pairs_label(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "пара"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "пары"
    else:
        word = "пар"
    return f"{count} {word}"


def subject_and_type(item: dict) -> tuple[str, str]:
    raw = str(item.get("дисциплина") or "Без названия").strip()
    match = re.match(r"^(лек|пр|лаб)\s+(.+)$", raw, flags=re.IGNORECASE)
    return (match.group(2).strip(), LESSON_TYPES[match.group(1).lower()]) if match else (raw, "Занятие")


def lesson_details(item: dict) -> list[str]:
    details = []
    if item.get("преподаватель"):
        details.append(f"Преподаватель: {item['преподаватель']}")
    if item.get("аудитория"):
        details.append(f"Место: {item['аудитория']}")
    if item.get("номерПодгруппы"):
        details.append(f"Подгруппа: {item['номерПодгруппы']}")
    if item.get("замена"):
        details.append("Замена")
    if item.get("тема"):
        details.append(f"Тема: {item['тема']}")
    if item.get("ссылка"):
        details.append(f"Ссылка: {item['ссылка']}")
    return details


def lesson_text(item: dict) -> str:
    start = item["датаНачала"][11:16]
    end = item["датаОкончания"][11:16]
    subject, lesson_type = subject_and_type(item)
    return "\n".join([f"{start}–{end} · {lesson_type}", subject, *lesson_details(item)])


def same_lesson(a: dict, b: dict) -> bool:
    return all(a.get(field) == b.get(field) for field in FIELDS if field not in ("датаНачала", "датаОкончания"))


def lesson_blocks(lessons: list[dict], change_kinds: dict[str, str] | None = None) -> list[list[dict]]:
    blocks = []
    for item in lessons:
        previous = blocks[-1][-1] if blocks else None
        adjacent = (previous is not None and slot_index(previous) is not None
                    and slot_index(item) == slot_index(previous) + 1)
        same_marker = (previous is None or not change_kinds
                       or change_kinds.get(previous["код"]) == change_kinds.get(item["код"]))
        if adjacent and same_marker and same_lesson(previous, item) and item["датаНачала"] > previous["датаНачала"]:
            blocks[-1].append(item)
        else:
            blocks.append([item])
    return blocks


def timeline_entries(lessons: list[dict], change_kinds: dict[str, str] | None = None) -> list[tuple[str, object]]:
    """Группы занятий и пустые стандартные слоты между первой и последней парой."""
    entries: list[tuple[str, object]] = []
    previous_slot: int | None = None
    for block in lesson_blocks(lessons, change_kinds):
        first_slot, last_slot = slot_index(block[0]), slot_index(block[-1])
        if previous_slot is not None and first_slot is not None and first_slot > previous_slot + 1:
            entries.append(("window", (previous_slot + 1, first_slot - 1)))
        entries.append(("lesson", block))
        if last_slot is not None:
            previous_slot = max(previous_slot if previous_slot is not None else -1, last_slot)
    return entries


def window_text(start_slot: int, end_slot: int) -> str:
    label = (f"{start_slot + 1}-я пара" if start_slot == end_slot
             else f"пары {start_slot + 1}–{end_slot + 1}")
    return f"Окно: {label} · {PAIR_SLOTS[start_slot][0]}–{PAIR_SLOTS[end_slot][1]}"


def block_text(block: list[dict]) -> str:
    if len(block) == 1:
        return lesson_text(block[0])
    first, last = block[0], block[-1]
    subject, lesson_type = subject_and_type(first)
    starts = ", ".join(item["датаНачала"][11:16] for item in block)
    lines = [
        f"{first['датаНачала'][11:16]}–{last['датаОкончания'][11:16]} · {pairs_label(len(block))} · {LESSON_TYPES_PLURAL.get(lesson_type, 'занятия')}",
        subject,
        f"Начало пар: {starts}",
        *lesson_details(first),
    ]
    return "\n".join(lines)


def day_message(snapshot: dict[str, dict], date: str, group_name: str) -> str:
    day = datetime.fromisoformat(date)
    heading = f"{WEEKDAYS[day.weekday()]}, {day:%d.%m.%Y} · {group_name}"
    lessons = sorted((x for x in snapshot.values() if lesson_date(x) == date), key=lesson_sort)
    if not lessons:
        return f"📅 {heading}\nЗанятий нет."
    slots = len({(x["датаНачала"], x["датаОкончания"]) for x in lessons})
    details = [block_text(value) if kind == "lesson" else window_text(*value)
               for kind, value in timeline_entries(lessons)]
    return f"📅 {heading}\nВсего: {pairs_label(slots)}\n\n" + "\n\n".join(details)


def card_blocks(lessons: list[dict], change_kinds: dict[str, str] | None = None) -> list[dict]:
    blocks = []
    for kind, value in timeline_entries(lessons, change_kinds):
        if kind == "window":
            start_slot, end_slot = value
            blocks.append({"window": True, "start": PAIR_SLOTS[start_slot][0],
                           "end": PAIR_SLOTS[end_slot][1],
                           "pairs": list(range(start_slot + 1, end_slot + 2))})
            continue
        group = value
        first, last = group[0], group[-1]
        subject, lesson_type = subject_and_type(first)
        blocks.append({
            "start": first["датаНачала"][11:16],
            "end": last["датаОкончания"][11:16],
            "codes": [x["код"] for x in group],
            "starts": [x["датаНачала"][11:16] for x in group],
            "pairs": [slot_index(x) + 1 if slot_index(x) is not None else None for x in group],
            "subject": subject,
            "type": lesson_type,
            "teacher": first.get("преподаватель", ""),
            "place": first.get("аудитория", ""),
            "subgroup": first.get("номерПодгруппы", 0),
            "theme": first.get("тема", ""),
            "link": first.get("ссылка", ""),
        })
    return blocks


def schedule_card(snapshot: dict[str, dict], date: str, group_name: str,
                  seasonal: bool = True) -> bytes:
    from card import render_card

    lessons = sorted((x for x in snapshot.values() if lesson_date(x) == date), key=lesson_sort)
    slots = len({(x["датаНачала"], x["датаОкончания"]) for x in lessons})
    return render_card(group_name, date, card_blocks(lessons), slots, card_theme(date, seasonal))


def week_card(snapshot: dict[str, dict], monday: str, group_name: str,
              highlights: dict | None = None, seasonal: bool = True) -> bytes:
    from card import render_week_card

    days = []
    start = datetime.fromisoformat(monday).date()
    for offset in range(7):
        date = (start + timedelta(days=offset)).isoformat()
        lessons = sorted((x for x in snapshot.values() if lesson_date(x) == date), key=lesson_sort)
        slots = len({(x["датаНачала"], x["датаОкончания"]) for x in lessons})
        changed = (highlights or {}).get("changed") or {}
        blocks = card_blocks(lessons, changed)
        if highlights:
            removed_for_day = [item for item in highlights.get("removed") or []
                               if lesson_date(item) == date]
            removed_pairs = {slot_index(item) + 1 for item in removed_for_day
                             if slot_index(item) is not None}
            if removed_pairs:
                without_duplicate_windows = []
                for block in blocks:
                    if not block.get("window"):
                        without_duplicate_windows.append(block)
                        continue
                    for pair in block["pairs"]:
                        if pair not in removed_pairs:
                            without_duplicate_windows.append({
                                "window": True, "start": PAIR_SLOTS[pair - 1][0],
                                "end": PAIR_SLOTS[pair - 1][1], "pairs": [pair]})
                blocks = without_duplicate_windows
            for block in blocks:
                if not block.get("window"):
                    marks = [changed[code] for code in block["codes"] if code in changed]
                    if marks:
                        block["change"] = marks[0] if len(set(marks)) == 1 else "ИЗМЕНЕНО"
            for old_item in removed_for_day:
                subject, lesson_type = subject_and_type(old_item)
                blocks.append({"removed": True, "change": "УДАЛЕНО",
                               "start": old_item["датаНачала"][11:16],
                               "end": old_item["датаОкончания"][11:16],
                               "pairs": [slot_index(old_item) + 1 if slot_index(old_item) is not None else None],
                               "subject": subject, "teacher": old_item.get("преподаватель", ""),
                               "place": old_item.get("аудитория", ""), "type": lesson_type})
            blocks.sort(key=lambda block: (block["start"], 0 if block.get("removed") else 1))
        days.append({"date": date, "blocks": blocks, "slots": slots})
    return render_week_card(group_name, monday, days, card_theme(monday, seasonal))


def week_entries(snapshot: dict[str, dict], monday: str,
                 highlights: dict | None = None) -> list[dict]:
    first_day = datetime.fromisoformat(monday).date()
    changed = (highlights or {}).get("changed") or {}
    entries = []
    for item in snapshot.values():
        date = lesson_date(item)
        if not monday <= date <= (first_day + timedelta(days=6)).isoformat():
            continue
        subject, kind = subject_and_type(item)
        entries.append({"date": date, "start": item["датаНачала"][11:16],
                        "end": item["датаОкончания"][11:16], "subject": subject,
                        "type": kind, "teacher": item.get("преподаватель", ""),
                        "place": item.get("аудитория", ""),
                        "subgroup": item.get("номерПодгруппы", 0),
                        "change": changed.get(item["код"], "")})
    for item in (highlights or {}).get("removed") or []:
        date = lesson_date(item)
        if monday <= date <= (first_day + timedelta(days=6)).isoformat():
            subject, kind = subject_and_type(item)
            entries.append({"date": date, "start": item["датаНачала"][11:16],
                            "end": item["датаОкончания"][11:16], "subject": subject,
                            "type": kind, "teacher": item.get("преподаватель", ""),
                            "place": item.get("аудитория", ""), "change": "УДАЛЕНО", "removed": True})
    return entries


def horizontal_week_card(snapshot: dict[str, dict], monday: str, group_name: str,
                         highlights: dict | None = None, seasonal: bool = True) -> bytes:
    from horizontal_card import render_horizontal_week_card

    return render_horizontal_week_card(group_name, monday, week_entries(snapshot, monday, highlights), PAIR_SLOTS,
                                       card_theme(monday, seasonal))


def list_week_card(snapshot: dict[str, dict], monday: str, group_name: str,
                   highlights: dict | None = None, seasonal: bool = True) -> bytes:
    from list_card import render_list_week_card

    return render_list_week_card(group_name, monday, week_entries(snapshot, monday, highlights), PAIR_SLOTS,
                                 card_theme(monday, seasonal))


def future_change_weeks(old: dict[str, dict], new: dict[str, dict], today: str,
                        alert_types: set[str] | None = None) -> dict[str, dict]:
    weeks: dict[str, dict] = {}
    for before, after in schedule_changes(old, new):
        if not allowed_change(before, after, alert_types):
            continue
        if after and lesson_date(after) > today:
            monday = week_start(lesson_date(after))
            marker = weeks.setdefault(monday, {"changed": {}, "removed": []})
            if before is None or lesson_date(before) != lesson_date(after):
                change = "ДОБАВЛЕНО"
            elif change_types(before, after) == {"room"}:
                change = "АУДИТОРИЯ ИЗМЕНЕНА"
            else:
                change = "ИЗМЕНЕНО"
            marker["changed"][after["код"]] = change
        if before and lesson_date(before) > today and (after is None or lesson_date(after) != lesson_date(before)):
            monday = week_start(lesson_date(before))
            weeks.setdefault(monday, {"changed": {}, "removed": []})["removed"].append(before)
    return weeks


def week_message(snapshot: dict[str, dict], monday: str, group_name: str) -> str:
    start = datetime.fromisoformat(monday).date()
    lines = [f"📅 Неделя {start:%d.%m}–{(start + timedelta(days=6)):%d.%m.%Y} · {group_name}"]
    for offset in range(7):
        date = (start + timedelta(days=offset)).isoformat()
        lines.append(day_message(snapshot, date, group_name))
    return "\n\n".join(lines)


def text_week_view(snapshot: dict[str, dict], monday: str, group_name: str) -> tuple[str, dict]:
    start = datetime.fromisoformat(monday).date()
    end = start + timedelta(days=6)
    lines = [f"<b>НЕДЕЛЯ {start:%d.%m}–{end:%d.%m.%Y} · {html.escape(group_name)}</b>", ""]
    buttons = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        lessons = sorted((item for item in snapshot.values() if lesson_date(item) == day.isoformat()),
                         key=lesson_sort)
        slots = len({(item["датаНачала"], item["датаОкончания"]) for item in lessons})
        if lessons:
            period = f"{lessons[0]['датаНачала'][11:16]}–{max(item['датаОкончания'][11:16] for item in lessons)}"
            detail = f"{pairs_label(slots)} · {period}"
        else:
            detail = "нет пар"
        lines.append(f"<b>{WEEKDAY_SHORT[offset]} {day:%d.%m}</b> · {html.escape(detail)}")
        if offset % 4 == 0:
            buttons.append([])
        buttons[-1].append({"text": f"{WEEKDAY_SHORT[offset]} {day.day}",
                            "callback_data": f"td:{day.isoformat()}"})
    available = sorted({week_start(lesson_date(item)) for item in snapshot.values()})
    arrows = []
    if available and (start - timedelta(days=7)).isoformat() >= available[0]:
        arrows.append({"text": "◀️ Неделя", "callback_data": f"tw:{(start - timedelta(days=7)).isoformat()}"})
    if available and (start + timedelta(days=7)).isoformat() <= available[-1]:
        arrows.append({"text": "Неделя ▶️", "callback_data": f"tw:{(start + timedelta(days=7)).isoformat()}"})
    if arrows:
        buttons.append(arrows)
    return "\n".join(lines), {"inline_keyboard": buttons}


def text_day_view(snapshot: dict[str, dict], date: str, group_name: str) -> tuple[str, dict]:
    day = datetime.fromisoformat(date).date()
    lessons = sorted((item for item in snapshot.values() if lesson_date(item) == date), key=lesson_sort)
    slots = len({(item["датаНачала"], item["датаОкончания"]) for item in lessons})
    lines = [f"<b>{WEEKDAYS[day.weekday()].upper()} · {day:%d.%m.%Y} · {html.escape(group_name)}</b>",
             f"{pairs_label(slots)}" if lessons else "Занятий нет"]
    symbols = {"Лекция": "🟢", "Практика": "🟠", "Лабораторная": "🟣"}
    for block in card_blocks(lessons):
        lines.append("")
        if block.get("window"):
            numbers = block["pairs"]
            label = f"{numbers[0]}-я пара" if len(numbers) == 1 else f"пары {numbers[0]}–{numbers[-1]}"
            lines.append(f"⏳ <b>ОКНО</b> · {label} · {block['start']}–{block['end']}")
            continue
        numbers = [number for number in block["pairs"] if number]
        label = (f"{numbers[0]} пара" if len(numbers) == 1 else f"{numbers[0]}–{numbers[-1]} пары") if numbers else "Пара"
        lines.append(f"{symbols.get(block['type'], '🔵')} <b>{label} · {block['start']}–{block['end']}</b>")
        lines.append(f"<b>{html.escape(block['subject'])}</b> · {html.escape(block['type'].lower())}")
        if len(block["starts"]) > 1:
            starts = " · ".join(f"{number}-я {start}" if number else start
                                for number, start in zip(block["pairs"], block["starts"]))
            lines.append(f"По парам: {starts}")
        if block.get("place"):
            lines.append(f"📍 <b>{html.escape(str(block['place']))}</b>")
        if block.get("teacher"):
            lines.append(f"👤 {html.escape(str(block['teacher']))}")
        if block.get("subgroup"):
            lines.append(f"Подгруппа {block['subgroup']}")
    monday = week_start(date)
    day_buttons = []
    if day.weekday() > 0:
        day_buttons.append({"text": "◀️ День", "callback_data": f"td:{(day - timedelta(days=1)).isoformat()}"})
    day_buttons.append({"text": "🗓 К неделе", "callback_data": f"tw:{monday}"})
    if day.weekday() < 6:
        day_buttons.append({"text": "День ▶️", "callback_data": f"td:{(day + timedelta(days=1)).isoformat()}"})
    buttons = {"inline_keyboard": [day_buttons]}
    return "\n".join(lines), buttons


LABELS = {
    "датаНачала": "начало", "датаОкончания": "конец", "дисциплина": "предмет",
    "преподаватель": "преподаватель", "аудитория": "аудитория",
    "номерПодгруппы": "подгруппа", "замена": "замена", "ссылка": "ссылка", "тема": "тема",
}


def change_value(field: str, value: object) -> str:
    if field in ("датаНачала", "датаОкончания") and isinstance(value, str) and value:
        return datetime.fromisoformat(value).strftime("%d.%m %H:%M")
    if field == "замена":
        return "да" if value else "нет"
    return str(value) if value else "—"


def changes_message(old: dict[str, dict], new: dict[str, dict], today: str,
                    group_name: str, alert_types: set[str] | None = None) -> str:
    lines = []
    enabled = alert_types if alert_types is not None else set(DEFAULT_ALERT_TYPES)
    for before, after in schedule_changes(old, new):
        if max(lesson_date(x) for x in (before, after) if x) < today:
            continue
        if not allowed_change(before, after, enabled):
            continue
        if before is None:
            lines.append("➕ Добавлено: " + datetime.fromisoformat(lesson_date(after)).strftime("%d.%m.%Y") + "\n" + lesson_text(after))
        elif after is None:
            lines.append("➖ Удалено: " + datetime.fromisoformat(lesson_date(before)).strftime("%d.%m.%Y") + "\n" + lesson_text(before))
        else:
            details = [f"{LABELS[key]}: {change_value(key, before.get(key))} → {change_value(key, after.get(key))}"
                       for key in FIELDS if FIELD_ALERT_TYPE[key] in enabled
                       and before.get(key) != after.get(key)]
            lines.append("✏️ Изменено: " + datetime.fromisoformat(lesson_date(after)).strftime("%d.%m.%Y")
                         + "\n" + lesson_text(after) + "\n    " + "; ".join(details))
    if not lines:
        return ""
    if len(lines) > 25:
        lines = lines[:25] + [f"…и ещё {len(lines) - 25} изменений. Проверьте расписание на сайте."]
    return f"🔔 Изменения в расписании {group_name}\n\n" + "\n\n".join(lines)


def split_message(message: str, limit: int = 3500) -> list[str]:
    parts, current = [], ""
    for line in message.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current.rstrip())
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current.rstrip())
            current = ""
        current += line
    if current.strip():
        parts.append(current.rstrip())
    return parts


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"snapshot": None, "pending": {"telegram": [], "vk": []},
                "daily_queued": {}, "weekly_queued": {}, "cards": {},
                "card_tracking_ready": True, "telegram_offset": 0,
                "telegram_keyboard_version": 0, "change_batches": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("pending"), dict):
        raise RuntimeError("Повреждён файл состояния; восстановите его из копии")
    state.setdefault("daily_queued", {})
    state.setdefault("weekly_queued", {})
    state.setdefault("card_tracking_ready", "cards" in state)
    state.setdefault("cards", {})
    state.setdefault("telegram_offset", 0)
    state.setdefault("telegram_keyboard_version", 0)
    state.setdefault("change_batches", {})
    state.setdefault("snapshot", None)
    for channel in ("telegram", "vk"):
        state["pending"].setdefault(channel, [])
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Bot:
    def __init__(self, root: Path):
        load_env(root / ".env")
        self.root = root
        self.group_name = os.getenv("GROUP_NAME", "ВКБ51")
        self.group_id = int(os.getenv("GROUP_ID", "72244"))
        self.daily_time = os.getenv("DAILY_TIME", "08:00")
        if not valid_time(self.daily_time):
            raise RuntimeError("DAILY_TIME должен быть в формате ЧЧ:ММ")
        self.week_layout = os.getenv("WEEK_LAYOUT", "horizontal").strip().lower()
        if self.week_layout not in ("horizontal", "vertical", "list"):
            raise RuntimeError("WEEK_LAYOUT должен быть horizontal, vertical или list")
        self.seasonal_theme = env_bool("SEASONAL_THEME", True)
        self.allow_multiple_users = env_bool("ALLOW_MULTIPLE_USERS", True)
        self.telegram_access = os.getenv("TELEGRAM_ACCESS", "public").strip().lower()
        if self.telegram_access not in ("public", "code"):
            raise RuntimeError("TELEGRAM_ACCESS должен быть public или code")
        self.interval = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "300")))
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.vk_token = os.getenv("VK_COMMUNITY_TOKEN", "")
        self.vk_peer_id = os.getenv("VK_PEER_ID", "")
        self.pair_code = os.getenv("TELEGRAM_PAIR_CODE", "")
        state_file = Path(os.getenv("STATE_FILE", "data/state.json"))
        self.state_file = state_file if state_file.is_absolute() else root / state_file
        self.state = load_state(self.state_file)
        self.group_id = int(self.state.get("group_id", self.group_id))
        configured_chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if configured_chat:
            new_chat_id = int(configured_chat)
            if self.state.get("telegram_chat_id") != new_chat_id:
                self.state["telegram_keyboard_version"] = 0
                self.state.pop("telegram_week_cursor", None)
            self.state["telegram_chat_id"] = new_chat_id
        if self.telegram_token and self.telegram_access == "code" and self.pair_code == "замените-на-свой-секретный-код":
            raise RuntimeError("Замените пример TELEGRAM_PAIR_CODE в .env на свой код")
        has_saved_subscriber = any(item.get("channel") == "telegram"
                                   for item in self.state.get("profiles", {}).values())
        if (self.telegram_token and self.telegram_access == "code" and
                not self.state.get("telegram_chat_id") and not has_saved_subscriber and not self.pair_code):
            raise RuntimeError("Для TELEGRAM_ACCESS=code укажите TELEGRAM_CHAT_ID или TELEGRAM_PAIR_CODE")
        if self.vk_token and not self.vk_peer_id:
            raise RuntimeError("Для VK_COMMUNITY_TOKEN нужен VK_PEER_ID")
        if not self.telegram_token and not self.vk_token:
            raise RuntimeError("Настройте Telegram или ВКонтакте в .env")

    def channels(self) -> list[str]:
        result = []
        if self.telegram_token and self.state.get("telegram_chat_id"):
            result.append("telegram")
        if self.vk_token and self.vk_peer_id:
            result.append("vk")
        return result

    def send(self, channel: str, message: str) -> None:
        if channel == "telegram":
            self.send_telegram_chat(self.state["telegram_chat_id"], message)
        else:
            response = request_json(
                "https://api.vk.com/method/messages.send",
                {
                    "access_token": self.vk_token, "v": "5.199", "peer_id": self.vk_peer_id,
                    "random_id": secrets.randbelow(2**31), "message": message,
                }, method="POST",
            )
            if "error" in response:
                raise RuntimeError(f"ВКонтакте: {response['error'].get('error_msg')}")

    def send_telegram_chat(self, chat_id: int, message: str, inline_markup: dict | None = None) -> None:
        params = {"chat_id": chat_id, "text": message}
        if inline_markup is not None:
            params["reply_markup"] = json.dumps(inline_markup, ensure_ascii=False)
        elif chat_id == self.state.get("telegram_chat_id"):
            params["reply_markup"] = json.dumps(telegram_keyboard(), ensure_ascii=False)
        response = request_json(
            f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
            params, method="POST",
        )
        if not response.get("ok"):
            raise RuntimeError(f"Telegram: {response.get('description')}")
        if inline_markup is None and chat_id == self.state.get("telegram_chat_id"):
            self.state["telegram_keyboard_version"] = 1

    def send_text_view(self, chat_id: int, view: str, date: str, message_id: int | None = None) -> None:
        snapshot = self.state.get("snapshot")
        if snapshot is None:
            self.send_telegram_chat(chat_id, "Расписание пока не загружено.")
            return
        message, markup = (text_week_view(snapshot, date, self.group_name) if view == "week"
                           else text_day_view(snapshot, date, self.group_name))
        method = "editMessageText" if message_id else "sendMessage"
        params = {"chat_id": chat_id, "text": message, "parse_mode": "HTML",
                  "reply_markup": json.dumps(markup, ensure_ascii=False)}
        if message_id:
            params["message_id"] = message_id
        response = request_json(f"https://api.telegram.org/bot{self.telegram_token}/{method}",
                                params, method="POST")
        if not response.get("ok") and "message is not modified" not in str(response.get("description", "")):
            raise RuntimeError(f"Telegram: {response.get('description')}")

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        response = request_json(f"https://api.telegram.org/bot{self.telegram_token}/answerCallbackQuery",
                                {"callback_query_id": callback_id, "text": text}, method="POST", timeout=8)
        if not response.get("ok"):
            raise RuntimeError(f"Telegram: {response.get('description')}")

    def send_telegram_card(self, chat_id: int, png: bytes, caption: str) -> int | None:
        response = request_json(
            f"https://api.telegram.org/bot{self.telegram_token}/sendPhoto",
            {"chat_id": chat_id, "caption": caption,
             "reply_markup": json.dumps(telegram_keyboard(), ensure_ascii=False)}, method="POST",
            files={"photo": ("raspisanie.png", png, "image/png")},
        )
        if not response.get("ok"):
            raise RuntimeError(f"Telegram: {response.get('description')}")
        self.state["telegram_keyboard_version"] = 1
        return (response.get("result") or {}).get("message_id")

    def ensure_telegram_keyboard(self) -> None:
        if not self.telegram_token or not self.state.get("telegram_chat_id"):
            return
        if self.state.get("telegram_keyboard_version") == 1:
            return
        try:
            self.send_telegram_chat(self.state["telegram_chat_id"],
                                    "Кнопки управления расписанием появились ниже. Выберите день или листайте недели.")
            save_state(self.state_file, self.state)
        except Exception as exc:
            LOG.warning("Не удалось показать кнопки Telegram: %s", exc)

    def edit_telegram_card(self, chat_id: int, message_id: int, png: bytes, caption: str) -> None:
        response = request_json(
            f"https://api.telegram.org/bot{self.telegram_token}/editMessageMedia",
            {"chat_id": chat_id, "message_id": message_id,
             "media": json.dumps({"type": "photo", "media": "attach://card", "caption": caption}, ensure_ascii=False)},
            method="POST", files={"card": ("raspisanie.png", png, "image/png")},
        )
        if not response.get("ok"):
            raise RuntimeError(f"Telegram: {response.get('description')}")

    def vk_photo_attachment(self, png: bytes) -> str:
        common = {"access_token": self.vk_token, "v": "5.199"}
        server = request_json("https://api.vk.com/method/photos.getMessagesUploadServer",
                              {**common, "peer_id": self.vk_peer_id}, method="POST")
        if "error" in server:
            raise RuntimeError(f"ВКонтакте: {server['error'].get('error_msg')}")
        upload_url = (server.get("response") or {}).get("upload_url", "")
        if urlsplit(upload_url).scheme != "https":
            raise RuntimeError("ВКонтакте не предоставил защищённый адрес загрузки фото")
        uploaded = request_json(upload_url, method="POST",
                                files={"photo": ("raspisanie.png", png, "image/png")},
                                proxy_override=proxy_for_url("https://api.vk.com"))
        saved = request_json("https://api.vk.com/method/photos.saveMessagesPhoto",
                             {**common, "server": uploaded["server"], "photo": uploaded["photo"],
                              "hash": uploaded["hash"]}, method="POST")
        if "error" in saved:
            raise RuntimeError(f"ВКонтакте: {saved['error'].get('error_msg')}")
        photos = saved.get("response") or []
        if not photos:
            raise RuntimeError("ВКонтакте не сохранил фото расписания")
        return f"photo{photos[0]['owner_id']}_{photos[0]['id']}"

    def send_vk_card(self, png: bytes, caption: str) -> int | None:
        attachment = self.vk_photo_attachment(png)
        common = {"access_token": self.vk_token, "v": "5.199"}
        response = request_json("https://api.vk.com/method/messages.send",
                                {**common, "peer_id": self.vk_peer_id,
                                 "random_id": secrets.randbelow(2**31), "message": caption,
                                 "attachment": attachment}, method="POST")
        if "error" in response:
            raise RuntimeError(f"ВКонтакте: {response['error'].get('error_msg')}")
        result = response.get("response")
        return result if isinstance(result, int) else None

    def edit_vk_card(self, message_id: int, png: bytes, caption: str) -> None:
        attachment = self.vk_photo_attachment(png)
        response = request_json("https://api.vk.com/method/messages.edit",
                                {"access_token": self.vk_token, "v": "5.199",
                                 "peer_id": self.vk_peer_id, "message_id": message_id,
                                 "message": caption, "attachment": attachment}, method="POST")
        if "error" in response:
            raise RuntimeError(f"ВКонтакте: {response['error'].get('error_msg')}")

    def send_card(self, channel: str, view: str, date: str, replace: bool = False) -> None:
        snapshot = self.state.get("snapshot")
        if snapshot is None:
            self.send(channel, "Расписание пока не загружено.")
            return
        caption = (f"Расписание {self.group_name} на {datetime.fromisoformat(date):%d.%m.%Y}"
                   if view == "day" else f"Расписание {self.group_name} на неделю с {datetime.fromisoformat(date):%d.%m.%Y}")
        key = f"{view}:{date}"
        cards = self.state["cards"].setdefault(channel, {})
        remembered = cards.get(key) or []
        ids = remembered if isinstance(remembered, list) else [remembered]
        try:
            png = (schedule_card(snapshot, date, self.group_name) if view == "day" else
                   horizontal_week_card(snapshot, date, self.group_name) if self.week_layout == "horizontal" else
                   list_week_card(snapshot, date, self.group_name) if self.week_layout == "list" else
                   week_card(snapshot, date, self.group_name))
            if replace and ids:
                updated, failed = [], []
                for message_id in ids:
                    try:
                        if channel == "telegram":
                            self.edit_telegram_card(self.state["telegram_chat_id"], message_id, png, caption)
                        else:
                            self.edit_vk_card(message_id, png, caption)
                        updated.append(message_id)
                    except Exception as exc:
                        failed.append(message_id)
                        LOG.warning("Не удалось заменить карточку %s в %s (ID %s): %s",
                                    key, channel, message_id, exc)
                cards[key] = updated
                if not failed:
                    return
            if channel == "telegram":
                new_id = self.send_telegram_card(self.state["telegram_chat_id"], png, caption)
            else:
                new_id = self.send_vk_card(png, caption)
            if new_id:
                current = cards.get(key, ids)
                cards[key] = [*(current if isinstance(current, list) else [current]), new_id]
        except Exception as exc:
            LOG.warning("Карточка не отправлена в %s, пробую текст: %s", channel, exc)
            fallback = (day_message(snapshot, date, self.group_name) if view == "day"
                        else week_message(snapshot, date, self.group_name))
            for part in split_message(fallback):
                self.send(channel, part)

    def send_schedule(self, channel: str, date: str, replace: bool = False) -> None:
        self.send_card(channel, "day", date, replace)

    def send_week(self, channel: str, monday: str, replace: bool = False) -> None:
        self.send_card(channel, "week", monday, replace)

    def queue(self, channel: str, message: str, kind: str = "change", date: str = "") -> None:
        for part in split_message(message):
            self.state["pending"][channel].append({"text": part, "kind": kind, "date": date})

    def queue_future_change(self, channel: str, message: str, batch_id: str) -> None:
        parts = split_message(message)
        for index, part in enumerate(parts):
            self.state["pending"][channel].append({
                "text": part, "kind": "future_change",
                "batch_id": batch_id if channel == "telegram" and index == len(parts) - 1 else "",
            })

    def queue_daily(self, channel: str, date: str) -> None:
        self.state["pending"][channel].append({"kind": "daily", "view": "day", "date": date, "format": "card"})

    def queue_weekly(self, channel: str, monday: str) -> None:
        self.state["pending"][channel].append({"kind": "weekly", "view": "week", "date": monday, "format": "card"})

    def queue_refresh(self, channel: str, view: str, date: str) -> None:
        pending = self.state["pending"][channel]
        if not any(item.get("kind") == "refresh" and item.get("view") == view
                   and item.get("date") == date for item in pending):
            pending.append({"kind": "refresh", "view": view, "date": date, "format": "card"})

    def drain(self, today: str) -> None:
        for channel in self.channels():
            pending = self.state["pending"][channel]
            while pending:
                item = pending[0]
                expired = ((item["kind"] in ("daily", "refresh") and item.get("view", "day") == "day"
                            and item["date"] < today)
                           or (item["kind"] in ("weekly", "refresh") and item.get("view") == "week"
                               and (datetime.fromisoformat(item["date"]) + timedelta(days=6)).date().isoformat() < today))
                if expired:
                    pending.pop(0)
                    save_state(self.state_file, self.state)
                    continue
                try:
                    if item.get("format") == "card":
                        self.send_card(channel, item.get("view", "day"), item["date"],
                                       replace=item["kind"] == "refresh")
                    elif channel == "telegram" and item.get("batch_id"):
                        markup = {"inline_keyboard": [[{"text": "🗓 Показать изменения",
                                                       "callback_data": f"changes:{item['batch_id']}"}]]}
                        self.send_telegram_chat(self.state["telegram_chat_id"], item["text"], markup)
                    else:
                        self.send(channel, item["text"])
                except Exception as exc:
                    LOG.error("Не удалось отправить сообщение в %s: %s", channel, exc)
                    break
                pending.pop(0)
                save_state(self.state_file, self.state)

    def navigate_week(self, today: str, step: int) -> None:
        if self.state.get("snapshot") is None:
            self.send("telegram", "Расписание пока не загружено.")
            return
        anchor = self.state.get("telegram_week_cursor") or week_start(today)
        try:
            anchor = week_start(anchor)
        except ValueError:
            anchor = week_start(today)
        target = (datetime.fromisoformat(anchor) + timedelta(days=7 * step)).date().isoformat()
        snapshot = self.state.get("snapshot") or {}
        if snapshot:
            dates = [lesson_date(item) for item in snapshot.values()]
            first, last = week_start(min(dates)), week_start(max(dates))
            if target < first:
                self.send("telegram", "Более ранних недель нет в полученном от ДГТУ расписании.")
                return
            if target > last:
                self.send("telegram", "Более поздних недель пока нет в полученном от ДГТУ расписании.")
                return
        self.state["telegram_week_cursor"] = target
        self.send_week("telegram", target)

    def show_change_batch(self, chat_id: int, batch_id: str) -> None:
        batch = self.state["change_batches"].get(batch_id)
        if not batch:
            self.send_telegram_chat(chat_id, "Это уведомление уже устарело. Откройте расписание недели кнопками ниже.")
            return
        snapshot = batch["snapshot"]
        for monday, marker in sorted(batch["weeks"].items()):
            caption = f"Изменения {self.group_name} · неделя с {datetime.fromisoformat(monday):%d.%m.%Y}"
            try:
                self.send_telegram_card(chat_id, week_card(snapshot, monday, self.group_name, marker), caption)
            except Exception as exc:
                LOG.warning("Не удалось отправить карточку изменений за %s: %s", monday, exc)
                for part in split_message(week_message(snapshot, monday, self.group_name)):
                    self.send_telegram_chat(chat_id, part)

    def telegram_commands(self, today: str) -> bool:
        if not self.telegram_token:
            return False
        try:
            response = request_json(
                f"https://api.telegram.org/bot{self.telegram_token}/getUpdates",
                {"offset": self.state["telegram_offset"], "timeout": 2,
                 "allowed_updates": '["message","callback_query"]'},
                timeout=6,
            )
            if not response.get("ok"):
                raise RuntimeError(response.get("description"))
            for update in response.get("result", []):
                callback = update.get("callback_query")
                if callback:
                    callback_message = callback.get("message") or {}
                    callback_chat = callback_message.get("chat") or {}
                    chat_id = callback_chat.get("id")
                    if callback_chat.get("type") != "private" or chat_id != self.state.get("telegram_chat_id"):
                        self.answer_callback(callback["id"], "Недоступно")
                        self.state["telegram_offset"] = update["update_id"] + 1
                        save_state(self.state_file, self.state)
                        continue
                    action = str(callback.get("data") or "")
                    if action.startswith("changes:"):
                        self.answer_callback(callback["id"])
                        self.show_change_batch(chat_id, action.split(":", 1)[1])
                    elif action.startswith(("tw:", "td:")):
                        self.answer_callback(callback["id"])
                        view, date = action.split(":", 1)
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                            self.send_text_view(chat_id, "week" if view == "tw" else "day", date,
                                                callback_message.get("message_id"))
                    else:
                        self.answer_callback(callback["id"], "Кнопка устарела")
                    self.state["telegram_offset"] = update["update_id"] + 1
                    save_state(self.state_file, self.state)
                    continue
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                command = (message.get("text") or "").strip()
                if chat.get("type") != "private" or not chat_id:
                    self.state["telegram_offset"] = update["update_id"] + 1
                    save_state(self.state_file, self.state)
                    continue
                pieces = command.split(maxsplit=1)
                verb = pieces[0].split("@", 1)[0].lower() if pieces else ""
                argument = pieces[1].strip() if len(pieces) > 1 else ""
                if not self.state.get("telegram_chat_id") and verb == "/start":
                    if self.pair_code and secrets.compare_digest(argument, self.pair_code):
                        self.state["telegram_chat_id"] = chat_id
                        save_state(self.state_file, self.state)
                        self.send("telegram", f"Готово! Вы подписаны на расписание {self.group_name}. Используйте кнопки ниже, чтобы выбрать день или листать недели.")
                        if self.state.get("snapshot") is not None:
                            self.send_schedule("telegram", today)
                            if datetime.now(MOSCOW).strftime("%H:%M") >= self.daily_time:
                                self.state["daily_queued"]["telegram"] = today
                    else:
                        self.send_telegram_chat(chat_id, "Код не подошёл. Отправьте команду /start ПРОБЕЛ ВАШ_КОД — именно с косой чертой и кодом из файла .env.")
                elif chat_id == self.state.get("telegram_chat_id"):
                    if verb == "/today" or command == BUTTON_TODAY:
                        self.send_schedule("telegram", today)
                    elif verb == "/tomorrow" or command == BUTTON_TOMORROW:
                        next_day = (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
                        self.send_schedule("telegram", next_day)
                    elif verb == "/week" or command == BUTTON_CURRENT_WEEK:
                        monday = week_start(today)
                        self.state["telegram_week_cursor"] = monday
                        self.send_week("telegram", monday)
                    elif verb == "/nextweek":
                        self.state["telegram_week_cursor"] = week_start(today)
                        self.navigate_week(today, 1)
                    elif command == BUTTON_PREVIOUS_WEEK:
                        self.navigate_week(today, -1)
                    elif command == BUTTON_NEXT_WEEK:
                        self.navigate_week(today, 1)
                    elif verb in ("/text", "/week_text") or command == BUTTON_TEXT:
                        self.send_text_view(chat_id, "week", week_start(today))
                    elif verb in ("/today_text", "/tomorrow_text"):
                        date = today if verb == "/today_text" else (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
                        self.send_text_view(chat_id, "day", date)
                    elif verb in ("/start", "/help"):
                        self.send("telegram", "Выберите день или листайте недели кнопками ниже. 📝 Текст откроет обзор недели с выбором дня.")
                save_state(self.state_file, self.state)
            return True
        except Exception as exc:
            LOG.error("Ошибка обработки команд Telegram: %s", exc)
            return False

    def cycle(self) -> None:
        now = datetime.now(MOSCOW)
        today = now.date().isoformat()
        self.telegram_commands(today)
        try:
            if self.state.get("group_checked_on") != today:
                try:
                    found = find_group_id(self.group_name)
                    if found != self.group_id:
                        LOG.info("ID группы обновился: %s -> %s", self.group_id, found)
                        self.group_id = found
                    self.state["group_id"] = self.group_id
                    self.state["group_checked_on"] = today
                except Exception as exc:
                    LOG.warning("Поиск группы недоступен, использую ID %s: %s", self.group_id, exc)
            snapshot = fetch_schedule(self.group_id, self.group_name)
            old = self.state.get("snapshot")
            if old and not snapshot and any(lesson_date(x) >= today for x in old.values()):
                raise RuntimeError("API неожиданно вернул пустое расписание; старые данные сохранены")
            if old is not None:
                current_old = {code: item for code, item in old.items() if lesson_date(item) == today}
                current_new = {code: item for code, item in snapshot.items() if lesson_date(item) == today}
                future_old = {code: item for code, item in old.items() if lesson_date(item) > today}
                future_new = {code: item for code, item in snapshot.items() if lesson_date(item) > today}
                current_message = changes_message(current_old, current_new, today, self.group_name)
                future_message = changes_message(future_old, future_new, today, self.group_name)
                if current_message or future_message:
                    weeks = future_change_weeks(old, snapshot, today) if future_message else {}
                    batch_id = secrets.token_hex(4) if weeks and "telegram" in self.channels() else ""
                    if batch_id:
                        batch_snapshot = {code: item for code, item in snapshot.items()
                                          if week_start(lesson_date(item)) in weeks}
                        batches = self.state["change_batches"]
                        batches[batch_id] = {"snapshot": batch_snapshot, "weeks": weeks, "created": today}
                        while len(batches) > 20:
                            batches.pop(next(iter(batches)))
                    for channel in self.channels():
                        if current_message:
                            self.queue(channel, "Сегодня: " + current_message)
                        if future_message:
                            self.queue_future_change(channel, "Будущие дни: " + future_message, batch_id)
                        changed = {lesson_date(item) for before, after in schedule_changes(old, snapshot)
                                   for item in (before, after) if item}
                        cards = self.state["cards"].get(channel, {})
                        for date in sorted(d for d in changed if d >= today):
                            if f"day:{date}" in cards:
                                self.queue_refresh(channel, "day", date)
                            monday = week_start(date)
                            if f"week:{monday}" in cards:
                                self.queue_refresh(channel, "week", monday)
                    LOG.info("Изменения расписания поставлены в очередь")
            else:
                LOG.info("Первый снимок расписания сохранён (%d занятий)", len(snapshot))
            self.state["snapshot"] = snapshot
            if now.strftime("%H:%M") >= self.daily_time:
                for channel in self.channels():
                    needs_migration = (not self.state["card_tracking_ready"]
                                       and self.state["daily_queued"].get(channel) == today
                                       and not any(item.get("kind") == "daily" and item.get("format") == "card"
                                                   and item.get("date") == today
                                                   for item in self.state["pending"][channel]))
                    if self.state["daily_queued"].get(channel) != today or needs_migration:
                        self.queue_daily(channel, today)
                        self.state["daily_queued"][channel] = today
                    if now.weekday() == 0 and self.state["weekly_queued"].get(channel) != today:
                        self.queue_weekly(channel, today)
                        self.state["weekly_queued"][channel] = today
            self.state["card_tracking_ready"] = True
            save_state(self.state_file, self.state)
        except Exception as exc:
            LOG.error("Не удалось обновить расписание: %s", exc)
        self.ensure_telegram_keyboard()
        self.drain(today)


class MultiBot(Bot):
    """Несколько подписчиков с личной группой, временем и оформлением."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.group_id = int(os.getenv("GROUP_ID", "72244"))
        self.vk_alert_types = parse_alert_types(os.getenv("VK_ALERT_TYPES", "all"))
        self.needs_refresh = False
        profiles = self.state.setdefault("profiles", {})
        if not self.state.get("profiles_migrated"):
            chat_id = self.state.get("telegram_chat_id")
            if self.telegram_token and chat_id:
                key = self.profile_key(int(chat_id))
                profiles.setdefault(key, self.new_profile("telegram", int(chat_id), legacy=True))
            if self.vk_token:
                profiles.setdefault("vk", self.new_profile("vk", legacy=True))
            self.state["profiles_migrated"] = True
            save_state(self.state_file, self.state)
        if self.vk_token and "vk" not in profiles:
            profiles["vk"] = self.new_profile("vk", legacy=self.state.get("snapshot") is not None)
            save_state(self.state_file, self.state)
        elif self.vk_token:
            vk = profiles["vk"]
            if compact_name(vk["group_name"]) != compact_name(self.group_name):
                vk.update({"group_name": self.group_name, "group_id": self.group_id,
                           "snapshot": None, "pending": [], "cards": {},
                           "daily_queued": "", "weekly_queued": "", "group_checked_on": ""})
            vk.update({"daily_time": self.daily_time, "week_layout": self.week_layout,
                       "seasonal_theme": self.seasonal_theme,
                       "alert_types": list(self.vk_alert_types)})
            save_state(self.state_file, self.state)
        chat_id = self.state.get("telegram_chat_id")
        if self.telegram_token and chat_id and self.profile_key(int(chat_id)) not in profiles:
            profiles[self.profile_key(int(chat_id))] = self.new_profile(
                "telegram", int(chat_id), legacy=self.state.get("snapshot") is not None)
            save_state(self.state_file, self.state)

    @staticmethod
    def profile_key(chat_id: int) -> str:
        return f"telegram:{chat_id}"

    def new_profile(self, channel: str, chat_id: int | None = None, legacy: bool = False) -> dict:
        old = self.state
        return {
            "channel": channel, "chat_id": chat_id,
            "group_name": self.group_name,
            "group_id": old.get("group_id", self.group_id) if legacy else self.group_id,
            "daily_time": self.daily_time, "week_layout": self.week_layout,
            "seasonal_theme": self.seasonal_theme,
            "alert_types": list(self.vk_alert_types if channel == "vk" else DEFAULT_ALERT_TYPES),
            "snapshot": old.get("snapshot") if legacy else None,
            "pending": list(old.get("pending", {}).get(channel, [])) if legacy else [],
            "daily_queued": old.get("daily_queued", {}).get(channel, "") if legacy else "",
            "weekly_queued": old.get("weekly_queued", {}).get(channel, "") if legacy else "",
            "cards": dict(old.get("cards", {}).get(channel, {})) if legacy else {},
            "card_tracking_ready": old.get("card_tracking_ready", True) if legacy else True,
            "group_checked_on": old.get("group_checked_on", "") if legacy else "",
            "week_cursor": old.get("telegram_week_cursor", "") if legacy and channel == "telegram" else "",
            "keyboard_version": old.get("telegram_keyboard_version", 0) if legacy and channel == "telegram" else 0,
            "change_batches": dict(old.get("change_batches", {})) if legacy and channel == "telegram" else {},
            "awaiting": "", "welcome_card_pending": False, "onboarding": False,
        }

    def profile_for_chat(self, chat_id: int | None) -> dict | None:
        return self.state["profiles"].get(self.profile_key(chat_id)) if chat_id else None

    def active_profiles(self) -> list[dict]:
        return [profile for profile in self.state["profiles"].values()
                if (profile["channel"] == "telegram" and self.telegram_token)
                or (profile["channel"] == "vk" and self.vk_token)]

    def send_telegram_chat(self, chat_id: int, message: str, inline_markup: dict | None = None) -> None:
        params = {"chat_id": chat_id, "text": message}
        profile = self.profile_for_chat(chat_id)
        if inline_markup is not None:
            params["reply_markup"] = json.dumps(inline_markup, ensure_ascii=False)
        elif profile:
            markup = {"remove_keyboard": True} if profile.get("onboarding") else telegram_keyboard()
            params["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        response = request_json(f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                                params, method="POST")
        if not response.get("ok"):
            raise RuntimeError(f"Telegram: {response.get('description')}")
        if profile and inline_markup is None and not profile.get("onboarding"):
            profile["keyboard_version"] = 2

    def send_profile_message(self, profile: dict, message: str,
                             inline_markup: dict | None = None) -> None:
        if profile["channel"] == "telegram":
            self.send_telegram_chat(profile["chat_id"], message, inline_markup)
        else:
            super().send("vk", message)

    def render_profile_card(self, profile: dict, view: str, date: str,
                            highlights: dict | None = None, layout_override: str | None = None) -> bytes:
        snapshot = profile.get("snapshot") or {}
        group_name = profile["group_name"]
        seasonal = profile["seasonal_theme"]
        if view == "day":
            return schedule_card(snapshot, date, group_name, seasonal)
        layout = layout_override or profile["week_layout"]
        if layout == "horizontal":
            return horizontal_week_card(snapshot, date, group_name, highlights, seasonal)
        if layout == "list":
            return list_week_card(snapshot, date, group_name, highlights, seasonal)
        return week_card(snapshot, date, group_name, highlights, seasonal)

    def send_profile_card(self, profile: dict, view: str, date: str,
                          replace: bool = False, layout_override: str | None = None,
                          track: bool = True) -> None:
        if profile.get("snapshot") is None:
            self.send_profile_message(profile, "Расписание пока не загружено.")
            return
        caption = (f"Расписание {profile['group_name']} на {datetime.fromisoformat(date):%d.%m.%Y}"
                   if view == "day" else
                   f"Расписание {profile['group_name']} на неделю с {datetime.fromisoformat(date):%d.%m.%Y}")
        if layout_override:
            caption = "Пример · " + caption
        key = f"{view}:{date}"
        remembered = profile["cards"].get(key) or []
        ids = remembered if isinstance(remembered, list) else [remembered]
        try:
            png = self.render_profile_card(profile, view, date, layout_override=layout_override)
            if replace and ids:
                updated = []
                for message_id in ids:
                    try:
                        if profile["channel"] == "telegram":
                            self.edit_telegram_card(profile["chat_id"], message_id, png, caption)
                        else:
                            self.edit_vk_card(message_id, png, caption)
                        updated.append(message_id)
                    except Exception as exc:
                        LOG.warning("Не удалось обновить карточку %s (ID %s): %s", key, message_id, exc)
                if len(updated) == len(ids):
                    return
                ids = updated
            message_id = (self.send_telegram_card(profile["chat_id"], png, caption)
                          if profile["channel"] == "telegram" else self.send_vk_card(png, caption))
            if profile["channel"] == "telegram":
                profile["keyboard_version"] = 2
            if track and message_id is not None:
                ids = (ids + [message_id])[-20:]
                profile["cards"][key] = ids
        except Exception as exc:
            LOG.warning("Не удалось отправить карточку %s: %s", key, exc)
            fallback = (day_message(profile["snapshot"], date, profile["group_name"])
                        if view == "day" else week_message(profile["snapshot"], date, profile["group_name"]))
            for part in split_message(fallback):
                self.send_profile_message(profile, part)

    def ensure_telegram_keyboards(self) -> None:
        for profile in self.active_profiles():
            if (profile["channel"] == "telegram" and not profile.get("onboarding")
                    and profile.get("group_name") and profile.get("keyboard_version") != 2):
                try:
                    self.send_telegram_chat(profile["chat_id"],
                                            "Кнопки расписания обновлены. Откройте «Настройки», чтобы выбрать группу и оформление.")
                except Exception as exc:
                    LOG.warning("Не удалось показать кнопки чату %s: %s", profile["chat_id"], exc)

    def settings_view(self, profile: dict) -> tuple[str, dict]:
        layout = {"horizontal": "горизонтальное", "vertical": "вертикальное",
                  "list": "чистый список"}.get(profile["week_layout"], "горизонтальное")
        seasonal = "включено" if profile["seasonal_theme"] else "выключено"
        alert_count = len(profile_alert_types(profile))
        message = (f"⚙️ Настройки расписания\n\n"
                   f"Группа: {profile['group_name']}\n"
                   f"Отправка: {profile['daily_time']} по Москве\n"
                   f"Неделя: {layout}\n"
                   f"Сезонное оформление: {seasonal}\n"
                   f"Уведомления: {alert_count} из {len(ALERT_LABELS)} видов\n\n"
                   "Осенняя палитра действует для дат сентября–ноября. "
                   "Изменение времени начнёт действовать со следующей ежедневной отправки.")
        markup = {"inline_keyboard": [
            [{"text": f"🎓 Группа · {profile['group_name']}", "callback_data": "settings:group"}],
            [{"text": f"⏰ Время · {profile['daily_time']}", "callback_data": "settings:time"}],
            [{"text": "🗓 Выберите вид недели", "callback_data": "settings:layouts"}],
            [{"text": f"🍂 Сезонная тема · {seasonal}",
              "callback_data": "settings:season:" + ("off" if profile["seasonal_theme"] else "on")}],
            [{"text": f"🔔 Уведомления · {alert_count}/{len(ALERT_LABELS)}",
              "callback_data": "settings:alerts"}],
        ]}
        return message, markup

    def layout_settings_view(self, profile: dict) -> tuple[str, dict]:
        names = (("horizontal", "↔️ Горизонтальная сетка"),
                 ("vertical", "↕️ Вертикальная неделя"),
                 ("list", "📋 Чистый список"))
        rows = []
        for key, name in names:
            selected = "✅ " if profile["week_layout"] == key else ""
            rows.append([{"text": selected + name, "callback_data": "settings:layout:" + key},
                         {"text": "👁 Пример", "callback_data": "settings:example:" + key}])
        rows.append([{"text": "⬅️ К настройкам", "callback_data": "settings:back"}])
        return "🗓 Вид недельного расписания\n\nВыберите вариант или откройте пример на своей группе.", \
               {"inline_keyboard": rows}

    def alert_settings_view(self, profile: dict) -> tuple[str, dict]:
        selected = profile_alert_types(profile)
        rows = []
        options = list(ALERT_LABELS.items())
        for index in range(0, len(options), 2):
            rows.append([{"text": ("✅ " if key in selected else "▫️ ") + label,
                          "callback_data": f"settings:alert:{key}"}
                         for key, label in options[index:index + 2]])
        rows.append([{"text": "Включить все", "callback_data": "settings:alert:all:on"},
                     {"text": "Выключить все", "callback_data": "settings:alert:all:off"}])
        rows.append([{"text": "⬅️ К настройкам", "callback_data": "settings:back"}])
        message = ("🔔 Уведомления об изменениях\n\n"
                   "Отмеченные виды изменений вызывают оповещение. "
                   "Ежедневная отправка расписания и обновление уже присланных карточек "
                   "работают независимо от этих переключателей.")
        return message, {"inline_keyboard": rows}

    def show_settings(self, profile: dict, message_id: int | None = None,
                      section: str = "main") -> None:
        message, markup = (self.alert_settings_view(profile) if section == "alerts" else
                           self.layout_settings_view(profile) if section == "layouts" else
                           self.settings_view(profile))
        method = "editMessageText" if message_id else "sendMessage"
        params = {"chat_id": profile["chat_id"], "text": message,
                  "reply_markup": json.dumps(markup, ensure_ascii=False)}
        if message_id:
            params["message_id"] = message_id
        response = request_json(f"https://api.telegram.org/bot{self.telegram_token}/{method}",
                                params, method="POST")
        if not response.get("ok") and "message is not modified" not in str(response.get("description", "")):
            if message_id:
                return self.show_settings(profile, section=section)
            raise RuntimeError(f"Telegram: {response.get('description')}")

    def send_profile_text_view(self, profile: dict, view: str, date: str,
                               message_id: int | None = None) -> None:
        snapshot = profile.get("snapshot")
        if snapshot is None:
            self.send_profile_message(profile, "Расписание пока не загружено.")
            return
        message, markup = (text_week_view(snapshot, date, profile["group_name"]) if view == "week"
                           else text_day_view(snapshot, date, profile["group_name"]))
        method = "editMessageText" if message_id else "sendMessage"
        params = {"chat_id": profile["chat_id"], "text": message, "parse_mode": "HTML",
                  "reply_markup": json.dumps(markup, ensure_ascii=False)}
        if message_id:
            params["message_id"] = message_id
        response = request_json(f"https://api.telegram.org/bot{self.telegram_token}/{method}",
                                params, method="POST")
        if not response.get("ok") and "message is not modified" not in str(response.get("description", "")):
            raise RuntimeError(f"Telegram: {response.get('description')}")

    def navigate_profile_week(self, profile: dict, today: str, step: int) -> None:
        snapshot = profile.get("snapshot")
        if snapshot is None:
            self.send_profile_message(profile, "Расписание пока не загружено.")
            return
        anchor = profile.get("week_cursor") or week_start(today)
        try:
            anchor = week_start(anchor)
        except ValueError:
            anchor = week_start(today)
        target = (datetime.fromisoformat(anchor) + timedelta(days=7 * step)).date().isoformat()
        if snapshot:
            dates = [lesson_date(item) for item in snapshot.values()]
            if target < week_start(min(dates)):
                self.send_profile_message(profile, "Более ранних недель нет в расписании ДГТУ.")
                return
            if target > week_start(max(dates)):
                self.send_profile_message(profile, "Более поздних недель пока нет в расписании ДГТУ.")
                return
        profile["week_cursor"] = target
        self.send_profile_card(profile, "week", target)

    def show_profile_change_batch(self, profile: dict, batch_id: str) -> None:
        batch = profile["change_batches"].get(batch_id)
        if not batch:
            self.send_profile_message(profile, "Это уведомление уже устарело. Откройте актуальную неделю кнопками ниже.")
            return
        for monday, marker in sorted(batch["weeks"].items()):
            caption = f"Изменения {profile['group_name']} · неделя с {datetime.fromisoformat(monday):%d.%m.%Y}"
            try:
                png = self.render_profile_card({**profile, "snapshot": batch["snapshot"]},
                                               "week", monday, marker)
                self.send_telegram_card(profile["chat_id"], png, caption)
                profile["keyboard_version"] = 2
            except Exception as exc:
                LOG.warning("Не удалось показать изменения за %s: %s", monday, exc)
                for part in split_message(week_message(batch["snapshot"], monday, profile["group_name"])):
                    self.send_profile_message(profile, part)

    def apply_user_input(self, profile: dict, value: str, today: str) -> None:
        awaiting = profile.get("awaiting")
        if awaiting == "time":
            if not valid_time(value):
                self.send_profile_message(profile, "Введите время в формате ЧЧ:ММ, например 08:00 или 19:30.")
                return
            profile["daily_time"] = value
            profile["awaiting"] = ""
            self.send_profile_message(profile, f"Время отправки установлено: {value} по Москве.")
            self.show_settings(profile)
            return
        if awaiting == "group":
            if not 1 <= len(value) <= 40:
                self.send_profile_message(profile, "Введите короткое название группы, например ВКБ51.")
                return
            try:
                group_id = find_group_id(value)
                snapshot = fetch_schedule(group_id, value)
            except Exception as exc:
                LOG.warning("Не удалось выбрать группу %s: %s", value, exc)
                self.send_profile_message(profile, "Группа не найдена или ДГТУ сейчас недоступен. Проверьте название и попробуйте ещё раз.")
                return
            profile.update({"group_name": value, "group_id": group_id, "snapshot": snapshot,
                            "group_checked_on": today, "pending": [], "cards": {},
                            "daily_queued": today, "weekly_queued": "", "week_cursor": "",
                            "change_batches": {}, "awaiting": "", "onboarding": False,
                            "welcome_card_pending": False})
            self.send_profile_message(profile, f"Теперь показываю расписание группы {value}.")
            self.send_profile_card(profile, "day", today)
            self.show_settings(profile)

    def handle_profile_callback(self, profile: dict, callback: dict) -> None:
        action = str(callback.get("data") or "")
        message_id = (callback.get("message") or {}).get("message_id")
        callback_id = callback["id"]
        if action.startswith("changes:"):
            self.answer_callback(callback_id)
            self.show_profile_change_batch(profile, action.split(":", 1)[1])
        elif action.startswith(("tw:", "td:")):
            self.answer_callback(callback_id)
            view, date = action.split(":", 1)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                self.send_profile_text_view(profile, "week" if view == "tw" else "day",
                                            date, message_id)
        elif action == "settings:layouts":
            self.answer_callback(callback_id)
            self.show_settings(profile, message_id, "layouts")
        elif action in ("settings:layout:horizontal", "settings:layout:vertical", "settings:layout:list"):
            self.answer_callback(callback_id)
            profile["week_layout"] = action.rsplit(":", 1)[1]
            self.show_settings(profile, message_id, "layouts")
        elif action in ("settings:season:on", "settings:season:off"):
            self.answer_callback(callback_id)
            profile["seasonal_theme"] = action.endswith(":on")
            self.show_settings(profile, message_id)
        elif action == "settings:alerts":
            self.answer_callback(callback_id)
            self.show_settings(profile, message_id, "alerts")
        elif action == "settings:back":
            self.answer_callback(callback_id)
            self.show_settings(profile, message_id)
        elif action in ("settings:alert:all:on", "settings:alert:all:off"):
            self.answer_callback(callback_id)
            profile["alert_types"] = list(DEFAULT_ALERT_TYPES) if action.endswith(":on") else []
            self.show_settings(profile, message_id, "alerts")
        elif action.startswith("settings:alert:") and action.rsplit(":", 1)[1] in ALERT_LABELS:
            self.answer_callback(callback_id)
            selected = profile_alert_types(profile)
            key = action.rsplit(":", 1)[1]
            if key in selected:
                selected.remove(key)
            else:
                selected.add(key)
            profile["alert_types"] = [name for name in DEFAULT_ALERT_TYPES if name in selected]
            self.show_settings(profile, message_id, "alerts")
        elif action in ("settings:group", "settings:time"):
            self.answer_callback(callback_id)
            profile["awaiting"] = action.split(":", 1)[1]
            prompt = ("Напишите название группы так, как на сайте ДГТУ, например ВКБ51."
                      if profile["awaiting"] == "group" else
                      "Напишите время отправки по Москве в формате ЧЧ:ММ, например 08:00.")
            self.send_profile_message(profile, prompt)
        elif action.startswith("settings:example:"):
            self.answer_callback(callback_id)
            layout = action.rsplit(":", 1)[1]
            if layout in ("horizontal", "vertical", "list"):
                today = datetime.now(MOSCOW).date().isoformat()
                self.send_profile_card(profile, "week", week_start(today),
                                       layout_override=layout, track=False)
        else:
            self.answer_callback(callback_id, "Кнопка устарела")

    def telegram_commands(self, today: str) -> bool:
        if not self.telegram_token:
            return False
        try:
            response = request_json(
                f"https://api.telegram.org/bot{self.telegram_token}/getUpdates",
                {"offset": self.state["telegram_offset"], "timeout": 2,
                 "allowed_updates": '["message","callback_query"]'}, timeout=6)
            if not response.get("ok"):
                raise RuntimeError(response.get("description"))
            for update in response.get("result", []):
                callback = update.get("callback_query")
                if callback:
                    chat = ((callback.get("message") or {}).get("chat") or {})
                    profile = self.profile_for_chat(chat.get("id"))
                    if chat.get("type") != "private" or not profile:
                        self.answer_callback(callback["id"], "Недоступно")
                    elif profile.get("onboarding") or not profile.get("group_name") or not profile.get("group_id"):
                        self.answer_callback(callback["id"], "Сначала выберите группу")
                        self.send_profile_message(profile, "Напишите название своей группы, например ВКБ51.")
                    else:
                        self.handle_profile_callback(profile, callback)
                    self.state["telegram_offset"] = update["update_id"] + 1
                    save_state(self.state_file, self.state)
                    continue
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                if chat.get("type") != "private" or not chat_id:
                    self.state["telegram_offset"] = update["update_id"] + 1
                    save_state(self.state_file, self.state)
                    continue
                command = (message.get("text") or "").strip()
                pieces = command.split(maxsplit=1)
                verb = pieces[0].split("@", 1)[0].lower() if pieces else ""
                argument = pieces[1].strip() if len(pieces) > 1 else ""
                profile = self.profile_for_chat(chat_id)
                known_buttons = {BUTTON_TODAY, BUTTON_TOMORROW, BUTTON_PREVIOUS_WEEK,
                                 BUTTON_CURRENT_WEEK, BUTTON_NEXT_WEEK, BUTTON_TEXT, BUTTON_SETTINGS}
                if not profile:
                    authorized = (self.telegram_access == "public" or
                                  bool(self.pair_code and secrets.compare_digest(argument, self.pair_code)))
                    if verb == "/start" and authorized:
                        has_user = any(item["channel"] == "telegram"
                                       for item in self.state["profiles"].values())
                        if has_user and not self.allow_multiple_users:
                            self.send_telegram_chat(chat_id, "Бот уже привязан к другому чату.")
                        else:
                            profile = self.new_profile("telegram", chat_id)
                            if self.telegram_access == "public":
                                profile.update({"group_name": "", "group_id": 0,
                                                "awaiting": "group", "onboarding": True})
                            else:
                                profile["welcome_card_pending"] = True
                            self.state["profiles"][self.profile_key(chat_id)] = profile
                            save_state(self.state_file, self.state)
                            if self.telegram_access == "public":
                                self.send_profile_message(
                                    profile,
                                    "Привет! Напишите название своей группы как на сайте ДГТУ, например ВКБ51. "
                                    "После выбора группы вы получите своё расписание и личные настройки.",
                                    {"remove_keyboard": True})
                            else:
                                self.needs_refresh = True
                                self.send_profile_message(profile, f"Готово! Вы подписаны на расписание {profile['group_name']}.")
                                self.show_settings(profile)
                    elif verb == "/start":
                        self.send_telegram_chat(chat_id, "Код не подошёл. Отправьте /start ПРОБЕЛ ВАШ_КОД из файла .env.")
                    else:
                        self.send_telegram_chat(chat_id, "Чтобы подключиться, откройте бота по ссылке и нажмите Start или отправьте /start.")
                    self.state["telegram_offset"] = update["update_id"] + 1
                    save_state(self.state_file, self.state)
                    continue
                if profile.get("onboarding") or not profile.get("group_name") or not profile.get("group_id"):
                    if command and not command.startswith("/") and command not in known_buttons:
                        profile["awaiting"] = "group"
                        self.apply_user_input(profile, command, today)
                    else:
                        self.send_profile_message(profile, "Напишите название своей группы, например ВКБ51.")
                    self.state["telegram_offset"] = update["update_id"] + 1
                    save_state(self.state_file, self.state)
                    continue
                if profile.get("awaiting") and command and not command.startswith("/") and command not in known_buttons:
                    self.apply_user_input(profile, command, today)
                elif verb == "/today" or command == BUTTON_TODAY:
                    self.send_profile_card(profile, "day", today)
                elif verb == "/tomorrow" or command == BUTTON_TOMORROW:
                    next_day = (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
                    self.send_profile_card(profile, "day", next_day)
                elif verb == "/week" or command == BUTTON_CURRENT_WEEK:
                    monday = week_start(today)
                    profile["week_cursor"] = monday
                    self.send_profile_card(profile, "week", monday)
                elif verb == "/nextweek":
                    profile["week_cursor"] = week_start(today)
                    self.navigate_profile_week(profile, today, 1)
                elif command == BUTTON_PREVIOUS_WEEK:
                    self.navigate_profile_week(profile, today, -1)
                elif command == BUTTON_NEXT_WEEK:
                    self.navigate_profile_week(profile, today, 1)
                elif verb in ("/text", "/week_text") or command == BUTTON_TEXT:
                    self.send_profile_text_view(profile, "week", week_start(today))
                elif verb in ("/today_text", "/tomorrow_text"):
                    date = (today if verb == "/today_text" else
                            (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat())
                    self.send_profile_text_view(profile, "day", date)
                elif verb == "/settings" or command == BUTTON_SETTINGS:
                    profile["awaiting"] = ""
                    self.show_settings(profile)
                elif verb in ("/start", "/help"):
                    self.send_profile_message(profile, "Выберите день или неделю кнопками ниже. ⚙️ Настройки меняют группу, время и оформление.")
                self.state["telegram_offset"] = update["update_id"] + 1
                save_state(self.state_file, self.state)
            return True
        except Exception as exc:
            LOG.error("Ошибка обработки команд Telegram: %s", exc)
            return False


    @staticmethod
    def queue_profile(profile: dict, message: str, kind: str = "change",
                      batch_id: str = "") -> None:
        parts = split_message(message)
        for index, part in enumerate(parts):
            profile["pending"].append({"text": part, "kind": kind,
                                       "batch_id": batch_id if index == len(parts) - 1 else ""})

    @staticmethod
    def queue_profile_card(profile: dict, view: str, date: str, kind: str) -> None:
        if kind == "refresh" and any(item.get("kind") == "refresh" and item.get("view") == view
                                     and item.get("date") == date for item in profile["pending"]):
            return
        profile["pending"].append({"kind": kind, "format": "card", "view": view, "date": date})

    def drain(self, today: str) -> None:
        for profile in self.active_profiles():
            pending = profile["pending"]
            while pending:
                item = pending[0]
                view = item.get("view")
                date = item.get("date", "")
                expired = ((item["kind"] in ("daily", "refresh") and view == "day" and date < today)
                           or (item["kind"] in ("weekly", "refresh") and view == "week"
                               and (datetime.fromisoformat(date) + timedelta(days=6)).date().isoformat() < today))
                if expired:
                    pending.pop(0)
                    save_state(self.state_file, self.state)
                    continue
                try:
                    if item.get("format") == "card":
                        self.send_profile_card(profile, view or "day", date, replace=item["kind"] == "refresh")
                    elif profile["channel"] == "telegram" and item.get("batch_id"):
                        markup = {"inline_keyboard": [[{"text": "🗓 Показать изменения",
                                                       "callback_data": f"changes:{item['batch_id']}"}]]}
                        self.send_profile_message(profile, item["text"], markup)
                    else:
                        self.send_profile_message(profile, item["text"])
                except Exception as exc:
                    LOG.error("Не удалось отправить сообщение подписчику %s: %s",
                              profile.get("chat_id") or profile["channel"], exc)
                    break
                pending.pop(0)
                save_state(self.state_file, self.state)

    def process_profile_snapshot(self, profile: dict, snapshot: dict, now: datetime) -> None:
        today = now.date().isoformat()
        old = profile.get("snapshot")
        if old and not snapshot and any(lesson_date(item) >= today for item in old.values()):
            raise RuntimeError("API неожиданно вернул пустое расписание; старые данные сохранены")
        if old is not None:
            selected_alerts = profile_alert_types(profile)
            current_old = {code: item for code, item in old.items() if lesson_date(item) == today}
            current_new = {code: item for code, item in snapshot.items() if lesson_date(item) == today}
            future_old = {code: item for code, item in old.items() if lesson_date(item) > today}
            future_new = {code: item for code, item in snapshot.items() if lesson_date(item) > today}
            current_message = changes_message(current_old, current_new, today,
                                              profile["group_name"], selected_alerts)
            future_message = changes_message(future_old, future_new, today,
                                             profile["group_name"], selected_alerts)
            if current_message or future_message:
                if current_message:
                    self.queue_profile(profile, "Сегодня: " + current_message)
                if future_message:
                    batch_id = ""
                    weeks = future_change_weeks(old, snapshot, today, selected_alerts)
                    if weeks and profile["channel"] == "telegram":
                        batch_id = secrets.token_hex(4)
                        batch_snapshot = {code: item for code, item in snapshot.items()
                                          if week_start(lesson_date(item)) in weeks}
                        batches = profile["change_batches"]
                        batches[batch_id] = {"snapshot": batch_snapshot, "weeks": weeks,
                                             "created": today}
                        while len(batches) > 20:
                            batches.pop(next(iter(batches)))
                    self.queue_profile(profile, "Будущие дни: " + future_message,
                                       "future_change", batch_id)
            changed_dates = {lesson_date(item) for before, after in schedule_changes(old, snapshot)
                             for item in (before, after) if item}
            for date in sorted(day for day in changed_dates if day >= today):
                if f"day:{date}" in profile["cards"]:
                    self.queue_profile_card(profile, "day", date, "refresh")
                monday = week_start(date)
                if f"week:{monday}" in profile["cards"]:
                    self.queue_profile_card(profile, "week", monday, "refresh")
        profile["snapshot"] = snapshot

    def queue_due(self, now: datetime) -> bool:
        today = now.date().isoformat()
        changed = False
        for profile in self.active_profiles():
            if profile.get("snapshot") is None or now.strftime("%H:%M") < profile["daily_time"]:
                continue
            needs_migration = (not profile.get("card_tracking_ready", True)
                               and profile.get("daily_queued") == today
                               and not any(item.get("kind") == "daily" and item.get("format") == "card"
                                           and item.get("date") == today for item in profile["pending"]))
            if profile.get("daily_queued") != today or needs_migration:
                self.queue_profile_card(profile, "day", today, "daily")
                profile["daily_queued"] = today
                changed = True
            if now.weekday() == 0 and profile.get("weekly_queued") != today:
                self.queue_profile_card(profile, "week", today, "weekly")
                profile["weekly_queued"] = today
                changed = True
            profile["card_tracking_ready"] = True
        return changed

    def cycle(self) -> None:
        now = datetime.now(MOSCOW)
        today = now.date().isoformat()
        self.telegram_commands(today)
        cache: dict[str, tuple[int, dict]] = {}
        for profile in self.active_profiles():
            if profile.get("onboarding") or not profile.get("group_name") or not profile.get("group_id"):
                continue
            group_key = compact_name(profile["group_name"])
            try:
                if group_key not in cache:
                    group_id = int(profile["group_id"])
                    if profile.get("group_checked_on") != today:
                        try:
                            group_id = find_group_id(profile["group_name"])
                        except Exception as exc:
                            LOG.warning("Поиск группы %s недоступен, использую ID %s: %s",
                                        profile["group_name"], group_id, exc)
                    snapshot = fetch_schedule(group_id, profile["group_name"])
                    cache[group_key] = (group_id, snapshot)
                group_id, snapshot = cache[group_key]
                profile["group_id"] = group_id
                profile["group_checked_on"] = today
                self.process_profile_snapshot(profile, snapshot, now)
                if profile.get("welcome_card_pending"):
                    self.queue_profile_card(profile, "day", today, "daily")
                    profile["daily_queued"] = today
                    profile["welcome_card_pending"] = False
            except Exception as exc:
                LOG.error("Не удалось обновить группу %s: %s", profile["group_name"], exc)
        self.queue_due(now)
        save_state(self.state_file, self.state)
        self.ensure_telegram_keyboards()
        self.drain(today)


def stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def doctor(root: Path) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_env(root / ".env")
    env_path = root / ".env"
    print(f"Файл .env: {'найден' if env_path.exists() else 'НЕ НАЙДЕН'} ({env_path})")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    pair_code = os.getenv("TELEGRAM_PAIR_CODE", "")
    access = os.getenv("TELEGRAM_ACCESS", "public").strip().lower()
    print(f"Токен Telegram: {'задан' if token else 'НЕ ЗАДАН'}")
    print(f"Доступ Telegram: {'по ссылке' if access == 'public' else 'по коду' if access == 'code' else 'НЕВЕРНОЕ ЗНАЧЕНИЕ'}")
    if access == "code":
        print(f"Код сопряжения: {'задан' if pair_code else 'НЕ ЗАДАН'}")
    for label, endpoint in (
        ("Telegram", "https://api.telegram.org/"),
        ("ДГТУ", "https://edu.donstu.ru/"),
        ("ВКонтакте", "https://api.vk.com/"),
    ):
        try:
            proxy = proxy_for_url(endpoint)
            print(f"Прокси {label}: {urlsplit(proxy).scheme if proxy else 'не задан'}")
        except Exception as exc:
            print(f"Прокси {label}: ошибка — {exc}")
    state_path = root / os.getenv("STATE_FILE", "data/state.json")
    try:
        state = load_state(state_path)
        profiles = state.get("profiles") or {}
        telegram_count = sum(item.get("channel") == "telegram" for item in profiles.values())
        snapshot_count = sum(item.get("snapshot") is not None for item in profiles.values())
        if profiles:
            print(f"Telegram-подписчиков: {telegram_count}")
            print(f"Загруженных расписаний: {snapshot_count} из {len(profiles)}")
        else:
            print(f"Чат Telegram: {'привязан' if state.get('telegram_chat_id') else 'ещё не привязан'}")
            print(f"Снимок расписания: {'есть' if state.get('snapshot') is not None else 'ещё нет'}")
    except Exception as exc:
        print(f"Файл состояния: ошибка — {exc}")
    if token:
        try:
            me = request_json(f"https://api.telegram.org/bot{token}/getMe")
            if not me.get("ok"):
                raise RuntimeError(me.get("description", "неизвестная ошибка"))
            print(f"Telegram API: подключён к @{me['result']['username']}")
            print(f"Ссылка для подключения: https://t.me/{me['result']['username']}")
            hook = request_json(f"https://api.telegram.org/bot{token}/getWebhookInfo")
            print(f"Webhook: {'включён — мешает опросу getUpdates' if (hook.get('result') or {}).get('url') else 'выключен'}")
        except Exception as exc:
            print(f"Telegram API: ошибка — {exc}")
    try:
        name = os.getenv("GROUP_NAME", "ВКБ51")
        group_id = find_group_id(name)
        snapshot = fetch_schedule(group_id, name)
        print(f"ДГТУ: группа {name}, ID {group_id}, занятий в ответе {len(snapshot)}")
    except Exception as exc:
        print(f"ДГТУ: ошибка — {exc}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--doctor" in sys.argv:
        return doctor(Path(__file__).resolve().parent)
    if any(option in sys.argv for option in
           ("--preview", "--preview-card", "--preview-week", "--preview-horizontal", "--preview-vertical", "--preview-list")):
        root = Path(__file__).resolve().parent
        load_env(root / ".env")
        group_name = os.getenv("GROUP_NAME", "ВКБ51")
        layout = os.getenv("WEEK_LAYOUT", "horizontal").strip().lower()
        if layout not in ("horizontal", "vertical", "list"):
            LOG.error("WEEK_LAYOUT должен быть horizontal, vertical или list")
            return 2
        seasonal = env_bool("SEASONAL_THEME", True)
        try:
            group_id = find_group_id(group_name)
            snapshot = fetch_schedule(group_id, group_name)
            today = datetime.now(MOSCOW).date().isoformat()
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(f"Группа: {group_name} (ID {group_id}); всего занятий в ответе: {len(snapshot)}")
            if "--preview-card" in sys.argv:
                preview_file = root / "data" / "preview.png"
                preview_file.parent.mkdir(parents=True, exist_ok=True)
                preview_file.write_bytes(schedule_card(snapshot, today, group_name, seasonal))
                print(f"Карточка сохранена: {preview_file}")
            elif any(option in sys.argv for option in ("--preview-week", "--preview-horizontal", "--preview-vertical", "--preview-list")):
                if "--preview-horizontal" in sys.argv:
                    layout = "horizontal"
                elif "--preview-vertical" in sys.argv:
                    layout = "vertical"
                elif "--preview-list" in sys.argv:
                    layout = "list"
                name = ("preview_week.png" if "--preview-week" in sys.argv else
                        "preview_horizontal.png" if layout == "horizontal" else
                        "preview_list.png" if layout == "list" else "preview_vertical.png")
                preview_file = root / "data" / name
                preview_file.parent.mkdir(parents=True, exist_ok=True)
                png = (horizontal_week_card(snapshot, week_start(today), group_name, seasonal=seasonal)
                       if layout == "horizontal" else
                       list_week_card(snapshot, week_start(today), group_name, seasonal=seasonal)
                       if layout == "list" else
                       week_card(snapshot, week_start(today), group_name, seasonal=seasonal))
                preview_file.write_bytes(png)
                print(f"Недельная карточка сохранена: {preview_file}")
            else:
                print(day_message(snapshot, today, group_name))
            return 0
        except Exception as exc:
            LOG.error("Не удалось получить расписание: %s", exc)
            return 1
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        bot = MultiBot(Path(__file__).resolve().parent)
    except Exception as exc:
        LOG.error("Ошибка настройки: %s", exc)
        return 2
    LOG.info("Бот запущен: %d подписчиков, проверка каждые %d секунд",
             len(bot.state["profiles"]), bot.interval)
    while not STOP:
        bot.cycle()
        deadline = time.monotonic() + bot.interval
        while not STOP and time.monotonic() < deadline:
            poll_ok = True
            now = datetime.now(MOSCOW)
            today = now.date().isoformat()
            if bot.telegram_token:
                poll_ok = bot.telegram_commands(today)
            if bot.needs_refresh:
                bot.needs_refresh = False
                break
            if bot.queue_due(now):
                save_state(bot.state_file, bot.state)
            bot.drain(today)
            pause = (0.2 if poll_ok else 2) if bot.telegram_token else 1
            time.sleep(min(pause, max(0, deadline - time.monotonic())))
    LOG.info("Бот остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
