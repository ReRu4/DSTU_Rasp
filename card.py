"""Рисование карточки расписания для Telegram и ВКонтакте."""

from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 960
BACKGROUND = "#101927"
CARD = "#1B2A3B"
WHITE = "#F5F8FF"
MUTED = "#A8B8CE"
CYAN = "#78D5C3"
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
TYPE_COLORS = {"Лекция": "#64D6A2", "Практика": "#FFC267", "Лабораторная": "#BE9AFF"}
TYPE_TINTS = {"Лекция": "#24483F", "Практика": "#4A3C2D", "Лабораторная": "#3E3454"}


def font_path(bold: bool) -> str:
    custom = os.getenv("CARD_FONT_BOLD" if bold else "CARD_FONT_REGULAR", "")
    candidates = [
        custom,
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("Не найден шрифт с поддержкой кириллицы для карточки")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(bold), size)


def text_width(draw: ImageDraw.ImageDraw, value: str, face: ImageFont.FreeTypeFont) -> int:
    return int(draw.textbbox((0, 0), value, font=face)[2])


def wrap(draw: ImageDraw.ImageDraw, value: str, face: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in (value or "").splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if text_width(draw, candidate, face) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            if text_width(draw, word, face) <= max_width:
                current = word
                continue
            # Длинное слово всё равно не должно выходить за границу карточки.
            chunk = ""
            for char in word:
                if chunk and text_width(draw, chunk + char, face) > max_width:
                    lines.append(chunk)
                    chunk = char
                else:
                    chunk += char
            current = chunk
        lines.append(current)
    return lines


def prepare_layout(group_name: str, date: str, blocks: list[dict], slot_count: int) -> tuple[list[dict], int]:
    measure = ImageDraw.Draw(Image.new("RGB", (WIDTH, 100), BACKGROUND))
    detail_font = font(24)
    layout = []
    y = 276
    for block in blocks:
        if block.get("window"):
            height = 116
            layout.append({"block": block, "y": y, "height": height})
            y += height + 20
            continue
        room_lines = (wrap(measure, str(block["place"]), font(29, True), 218)
                      if block.get("place") else [])
        room_height = 48 + len(room_lines) * 37 if room_lines else 0
        title_font = font(30 if room_lines else 32, True)
        title_lines = wrap(measure, block["subject"], title_font, 550 if room_lines else 740)
        title_span = max(len(title_lines) * 43, room_height)
        details = []
        if len(block["starts"]) > 1:
            starts = [f"{number}-я {start}" if number else start
                      for number, start in zip(block["pairs"], block["starts"])]
            details.append(("ПО ПАРАМ", " · ".join(starts)))
        if block.get("teacher"):
            details.append(("ПРЕПОДАВАТЕЛЬ", block["teacher"]))
        if block.get("subgroup"):
            details.append(("ПОДГРУППА", str(block["subgroup"])))
        if block.get("theme"):
            details.append(("ТЕМА", block["theme"]))
        if block.get("link"):
            details.append(("ССЫЛКА", block["link"]))
        detail_rows = [(label, wrap(measure, value, detail_font, 610)) for label, value in details]
        height = 126 + title_span + sum(max(35, len(lines) * 33) + 9 for _, lines in detail_rows) + 25
        layout.append({"block": block, "y": y, "height": height, "title_lines": title_lines,
                       "title_span": title_span, "room_lines": room_lines, "room_height": room_height,
                       "title_font_size": 30 if room_lines else 32,
                       "details": detail_rows})
        y += height + 20
    if not blocks:
        y += 220
    return layout, max(590, y + 90)


def render_card(group_name: str, date: str, blocks: list[dict], slot_count: int) -> bytes:
    """Вернуть PNG в памяти. blocks содержат уже подготовленные данные занятий."""
    layout, height = prepare_layout(group_name, date, blocks, slot_count)
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    regular_24 = font(24)
    regular_22 = font(22)
    medium_26 = font(26, True)
    bold_32 = font(32, True)
    bold_38 = font(38, True)
    bold_52 = font(52, True)

    # Верхняя акцентная полоса и шапка.
    draw.rounded_rectangle((42, 40, 918, 52), radius=6, fill=CYAN)
    draw.text((48, 80), "РАСПИСАНИЕ НА ДЕНЬ", font=medium_26, fill=CYAN)
    draw.rounded_rectangle((772, 74, 912, 122), radius=18, fill="#243951")
    group_width = text_width(draw, group_name, regular_24)
    draw.text((842 - group_width / 2, 83), group_name, font=regular_24, fill=WHITE)

    day = datetime.fromisoformat(date)
    heading = f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"
    draw.text((48, 134), heading, font=bold_52, fill=WHITE)
    draw.text((50, 201), str(day.year), font=regular_24, fill=MUTED)
    count_text = f"{slot_count} " + ("пара" if slot_count % 10 == 1 and slot_count % 100 != 11 else "пары" if slot_count % 10 in (2, 3, 4) and slot_count % 100 not in (12, 13, 14) else "пар")
    draw.rounded_rectangle((722, 186, 912, 238), radius=22, fill="#263A50")
    count_width = text_width(draw, count_text, medium_26)
    draw.text((817 - count_width / 2, 195), count_text, font=medium_26, fill=WHITE)

    if not blocks:
        draw.rounded_rectangle((42, 276, 918, 496), radius=28, fill=CARD)
        draw.ellipse((80, 325, 154, 399), outline=CYAN, width=5)
        draw.line((116, 342, 116, 364, 135, 376), fill=CYAN, width=5, joint="curve")
        draw.text((185, 326), "Занятий нет", font=bold_38, fill=WHITE)
        draw.text((187, 379), "На этот день в расписании нет пар", font=regular_24, fill=MUTED)
    for row in layout:
        block = row["block"]
        x1, y1, x2, y2 = 42, row["y"], 918, row["y"] + row["height"]
        if block.get("window"):
            draw.rounded_rectangle((x1, y1, x2, y2), radius=24, fill="#172332", outline="#496176", width=2)
            pair_text = (f"{block['pairs'][0]}-я пара" if len(block["pairs"]) == 1
                         else f"Пары {block['pairs'][0]}–{block['pairs'][-1]}")
            draw.text((78, y1 + 21), "ОКНО", font=medium_26, fill="#B5C8D6")
            draw.text((78, y1 + 57), pair_text, font=regular_22, fill=MUTED)
            time_text = f"{block['start']}–{block['end']}"
            draw.text((884 - text_width(draw, time_text, bold_32), y1 + 39), time_text,
                      font=bold_32, fill=WHITE)
            continue
        draw.rounded_rectangle((x1, y1, x2, y2), radius=28, fill=CARD)
        accent = TYPE_COLORS.get(block["type"], CYAN)
        draw.rounded_rectangle((x1, y1 + 26, x1 + 7, y2 - 26), radius=3, fill=accent)
        draw.text((78, y1 + 26), f"{block['start']}–{block['end']}", font=bold_38, fill=WHITE)
        type_text = block["type"].upper()
        if len(block["starts"]) > 1:
            kind = {"Лекция": "ЛЕКЦИИ", "Практика": "ПРАКТИКА", "Лабораторная": "ЛАБОРАТОРНЫЕ"}.get(block["type"], "ЗАНЯТИЯ")
            numbers = [number for number in block["pairs"] if number]
            range_text = f"{numbers[0]}–{numbers[-1]}" if numbers else str(len(block["starts"]))
            type_text = f"{range_text} ПАРЫ · {kind}"
        elif block["pairs"][0]:
            type_text = f"{block['pairs'][0]}-Я ПАРА · {type_text}"
        chip_width = text_width(draw, type_text, regular_22) + 36
        draw.rounded_rectangle((886 - chip_width, y1 + 28, 886, y1 + 70), radius=16,
                               fill=TYPE_TINTS.get(block["type"], "#263A50"))
        draw.text((904 - chip_width, y1 + 34), type_text, font=regular_22, fill=accent)
        title_y = y1 + 91
        for line in row["title_lines"]:
            draw.text((78, title_y), line, font=font(row["title_font_size"], True), fill=WHITE)
            title_y += 43
        if row["room_lines"]:
            room_top = y1 + 91
            draw.rounded_rectangle((648, room_top, 900, room_top + row["room_height"]),
                                   radius=18, fill="#263A50", outline=accent, width=3)
            draw.text((664, room_top + 8), "АУДИТОРИЯ", font=font(20, True), fill=MUTED)
            for index, line in enumerate(row["room_lines"]):
                draw.text((664, room_top + 38 + index * 37), line,
                          font=font(29, True), fill=WHITE)
        title_y = y1 + 91 + row["title_span"] + 15
        for label, lines in row["details"]:
            draw.text((78, title_y), label, font=regular_22, fill=MUTED)
            label_width = text_width(draw, label, regular_22)
            value_x = 96 + label_width
            for index, line in enumerate(lines):
                draw.text((value_x if index == 0 else 78, title_y + index * 33), line, font=regular_24, fill=WHITE)
            title_y += max(35, len(lines) * 33) + 9

    footer_y = height - 55
    draw.text((48, footer_y), "Источник: ДГТУ", font=regular_22, fill="#72859E")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_week_card(group_name: str, monday: str, days: list[dict]) -> bytes:
    """Компактная карточка недели: семь дней, цветные пары и отдельные окна."""
    measure = ImageDraw.Draw(Image.new("RGB", (WIDTH, 100), BACKGROUND))
    title_font = font(28, True)
    layouts = []
    y = 270
    for day in days:
        rows = []
        for block in day["blocks"]:
            if block.get("window"):
                rows.append({"block": block, "height": 66})
            else:
                has_room = bool(block.get("place"))
                text_width_limit = 370 if has_room else 575
                title_lines = wrap(measure, block["subject"], title_font, text_width_limit)
                teacher_lines = (wrap(measure, str(block["teacher"]), font(22), text_width_limit)
                                 if block.get("teacher") else [])
                room_lines = (wrap(measure, str(block["place"]), font(23, True), 176)
                              if has_room else [])
                room_height = 37 + len(room_lines) * 32 if room_lines else 0
                rows.append({"block": block,
                             "height": max(118, 24 + len(title_lines) * 37 + len(teacher_lines) * 29 + 16,
                                           28 + room_height) + (45 if block.get("change") else 0),
                             "title_lines": title_lines, "teacher_lines": teacher_lines,
                             "room_lines": room_lines, "room_height": room_height})
        section_height = 76 + (sum(row["height"] + 10 for row in rows) if rows else 76) + 12
        layouts.append({"day": day, "rows": rows, "y": y, "height": section_height})
        y += section_height + 18
    height = y + 70
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    small = font(20)
    medium = font(25)
    medium_bold = font(26, True)
    large = font(42, True)
    draw.rounded_rectangle((42, 38, 918, 50), radius=6, fill=CYAN)
    draw.text((48, 78), "РАСПИСАНИЕ НА НЕДЕЛЮ", font=font(26, True), fill=CYAN)
    draw.rounded_rectangle((772, 73, 912, 122), radius=18, fill="#263A50")
    draw.text((842 - text_width(draw, group_name, medium) / 2, 83), group_name, font=medium, fill=WHITE)
    start = datetime.fromisoformat(monday)
    end = datetime.fromordinal(start.toordinal() + 6)
    period = f"{start.day} {MONTHS[start.month - 1]} — {end.day} {MONTHS[end.month - 1]}"
    draw.text((48, 135), period, font=large, fill=WHITE)
    years = str(start.year) if start.year == end.year else f"{start.year}–{end.year}"
    draw.text((50, 191), years, font=medium, fill=MUTED)
    legend = (("ЛЕКЦИЯ", "Лекция"), ("ПРАКТИКА", "Практика"), ("ЛАБА", "Лабораторная"))
    lx = 48
    for label, kind in legend:
        draw.rounded_rectangle((lx, 226, lx + 15, 241), radius=4, fill=TYPE_COLORS[kind])
        draw.text((lx + 23, 222), label, font=small, fill=MUTED)
        lx += text_width(draw, label, small) + 70
    for section in layouts:
        day = section["day"]
        sy = section["y"]
        draw.rounded_rectangle((42, sy, 918, sy + section["height"]), radius=26, fill="#172538")
        date = datetime.fromisoformat(day["date"])
        day_title = f"{WEEKDAYS[date.weekday()]}, {date.day} {MONTHS[date.month - 1]}"
        draw.text((68, sy + 20), day_title, font=font(28, True), fill=WHITE)
        count = f"{day['slots']} пар" if day["slots"] != 1 else "1 пара"
        draw.text((888 - text_width(draw, count, medium), sy + 24), count, font=medium, fill=MUTED)
        ry = sy + 76
        if not section["rows"]:
            draw.text((70, ry + 12), "Занятий нет", font=medium, fill=MUTED)
        for row in section["rows"]:
            block = row["block"]
            h = row["height"]
            if block.get("window"):
                draw.rounded_rectangle((62, ry, 898, ry + h), radius=16, fill="#233346")
                pairs = block["pairs"]
                label = f"{pairs[0]}-я пара" if len(pairs) == 1 else f"пары {pairs[0]}–{pairs[-1]}"
                draw.text((82, ry + 19), label, font=medium, fill=MUTED)
                draw.text((300, ry + 19), "ОКНО", font=medium_bold, fill="#B5C8D6")
                time_text = f"{block['start']}–{block['end']}"
                draw.text((874 - text_width(draw, time_text, medium), ry + 19), time_text,
                          font=medium, fill=WHITE)
            else:
                changed = bool(block.get("change"))
                accent = "#FF6472" if changed else TYPE_COLORS.get(block["type"], CYAN)
                draw.rounded_rectangle((62, ry, 898, ry + h), radius=16,
                                       fill="#4A242D" if changed else TYPE_TINTS.get(block["type"], CARD),
                                       outline="#FF6472" if changed else None, width=4 if changed else 1)
                draw.rounded_rectangle((62, ry + 10, 70, ry + h - 10), radius=3, fill=accent)
                numbers = [n for n in block["pairs"] if n]
                label = ((f"{numbers[0]}-я пара" if len(numbers) == 1
                          else f"пары {numbers[0]}–{numbers[-1]}") if numbers else "Пара")
                draw.text((82, ry + 18), label, font=medium_bold, fill=accent)
                draw.text((82, ry + 53), f"{block['start']}–{block['end']}", font=font(22, True), fill=WHITE)
                title_y = ry + 14
                for line in row["title_lines"]:
                    draw.text((300, title_y), line, font=title_font, fill=WHITE)
                    title_y += 37
                detail_y = title_y + 3
                for line in row["teacher_lines"]:
                    draw.text((300, detail_y), line, font=font(22), fill=MUTED)
                    detail_y += 29
                if row["room_lines"]:
                    room_top = ry + 14
                    draw.rounded_rectangle((690, room_top, 898, room_top + row["room_height"]),
                                           radius=12, fill="#263A50", outline=accent, width=3)
                    draw.text((704, room_top + 5), "АУДИТОРИЯ", font=font(17, True), fill=MUTED)
                    for index, line in enumerate(row["room_lines"]):
                        draw.text((704, room_top + 28 + index * 32), line,
                                  font=font(23, True), fill=WHITE)
                if changed:
                    marker = block["change"]
                    draw.rounded_rectangle((300, ry + h - 43, 534, ry + h - 8),
                                           radius=9, fill="#FF6472")
                    draw.text((315, ry + h - 39), marker, font=font(21, True), fill="#25151A")
            ry += h + 10
    draw.text((48, height - 55), "Источник: ДГТУ", font=small, fill="#8294A9")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
