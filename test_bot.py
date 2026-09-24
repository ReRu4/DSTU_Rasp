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

from bot import (BUTTON_CURRENT_WEEK, BUTTON_NEXT_WEEK, BUTTON_PREVIOUS_WEEK, Bot,
                 changes_message, day_message, normalize_lesson, proxy_for_url,
                 request_json, schedule_card, split_message, week_card)


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
