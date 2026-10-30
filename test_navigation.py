import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bot
from app_bot import ScheduleBot as NavigationApp
from navigation import adjacent_day, month_entries, shift_month
from test_bot import lesson


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = NavigationApp(Path(self.folder.name))
        self.owner = self.app.new_profile("telegram", 42)
        self.owner["snapshot"] = {"1": lesson(start="2026-09-24T08:30:00")}
        self.app.state["profiles"] = {"telegram:42": self.owner}

    def test_calendar_picker_shows_six_columns_and_only_selected_month(self):
        picker = self.app._nav_picker(self.owner, "month", "2026-11-01")
        rows = picker["inline_keyboard"]
        self.assertEqual([button["text"] for button in rows[0]], list(bot.WEEKDAY_SHORT[:6]))
        dates = [button["callback_data"].rsplit(":", 1)[1] for row in rows[1:-1]
                 for button in row if button["callback_data"].startswith("nav:show:")]
        self.assertEqual(len(dates), 25)
        self.assertIn("2026-11-30", dates)
        self.assertNotIn("2026-11-01", dates)
        self.assertTrue(all(date.fromisoformat(value).weekday() < 6 for value in dates))
        self.assertTrue(all(len(button["callback_data"].encode("utf-8")) <= 64 for row in rows for button in row))

    def test_calendar_boundaries_and_sunday_skip(self):
        self.assertEqual(shift_month("2026-12-31", 1), "2027-01-01")
        self.assertEqual(shift_month("2027-01-01", -1), "2026-12-01")
        self.assertEqual(shift_month("2028-03-31", -1), "2028-02-01")
        self.assertEqual(adjacent_day("2026-09-26", 1), "2026-09-28")
        self.assertEqual(adjacent_day("2026-09-28", -1), "2026-09-26")

    def test_month_entries_include_late_month_and_parallel_records(self):
        snapshot = {"1": lesson(code=1, start="2026-11-02T08:30:00"),
                    "2": lesson(code=2, start="2026-11-30T08:30:00", room="1-33А"),
                    "3": lesson(code=3, start="2026-11-30T08:30:00", room="1-44Б"),
                    "4": lesson(code=4, start="2026-12-01T08:30:00")}
        items = month_entries(snapshot, "2026-11-01")
        self.assertEqual(len(items), 3)
        self.assertEqual([item["place"] for item in items[-2:]], ["1-33А", "1-44Б"])
        self.assertEqual(items[-1]["date"], "2026-11-30")

    def test_photo_navigation_edits_in_place_and_reassigns_tracking(self):
        self.owner["cards"] = {"week:2026-09-21": [15]}
        callback = {"id": "cb", "data": f"nav:show:{self.owner['group_id']}:day:2026-09-24",
                    "message": {"message_id": 15, "photo": [{"file_id": "dummy"}]}}
        with patch.object(self.app, "answer_callback"), \
             patch.object(self.app, "render_profile_card", return_value=b"png"), \
             patch("bot.request_json", return_value={"ok": True, "result": {"message_id": 15}}) as request:
            self.app.handle_profile_callback(self.owner, callback)
        self.assertTrue(request.call_args.args[0].endswith("/editMessageMedia"))
        self.assertEqual(request.call_args.args[1]["message_id"], 15)
        self.assertNotIn("week:2026-09-21", self.owner["cards"])
        self.assertEqual(self.owner["cards"]["day:2026-09-24"], [15])
        markup = json.loads(request.call_args.args[1]["reply_markup"])
        self.assertEqual(len(markup["inline_keyboard"]), 1)
        self.assertEqual(len(markup["inline_keyboard"][0]), 3)

    def test_picker_changes_only_keyboard_without_render_or_fetch(self):
        callback = {"id": "cb", "data": f"nav:pick:{self.owner['group_id']}:month:2026-11-01",
                    "message": {"message_id": 15, "photo": [{}]}}
        with patch.object(self.app, "answer_callback"), patch.object(self.app, "render_profile_card") as render, \
             patch("bot.fetch_schedule") as fetch, patch("bot.request_json", return_value={"ok": True}) as request:
            self.app.handle_profile_callback(self.owner, callback)
        self.assertTrue(request.call_args.args[0].endswith("/editMessageReplyMarkup"))
        render.assert_not_called()
        fetch.assert_not_called()

    def test_text_preference_and_selected_group_do_not_redirect_scheduler(self):
        other = self.app.new_profile("telegram", 42)
        other.update({"group_name": "ПИ31", "group_id": 54321, "snapshot": {"2": lesson(code=2)}})
        self.owner.update({"multigroup_enabled": True, "watched_groups": {"54321": other},
                           "selected_group_id": 54321, "display_format": "text"})
        with patch("bot.request_json", return_value={"ok": True, "result": {"message_id": 20}}) as request:
            self.app.show_schedule(self.owner, "week", "2026-09-21")
        self.assertIn("ПИ31", request.call_args.args[1]["text"])
        with patch("bot.request_json", return_value={"ok": True, "result": {"message_id": 21}}) as request:
            self.app.send_profile_card(self.owner, "day", "2026-09-24")
        self.assertIn("ВКБ51", request.call_args.args[1]["text"])
        self.assertTrue(request.call_args.args[0].endswith("/sendMessage"))
        self.assertEqual(other["cards"]["week:2026-09-21"], [20])

    def test_month_is_image_and_preview_ignores_text_preference(self):
        self.owner["display_format"] = "text"
        with patch.object(self.app, "render_profile_card", return_value=b"png") as render, \
             patch("bot.request_json", return_value={"ok": True, "result": {"message_id": 20}}) as request:
            self.app.send_profile_card(self.owner, "month", "2026-11-26")
            self.assertEqual(render.call_args.args[1:3], ("month", "2026-11-01"))
            self.assertTrue(request.call_args.args[0].endswith("/sendPhoto"))
            self.app.send_profile_card(self.owner, "week", "2026-09-21", layout_override="list", track=False)
            self.assertTrue(request.call_args.args[0].endswith("/sendPhoto"))
            self.assertIn("Пример", request.call_args.args[1]["caption"])

    def test_unknown_group_callback_is_rejected_without_switching(self):
        callback = {"id": "cb", "data": "nav:show:999999:day:2026-09-24", "message": {"message_id": 3}}
        with patch.object(self.app, "answer_callback") as answer, patch("bot.request_json") as request:
            self.app.handle_profile_callback(self.owner, callback)
        request.assert_not_called()
        self.assertIn("Кнопка устарела", answer.call_args.args[1])
        self.assertNotIn("selected_group_id", self.owner)

    def test_refresh_appears_only_for_failed_or_missing_snapshot(self):
        markup = self.app._nav_markup(self.owner, self.owner, "week", "2026-09-21")
        self.assertEqual(len(markup["inline_keyboard"]), 1)
        self.owner["last_fetch_failed"] = True
        markup = self.app._nav_markup(self.owner, self.owner, "month", "2026-11-01")
        self.assertEqual(markup["inline_keyboard"][-1][0]["callback_data"],
                         f"refresh:{self.owner['group_id']}:month:2026-11-01")

    def test_refresh_targets_the_group_on_the_clicked_card(self):
        other = self.app.new_profile("telegram", 42)
        other.update({"group_name": "ПИ31", "group_id": 54321, "snapshot": {"2": lesson(code=2)}})
        self.owner.update({"multigroup_enabled": True, "watched_groups": {"54321": other},
                           "selected_group_id": 54321, "last_fetch_failed": True})
        callback = {"id": "cb", "data": f"refresh:{self.owner['group_id']}:day:2026-09-24",
                    "message": {"message_id": 25, "photo": [{}]}}
        with patch.object(self.app, "answer_callback"), \
             patch.object(self.app, "refresh_profiles") as refresh, \
             patch.object(self.app, "show_schedule") as show:
            self.app.handle_profile_callback(self.owner, callback)
        self.assertEqual([profile["group_id"] for profile in refresh.call_args.args[0]],
                         [self.owner["group_id"]])
        self.assertEqual(self.owner["selected_group_id"], self.owner["group_id"])
        self.assertEqual(show.call_args.kwargs["message_id"], 25)

    def test_format_setting_survives_restart(self):
        callback = {"id": "cb", "data": "settings:format:text", "message": {"message_id": 5}}
        with patch.object(self.app, "answer_callback"), patch("bot.request_json", return_value={"ok": True}):
            self.app.handle_profile_callback(self.owner, callback)
        restored = NavigationApp(Path(self.folder.name))
        self.assertEqual(restored.profile_for_chat(42)["display_format"], "text")

    def test_failed_manual_edit_replaces_tracking_instead_of_retrying_dead_message(self):
        self.owner["cards"] = {"day:2026-09-24": [5]}
        replies = [{"ok": False, "description": "message to edit not found"},
                   {"ok": True, "result": {"message_id": 9}}]
        with patch.object(self.app, "render_profile_card", return_value=b"png"), \
             patch("bot.request_json", side_effect=replies):
            self.app.show_schedule(self.owner, "day", "2026-09-24", message_id=5, message_kind="photo")
        self.assertEqual(self.owner["cards"]["day:2026-09-24"], [9])

    def test_failed_automatic_edit_never_sends_a_new_card(self):
        self.owner["cards"] = {"day:2026-09-24": [5]}
        self.owner["send_schedule_on_change"] = False
        with patch.object(self.app, "render_profile_card", return_value=b"png"), \
             patch("bot.request_json", return_value={"ok": False, "description": "message to edit not found"}) as request:
            self.app.send_profile_card(self.owner, "day", "2026-09-24", replace=True)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[0].endswith("/editMessageMedia"))
        self.assertEqual(self.owner["cards"]["day:2026-09-24"], [5])

    def test_automatic_edit_updates_existing_card_when_new_cards_are_disabled(self):
        self.owner["cards"] = {"day:2026-09-24": [5]}
        self.owner["send_schedule_on_change"] = False
        with patch.object(self.app, "render_profile_card", return_value=b"updated-png"), \
             patch("bot.request_json", return_value={"ok": True, "result": {"message_id": 5}}) as request:
            self.app.send_profile_card(self.owner, "day", "2026-09-24", replace=True)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[0].endswith("/editMessageMedia"))
        self.assertEqual(request.call_args.kwargs["files"]["card"][1], b"updated-png")
        self.assertEqual(self.owner["cards"]["day:2026-09-24"], [5])

    def test_stale_automatic_refresh_without_tracked_message_sends_nothing(self):
        # The user already paged that message to another day before the queued refresh ran.
        self.owner["cards"] = {"day:2026-09-25": [5]}
        with patch("bot.request_json") as request:
            self.app.send_profile_card(self.owner, "day", "2026-09-24", replace=True)
        request.assert_not_called()

    def test_longer_text_does_not_create_new_photo_during_automatic_edit(self):
        self.owner["cards"] = {"day:2026-09-24": [5]}
        self.owner["schedule_messages"] = {"5": {"kind": "text"}}
        with patch.object(self.app, "_nav_text", return_value="a" * 4500), \
             patch.object(self.app, "render_profile_card", return_value=b"png"), \
             patch("bot.request_json") as request:
            self.app.send_profile_card(self.owner, "day", "2026-09-24", replace=True)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
