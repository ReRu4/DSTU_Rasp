import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from teacher_api import TeacherAPI, avatar_url, names_match


def search_response():
    # Reduced public response shape from /api/search, verified 2026-11-17.
    return {"state": 1, "data": {"results": [
        {"name": "Савельев Василий Александрович", "type": "Преподаватель", "objectID": 5104, "additionalObjectID": 3704},
        {"name": "Савельев Владимир Александрович", "type": "Преподаватель", "objectID": 9000, "additionalObjectID": 9001},
        {"name": "Савельев Владимир Игоревич", "type": "Преподаватель", "objectID": 9830, "additionalObjectID": 32349},
        {"name": "Савельева Екатерина Сергеевна", "type": "Преподаватель", "objectID": 8217, "additionalObjectID": 30194},
        {"name": "Савельев В.А.", "type": "Группа", "objectID": 3, "additionalObjectID": 4},
    ]}}


def schedule_response():
    # Public fields retained by teacher integration in addition to core FIELDS.
    return {"state": 1, "data": {"info": {"prepod": {"name": "Савельев В. А.", "userID": 5104}},
        "rasp": [{"код": 378639929, "датаНачала": "2026-11-20T08:30:00", "датаОкончания": "2026-11-20T10:05:00",
                  "дисциплина": "лек Программирование алгоритмов защиты информации", "преподаватель": "Савельев В.А.",
                  "аудитория": "8-331", "группа": "ВКБ51, ВКБ52, ВКБ53", "кодПреподавателя": 3704,
                  "кодПользователя": 5104, "замена": False}]}}


