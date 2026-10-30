from datetime import date
from io import BytesIO
import unittest

from PIL import Image

from month_card import day_rows, military_day, month_weeks, photo_size, render_month_card


SLOTS = (("08:30", "10:05"), ("10:15", "11:50"), ("12:00", "13:35"),
         ("14:15", "15:50"), ("16:00", "17:35"), ("17:45", "19:20"), ("19:30", "21:05"))


def entry(day="2026-11-02", start="08:30", end="10:05", **extra):
    return {"date": day, "start": start, "end": end, "subject": "Программирование алгоритмов защиты информации",
            "type": "Лекция", "teacher": "Преподаватель А.А.", "place": "1-319Б", **extra}


class MonthCardTests(unittest.TestCase):
    def test_calendar_keeps_month_boundaries_leap_day_and_monday_columns(self):
        november = month_weeks("2026-11-20")
        self.assertEqual(november[0][0], date(2026, 11, 2))
        self.assertEqual(november[-1], [date(2026, 11, 30)] + [None] * 5)
        for month in ("2024-02-01", "2026-11-01", "2027-01-01", "2027-12-31"):
            for week in month_weeks(month):
                self.assertEqual(len(week), 6)
                for column, day in enumerate(week):
                    if day:
                        self.assertEqual(day.weekday(), column)
                        self.assertEqual(day.strftime("%Y-%m"), month[:7])
        self.assertIn(date(2024, 2, 29), [day for week in month_weeks("2024-02-01") for day in week])

    def test_parallel_and_nonstandard_lessons_are_preserved_and_windows_do_not_overlap(self):
        records = [entry(place="1-319"), entry(place="6-402", subgroup=2),
                   entry(place="3-214", subgroup=3),
                   entry(start="12:00", end="13:35"),
                   entry(start="14:50", end="16:30", subject="Нестандартная пара")]
        rows = day_rows(records, SLOTS)
        self.assertEqual(len([row for row in rows if not row.get("window")]), len(records))
        self.assertEqual([row["start"] for row in rows if row.get("window")], ["10:15"])
        self.assertEqual(len([row for row in rows if row["start"] == "08:30"]), 3)
        self.assertEqual(rows[-1]["start"], "14:50")

    def test_hidden_sunday_and_other_month_cannot_add_rows_to_png(self):
        visible = [entry()]
        hidden = [entry(day="2026-11-01", start="05:00", end="23:00"),
                  entry(day="2026-10-31"), entry(day="2026-12-01")]
        self.assertEqual(render_month_card("ВКБ51", "2026-11-01", visible, SLOTS),
                         render_month_card("ВКБ51", "2026-11-01", visible + hidden, SLOTS))

    def test_military_block_omits_details_but_does_not_swallow_other_lessons(self):
        military = [entry(day="2026-11-05", start=start, end=end,
                          subject="Военная кафедра (ЦВПР с.запаса чт)", place="Шаповалова10")
                    for start, end in SLOTS[:6]]
        self.assertTrue(military_day(military))
        changed_details = [{**record, "teacher": "Другое имя", "place": "Другое место"} for record in military]
        self.assertEqual(render_month_card("ВКБ51", "2026-11-01", military, SLOTS),
                         render_month_card("ВКБ51", "2026-11-01", changed_details, SLOTS))
        self.assertFalse(military_day(military + [entry(day="2026-11-05")]))
        self.assertFalse(military_day([{**military[0], "change": "ДОБАВЛЕНО"}]))

    def test_busy_month_long_room_and_both_themes_render_with_photo_limits(self):
        records = [entry(place="Корпус Б, лаборатория инженерного центра 204А"),
                   entry(start="19:30", end="21:05", subject="Очень длинное название учебной дисциплины")]
        colors = []
        for theme in ("classic", "autumn"):
            with Image.open(BytesIO(render_month_card("ВКБ51-дополнительная", "2026-11-01", records, SLOTS, theme))) as image:
                self.assertEqual(image.format, "PNG")
                self.assertLessEqual(image.width + image.height, 10000)
                colors.append(image.getpixel((0, 0)))
        self.assertNotEqual(colors[0], colors[1])
        with Image.open(BytesIO(render_month_card("ВКБ51", "2026-11-01", records * 160, SLOTS))) as image:
            self.assertLessEqual(image.width + image.height, 10000)
            self.assertLessEqual(max(image.size) / min(image.size), 20)
        for size in ((2160, 4000), (2160, 100000), (100000, 2160)):
            width, height, scale = photo_size(*size)
            self.assertLessEqual(width + height, 10000)
            self.assertLessEqual(max(width, height) / min(width, height), 20)
            self.assertGreater(scale, 0)


if __name__ == "__main__":
    unittest.main()
