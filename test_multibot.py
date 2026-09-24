"""Сценарии личных настроек и нескольких подписчиков."""

import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from bot import MultiBot, card_theme, horizontal_week_card, schedule_card, week_card
from test_bot import lesson


class MultiBotTests(unittest.TestCase):
    def test_old_chat_and_snapshot_migrate_without_pairing_again(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            legacy = {"telegram_chat_id": 42, "snapshot": {"1": lesson()},
                      "pending": {"telegram": [], "vk": []},
                      "daily_queued": {"telegram": "2026-09-24"},
                      "weekly_queued": {}, "cards": {"telegram": {"day:2026-09-24": [91]}},
                      "telegram_offset": 3}
            (root / "data" / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=True):
                app = MultiBot(root)
            profile = app.state["profiles"]["telegram:42"]
            self.assertEqual(profile["snapshot"], legacy["snapshot"])
            self.assertEqual(profile["cards"]["day:2026-09-24"], [91])
            self.assertEqual(profile["daily_queued"], "2026-09-24")

    def test_two_chats_pair_and_keep_independent_settings(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                updates = [{"update_id": index, "message": {"chat": {"id": chat_id, "type": "private"},
                                                              "text": "/start secret"}}
                           for index, chat_id in ((1, 42), (2, 77))]
                def reply(url, *args, **kwargs):
                    return {"ok": True, "result": updates} if url.endswith("/getUpdates") else {"ok": True}
                with patch("bot.request_json", side_effect=reply):
                    app.telegram_commands("2026-09-24")
                first = app.state["profiles"]["telegram:42"]
                second = app.state["profiles"]["telegram:77"]
                first["week_layout"] = "vertical"
                first["seasonal_theme"] = False
                first["awaiting"] = "time"
                with patch("bot.request_json", return_value={"ok": True}):
                    app.apply_user_input(first, "19:30", "2026-09-24")
                self.assertEqual(first["daily_time"], "19:30")
                self.assertEqual(second["daily_time"], "08:00")
                self.assertEqual(second["week_layout"], "horizontal")
                self.assertTrue(second["seasonal_theme"])

    def test_group_change_only_affects_one_chat(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                one = app.new_profile("telegram", 42)
                other = app.new_profile("telegram", 77)
                app.state["profiles"] = {"telegram:42": one, "telegram:77": other}
                one["awaiting"] = "group"
                new_snapshot = {"1": lesson()}
                with patch("bot.find_group_id", return_value=12345), \
                     patch("bot.fetch_schedule", return_value=new_snapshot), \
                     patch.object(app, "send_profile_message"), \
                     patch.object(app, "send_profile_card") as card, \
                     patch.object(app, "show_settings"):
                    app.apply_user_input(one, "ДругаяГруппа", "2026-09-24")
                self.assertEqual(one["group_id"], 12345)
                self.assertEqual(one["snapshot"], new_snapshot)
                self.assertEqual(other["group_name"], "ВКБ51")
                self.assertEqual(card.call_args.args[0], one)

    def test_same_group_is_fetched_once_and_time_is_per_chat(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 24, 9, 0, tzinfo=tz)

        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                first, second = app.new_profile("telegram", 42), app.new_profile("telegram", 77)
                app.state["profiles"] = {"telegram:42": first, "telegram:77": second}
                snapshot = {"1": lesson()}
                for profile in (first, second):
                    profile["snapshot"] = snapshot
                    profile["group_checked_on"] = "2026-09-24"
                    profile["keyboard_version"] = 3
                second["daily_time"] = "10:00"
                with patch("bot.datetime", FixedDateTime), \
                     patch.object(app, "telegram_commands", return_value=True), \
                     patch("bot.fetch_schedule", return_value=snapshot) as fetch, \
                     patch.object(app, "send_profile_card") as send:
                    app.cycle()
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(send.call_count, 1)
                self.assertIs(send.call_args.args[0], first)
                self.assertEqual(first["daily_queued"], "2026-09-24")
                self.assertEqual(second["daily_queued"], "")

    def test_both_themes_and_week_formats_render(self):
        first = lesson(start="2026-09-22T16:00:00", room="1-027")
        first["датаОкончания"] = "2026-09-22T17:35:00"
        second = lesson(code=2, start="2026-09-22T16:00:00", room="6-402")
        second["датаОкончания"] = "2026-09-22T17:35:00"
        snapshot = {"1": first, "2": second}
        with Image.open(BytesIO(horizontal_week_card(snapshot, "2026-09-21", "ВКБ51"))) as horizontal:
            self.assertEqual(horizontal.width, 2880)
            self.assertGreater(horizontal.height, 2000)
            self.assertEqual(horizontal.getpixel((0, 0)), (33, 27, 25))
        with Image.open(BytesIO(week_card(snapshot, "2026-09-21", "ВКБ51", seasonal=False))) as vertical:
            self.assertEqual(vertical.width, 960)
            self.assertEqual(vertical.getpixel((0, 0)), (16, 25, 39))
        with Image.open(BytesIO(schedule_card(snapshot, "2026-09-22", "ВКБ51"))) as day:
            self.assertEqual(day.getpixel((0, 0)), (33, 27, 25))
        self.assertEqual(card_theme("2026-12-01"), "classic")

    def test_settings_callbacks_change_only_one_profile_and_offer_examples(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                first, second = app.new_profile("telegram", 42), app.new_profile("telegram", 77)
                first["snapshot"] = {"1": lesson()}
                app.state["profiles"] = {"telegram:42": first, "telegram:77": second}
                callback = lambda action: {"id": "cb", "data": action,
                                           "message": {"message_id": 10}}
                with patch.object(app, "answer_callback"), patch.object(app, "show_settings"), \
                     patch.object(app, "send_profile_card") as send_card:
                    app.handle_profile_callback(first, callback("settings:layout:vertical"))
                    app.handle_profile_callback(first, callback("settings:season:off"))
                    app.handle_profile_callback(first, callback("settings:example:horizontal"))
                self.assertEqual(first["week_layout"], "vertical")
                self.assertFalse(first["seasonal_theme"])
                self.assertEqual(second["week_layout"], "horizontal")
                self.assertTrue(second["seasonal_theme"])
                self.assertEqual(send_card.call_args.kwargs["layout_override"], "horizontal")
                self.assertFalse(send_card.call_args.kwargs["track"])

    def test_due_time_queues_once_between_schedule_checks(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                profile = app.new_profile("telegram", 42)
                profile["snapshot"] = {"1": lesson()}
                profile["daily_time"] = "09:15"
                app.state["profiles"] = {"telegram:42": profile}
                zone = ZoneInfo("Europe/Moscow")
                self.assertFalse(app.queue_due(datetime(2026, 9, 24, 9, 14, tzinfo=zone)))
                self.assertTrue(app.queue_due(datetime(2026, 9, 24, 9, 15, tzinfo=zone)))
                self.assertFalse(app.queue_due(datetime(2026, 9, 24, 9, 16, tzinfo=zone)))
                self.assertEqual(len(profile["pending"]), 1)
                self.assertEqual(profile["pending"][0]["kind"], "daily")

    def test_future_changes_are_queued_separately_for_each_group(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                first, other = app.new_profile("telegram", 42), app.new_profile("telegram", 77)
                first["group_name"] = "ВКБ51"
                other["group_name"] = "Другая"
                first["snapshot"] = {"1": lesson(start="2026-09-25T08:30:00", room="101")}
                other["snapshot"] = {"2": lesson(code=2, start="2026-09-25T08:30:00")}
                new = {"1": lesson(start="2026-09-25T08:30:00", room="102")}
                app.process_profile_snapshot(first, new,
                                             datetime(2026, 9, 24, 8, 0,
                                                      tzinfo=ZoneInfo("Europe/Moscow")))
                self.assertEqual(len(first["change_batches"]), 1)
                self.assertTrue(any(item["kind"] == "future_change" for item in first["pending"]))
                self.assertEqual(other["pending"], [])
                self.assertEqual(other["change_batches"], {})

    def test_pairing_requests_immediate_refresh_and_welcome_card(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                update = {"update_id": 7, "message": {"chat": {"id": 42, "type": "private"},
                                                    "text": "/start secret"}}
                def reply(url, *args, **kwargs):
                    return {"ok": True, "result": [update]} if url.endswith("/getUpdates") else {"ok": True}
                with patch("bot.request_json", side_effect=reply):
                    self.assertTrue(app.telegram_commands("2026-09-24"))
                profile = app.state["profiles"]["telegram:42"]
                self.assertTrue(app.needs_refresh)
                self.assertTrue(profile["welcome_card_pending"])
                self.assertEqual(app.state["telegram_offset"], 8)

    def test_vk_env_changes_update_existing_vk_profile(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict("os.environ", {"VK_COMMUNITY_TOKEN": "test-token",
                                        "VK_PEER_ID": "123"}, clear=True):
                app = MultiBot(root)
                app.state["profiles"]["vk"]["snapshot"] = {"1": lesson()}
                app.state_file.write_text(json.dumps(app.state), encoding="utf-8")
            with patch.dict("os.environ", {"VK_COMMUNITY_TOKEN": "test-token",
                                        "VK_PEER_ID": "123", "GROUP_NAME": "Другая",
                                        "GROUP_ID": "54321", "WEEK_LAYOUT": "vertical"}, clear=True):
                restarted = MultiBot(root)
            profile = restarted.state["profiles"]["vk"]
            self.assertEqual(profile["group_name"], "Другая")
            self.assertEqual(profile["group_id"], 54321)
            self.assertEqual(profile["week_layout"], "vertical")
            self.assertIsNone(profile["snapshot"])

    def test_old_reply_keyboard_is_removed_once(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                profile = app.new_profile("telegram", 42)
                profile["keyboard_version"] = 2
                app.state["profiles"] = {"telegram:42": profile}
                calls = []
                def reply(url, params=None, **kwargs):
                    calls.append((url, params))
                    return {"ok": True}
                with patch("bot.request_json", side_effect=reply):
                    app.ensure_telegram_keyboards()
                    app.ensure_telegram_keyboards()
                self.assertEqual(profile["keyboard_version"], 3)
                self.assertEqual(len(calls), 2)
                self.assertEqual(json.loads(calls[0][1]["reply_markup"]), {"remove_keyboard": True})
                self.assertIn("inline_keyboard", json.loads(calls[1][1]["reply_markup"]))

    def test_photo_has_inline_navigation_and_input_uses_force_reply(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                profile = app.new_profile("telegram", 42)
                profile["snapshot"] = {"1": lesson()}
                app.state["profiles"] = {"telegram:42": profile}
                calls = []
                def reply(url, params=None, **kwargs):
                    calls.append((url, params))
                    return {"ok": True, "result": {"message_id": 17}}
                with patch("bot.request_json", side_effect=reply):
                    app.send_profile_card(profile, "day", "2026-09-24")
                    with patch.object(app, "answer_callback"):
                        app.handle_profile_callback(profile, {"id": "cb", "data": "settings:time"})
                photo_markup = json.loads(calls[0][1]["reply_markup"])
                prompt_markup = json.loads(calls[1][1]["reply_markup"])
                self.assertIn("inline_keyboard", photo_markup)
                self.assertNotIn("keyboard", photo_markup)
                self.assertTrue(prompt_markup["force_reply"])
                self.assertEqual(profile["awaiting"], "time")

    def test_inline_week_navigation_uses_clicked_card_and_checks_range(self):
        with TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token",
                                        "TELEGRAM_PAIR_CODE": "secret"}, clear=True):
                app = MultiBot(Path(temp))
                profile = app.new_profile("telegram", 42)
                profile["snapshot"] = {"1": lesson()}
                app.state["profiles"] = {"telegram:42": profile}
                with patch.object(app, "answer_callback"), \
                     patch.object(app, "send_profile_card") as card, \
                     patch.object(app, "send_profile_message") as message:
                    app.handle_profile_callback(profile, {"id": "cb", "data": "nav:week:2026-09-21"})
                    app.handle_profile_callback(profile, {"id": "cb", "data": "nav:week:2026-09-14"})
                self.assertEqual(card.call_count, 1)
                self.assertEqual(card.call_args.args[1:], ("week", "2026-09-21"))
                self.assertEqual(message.call_count, 1)


if __name__ == "__main__":
    unittest.main()
