"""Светлая недельная карточка: каждый день читается как короткий список."""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from card import MONTHS, WEEKDAYS, draw_change_badge, font, status_kind, text_width, wrap


WIDTH = 1300
BG = "#F8F7F2"
INK = "#1D2725"
MUTED = "#64716A"
LINE = "#D8DED5"
TYPE_COLORS = {"Лекция": "#287C63", "Практика": "#BD702B", "Лабораторная": "#79499B"}
STATUS = {"added": "#FFE45C", "changed": "#79A8F4", "removed": "#F16D65"}


def fitted_font(draw: ImageDraw.ImageDraw, text: str, sizes: tuple[int, ...], max_width: int):
    return next((font(size, True) for size in sizes
                 if text_width(draw, text, font(size, True)) <= max_width),
                font(sizes[-1], True))


def military_day(items: list[dict]) -> bool:
    return (len(items) >= 3 and not any(item.get("change") or item.get("removed") for item in items)
            and all("военная кафедра" in item["subject"].casefold() for item in items))


def build_rows(items: list[dict], slots: tuple) -> list[dict]:
    items = sorted(items, key=lambda item: (item["start"], 0 if item.get("removed") else 1,
                                           item["subject"], str(item.get("place") or "")))
    if not items:
        return []
    occupied = {item["start"] for item in items if not item.get("removed")}
    slot_starts = [start for start, _ in slots]
    known = [slot_starts.index(start) for start in occupied if start in slot_starts]
    windows = []
    if known:
        for index in range(min(known), max(known) + 1):
            if slot_starts[index] not in occupied:
                windows.append({"window": True, "start": slots[index][0], "end": slots[index][1]})
    return sorted(items + windows, key=lambda item: (item["start"],
                                                     0 if item.get("removed") else 1))


def render_list_week_card(group_name: str, monday: str, entries: list[dict],
                          base_slots: tuple, theme: str = "classic") -> bytes:
    """Список остаётся светлым, а сезонная тема меняет оттенки бумаги и меток."""
    bg, ink, muted, divider, room_fill = (
        (BG, INK, MUTED, LINE, "#E7EEE7") if theme == "autumn" else
        ("#F8FBFE", "#172636", "#617382", "#D8E2EA", "#E8F0F7"))
    type_colors = (TYPE_COLORS if theme == "autumn" else
                   {"Лекция": "#247A61", "Практика": "#B66D25", "Лабораторная": "#7851A1"})
    start = datetime.fromisoformat(monday).date()
    end = start + timedelta(days=6)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in entries:
        grouped[item["date"]].append(item)
    measure = ImageDraw.Draw(Image.new("RGB", (WIDTH, 100), bg))
    days = []
    y = 274
    for offset in range(7):
        day = start + timedelta(days=offset)
        items = grouped.get(day.isoformat(), [])
        rows = []
        if military_day(items):
            height = 126
        elif not items:
            height = 106
        else:
            for item in build_rows(items, base_slots):
                if item.get("window"):
                    row_height = 52
                    title_lines = []
                else:
                    changed = bool(item.get("change"))
                    title_lines = wrap(measure, item["subject"], font(25, True),
                                       495 if changed else 525)
                    row_height = max(104, 48 + len(title_lines) * 31 +
                                     (27 if item.get("teacher") else 0) + (28 if changed else 0))
                rows.append((item, title_lines, row_height))
            height = max(118, 14 + sum(row_height + 8 for _, _, row_height in rows))
        days.append((day, items, rows, y, height))
        y += height + 12

    image = Image.new("RGB", (WIDTH, y + 65), bg)
    draw = ImageDraw.Draw(image)
    draw.text((70, 54), "РАСПИСАНИЕ НА НЕДЕЛЮ", font=font(20, True), fill=muted)
    period = (f"{start.day}–{end.day} {MONTHS[start.month - 1]} {start.year}"
              if start.month == end.month and start.year == end.year else
              f"{start.day} {MONTHS[start.month - 1]} — {end.day} {MONTHS[end.month - 1]} {end.year}")
    heading = f"{period}  ·  {group_name}"
    heading_font = fitted_font(draw, heading, (62, 58, 54, 50, 46, 42, 38), 1160)
    draw.text((68, 111), heading, font=heading_font, fill=ink)
    draw.line((70, 253, 1230, 253), fill=ink, width=3)

    for day, items, rows, top, height in days:
        draw.text((72, top + 11), str(day.day), font=font(59, True), fill=ink)
        draw.text((153, top + 21), WEEKDAYS[day.weekday()].upper(),
                  font=font(22, True), fill=muted)
        if military_day(items):
            draw.rounded_rectangle((337, top + 17, 345, top + 100), radius=4,
                                   fill=type_colors["Лекция"])
            draw.text((365, top + 33), "ВОЕННАЯ КАФЕДРА", font=font(37, True), fill=ink)
        elif not items:
            label = "ВЫХОДНОЙ" if day.weekday() == 6 else "Нет занятий"
            draw.text((365, top + 35), label, font=font(29, True), fill=muted)
        else:
            row_top = top + 13
            for item, title_lines, row_height in rows:
                if item.get("window"):
                    draw.text((365, row_top + 7),
                              f"{item['start']}–{item['end']}  /  ОКНО",
                              font=font(21, True), fill="#6B8C85")
                    row_top += row_height + 8
                    continue
                marker = item.get("change", "")
                kind = status_kind(marker) if marker else ""
                accent = type_colors.get(item["type"], muted)
                if kind == "removed":
                    draw.rounded_rectangle((320, row_top, 1230, row_top + row_height - 3),
                                           radius=14, fill="#F9DFDC" if theme == "autumn" else "#FCE4E5")
                draw.rounded_rectangle((337, row_top + 4, 345, row_top + row_height - 13),
                                       radius=4, fill=accent)
                draw.text((365, row_top + 3), item["start"], font=font(27, True), fill=ink)
                draw.text((365, row_top + 39), item["end"], font=font(20), fill=muted)
                draw.text((482, row_top + 5),
                          {"Лекция": "ЛЕК", "Практика": "ПР", "Лабораторная": "ЛАБ"}.get(item["type"], "ПАРА"),
                          font=font(18, True), fill=accent)
                if marker:
                    draw_change_badge(draw, 1224, row_top + 4, marker, STATUS[kind], 17)
                title_y = row_top + (38 if marker else 30)
                for line in title_lines:
                    draw.text((482, title_y), line, font=font(25, True), fill=ink)
                    title_y += 31
                if item.get("teacher"):
                    draw.text((482, title_y + 3), str(item["teacher"]),
                              font=font(19), fill=muted)
                room = str(item.get("place") or "")
                if room:
                    room_face = fitted_font(draw, room, (26, 24, 22, 20, 18), 256)
                    room_width = text_width(draw, room, room_face) + 32
                    room_left = 1226 - room_width
                    room_changed = marker == "АУДИТОРИЯ ИЗМЕНЕНА"
                    draw.rounded_rectangle((room_left, row_top + row_height - 65,
                                            1226, row_top + row_height - 12),
                                           radius=13,
                                           fill=STATUS["changed"] if room_changed else room_fill)
                    draw.text((room_left + 16, row_top + row_height - 54), room,
                              font=room_face, fill="#25151A" if room_changed else ink)
                row_top += row_height + 8
        draw.line((70, top + height, 1230, top + height), fill=divider, width=2)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
