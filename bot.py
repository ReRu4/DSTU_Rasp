#!/usr/bin/env python3
"""Уведомления об изменениях расписания ДГТУ для одной группы."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import sys
import time
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


LESSON_TYPES = {"лек": "Лекция", "пр": "Практика", "лаб": "Лабораторная"}
LESSON_TYPES_PLURAL = {"Лекция": "лекции", "Практика": "практики", "Лабораторная": "лабораторные"}
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
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


def telegram_keyboard() -> dict:
    return {
        "keyboard": [[{"text": BUTTON_TODAY}, {"text": BUTTON_TOMORROW}],
                     [{"text": BUTTON_PREVIOUS_WEEK}, {"text": BUTTON_NEXT_WEEK}],
                     [{"text": BUTTON_CURRENT_WEEK}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def week_start(date: str) -> str:
    day = datetime.fromisoformat(date).date()
    return (day - timedelta(days=day.weekday())).isoformat()


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


def lesson_blocks(lessons: list[dict]) -> list[list[dict]]:
    blocks = []
    for item in lessons:
        previous = blocks[-1][-1] if blocks else None
        adjacent = (previous is not None and slot_index(previous) is not None
                    and slot_index(item) == slot_index(previous) + 1)
        if adjacent and same_lesson(previous, item) and item["датаНачала"] > previous["датаНачала"]:
            blocks[-1].append(item)
        else:
            blocks.append([item])
    return blocks


def timeline_entries(lessons: list[dict]) -> list[tuple[str, object]]:
    """Группы занятий и пустые стандартные слоты между первой и последней парой."""
    entries: list[tuple[str, object]] = []
    previous_slot: int | None = None
    for block in lesson_blocks(lessons):
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


def card_blocks(lessons: list[dict]) -> list[dict]:
    blocks = []
    for kind, value in timeline_entries(lessons):
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


def schedule_card(snapshot: dict[str, dict], date: str, group_name: str) -> bytes:
    from card import render_card

    lessons = sorted((x for x in snapshot.values() if lesson_date(x) == date), key=lesson_sort)
    slots = len({(x["датаНачала"], x["датаОкончания"]) for x in lessons})
    return render_card(group_name, date, card_blocks(lessons), slots)


def week_card(snapshot: dict[str, dict], monday: str, group_name: str) -> bytes:
    from card import render_week_card

    days = []
    start = datetime.fromisoformat(monday).date()
    for offset in range(7):
        date = (start + timedelta(days=offset)).isoformat()
        lessons = sorted((x for x in snapshot.values() if lesson_date(x) == date), key=lesson_sort)
        slots = len({(x["датаНачала"], x["датаОкончания"]) for x in lessons})
        days.append({"date": date, "blocks": card_blocks(lessons), "slots": slots})
    return render_week_card(group_name, monday, days)


def week_message(snapshot: dict[str, dict], monday: str, group_name: str) -> str:
    start = datetime.fromisoformat(monday).date()
    lines = [f"📅 Неделя {start:%d.%m}–{(start + timedelta(days=6)):%d.%m.%Y} · {group_name}"]
    for offset in range(7):
        date = (start + timedelta(days=offset)).isoformat()
        lines.append(day_message(snapshot, date, group_name))
    return "\n\n".join(lines)


LABELS = {
    "датаНачала": "начало", "датаОкончания": "конец", "дисциплина": "предмет",
    "преподаватель": "преподаватель", "аудитория": "аудитория",
    "номерПодгруппы": "подгруппа", "замена": "замена", "ссылка": "ссылка", "тема": "тема",
}


def changes_message(old: dict[str, dict], new: dict[str, dict], today: str, group_name: str) -> str:
    lines = []
    for code in sorted(set(old) | set(new), key=lambda c: lesson_sort(new.get(c) or old[c])):
        before, after = old.get(code), new.get(code)
        item = after or before
        if max(lesson_date(x) for x in (before, after) if x) < today:
            continue
        if before is None:
            lines.append("➕ Добавлено: " + lesson_date(after) + "\n" + lesson_text(after))
        elif after is None:
            lines.append("➖ Удалено: " + lesson_date(before) + "\n" + lesson_text(before))
        elif before != after:
            details = [f"{LABELS[key]}: {before.get(key) or '—'} → {after.get(key) or '—'}" for key in FIELDS if before.get(key) != after.get(key)]
            lines.append("✏️ Изменено: " + lesson_date(after) + "\n" + lesson_text(after) + "\n    " + "; ".join(details))
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
                "card_tracking_ready": True, "telegram_offset": 0, "telegram_keyboard_version": 0}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("pending"), dict):
        raise RuntimeError("Повреждён файл состояния; восстановите его из копии")
    state.setdefault("daily_queued", {})
    state.setdefault("weekly_queued", {})
    state.setdefault("card_tracking_ready", "cards" in state)
    state.setdefault("cards", {})
    state.setdefault("telegram_offset", 0)
    state.setdefault("telegram_keyboard_version", 0)
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
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.daily_time):
            raise RuntimeError("DAILY_TIME должен быть в формате ЧЧ:ММ")
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
        if self.telegram_token and self.pair_code == "замените-на-свой-секретный-код":
            raise RuntimeError("Замените пример TELEGRAM_PAIR_CODE в .env на свой код")
        if self.telegram_token and not self.state.get("telegram_chat_id") and not self.pair_code:
            raise RuntimeError("Укажите TELEGRAM_CHAT_ID или TELEGRAM_PAIR_CODE")
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

    def send_telegram_chat(self, chat_id: int, message: str) -> None:
        params = {"chat_id": chat_id, "text": message}
        if chat_id == self.state.get("telegram_chat_id"):
            params["reply_markup"] = json.dumps(telegram_keyboard(), ensure_ascii=False)
        response = request_json(
            f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
            params, method="POST",
        )
        if not response.get("ok"):
            raise RuntimeError(f"Telegram: {response.get('description')}")
        if chat_id == self.state.get("telegram_chat_id"):
            self.state["telegram_keyboard_version"] = 1

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
            png = (schedule_card(snapshot, date, self.group_name) if view == "day"
                   else week_card(snapshot, date, self.group_name))
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

    def telegram_commands(self, today: str) -> None:
        if not self.telegram_token:
            return
        try:
            response = request_json(
                f"https://api.telegram.org/bot{self.telegram_token}/getUpdates",
                {"offset": self.state["telegram_offset"], "timeout": 0, "allowed_updates": '["message"]'},
                timeout=10,
            )
            if not response.get("ok"):
                raise RuntimeError(response.get("description"))
            for update in response.get("result", []):
                self.state["telegram_offset"] = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                command = (message.get("text") or "").strip()
                if chat.get("type") != "private" or not chat_id:
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
                    elif verb in ("/today_text", "/tomorrow_text"):
                        date = today if verb == "/today_text" else (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
                        message = (day_message(self.state["snapshot"], date, self.group_name)
                                   if self.state.get("snapshot") is not None else "Расписание пока не загружено.")
                        for part in split_message(message):
                            self.send("telegram", part)
                    elif verb in ("/start", "/help"):
                        self.send("telegram", "Выберите день или листайте недели кнопками ниже. Команды /today, /tomorrow, /week и /nextweek тоже работают; /today_text и /tomorrow_text дают текст.")
                save_state(self.state_file, self.state)
        except Exception as exc:
            LOG.error("Ошибка обработки команд Telegram: %s", exc)

    def cycle(self) -> None:
        now = datetime.now(MOSCOW)
        today = now.date().isoformat()
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
                message = changes_message(old, snapshot, today, self.group_name)
                if message:
                    for channel in self.channels():
                        self.queue(channel, message)
                        changed = set()
                        for code in set(old) | set(snapshot):
                            if old.get(code) != snapshot.get(code):
                                changed.update(lesson_date(item) for item in (old.get(code), snapshot.get(code)) if item)
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
        self.telegram_commands(today)
        self.ensure_telegram_keyboard()
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
    print(f"Токен Telegram: {'задан' if token else 'НЕ ЗАДАН'}")
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
    if any(option in sys.argv for option in ("--preview", "--preview-card", "--preview-week")):
        root = Path(__file__).resolve().parent
        load_env(root / ".env")
        group_name = os.getenv("GROUP_NAME", "ВКБ51")
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
                preview_file.write_bytes(schedule_card(snapshot, today, group_name))
                print(f"Карточка сохранена: {preview_file}")
            elif "--preview-week" in sys.argv:
                preview_file = root / "data" / "preview_week.png"
                preview_file.parent.mkdir(parents=True, exist_ok=True)
                preview_file.write_bytes(week_card(snapshot, week_start(today), group_name))
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
        bot = Bot(Path(__file__).resolve().parent)
    except Exception as exc:
        LOG.error("Ошибка настройки: %s", exc)
        return 2
    LOG.info("Бот запущен для группы %s, проверка каждые %d секунд", bot.group_name, bot.interval)
    while not STOP:
        bot.cycle()
        deadline = time.monotonic() + bot.interval
        next_command_check = time.monotonic() + 10
        while not STOP and time.monotonic() < deadline:
            if time.monotonic() >= next_command_check:
                today = datetime.now(MOSCOW).date().isoformat()
                bot.telegram_commands(today)
                bot.drain(today)
                next_command_check = time.monotonic() + 10
            time.sleep(min(1, deadline - time.monotonic()))
    LOG.info("Бот остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
