"""Месячный календарь предметов: понедельник–суббота без скрытых записей."""

from __future__ import annotations

import calendar
import io
import math
import re
from collections import defaultdict
from datetime import date, datetime

from PIL import Image, ImageDraw

from card import font, palette, status_kind, text_width, wrap


WIDTH = 2160
MARGIN = 48
GAP = 12
CELL_WIDTH = (WIDTH - 2 * MARGIN - 5 * GAP) // 6
TOP = 220
MONTH_NAMES = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
               "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
WEEKDAYS = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ")


def month_weeks(month: str) -> list[list[date | None]]:
    """Полные календарные строки выбранного месяца; воскресенья исключены."""
    first = datetime.fromisoformat(month).date().replace(day=1)
    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(first.year, first.month)
    return [[date(first.year, first.month, day) if day else None for day in week[:6]]
            for week in weeks if any(week[:6])]


def day_rows(entries: list[dict], base_slots: tuple) -> list[dict]:
    """Все параллельные записи сохраняются; окна — только целиком свободные слоты."""
    rows = [dict(entry) for entry in entries]
    active = [entry for entry in entries if not entry.get("removed")]
    if active:
        begin = min(entry["start"] for entry in active)
        finish = max(entry["end"] for entry in active)
        for start, end in base_slots:
            if (begin < start and end < finish
                    and not any(entry["start"] < end and entry["end"] > start for entry in active)
                    and not any(entry["start"] < end and entry["end"] > start for entry in entries)):
                rows.append({"window": True, "start": start, "end": end})
    return sorted(rows, key=lambda row: (row["start"], row.get("removed", False),
                                         row.get("subject", ""), str(row.get("place", ""))))


def military_day(entries: list[dict]) -> bool:
    return bool(entries) and all("военная кафедра" in str(item.get("subject", "")).casefold()
                                 and not item.get("change") and not item.get("removed")
                                 for item in entries)


def _short_subject(subject: str) -> str:
    value = re.sub(r"\s*[,;]?\s*п\s*/\s*г\s*\d+\s*$", "", subject, flags=re.I).strip()
    substitutions = (
        ("программные и программно-аппаратные средства", "Прог. средства"),
        ("программирование алгоритмов", "Прог. алгоритмов"),
        ("иностранный язык", "Ин. язык"),
        ("математические методы", "Мат. методы"),
        ("быстрые алгоритмы", "Быстрые алг."),
    )
    for prefix, short in substitutions:
        if value.casefold().startswith(prefix):
            return short
    return value or "Занятие"


def _ellipsize(draw: ImageDraw.ImageDraw, value: str, size: int, width: int,
               bold: bool = True) -> str:
    face = font(size, bold)
    if text_width(draw, value, face) <= width:
        return value
    shortened = value
    while shortened and text_width(draw, shortened.rstrip() + "…", face) > width:
        shortened = shortened[:-1]
    return shortened.rstrip() + "…"


def _row_layout(draw: ImageDraw.ImageDraw, row: dict) -> dict:
    if row.get("window"):
        return {"row": row, "height": 36}
    room = str(row.get("place") or "—")
    room_size = 18
    room_width = text_width(draw, room, font(room_size, True))
    # Small rooms remain on the same line. Longer labels keep their full text below it.
    room_below = room_width > 78
    subject_width = CELL_WIDTH - 113 - (0 if room_below else room_width)
    subject = _short_subject(str(row.get("subject") or ""))
    subgroup = row.get("subgroup")
    if not subgroup:
        match = re.search(r"п\s*/\s*г\s*(\d+)\s*$", str(row.get("subject") or ""), re.I)
        subgroup = match.group(1) if match else None
    suffix = f" · п/г{subgroup}" if subgroup else ""
    size = 20
    if suffix:
        size = 18
    title = _ellipsize(draw, subject, size,
                       max(20, subject_width - text_width(draw, suffix, font(size, True)))) + suffix
    room_lines = wrap(draw, room, font(room_size, True), CELL_WIDTH - 43) if room_below else []
    changed = bool(row.get("change") or row.get("removed"))
    return {"row": row, "height": 39 + len(room_lines) * 24 + (25 if changed else 0),
            "room": room, "room_size": room_size, "room_width": room_width,
            "room_lines": room_lines, "title": title, "title_size": size}