class TeacherAPITests(unittest.TestCase):
    def setUp(self):
        self.now = 1794900000.0  # 2026-11-17
        self.request = Mock()
        self.api = TeacherAPI(requester=self.request, clock=lambda: self.now)

    def test_search_uses_teacher_schedule_id_and_keeps_ambiguous_initials(self):
        self.request.return_value = search_response()
        found = self.api.resolve("Савельев В.А.")
        self.assertEqual([item["id"] for item in found], [3704, 9001])
        self.assertEqual(found[0]["user_id"], 5104)
        self.assertIn("/Photo?userID=5104&miniature=true", found[0]["avatar_url"])
        self.request.assert_called_once_with("https://edu.donstu.ru/api/search", {"searchQuery": "савельев"}, timeout=15)

    def test_full_name_selects_only_correct_person(self):
        self.request.return_value = search_response()
        self.assertEqual([item["id"] for item in self.api.resolve("Савельев Василий Александрович")], [3704])

    def test_titles_yo_and_spaces_are_normalized_without_fuzzy_surnames(self):
        self.assertTrue(names_match("преп.Молодцова А.И.", "Молодцова Анна Ивановна"))
        self.assertTrue(names_match("Доц. Сафьян О. Ё.", "Сафьян Олег Евгеньевич"))
        self.assertFalse(names_match("Савельев В.А.", "Савельева Василиса Александровна"))
        self.assertFalse(names_match("Савельев В.А.", "Савельев Владимир Игоревич"))
        self.assertFalse(names_match("Савельев В.А.", "Савельев Владимир"))
        self.assertTrue(names_match("Преподкин А.А.", "Преподкин Александр Алексеевич"))

    def test_invalid_ids_and_arbitrary_photo_urls_are_rejected(self):
        for value in [True, 0, -1, "../photo", "https://example.com", "1?x=2", "001"]:
            self.assertIsNone(avatar_url(value))
            with self.assertRaises(ValueError):
                self.api.schedule(value)
        self.request.assert_not_called()

    def test_real_schedule_shape_and_all_joint_groups_are_preserved(self):
        self.request.return_value = schedule_response()
        result = self.api.schedule(3704, "Савельев В.А.")
        self.request.assert_called_once_with("https://edu.donstu.ru/api/Rasp", {"idTeacher": 3704}, timeout=20)
        self.assertEqual(result["teacher"]["user_id"], 5104)
        lesson = result["snapshot"]["378639929"]
        self.assertEqual(lesson["group_name"], "ВКБ51, ВКБ52, ВКБ53")
        self.assertEqual(lesson["teacher_id"], 3704)
        self.assertTrue(result["has_snapshot"])
        self.assertFalse(result["last_fetch_failed"])

    def test_wrong_teacher_is_not_accepted(self):
        self.request.return_value = schedule_response()
        with self.assertRaisesRegex(RuntimeError, "другого преподавателя"):
            self.api.schedule(3704, "Петров П.П.")
        self.now += 31
        wrong = schedule_response()
        wrong["data"]["rasp"][0]["кодПреподавателя"] = 999
        self.request.return_value = wrong
        with self.assertRaisesRegex(RuntimeError, "другого преподавателя"):
            self.api.schedule(3704)

    def test_known_user_id_must_match_profile(self):
        self.request.return_value = search_response()
        self.api.resolve("Савельев Василий Александрович")
        wrong = schedule_response()
        wrong["data"]["info"]["prepod"]["userID"] = 999
        self.request.return_value = wrong
        with self.assertRaisesRegex(RuntimeError, "другого преподавателя"):
            self.api.schedule(3704)

    def test_cache_avoids_network_and_does_not_leak_mutable_data(self):
        self.request.return_value = schedule_response()
        first = self.api.schedule(3704)
        first["snapshot"].clear()
        self.assertEqual(len(self.api.schedule(3704)["snapshot"]), 1)
        self.assertEqual(self.request.call_count, 1)

    def test_disk_cache_survives_restart_and_network_failure(self):
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "teachers.sqlite3"
            self.request.return_value = schedule_response()
            first_api = TeacherAPI(cache, requester=self.request, clock=lambda: self.now)
            first = first_api.schedule(3704)
            self.now += 301
            unavailable = Mock(side_effect=RuntimeError("Network unavailable"))
            second_api = TeacherAPI(cache, requester=unavailable, clock=lambda: self.now)
            fallback = second_api.schedule(3704)
            self.assertTrue(fallback["last_fetch_failed"])
            self.assertEqual(fallback["snapshot"], first["snapshot"])
            self.assertEqual(fallback["fetched_at"], first["fetched_at"])
            self.assertNotEqual(fallback["last_fetch_attempt_at"], first["last_fetch_attempt_at"])

    def test_outage_without_cache_is_an_error(self):
        self.request.side_effect = RuntimeError("Network unavailable")
        with self.assertRaises(RuntimeError):
            self.api.schedule(3704)
        with self.assertRaises(RuntimeError):
            self.api.schedule(3704)
        self.assertEqual(self.request.call_count, 1)

    def test_stale_cache_failure_has_short_retry_cooldown(self):
        self.request.return_value = schedule_response()
        self.api.schedule(3704)
        self.now += 301
        self.request.side_effect = RuntimeError("Offline")
        self.assertTrue(self.api.schedule(3704)["last_fetch_failed"])
        self.assertTrue(self.api.schedule(3704)["last_fetch_failed"])
        self.assertEqual(self.request.call_count, 2)
        self.now += 31
        self.api.schedule(3704)
        self.assertEqual(self.request.call_count, 3)

    def test_empty_response_cannot_erase_upcoming_saved_lessons(self):
        self.request.return_value = schedule_response()
        first = self.api.schedule(3704)
        self.now += 301
        empty = schedule_response()
        empty["data"]["rasp"] = []
        self.request.return_value = empty
        fallback = self.api.schedule(3704)
        self.assertTrue(fallback["last_fetch_failed"])
        self.assertEqual(fallback["snapshot"], first["snapshot"])

    def test_bad_search_format_and_incomplete_ids_are_not_silently_accepted(self):
        self.request.return_value = {"state": 1, "data": {"results": None}}
        with self.assertRaises(RuntimeError):
            self.api.resolve("Савельев В.А.")
        bad = search_response()
        bad["data"]["results"][0]["additionalObjectID"] = None
        self.request.return_value = bad
        self.assertEqual([p["id"] for p in self.api.resolve("Савельев В.А.")], [9001])

    def test_search_cache_is_persistent_and_stale_candidates_remain_available(self):
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "teachers.sqlite3"
            self.request.return_value = search_response()
            first_api = TeacherAPI(cache, requester=self.request, clock=lambda: self.now)
            expected = first_api.resolve("Савельев В.А.")
            self.now += 3601
            restarted = TeacherAPI(cache, requester=Mock(side_effect=RuntimeError("Offline")), clock=lambda: self.now)
            self.assertEqual(restarted.resolve("Савельев В.А."), expected)


if __name__ == "__main__":
    unittest.main()
