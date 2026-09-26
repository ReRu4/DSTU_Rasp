"""Недельная карточка с днями по горизонтали и парами по вертикали."""

from __future__ import annotations

import io
import re
from collections import defaultdict
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from card import (MONTHS, draw_change_badge, draw_leaf, font, palette,
                  status_kind, text_width, wrap)


WIDTH = 2880
MARGIN = 64
TIME_WIDTH = 136
GAP = 12
DAY_WIDTH = (WIDTH - 2 * MARGIN - TIME_WIDTH - 8 * GAP) // 7
TOP = 330
CARD_HEIGHT = 265
ROW_GAP = 10
WEEKDAYS = ("ПОНЕДЕЛЬНИК", "ВТОРНИК", "СРЕДА", "ЧЕТВЕРГ", "ПЯТНИЦА", "СУББОТА", "ВОСКРЕСЕНЬЕ")


def horizontal_colors(theme: str) -> dict:
    base = palette(theme)
    if theme == "autumn":
        return {**base, "day_header": "#554034", "time": "#4B382F",
                "teacher": "#655143", "room": "#433932", "empty": "#29221F",
                "overlap": "#F4C074", "teacher_label": "#F2DBC3"}
    return {**base, "day_header": "#26394D", "time": "#25364A",
            "teacher": "#3D5770", "room": "#25384B", "empty": "#141F2D",
            "overlap": "#FFC56C", "teacher_label": "#D6E3EF"}


def fitted_lines(draw: ImageDraw.ImageDraw, value: str,
                 max_width: int, max_height: int) -> tuple:
    for size in (27, 25, 23, 21, 19, 18):
        face = font(size, True)
        lines = wrap(draw, value, face, max_width)
        if len(lines) * (size + 4) <= max_height:
            return face, lines
    face = font(18, True)
    return face, wrap(draw, value, face, max_width)


def fitted_font(draw: ImageDraw.ImageDraw, value: str, sizes: tuple[int, ...],
                max_width: int):
    return next((font(size, True) for size in sizes
                 if text_width(draw, value, font(size, True)) <= max_width),
                font(sizes[-1], True))


def paired(items: list[dict]) -> bool:
    return len(items) == 2 and not any(item.get("change") or item.get("removed") for item in items)


def common_subject(value: str) -> str:
    return re.sub(r"\s*[,;]?\s*п\s*/\s*г\s*\d+\s*$", "", value,
                  flags=re.IGNORECASE).rstrip(" ,;·")