def photo_size(width: int, height: int) -> tuple[int, int, float]:
    """Telegram photo limits, without clipping even an unusually busy month."""
    padded_width = max(width, math.ceil(height / 20))
    padded_height = max(height, math.ceil(width / 20))
    scale = min(1.0, 9998 / (padded_width + padded_height))
    return max(1, math.ceil(padded_width * scale)), max(1, math.ceil(padded_height * scale)), scale


class _Canvas:
    """Draw directly at the final scale instead of allocating an enormous temporary image."""

    def __init__(self, image: Image.Image, scale: float, offset: float = 0):
        self.draw = ImageDraw.Draw(image)
        self.scale = scale
        self.offset = offset
        self.faces = {}

    def rect(self, box: tuple, fill: str, radius: int = 10, outline: str | None = None):
        x, y, right, bottom = box
        self.draw.rounded_rectangle(((x + self.offset) * self.scale, y * self.scale,
                                     (right + self.offset) * self.scale, bottom * self.scale),
                                    radius=max(1, round(radius * self.scale)), fill=fill,
                                    outline=outline, width=max(1, round(self.scale)))

    def text(self, x: float, y: float, value: str, size: int, fill: str, bold: bool = False):
        key = (size, bold)
        if key not in self.faces:
            self.faces[key] = font(max(1, round(size * self.scale)), bold)
        self.draw.text(((x + self.offset) * self.scale, y * self.scale), value,
                       font=self.faces[key], fill=fill)

    def center(self, x: float, y: float, width: int, value: str, size: int, fill: str):
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        self.text(x + (width - text_width(measure, value, font(size, True))) / 2,
                  y, value, size, fill, True)


