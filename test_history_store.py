"""Durable identity and actual observation history, without Telegram/network."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bot import normalize_lesson
from history_store import HistoryStore


def lesson(code="1", room="1-101", start="2026-11-17T10:15:00", teacher="Иванов И. И.", subgroup=0):
    return normalize_lesson({
        "код": code, "датаНачала": start, "датаОкончания": start[:11] + "11:50:00",
        "дисциплина": "пр Защита информации", "преподаватель": teacher,
        "аудитория": room, "номерПодгруппы": subgroup,
    })


def snapshot(*items):
    return {item["код"]: item for item in items}


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = HistoryStore(self.path)
        self.t1 = "2026-11-17T08:55:00+03:00"
        self.t2 = "2026-11-17T09:00:00+03:00"
        self.t3 = "2026-11-17T09:05:00+03:00"

    def observe(self, *items, at=None, group=10, baseline=None):
        self.store.observe(group, "ВКБ51" if group == 10 else "ВКБ52", snapshot(*items),
                           at or self.t1, baseline=baseline)

    def rows(self, group=10, include_removed=True):
        return self.store.schedule(group, "2026-11-01", "2026-11-30", include_removed)

    def test_initial_snapshot_marks_observation_not_fictional_addition(self):
        self.observe(lesson())
        row = self.rows()[0]
        self.assertFalse(row["deleted"])
        self.assertEqual(row["group_name"], "ВКБ51")
        self.assertEqual(row["history_count"], 1)
        event = self.store.history(row["id"])[0]
        self.assertEqual(event["kind"], "baseline")
        self.assertEqual(event["types"], [])
        self.assertEqual(event["detected_at"], self.t1)
        self.assertIsNone(event["before"])

    def test_seed_from_existing_state_and_new_snapshot_is_atomic(self):
        self.observe(lesson(room="1-282"), baseline=snapshot(lesson(room="1-140")))
        row = self.rows()[0]
        events = self.store.history(row["id"])
        self.assertEqual([event["kind"] for event in events], ["changed", "baseline"])
        self.assertEqual(events[0]["before"]["аудитория"], "1-140")
        self.assertEqual(events[0]["after"]["аудитория"], "1-282")
        self.assertTrue(all(event["detected_at"] == self.t1 for event in events))

    def test_churning_source_codes_and_optional_metadata_do_not_create_events(self):
        self.observe(lesson())
        original = self.rows()[0]
        item = {**lesson(code="777"), "teacherID": 72}
        self.observe(item, at=self.t2)
        row = self.rows()[0]
        self.assertEqual(row["id"], original["id"])
        self.assertEqual(row["lesson"]["код"], "777")
        self.assertEqual(row["lesson"]["teacherID"], 72)
        self.assertEqual(row["history_count"], 1)
        self.assertEqual(row["observed_at"], self.t2)

    def test_room_and_time_changes_keep_uid_despite_new_source_code(self):
        self.observe(lesson())
        uid = self.rows()[0]["id"]
        self.observe(lesson(code="2", room="1-282"), at=self.t2)
        self.observe(lesson(code="3", room="1-282", start="2026-11-17T12:00:00"), at=self.t3)
        self.assertEqual(self.rows()[0]["id"], uid)
        events = self.store.history(uid)
        self.assertEqual([event["types"] for event in events[:2]], [["time"], ["room"]])
        self.assertEqual(events[1]["before"]["аудитория"], "1-101")
        self.assertEqual(events[1]["after"]["аудитория"], "1-282")

    def test_parallel_subgroups_retain_their_individual_history(self):
        self.observe(lesson(code="1", subgroup=1), lesson(code="2", room="6-402", teacher="Петров П. П.", subgroup=2))
        ids = {row["lesson"]["номерПодгруппы"]: row["id"] for row in self.rows()}
        self.observe(lesson(code="20", room="6-403", teacher="Петров П. П.", subgroup=2),
                     lesson(code="10", subgroup=1), at=self.t2)
        rows = {row["lesson"]["номерПодгруппы"]: row for row in self.rows()}
        self.assertEqual(rows[1]["id"], ids[1])
        self.assertEqual(rows[2]["id"], ids[2])
        self.assertEqual(rows[1]["history_count"], 1)
        self.assertEqual(rows[2]["history_count"], 2)

    def test_exact_identical_parallel_records_prefer_their_own_code(self):
        self.observe(lesson(code="1"), lesson(code="2"))
        ids = {row["lesson"]["код"]: row["id"] for row in self.rows()}
        self.observe(lesson(code="1", room="1-282"), lesson(code="2"), at=self.t2)
        rows = {row["lesson"]["код"]: row for row in self.rows()}
        self.assertEqual(rows["1"]["id"], ids["1"])
        self.assertEqual(rows["2"]["id"], ids["2"])
        self.assertEqual(rows["2"]["history_count"], 1)

    def test_deleted_then_restored_lesson_has_continuous_history(self):
        self.observe(lesson())
        uid = self.rows()[0]["id"]
        self.observe(at=self.t2)
        self.assertTrue(self.rows()[0]["deleted"])
        self.assertEqual(self.rows(include_removed=False), [])
        self.observe(lesson(code="99", room="1-282"), at=self.t3)
        row = self.rows()[0]
        self.assertEqual(row["id"], uid)
        self.assertFalse(row["deleted"])
        events = self.store.history(uid)
        self.assertEqual([event["kind"] for event in events], ["restored", "removed", "baseline"])
        self.assertEqual(events[0]["types"], ["added", "room"])
        self.assertEqual(events[0]["before"]["аудитория"], "1-101")

    def test_ambiguous_deleted_lessons_are_not_merged(self):
        self.observe(lesson(code="1"), lesson(code="2"))
        previous_ids = {row["id"] for row in self.rows()}
        self.observe(at=self.t2)
        self.observe(lesson(code="99"), at=self.t3)
        active = self.rows(include_removed=False)
        self.assertEqual(len(active), 1)
        self.assertNotIn(active[0]["id"], previous_ids)
        self.assertEqual(active[0]["last_event"]["kind"], "added")

    def test_persists_across_reopening_and_filters_date_group_and_deleted(self):
        self.observe(lesson(), lesson(code="2", start="2026-12-01T10:15:00"))
        self.observe(lesson(), group=20)
        self.store = HistoryStore(self.path)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["group_id"], 10)
        self.assertEqual(self.store.lesson(self.rows()[0]["id"])["group_name"], "ВКБ51")
        self.assertEqual(self.store.lesson("missing"), None)
        self.assertEqual(self.store.history("missing"), [])

    def test_changes_feed_is_newest_first_and_scoped_by_group(self):
        self.observe(lesson())
        self.observe(lesson(code="2"), group=20)
        self.observe(lesson(room="1-282"), at=self.t2)
        all_events = self.store.events([10, 20])
        self.assertEqual([event["kind"] for event in all_events], ["changed", "baseline", "baseline"])
        self.assertEqual(self.store.events([20])[0]["group_name"], "ВКБ52")
        self.assertTrue(all(event["group_id"] == 10 for event in self.store.events([10])))
        self.assertEqual(self.store.events([10, 20], limit=1, offset=1)[0]["id"], all_events[1]["id"])
        self.assertEqual(self.store.events([]), [])

    def test_stale_observation_cannot_roll_back_current_schedule(self):
        self.observe(lesson(room="1-282"), at=self.t2)
        self.observe(lesson(room="1-140"), at=self.t1)
        self.assertEqual(self.rows()[0]["lesson"]["аудитория"], "1-282")
        self.assertEqual(self.rows()[0]["history_count"], 1)

    def test_changes_feed_filters_baselines_before_pagination(self):
        self.observe(lesson())
        self.observe(lesson(room="1-282"), at=self.t2)
        self.observe(lesson(code="2"), group=20)
        self.observe(lesson(room="1-333"), at=self.t3)
        self.observe(lesson(code="3"), group=30)
        first = self.store.events([10, 20, 30], limit=1, include_baseline=False)
        second = self.store.events([10, 20, 30], limit=1, offset=1, include_baseline=False)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]["after"]["аудитория"], "1-333")
        self.assertEqual(second[0]["after"]["аудитория"], "1-282")
        self.assertEqual(self.store.events([10, 20, 30], offset=2, include_baseline=False), [])
        self.assertEqual(self.store.events([20, 30], include_baseline=False), [])
        self.assertEqual(len(self.store.events([10, 20, 30])), 5)

    def test_failed_event_insert_rolls_back_lesson_update(self):
        self.observe(lesson())
        uid = self.rows()[0]["id"]
        with patch.object(HistoryStore, "_event", side_effect=RuntimeError("disk failure")):
            with self.assertRaisesRegex(RuntimeError, "disk failure"):
                self.observe(lesson(room="1-282"), at=self.t2)
        row = self.store.lesson(uid)
        self.assertEqual(row["lesson"]["аудитория"], "1-101")
        self.assertEqual(row["history_count"], 1)
        self.assertEqual(row["observed_at"], self.t1)

    def test_concurrent_identical_polls_do_not_duplicate_history(self):
        self.observe(lesson())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [pool.submit(self.observe, lesson(code="2", room="1-282"), at=self.t2)
                       for _ in range(8)]
            for result in results:
                result.result()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["history_count"], 2)

    def test_bad_input_is_rejected_before_any_database_mutation(self):
        with self.assertRaises(ValueError):
            self.observe(lesson(), at="2026-11-17T08:55:00")
        with self.assertRaises(ValueError):
            self.store.observe(10, "ВКБ51", {"wrong-code": lesson()}, self.t1)
        self.assertEqual(self.rows(), [])
        with self.assertRaises(ValueError):
            self.store.schedule(10, "2026-11-30", "2026-11-01")

    def test_has_group_survives_restart_and_includes_empty_snapshots(self):
        self.assertFalse(self.store.has_group(10))
        self.observe()
        self.assertTrue(self.store.has_group(10))
        self.assertFalse(self.store.has_group(20))
        reopened = HistoryStore(self.path)
        self.assertTrue(reopened.has_group(10))
        self.assertEqual(reopened.schedule(10, "2026-11-01", "2026-11-30"), [])


if __name__ == "__main__":
    unittest.main()