def draw_lesson(draw: ImageDraw.ImageDraw, x: int, y: int, height: int,
                item: dict, number: int, total: int, colors: dict) -> None:
    marker = item.get("change", "")
    changed = bool(marker)
    kind = status_kind(marker) if changed else ""
    status_color = colors["status"].get(kind)
    accent = colors["types"].get(item["type"], colors["accent"])
    fill = "#4A2D29" if kind == "removed" else colors["tints"].get(item["type"], colors["card"])
    x2, y2 = x + DAY_WIDTH, y + height
    draw.rounded_rectangle((x, y, x2, y2), radius=20, fill=fill)
    draw.rounded_rectangle((x + 2, y + 18, x + 9, y2 - 18), radius=4, fill=accent)
    kind_label = item["type"].upper() + (f" · {number}/{total}" if total > 1 else "")
    draw.text((x + 24, y + (52 if changed else 15)), kind_label,
              font=font(19, True), fill=accent)
    if changed:
        draw_change_badge(draw, x2 - 13, y + 11, marker, status_color, 15)

    teacher_y = y2 - 45
    title_top = y + (82 if changed else 50)
    title_face, title_lines = fitted_lines(draw, item["subject"], DAY_WIDTH - 45,
                                           teacher_y - title_top - 8)
    ty = title_top
    for line in title_lines[:max(1, (teacher_y - title_top - 6) // (title_face.size + 4))]:
        draw.text((x + 24, ty), line, font=title_face, fill=colors["white"])
        ty += title_face.size + 4

    teacher = str(item.get("teacher") or "—")
    room = str(item.get("place") or "—")
    room_face = fitted_font(draw, room, (24, 22, 20, 18, 16), DAY_WIDTH - 86)
    room_width = min(DAY_WIDTH - 40, text_width(draw, room, room_face) + 26)
    room_left = x2 - 15 - room_width
    room_changed = marker == "АУДИТОРИЯ ИЗМЕНЕНА"
    draw.rounded_rectangle((room_left, y2 - 51, x2 - 15, y2 - 12), radius=9,
                           fill=status_color if room_changed else colors["room"])
    draw.text((room_left + 13, y2 - 46), room, font=room_face,
              fill="#25151A" if room_changed else colors["white"])
    teacher_face = fitted_font(draw, teacher, (20, 18, 16, 14),
                               max(65, room_left - x - 35))
    draw.text((x + 20, teacher_y + 1), teacher, font=teacher_face, fill=colors["white"])


def draw_parallel(draw: ImageDraw.ImageDraw, x: int, y: int, height: int,
                  items: list[dict], colors: dict) -> None:
    """Две записи в одном слоте: каждая сторона сохраняет свои данные."""
    first = items[0]
    accent = colors["types"].get(first["type"], colors["accent"])
    fill = colors["tints"].get(first["type"], colors["card"])
    x2, y2 = x + DAY_WIDTH, y + height
    draw.rounded_rectangle((x, y, x2, y2), radius=20, fill=fill)
    draw.rounded_rectangle((x + 2, y + 18, x + 9, y2 - 18), radius=4, fill=accent)
    shared = (common_subject(items[0]["subject"]) == common_subject(items[1]["subject"])
              and items[0]["type"] == items[1]["type"])
    if shared:
        draw.text((x + 22, y + 13), first["type"].upper(), font=font(19, True), fill=accent)
        title_face, title_lines = fitted_lines(draw, common_subject(first["subject"]),
                                               DAY_WIDTH - 43, 102)
        for index, line in enumerate(title_lines[:4]):
            draw.text((x + 22, y + 43 + index * (title_face.size + 3)),
                      line, font=title_face, fill=colors["white"])
        divider_y = y + 154
        draw.line((x + 18, divider_y, x2 - 17, divider_y), fill=accent, width=2)
    else:
        divider_y = y + 15
    middle = x + DAY_WIDTH // 2
    draw.line((middle, divider_y + 8, middle, y2 - 12), fill=accent, width=2)
    for index, item in enumerate(items, 1):
        left = x + 18 if index == 1 else middle + 10
        right = middle - 8 if index == 1 else x2 - 15
        subgroup = item.get("subgroup")
        distinct_subgroups = (items[0].get("subgroup") and items[1].get("subgroup")
                              and items[0]["subgroup"] != items[1]["subgroup"])
        label = (f"П/Г {subgroup}" if distinct_subgroups else f"ЗАПИСЬ {index}") if shared else f"{index}. {item['type'].upper()}"
        draw.text((left, y + (164 if shared else 17)), label,
                  font=font(16, True), fill=colors["types"].get(item["type"], accent))
        if not shared:
            title_face, title_lines = fitted_lines(draw, item["subject"], right - left,
                                                   height - 125)
            title_y = y + 47
            for line in title_lines[:4]:
                draw.text((left, title_y), line, font=title_face, fill=colors["white"])
                title_y += title_face.size + 3
        teacher = str(item.get("teacher") or "—")
        if shared:
            teacher_face = font(19, True)
            teacher_lines = wrap(draw, teacher, teacher_face, right - left)
            if len(teacher_lines) > 2:
                teacher_face = font(16, True)
                teacher_lines = wrap(draw, teacher, teacher_face, right - left)
            for line_number, text in enumerate(teacher_lines[:2]):
                draw.text((left, y + 199 + line_number * (teacher_face.size + 3)),
                          text, font=teacher_face, fill=colors["white"])
        else:
            teacher_face = fitted_font(draw, teacher, (17, 16, 15, 14), right - left)
            draw.text((left, y + height - 84), teacher,
                      font=teacher_face, fill=colors["white"])
        room = str(item.get("place") or "—")
        room_face = fitted_font(draw, room, (20, 18, 16, 14), right - left - 16)
        room_width = min(right - left, text_width(draw, room, room_face) + 16)
        draw.rounded_rectangle((left, y2 - 46, left + room_width, y2 - 12),
                               radius=8, fill=colors["room"])
        draw.text((left + 8, y2 - 43), room, font=room_face, fill=colors["white"])


def render_horizontal_week_card(group_name: str, monday: str, entries: list[dict],
                                base_slots: tuple, theme: str = "classic") -> bytes:
    """Все совпадающие по времени записи получают отдельные блоки в общей строке."""
    colors = horizontal_colors(theme)
    start = datetime.fromisoformat(monday).date()
    end = start + timedelta(days=6)

    # Нестандартное время тоже должно появиться в таблице.
    slot_ends = {begin: finish for begin, finish in base_slots}
    for item in entries:
        begin, finish = item["start"], item["end"]
        if begin not in slot_ends or finish > slot_ends[begin]:
            slot_ends[begin] = finish
    slots = sorted(slot_ends.items())
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in entries:
        grouped[(item["date"], item["start"])].append(item)
    for items in grouped.values():
        items.sort(key=lambda item: (item.get("removed", False), item["subject"], item.get("place", "")))

    military_days = set()
    for day in range(7):
        date = (start + timedelta(days=day)).isoformat()
        daily = [item for item in entries if item["date"] == date]
        if (len(daily) >= 3 and not any(item.get("change") or item.get("removed") for item in daily)
                and all("военная кафедра" in item["subject"].casefold() for item in daily)):
            military_days.add(date)
            for key in list(grouped):
                if key[0] == date:
                    del grouped[key]

    row_counts = [max((1 if paired(items) else len(items))
                      for day in range(7)
                      for items in [grouped.get(((start + timedelta(days=day)).isoformat(), begin), [])])
                  for begin, _ in slots]
    row_heights = [max(CARD_HEIGHT,
                       *(CARD_HEIGHT + 35 if paired(items) else
                         len(items) * CARD_HEIGHT + max(0, len(items) - 1) * ROW_GAP
                         for day in range(7)
                         for items in [grouped.get(((start + timedelta(days=day)).isoformat(), begin), [])]))
                   for begin, _ in slots]
    row_tops = []
    y = TOP
    for height in row_heights:
        row_tops.append(y)
        y += height + ROW_GAP
    bottom = y - ROW_GAP
    height = bottom + 114
    image = Image.new("RGB", (WIDTH, height), colors["background"])
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((MARGIN, 45, WIDTH - MARGIN, 57), radius=6, fill=colors["accent"])
    draw.text((MARGIN, 80), "НЕДЕЛЬНОЕ РАСПИСАНИЕ", font=font(32, True), fill=colors["accent"])
    period = (f"{start.day}–{end.day} {MONTHS[start.month - 1]} {start.year}"
              if start.month == end.month and start.year == end.year else
              f"{start.day} {MONTHS[start.month - 1]} — {end.day} {MONTHS[end.month - 1]} {end.year}")
    draw.text((MARGIN, 125), period, font=font(62, True), fill=colors["white"])
    group_font = next((font(size, True) for size in (31, 28, 25, 22, 20)
                       if text_width(draw, group_name, font(size, True)) <= 500), font(20, True))
    group_width = text_width(draw, group_name, group_font) + 38
    group_left = WIDTH - MARGIN - group_width
    draw.rounded_rectangle((group_left, 84, WIDTH - MARGIN, 142),
                           radius=18, fill=colors["header"])
    draw.text((group_left + 19, 93), group_name, font=group_font, fill=colors["white"])
    legend_x = MARGIN
    for name in ("Лекция", "Практика", "Лабораторная"):
        accent = colors["types"][name]
        draw.rounded_rectangle((legend_x, 217, legend_x + 19, 236), radius=5, fill=accent)
        draw.text((legend_x + 30, 210), name, font=font(24), fill=colors["muted"])
        legend_x += text_width(draw, name, font(24)) + 74
    if theme == "autumn":
        draw_leaf(draw, WIDTH - 520, 155, 46, -0.65, "#A85F38")
        draw_leaf(draw, WIDTH - 430, 126, 39, 0.7, "#D4914C")
        draw_leaf(draw, WIDTH - 335, 183, 31, -0.25, "#8C7149")
        draw.text((WIDTH - MARGIN - 310, 215), f"ОСЕНЬ · {start.year}",
                  font=font(22, True), fill=colors["accent"])

    occupied_by_day: dict[str, list[int]] = {}
    slot_index = {begin: index for index, (begin, _) in enumerate(slots)}
    for day in range(7):
        date = (start + timedelta(days=day)).isoformat()
        occupied_by_day[date] = [slot_index[begin] for (item_date, begin), values in grouped.items()
                                 if item_date == date and any(not value.get("removed") for value in values)]
        x = MARGIN + TIME_WIDTH + GAP + day * (DAY_WIDTH + GAP)
        draw.rounded_rectangle((x, 265, x + DAY_WIDTH, 316), radius=15, fill=colors["day_header"])
        name = WEEKDAYS[day]
        draw.text((x + 14, 274), name, font=font(21 if len(name) > 9 else 23, True),
                  fill=colors["white"])
        date_label = (start + timedelta(days=day)).strftime("%d.%m")
        draw.text((x + DAY_WIDTH - 16 - text_width(draw, date_label, font(23, True)), 273),
                  date_label, font=font(23, True), fill=colors["accent"])
        if date in military_days:
            continue
        if not occupied_by_day[date] and not any(item_date == date for item_date, _ in grouped):
            draw.rounded_rectangle((x, TOP, x + DAY_WIDTH, bottom), radius=20,
                                   fill=colors["section"])
            label = "ВЫХОДНОЙ" if day == 6 else "НЕТ ЗАНЯТИЙ"
            label_font = fitted_font(draw, label, (35, 32, 29, 26), DAY_WIDTH - 28)
            draw.text((x + (DAY_WIDTH - text_width(draw, label, label_font)) / 2,
                       TOP + (bottom - TOP) / 2), label, font=label_font, fill=colors["muted"])
            if day == 6:
                extra = "Нет занятий"
                draw.text((x + (DAY_WIDTH - text_width(draw, extra, font(22))) / 2,
                           TOP + (bottom - TOP) / 2 + 54), extra,
                          font=font(22), fill=colors["muted"])

    for index, (begin, finish) in enumerate(slots):
        y, row_height = row_tops[index], row_heights[index]
        draw.rounded_rectangle((MARGIN, y, MARGIN + TIME_WIDTH, y + row_height),
                               radius=17, fill=colors["time"])
        draw.text((MARGIN + 24, y + 25), f"{index + 1:02d}", font=font(39, True), fill=colors["white"])
        draw.text((MARGIN + 21, y + 89), begin, font=font(23, True), fill=colors["accent"])
        draw.text((MARGIN + 21, y + 122), finish, font=font(21), fill=colors["muted"])
        if row_counts[index] > 1:
            draw.text((MARGIN + 18, y + 175), f"{row_counts[index]} ЗАПИСИ",
                      font=font(15, True), fill=colors["overlap"])
        for day in range(7):
            date = (start + timedelta(days=day)).isoformat()
            x = MARGIN + TIME_WIDTH + GAP + day * (DAY_WIDTH + GAP)
            if date in military_days:
                continue
            if not occupied_by_day[date] and not any(item_date == date for item_date, _ in grouped):
                continue
            items = grouped.get((date, begin), [])
            if items:
                if paired(items):
                    draw_parallel(draw, x, y, row_height, items, colors)
                elif len(items) == 1:
                    draw_lesson(draw, x, y, row_height, items[0], 1, 1, colors)
                else:
                    for number, item in enumerate(items, 1):
                        draw_lesson(draw, x, y + (number - 1) * (CARD_HEIGHT + ROW_GAP),
                                    CARD_HEIGHT, item, number, len(items), colors)
                continue
            occupied = occupied_by_day[date]
            inside = bool(occupied) and min(occupied) < index < max(occupied)
            draw.rounded_rectangle((x, y, x + DAY_WIDTH, y + row_height), radius=20,
                                   fill=colors["section"] if inside else colors["empty"])
            if inside:
                draw.rounded_rectangle((x + 17, y + 17, x + DAY_WIDTH - 17, y + row_height - 17),
                                       radius=14, outline=colors["window_outline"], width=2)
                label = "ОКНО"
                draw.text((x + (DAY_WIDTH - text_width(draw, label, font(29, True))) / 2,
                           y + row_height / 2 - 18), label, font=font(29, True), fill=colors["window_text"])
            else:
                draw.text((x + DAY_WIDTH / 2 - 9, y + row_height / 2 - 18),
                          "·", font=font(36), fill=colors["muted"])

    for date in military_days:
        day = (datetime.fromisoformat(date).date() - start).days
        x = MARGIN + TIME_WIDTH + GAP + day * (DAY_WIDTH + GAP)
        draw.rounded_rectangle((x, TOP, x + DAY_WIDTH, bottom), radius=20,
                               fill=colors["tints"]["Лекция"])
        draw.rounded_rectangle((x + 2, TOP + 18, x + 11, bottom - 18),
                               radius=4, fill=colors["types"]["Лекция"])
        for index, line in enumerate(("ВОЕННАЯ", "КАФЕДРА")):
            title_font = fitted_font(draw, line, (50, 45, 40, 36), DAY_WIDTH - 36)
            draw.text((x + (DAY_WIDTH - text_width(draw, line, title_font)) / 2,
                       TOP + (bottom - TOP) / 2 - 45 + index * 58),
                      line, font=title_font, fill=colors["white"])

    draw.text((MARGIN, height - 70), "ИСТОЧНИК: ДГТУ", font=font(21), fill=colors["footer"])
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
