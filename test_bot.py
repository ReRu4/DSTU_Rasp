import json
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

from bot import (BUTTON_CURRENT_WEEK, BUTTON_NEXT_WEEK, BUTTON_PREVIOUS_WEEK, BUTTON_TEXT, Bot,
                 changes_message, day_message, future_change_weeks, horizontal_week_card, normalize_lesson,
                 proxy_for_url, request_json, schedule_card, schedule_changes, split_message, text_day_view,
                 text_week_view, week_card)


def lesson(code=1, room="101", start="2026-09-24T09:00:00"):
    return normalize_lesson({
        "код": code,
        "датаНачала": start,
        "датаОкончания": start[:11] + "10:35:00",
        "дисциплина": "лек Тестовый предмет",
        "преподаватель": "Иванов И.И.",
        "аудитория": room,
        "номерПодгруппы": 0,
    })


class ScheduleTests(unittest.TestCase):
    def test_room_change_is_reported_once(self):
        old = {"1": lesson(room="101")}
        new = {"1": lesson(room="102")}
        message = changes_message(old, new, "2026-09-24", "ВКБ51")
        self.assertIn("аудитория: 101 → 102", message)
        self.assertEqual(message.count("Изменено:"), 1)

    def test_change_notice_shows_readable_date_and_time(self):
        old = {"1": lesson(start="2026-09-25T08:30:00")}
        new = {"1": lesson(start="2026-09-25T10:15:00")}
        message = changes_message(old, new, "2026-09-24", "ВКБ51")
        self.assertIn("25.09.2026", message)
        self.assertIn("начало: 25.09 08:30 → 25.09 10:15", message)

    def test_past_changes_are_ignored(self):
        old = {"1": lesson(room="101", start="2026-09-23T09:00:00")}
        new = {"1": lesson(room="102", start="2026-09-23T09:00:00")}
        self.assertEqual(changes_message(old, new, "2026-09-24", "ВКБ51"), "")

    def test_daily_message_uses_the_selected_date(self):
        snapshot = {"1": lesson(), "2": lesson(code=2, start="2026-09-25T09:00:00")}
        text = day_message(snapshot, "2026-09-24", "ВКБ51")
        self.assertIn("24.09.2026", text)
        self.assertEqual(text.count("Тестовый предмет"), 1)

    def test_repeated_lessons_are_one_readable_block(self):
        starts = ("08:30", "10:15", "12:00", "14:15", "16:00", "17:45")
        ends = ("10:05", "11:50", "13:35", "15:50", "17:35", "19:20")
        snapshot = {}
        for number, (start, end) in enumerate(zip(starts, ends), 1):
            item = lesson(code=number, start=f"2026-09-24T{start}:00")
            item["датаОкончания"] = f"2026-09-24T{end}:00"
            snapshot[str(number)] = item
        text = day_message(snapshot, "2026-09-24", "ВКБ51")
        self.assertEqual(text.count("Тестовый предмет"), 1)
        self.assertIn("Всего: 6 пар", text)
        self.assertIn("Начало пар: 08:30, 10:15", text)

    def test_parallel_groups_count_as_one_time_slot(self):
        first = lesson(code=1)
        second = lesson(code=2)
        second["дисциплина"] = "пр Другой предмет"
        text = day_message({"1": first, "2": second}, "2026-09-24", "ВКБ51")
        self.assertIn("Всего: 1 пара", text)
        self.assertIn("Тестовый предмет", text)
        self.assertIn("Другой предмет", text)

    def test_missing_middle_pair_is_shown_as_window(self):
        second = lesson(code=1, start="2026-09-24T10:15:00")
        second["датаОкончания"] = "2026-09-24T11:50:00"
        fourth = lesson(code=2, start="2026-09-24T14:15:00")
        fourth["датаОкончания"] = "2026-09-24T15:50:00"
        snapshot = {"1": second, "2": fourth}
        text = day_message(snapshot, "2026-09-24", "ВКБ51")
        self.assertIn("Окно: 3-я пара · 12:00–13:35", text)
        png = schedule_card(snapshot, "2026-09-24", "ВКБ51")
        with Image.open(BytesIO(png)) as image:
            self.assertGreater(image.height, 850)

    def test_week_card_contains_seven_days(self):
        png = week_card({"1": lesson()}, "2026-09-21", "ВКБ51")
        with Image.open(BytesIO(png)) as image:
            self.assertEqual(image.width, 960)
            self.assertGreater(image.height, 1200)

    def test_future_changes_are_grouped_into_affected_weeks(self):
        old = {"1": lesson(start="2026-09-25T09:00:00"),
               "2": lesson(code=2, start="2026-10-01T09:00:00")}
        new = {"1": lesson(room="102", start="2026-09-25T09:00:00")}
        weeks = future_change_weeks(old, new, "2026-09-24")
        self.assertEqual(set(weeks), {"2026-09-21", "2026-09-28"})
        self.assertEqual(weeks["2026-09-21"]["changed"], {"1": "АУДИТОРИЯ ИЗМЕНЕНА"})
        self.assertEqual(weeks["2026-09-28"]["removed"][0]["код"], "2")

    def test_future_change_markers_distinguish_added_room_and_other_changes(self):
        old = {"1": lesson(room="101", start="2026-09-25T09:00:00"),
               "2": lesson(code=2, start="2026-09-25T10:15:00"),
               "3": lesson(code=3, start="2026-09-25T12:00:00")}
        new = {"1": lesson(room="102", start="2026-09-25T09:00:00"),
               "2": lesson(code=2, start="2026-09-25T10:15:00"),
               "4": lesson(code=4, start="2026-09-25T14:15:00")}
        new["2"]["преподаватель"] = "Петров П.П."
        new["4"]["дисциплина"] = "лаб Новый предмет"
        weeks = future_change_weeks(old, new, "2026-09-24")
        self.assertEqual(weeks["2026-09-21"]["changed"], {
            "1": "АУДИТОРИЯ ИЗМЕНЕНА", "2": "ИЗМЕНЕНО", "4": "ДОБАВЛЕНО"})
        self.assertEqual([item["код"] for item in weeks["2026-09-21"]["removed"]], ["3"])

    def test_new_api_codes_for_identical_lessons_do_not_trigger_alerts(self):
        starts = ("08:30", "10:15", "12:00", "14:15", "16:00", "17:45")
        old, new = {}, {}
        for index, start in enumerate(starts, 1):
            before = lesson(code=index, start=f"2026-10-01T{start}:00")
            after = dict(before, код=str(index + 100))
            old[before["код"]] = before
            new[after["код"]] = after
        self.assertEqual(schedule_changes(old, new), [])
        self.assertEqual(changes_message(old, new, "2026-09-24", "ВКБ51"), "")
        self.assertEqual(future_change_weeks(old, new, "2026-09-24"), {})

    def test_new_code_with_room_change_is_one_change(self):
        old = {"1": lesson(room="101", start="2026-10-01T08:30:00")}
        after = lesson(code=91, room="102", start="2026-10-01T08:30:00")
        new = {"91": after}
        message = changes_message(old, new, "2026-09-24", "ВКБ51")
        self.assertIn("аудитория: 101 → 102", message)
        self.assertEqual(message.count("✏️ Изменено:"), 1)
        self.assertNotIn("Добавлено:", message)
        self.assertNotIn("Удалено:", message)
        self.assertEqual(future_change_weeks(old, new, "2026-09-24")
                         ["2026-09-28"]["changed"], {"91": "АУДИТОРИЯ ИЗМЕНЕНА"})

    def test_parallel_lessons_with_new_codes_keep_their_teachers(self):
        first = lesson(code=1, room="101", start="2026-10-01T08:30:00")
        second = lesson(code=2, room="201", start="2026-10-01T08:30:00")
        second["преподаватель"] = "Петров П.П."
        changed_first = dict(first, код="91", аудитория="102")
        changed_second = dict(second, код="92", аудитория="202")
        changes = schedule_changes({"1": first, "2": second},
                                   {"91": changed_first, "92": changed_second})
        self.assertEqual([(before["преподаватель"], after["преподаватель"])
                          for before, after in changes],
                         [("Иванов И.И.", "Иванов И.И."), ("Петров П.П.", "Петров П.П.")])
        self.assertTrue(all(after["аудитория"] != before["аудитория"]
                            for before, after in changes))

    def test_alert_filter_reports_only_selected_fields(self):
        old = {"1": lesson(room="101", start="2026-10-01T08:30:00")}
        after = lesson(room="102", start="2026-10-01T08:30:00")
        after["преподаватель"] = "Петров П.П."
        new = {"1": after}
        self.assertEqual(changes_message(old, new, "2026-09-24", "ВКБ51", {"time"}), "")
        message = changes_message(old, new, "2026-09-24", "ВКБ51", {"room"})
        self.assertIn("аудитория: 101 → 102", message)
        self.assertNotIn("преподаватель:", message)
        self.assertEqual(future_change_weeks(old, new, "2026-09-24", {"time"}), {})

    def test_highlight_marks_only_the_changed_pair_in_a_chain(self):
        first = lesson(start="2026-09-25T08:30:00")
        first["датаОкончания"] = "2026-09-25T10:05:00"
        second = lesson(code=2, start="2026-09-25T10:15:00")
        second["датаОкончания"] = "2026-09-25T11:50:00"
        with patch("card.render_week_card", return_value=b"png") as render:
            week_card({"1": first, "2": second}, "2026-09-21", "ВКБ51",
                      {"changed": {"1": "ИЗМЕНЕНО"}, "removed": []})
        blocks = render.call_args.args[2][4]["blocks"]
        self.assertEqual([block.get("change") for block in blocks], ["ИЗМЕНЕНО", None])

    def test_removed_pair_replaces_its_window_in_vertical_week(self):
        removed = lesson(start="2026-09-21T14:15:00")
        removed["датаОкончания"] = "2026-09-21T15:50:00"
        remaining = lesson(code=2, start="2026-09-21T16:00:00")
        remaining["датаОкончания"] = "2026-09-21T17:35:00"
        with patch("card.render_week_card", return_value=b"png") as render:
            week_card({"2": remaining}, "2026-09-21", "ВКБ51",
                      {"changed": {}, "removed": [removed]})
        blocks = render.call_args.args[2][0]["blocks"]
        self.assertEqual(sum(block.get("change") == "УДАЛЕНО" for block in blocks), 1)
        self.assertFalse(any(block.get("window") and 4 in block["pairs"] for block in blocks))

    def test_autumn_week_cards_render_all_three_change_colors(self):
        old = {"1": lesson(start="2026-09-22T08:30:00"),
               "2": lesson(code=2, start="2026-09-23T08:30:00")}
        new = {"1": lesson(room="102", start="2026-09-22T08:30:00"),
               "3": lesson(code=3, start="2026-09-21T08:30:00")}
        markers = future_change_weeks(old, new, "2026-09-20")["2026-09-21"]
        expected = {(255, 228, 92), (121, 168, 244), (241, 109, 101)}
        for renderer in (week_card, horizontal_week_card):
            with self.subTest(renderer=renderer.__name__):
                with Image.open(BytesIO(renderer(new, "2026-09-21", "ВКБ51", markers, True))) as image:
                    colors = {color for _, color in image.getcolors(100_000)}
                self.assertTrue(expected <= colors)

    def test_text_week_and_day_have_inline_navigation_and_windows(self):
        second = lesson(start="2026-09-24T10:15:00")
        second["датаОкончания"] = "2026-09-24T11:50:00"
        fourth = lesson(code=2, start="2026-09-24T14:15:00")
        fourth["датаОкончания"] = "2026-09-24T15:50:00"
        snapshot = {"1": second, "2": fourth}
        overview, navigation = text_week_view(snapshot, "2026-09-21", "ВКБ51")
        detail, day_navigation = text_day_view(snapshot, "2026-09-24", "ВКБ51")
        self.assertIn("<b>Чт 24.09</b>", overview)
        self.assertIn("<b>ОКНО</b> · 3-я пара", detail)
        self.assertIn("<b>101</b>", detail)
        self.assertTrue(any(button["callback_data"] == "td:2026-09-24"
                            for row in navigation["inline_keyboard"] for button in row))
        self.assertEqual(day_navigation["inline_keyboard"][0][1]["callback_data"], "tw:2026-09-21")

    def test_schedule_card_is_a_readable_png_even_without_lessons(self):
        for snapshot in ({"1": lesson()}, {}):
            with self.subTest(has_lesson=bool(snapshot)):
                png = schedule_card(snapshot, "2026-09-24", "ВКБ51")
                with Image.open(BytesIO(png)) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.width, 960)
                    self.assertGreaterEqual(image.height, 590)

    def test_telegram_schedule_is_sent_as_photo(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson()}
                with patch("bot.request_json", return_value={"ok": True}) as send:
                    app.send_schedule("telegram", "2026-09-24")
                self.assertIn("/sendPhoto", send.call_args.args[0])
                self.assertEqual(send.call_args.kwargs["files"]["photo"][2], "image/png")
                markup = json.loads(send.call_args.args[1]["reply_markup"])
                self.assertTrue(markup["is_persistent"])
                labels = [button["text"] for row in markup["keyboard"] for button in row]
                self.assertIn(BUTTON_PREVIOUS_WEEK, labels)
                self.assertIn(BUTTON_NEXT_WEEK, labels)

    def test_existing_chat_gets_navigation_keyboard_once(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(Path(temp))
                with patch("bot.request_json", return_value={"ok": True}) as api:
                    app.ensure_telegram_keyboard()
                    app.ensure_telegram_keyboard()
                self.assertEqual(api.call_count, 1)
                markup = json.loads(api.call_args.args[1]["reply_markup"])
                self.assertTrue(markup["resize_keyboard"])
                self.assertEqual(app.state["telegram_keyboard_version"], 1)

    def test_week_buttons_can_browse_multiple_past_weeks(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson(start="2026-09-01T09:00:00"), "2": lesson(code=2)}
                labels = [BUTTON_CURRENT_WEEK, BUTTON_PREVIOUS_WEEK,
                          BUTTON_PREVIOUS_WEEK, BUTTON_NEXT_WEEK]
                updates = [{"update_id": index, "message": {"chat": {"id": 42, "type": "private"}, "text": label}}
                           for index, label in enumerate(labels, 1)]
                with patch("bot.request_json", return_value={"ok": True, "result": updates}), \
                     patch.object(app, "send_week") as send_week:
                    app.telegram_commands("2026-09-24")
                dates = [call.args[1] for call in send_week.call_args_list]
                self.assertEqual(dates, ["2026-09-21", "2026-09-14", "2026-09-07", "2026-09-14"])
                self.assertEqual(app.state["telegram_week_cursor"], "2026-09-14")

    def test_week_command_sends_current_week_card(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson()}
                updates = [{"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}, "text": "/week"}}]
                def reply(url, *args, **kwargs):
                    if url.endswith("/getUpdates"):
                        return {"ok": True, "result": updates}
                    if url.endswith("/sendPhoto"):
                        return {"ok": True, "result": {"message_id": 91}}
                    return {"ok": True}
                with patch("bot.request_json", side_effect=reply) as api:
                    app.telegram_commands("2026-09-24")
                self.assertEqual(app.state["cards"]["telegram"]["week:2026-09-21"], [91])
                self.assertTrue(any(call.args[0].endswith("/sendPhoto") for call in api.call_args_list))

    def test_vk_schedule_photo_upload_flow(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"VK_COMMUNITY_TOKEN": "test-token", "VK_PEER_ID": "123"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson()}
                replies = [
                    {"response": {"upload_url": "https://upload.vk.com/photo"}},
                    {"server": 1, "photo": "uploaded", "hash": "hash"},
                    {"response": [{"owner_id": 12, "id": 34}]},
                    {"response": 1},
                ]
                with patch("bot.request_json", side_effect=replies) as send:
                    app.send_schedule("vk", "2026-09-24")
                self.assertEqual(send.call_count, 4)
                self.assertEqual(send.call_args.args[1]["attachment"], "photo12_34")

    def test_vk_existing_photo_message_is_edited(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"VK_COMMUNITY_TOKEN": "test-token", "VK_PEER_ID": "123"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson()}
                app.state["cards"] = {"vk": {"day:2026-09-24": [77]}}
                with patch.object(app, "vk_photo_attachment", return_value="photo12_34"), \
                     patch("bot.request_json", return_value={"response": 1}) as api:
                    app.send_schedule("vk", "2026-09-24", replace=True)
                self.assertIn("messages.edit", api.call_args.args[0])
                self.assertEqual(api.call_args.args[1]["message_id"], 77)
                self.assertEqual(api.call_args.args[1]["attachment"], "photo12_34")

    def test_failed_photo_falls_back_to_text(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson()}
                with patch.object(app, "send_telegram_card", side_effect=RuntimeError("upload failed")), \
                     patch.object(app, "send_telegram_chat") as send_text:
                    app.send_schedule("telegram", "2026-09-24")
                self.assertIn("Тестовый предмет", send_text.call_args.args[1])

    def test_schedule_change_edits_day_and_week_cards(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 24, 9, 0, tzinfo=tz)

        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson(room="101")}
                app.state["cards"] = {"telegram": {"day:2026-09-24": [77], "week:2026-09-21": [88]}}
                app.state["daily_queued"]["telegram"] = "2026-09-24"
                with patch("bot.datetime", FixedDateTime), \
                     patch("bot.find_group_id", return_value=72244), \
                     patch("bot.fetch_schedule", return_value={"1": lesson(room="102")}), \
                     patch("bot.request_json", return_value={"ok": True, "result": []}) as api:
                    app.cycle()
                methods = [call.args[0].rsplit("/", 1)[-1] for call in api.call_args_list]
                self.assertEqual(methods.count("editMessageMedia"), 2)
                self.assertEqual(methods.count("sendPhoto"), 0)
                self.assertEqual(app.state["pending"]["telegram"], [])

    def test_future_alert_button_opens_each_affected_week(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 24, 9, 0, tzinfo=tz)

        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42",
                                        "DAILY_TIME": "23:59"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {
                    "1": lesson(start="2026-09-25T09:00:00", room="101"),
                    "2": lesson(code=2, start="2026-10-01T09:00:00", room="201"),
                }
                app.state["group_checked_on"] = "2026-09-24"
                app.state["telegram_keyboard_version"] = 1
                updated = {
                    "1": lesson(start="2026-09-25T09:00:00", room="102"),
                    "2": lesson(code=2, start="2026-10-01T09:00:00", room="202"),
                }
                with patch("bot.datetime", FixedDateTime), \
                     patch("bot.fetch_schedule", return_value=updated), \
                     patch("bot.request_json", return_value={"ok": True, "result": []}) as api:
                    app.cycle()
                alert = next(call for call in api.call_args_list if call.args[0].endswith("/sendMessage"))
                self.assertIn("Будущие дни", alert.args[1]["text"])
                self.assertIn("аудитория: 101 → 102", alert.args[1]["text"])
                markup = json.loads(alert.args[1]["reply_markup"])
                callback_data = markup["inline_keyboard"][0][0]["callback_data"]
                self.assertTrue(callback_data.startswith("changes:"))
                self.assertEqual(len(app.state["change_batches"]), 1)
                restarted = Bot(Path(temp))
                update = {"update_id": 1, "callback_query": {
                    "id": "cb1", "data": callback_data,
                    "message": {"chat": {"id": 42, "type": "private"}, "message_id": 5}}}
                def reply(url, *args, **kwargs):
                    if url.endswith("/getUpdates"):
                        return {"ok": True, "result": [update]}
                    if url.endswith("/sendPhoto"):
                        return {"ok": True, "result": {"message_id": 91}}
                    return {"ok": True}
                with patch("bot.request_json", side_effect=reply) as callback_api:
                    restarted.telegram_commands("2026-09-24")
                methods = [call.args[0].rsplit("/", 1)[-1] for call in callback_api.call_args_list]
                self.assertEqual(methods.count("answerCallbackQuery"), 1)
                self.assertEqual(methods.count("sendPhoto"), 2)
                self.assertEqual(restarted.state["pending"]["telegram"], [])

    def test_text_button_edits_same_message_when_a_day_is_chosen(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson()}
                updates = [
                    {"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}, "text": BUTTON_TEXT}},
                    {"update_id": 2, "callback_query": {"id": "cb2", "data": "td:2026-09-24",
                        "message": {"chat": {"id": 42, "type": "private"}, "message_id": 50}}},
                ]
                def reply(url, *args, **kwargs):
                    if url.endswith("/getUpdates"):
                        return {"ok": True, "result": updates}
                    return {"ok": True}
                with patch("bot.request_json", side_effect=reply) as api:
                    app.telegram_commands("2026-09-24")
                calls = {call.args[0].rsplit("/", 1)[-1]: call for call in api.call_args_list}
                self.assertEqual(calls["sendMessage"].args[1]["parse_mode"], "HTML")
                self.assertEqual(calls["editMessageText"].args[1]["message_id"], 50)
                self.assertIn("<b>101</b>", calls["editMessageText"].args[1]["text"])

    def test_previous_version_state_gets_new_tracked_card(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 24, 9, 0, tzinfo=tz)

        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            old_state = {"snapshot": {"1": lesson()}, "pending": {"telegram": [], "vk": []},
                         "daily_queued": {"telegram": "2026-09-24"}, "telegram_offset": 0}
            (root / "data" / "state.json").write_text(json.dumps(old_state), encoding="utf-8")
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(root)
                self.assertFalse(app.state["card_tracking_ready"])
                def reply(url, *args, **kwargs):
                    if url.endswith("/getUpdates"):
                        return {"ok": True, "result": []}
                    if url.endswith("/sendPhoto"):
                        return {"ok": True, "result": {"message_id": 91}}
                    return {"ok": True}
                with patch("bot.datetime", FixedDateTime), \
                     patch("bot.find_group_id", return_value=72244), \
                     patch("bot.fetch_schedule", return_value={"1": lesson()}), \
                     patch("bot.request_json", side_effect=reply):
                    app.cycle()
                self.assertEqual(app.state["cards"]["telegram"]["day:2026-09-24"], [91])

    def test_uneditable_card_is_replaced(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"}, clear=True):
                app = Bot(Path(temp))
                app.state["snapshot"] = {"1": lesson()}
                app.state["cards"] = {"telegram": {"day:2026-09-24": [77]}}
                with patch.object(app, "edit_telegram_card", side_effect=RuntimeError("expired")), \
                     patch.object(app, "send_telegram_card", return_value=99):
                    app.send_schedule("telegram", "2026-09-24", replace=True)
                self.assertEqual(app.state["cards"]["telegram"]["day:2026-09-24"], [99])

    def test_message_parts_stay_within_limit(self):
        self.assertTrue(all(len(x) <= 100 for x in split_message("x" * 230, 100)))

    def test_telegram_pairing_replies_and_saves_chat(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_PAIR_CODE=secret\n", encoding="utf-8")
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_PAIR_CODE": "secret"}):
                app = Bot(root)
                replies = []
                updates = [
                    {"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}, "text": "/start wrong"}},
                    {"update_id": 2, "message": {"chat": {"id": 42, "type": "private"}, "text": "/start@my_bot secret"}},
                ]
                with patch("bot.request_json", return_value={"ok": True, "result": updates}), patch.object(app, "send_telegram_chat", side_effect=lambda chat, text: replies.append((chat, text))):
                    app.telegram_commands("2026-09-24")
                self.assertEqual(app.state["telegram_chat_id"], 42)
                self.assertTrue((root / "data" / "state.json").exists())
                self.assertIn("Код не подошёл", replies[0][1])
                self.assertIn("Готово", replies[1][1])

    def test_proxy_is_used_only_for_configured_service(self):
        proxy = "http://example:password@127.0.0.1:1234"
        response = SimpleNamespace(status_code=200, content=b"{}")
        with patch.dict("os.environ", {"TELEGRAM_PROXY_URL": proxy, "PROXY_URL": "", "DONSTU_PROXY_URL": ""}):
            with patch("bot.HTTP.request", return_value=response) as send:
                request_json("https://api.telegram.org/test")
                self.assertEqual(send.call_args.kwargs["proxies"], {"http": proxy, "https": proxy})
                request_json("https://edu.donstu.ru/api/search")
                self.assertEqual(send.call_args.kwargs["proxies"], {})

    def test_proxy_password_is_not_in_error(self):
        with patch.dict("os.environ", {"TELEGRAM_PROXY_URL": "http://user:secretpassword@127.0.0.1:1234"}):
            with patch("bot.HTTP.request", side_effect=requests.exceptions.ProxyError("secretpassword")):
                with self.assertRaises(RuntimeError) as caught:
                    request_json("https://api.telegram.org/test")
        self.assertNotIn("secretpassword", str(caught.exception))

    def test_proxy_address_copied_without_scheme(self):
        with patch.dict("os.environ", {"TELEGRAM_PROXY_URL": "user:pass@127.0.0.1:1234"}):
            self.assertEqual(proxy_for_url("https://api.telegram.org/test"), "http://user:pass@127.0.0.1:1234")


if __name__ == "__main__":
    unittest.main()