def render_month_card(group_name: str, month: str, entries: list[dict],
                      base_slots: tuple, theme: str = "classic") -> bytes:
    """Компактный календарь месяца: время, короткий предмет, аудитория, окна."""
    colors = palette(theme)
    first = datetime.fromisoformat(month).date().replace(day=1)
    grouped: dict[date, list[dict]] = defaultdict(list)
    for item in entries:
        day = datetime.fromisoformat(item["date"]).date()
        if (day.year, day.month) == (first.year, first.month) and day.weekday() < 6:
            grouped[day].append(item)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    layouts = []
    y = TOP
    for week in month_weeks(month):
        cells = []
        for day in week:
            items = grouped.get(day, [])
            military = military_day(items)
            rows = [] if military else [_row_layout(measure, row) for row in day_rows(items, base_slots)]
            cells.append({"date": day, "military": military, "rows": rows})
        height = max(260, *(63 + sum(row["height"] + 5 for row in cell["rows"]) for cell in cells))
        layouts.append((cells, y, height))
        y += height + GAP
    natural_height = y + 65
    width, height, scale = photo_size(WIDTH, natural_height)
    image = Image.new("RGB", (width, height), colors["background"])
    offset = (max(WIDTH, math.ceil(natural_height / 20)) - WIDTH) / 2
    canvas = _Canvas(image, scale, offset)
    canvas.text(MARGIN, 28, "РАСПИСАНИЕ НА МЕСЯЦ", 23, colors["accent"], True)
    canvas.text(MARGIN - 3, 67, f"{MONTH_NAMES[first.month - 1].upper()} {first.year}", 54, colors["white"], True)
    group_size = next((size for size in (30, 27, 24, 21, 18)
                       if text_width(measure, group_name, font(size, True)) <= 650), 18)
    group_lines = wrap(measure, group_name, font(group_size, True), 650)
    group_height = max(49, 16 + len(group_lines) * (group_size + 3))
    group_width = max(text_width(measure, line, font(group_size, True)) for line in group_lines) + 38
    gx = WIDTH - MARGIN - group_width
    canvas.rect((gx, 67, WIDTH - MARGIN, 67 + group_height), colors["header"], 13)
    for line_number, line in enumerate(group_lines):
        canvas.text(gx + 19, 73 + line_number * (group_size + 3), line, group_size, colors["white"], True)
    x = MARGIN
    for name in ("Лекция", "Практика", "Лабораторная"):
        canvas.rect((x, 151, x + 14, 165), colors["types"][name], 3)
        canvas.text(x + 24, 144, name, 20, colors["muted"])
        x += text_width(measure, name, font(20)) + 62
    for column, name in enumerate(WEEKDAYS):
        canvas.text(MARGIN + column * (CELL_WIDTH + GAP) + 12, 185, name, 23, colors["accent"], True)

    for cells, y, week_height in layouts:
        for column, cell in enumerate(cells):
            day = cell["date"]
            if day is None:
                continue
            x = MARGIN + column * (CELL_WIDTH + GAP)
            canvas.rect((x, y, x + CELL_WIDTH, y + week_height), colors["section"], 16)
            canvas.text(x + 14, y + 8, str(day.day), 31, colors["white"], True)
            if cell["military"]:
                canvas.rect((x + 10, y + 52, x + CELL_WIDTH - 10, y + week_height - 12),
                            colors["tints"]["Лекция"], 12)
                middle = y + 52 + (week_height - 64) / 2
                for line_number, word in enumerate(("ВОЕННАЯ", "КАФЕДРА")):
                    canvas.center(x, middle - 35 + line_number * 37, CELL_WIDTH, word, 28, colors["types"]["Лекция"])
                continue
            if not cell["rows"]:
                canvas.center(x, y + week_height / 2, CELL_WIDTH, "Нет записей", 25, colors["muted"])
                continue
            top = y + 53
            for layout in cell["rows"]:
                item, row_height = layout["row"], layout["height"]
                if item.get("window"):
                    canvas.rect((x + 9, top, x + CELL_WIDTH - 9, top + row_height), colors["window"],
                                8, colors["window_outline"])
                    canvas.text(x + 21, top + 6, f"{item['start']}   ОКНО", 18, colors["window_text"])
                    top += row_height + 5
                    continue
                marker = "УДАЛЕНО" if item.get("removed") else item.get("change", "")
                kind = item.get("type", "")
                status = colors["status"].get(status_kind(marker)) if marker else None
                fill = "#792F32" if marker == "УДАЛЕНО" else colors["tints"].get(kind, colors["card"])
                accent = colors["types"].get(kind, colors["accent"])
                canvas.rect((x + 9, top, x + CELL_WIDTH - 9, top + row_height), fill, 8)
                canvas.rect((x + 9, top + 4, x + 13, top + row_height - 4), accent, 2)
                line_top = top + 5
                if marker:
                    label = {"ДОБАВЛЕНО": "+ ДОБАВЛЕНО", "УДАЛЕНО": "− УДАЛЕНО",
                             "АУДИТОРИЯ ИЗМЕНЕНА": "↔ АУДИТОРИЯ"}.get(marker, "↔ ИЗМЕНЕНО")
                    label_width = text_width(measure, label, font(14, True)) + 16
                    label_x = x + CELL_WIDTH - label_width - 15
                    canvas.rect((label_x, line_top, x + CELL_WIDTH - 15, line_top + 21), status, 6)
                    canvas.text(label_x + 8, line_top + 1, label, 14, "#25151A", True)
                    line_top += 25
                canvas.text(x + 21, line_top + 1, item["start"], 19, accent, True)
                canvas.text(x + 85, line_top, layout["title"], layout["title_size"], colors["white"], True)
                room_color = status if marker == "АУДИТОРИЯ ИЗМЕНЕНА" else colors["muted"]
                if layout["room_lines"]:
                    for line_number, line in enumerate(layout["room_lines"]):
                        canvas.text(x + 21, line_top + 27 + line_number * 24, line, 18, room_color, True)
                else:
                    canvas.text(x + CELL_WIDTH - 18 - layout["room_width"], line_top + 1,
                                layout["room"], layout["room_size"], room_color, True)
                top += row_height + 5
    canvas.text(MARGIN, natural_height - 43, "ИСТОЧНИК: ДГТУ", 18, colors["footer"])
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
