"""Недельная карточка с днями по горизонтали и парами по вертикали."""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from card import MONTHS, draw_leaf, font, palette, status_kind, text_width, wrap


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


def draw_lesson(draw: ImageDraw.ImageDraw, x: int, y: int, height: int,
                item: dict, number: int, total: int, colors: dict) -> None:
    marker = item.get("change", "")
    changed = bool(marker)
    kind = status_kind(marker) if changed else ""
    status_color = colors["status"].get(kind)
    accent = colors["types"].get(item["type"], colors["accent"])
    fill = "#4A2D29" if kind == "removed" else colors["tints"].get(item["type"], colors["card"])
    x2, y2 = x + DAY_WIDTH, y + height
    draw.rounded_rectangle((x, y, x2, y2), radius=20, fill=fill,
                           outline=status_color, width=4 if changed else 1)
    draw.rounded_rectangle((x + 2, y + 18, x + 9, y2 - 18), radius=4, fill=accent)
    kind = item["type"].upper() + (f" · {number}/{total}" if total > 1 else "")
    draw.text((x + 24, y + 15), kind, font=font(20, True), fill=accent)
    if changed:
        mark = "АУД. ИЗМЕНЕНА" if marker == "АУДИТОРИЯ ИЗМЕНЕНА" else marker
        mark_face = font(15, True)
        mark_width = text_width(draw, mark, mark_face) + 20
        draw.rounded_rectangle((x2 - mark_width - 14, y + 11, x2 - 14, y + 42),
                               radius=8, fill=status_color)
        draw.text((x2 - mark_width - 4, y + 16), mark, font=mark_face, fill="#25151A")

    teacher_y = y2 - 92
    title_face, title_lines = fitted_lines(draw, item["subject"], DAY_WIDTH - 45,
                                           teacher_y - (y + 52) - 8)
    ty = y + 50
    for line in title_lines:
        draw.text((x + 24, ty), line, font=title_face, fill=colors["white"])
        ty += title_face.size + 4

    teacher = str(item.get("teacher") or "—")
    draw.rounded_rectangle((x + 18, teacher_y, x2 - 16, teacher_y + 39),
                           radius=10, fill=colors["teacher"])
    draw.text((x + 29, teacher_y + 8), "ПРЕП.", font=font(17, True),
              fill=colors["teacher_label"])
    teacher_face = next((font(size, True) for size in (24, 22, 20, 18, 16)
                         if text_width(draw, teacher, font(size, True)) < DAY_WIDTH - 125), font(16, True))
    draw.text((x2 - 27 - text_width(draw, teacher, teacher_face), teacher_y + 5),
              teacher, font=teacher_face, fill=colors["white"])

    room = str(item.get("place") or "—")
    room_y = y2 - 48
    draw.rounded_rectangle((x + 18, room_y, x2 - 16, y2 - 12), radius=10,
                           fill=colors["room"],
                           outline=status_color if marker == "АУДИТОРИЯ ИЗМЕНЕНА" else accent,
                           width=2)
    room_face = next((font(size, True) for size in (25, 23, 21, 19, 17)
                      if text_width(draw, room, font(size, True)) < DAY_WIDTH - 104), font(17, True))
    draw.text((x + 30, room_y + 5), "АУД.", font=font(18, True), fill=colors["muted"])
    draw.text((x2 - 28 - text_width(draw, room, room_face), room_y + 3),
              room, font=room_face, fill=colors["white"])


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

    row_counts = [max(len(grouped.get(((start + timedelta(days=day)).isoformat(), begin), []))
                      for day in range(7)) for begin, _ in slots]
    row_heights = [max(CARD_HEIGHT, count * CARD_HEIGHT + max(0, count - 1) * ROW_GAP)
                   for count in row_counts]
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
    draw.rounded_rectangle((WIDTH - MARGIN - 165, 84, WIDTH - MARGIN, 142),
                           radius=18, fill=colors["header"])
    group_font = next((font(size, True) for size in (31, 28, 25, 22, 20)
                       if text_width(draw, group_name, font(size, True)) <= 145), font(20, True))
    draw.text((WIDTH - MARGIN - 82 - text_width(draw, group_name, group_font) / 2, 93),
              group_name, font=group_font, fill=colors["white"])
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
        if not occupied_by_day[date] and not any(item_date == date for item_date, _ in grouped):
            draw.rounded_rectangle((x, TOP, x + DAY_WIDTH, bottom), radius=20,
                                   fill=colors["section"])
            label = "СВОБОДНЫЙ ДЕНЬ"
            draw.text((x + (DAY_WIDTH - text_width(draw, label, font(21, True))) / 2,
                       TOP + (bottom - TOP) / 2), label, font=font(21, True), fill=colors["muted"])

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
            if not occupied_by_day[date] and not any(item_date == date for item_date, _ in grouped):
                continue
            items = grouped.get((date, begin), [])
            if items:
                if len(items) == 1:
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

    draw.text((MARGIN, height - 70), "ИСТОЧНИК: ДГТУ", font=font(21), fill=colors["footer"])
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
