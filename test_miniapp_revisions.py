"""Visible revisions and durable change-feed behavior across real bot snapshots."""
import unittest
from datetime import timedelta

import bot
from app_bot import ScheduleBot
from miniapp_server import MiniAppService
import test_miniapp as existing_tests
from watch_groups import notification_subject


class RevisionAndJournalTests(unittest.TestCase):
    # Reuse the isolated application fixture, without rediscovering its tests.
    setUp = existing_tests.MiniAppTests.setUp
    persist = existing_tests.MiniAppTests.persist
    dispatch = existing_tests.MiniAppTests.dispatch

    def schedule(self, start=None, end=None):
        day = self.raw['датаНачала'][:10]
        return self.dispatch('/api/schedule', {'from': start or day, 'to': end or day})

    def observe(self, snapshot, seconds):
        self.app.process_profile_snapshot(self.owner, snapshot, self.now + timedelta(seconds=seconds))
        self.persist()

    def test_checks_and_source_code_churn_do_not_change_visible_revision(self):
        first = self.schedule()
        self.observe(self.snapshot, 1)
        checked = self.schedule()
        self.assertNotEqual(first['freshness'][0]['last_fetch_at'], checked['freshness'][0]['last_fetch_at'])
        self.assertNotEqual(first['lessons'][0]['observed_at'], checked['lessons'][0]['observed_at'])
        self.assertEqual(first['revision'], checked['revision'])
        self.assertEqual(first['day_revisions'], checked['day_revisions'])
        self.observe({'999': {**self.raw, 'код': '999'}}, 2)
        recoded = self.schedule()
        self.assertEqual(first['revision'], recoded['revision'])
        self.assertEqual(first['lessons'][0]['id'], recoded['lessons'][0]['id'])
        self.assertEqual(self.dispatch('/api/changes')['total_events'], 0)

    def test_edit_on_another_day_only_changes_that_days_revision(self):
        day = self.raw['датаНачала'][:10]
        other_day = (self.now + timedelta(days=2)).date().isoformat()
        second = {**self.raw, 'код': '2', 'датаНачала': other_day + 'T14:15:00',
                  'датаОкончания': other_day + 'T15:50:00'}
        self.observe({**self.snapshot, '2': second}, 1)
        first = self.schedule(day, other_day)
        self.observe({**self.snapshot, '2': {**second, 'аудитория': '6-402'}}, 2)
        changed = self.schedule(day, other_day)
        self.assertNotEqual(first['revision'], changed['revision'])
        self.assertEqual(first['day_revisions'][day], changed['day_revisions'][day])
        self.assertNotEqual(first['day_revisions'][other_day], changed['day_revisions'][other_day])

    def test_empty_days_also_have_stable_revisions(self):
        day = (self.now + timedelta(days=10)).date().isoformat()
        first = self.schedule(day, day)
        self.assertEqual(first['lessons'], [])
        self.assertIn(day, first['day_revisions'])
        self.observe(self.snapshot, 1)
        self.assertEqual(first['revision'], self.schedule(day, day)['revision'])

    def test_journal_records_all_actions_despite_filters_and_survives_restart(self):
        key, name = notification_subject(self.raw)
        self.owner.update({'alert_types': [], 'muted_subjects': {key: name}})
        original = {**self.raw, 'аудитория': '1-500'}
        extra = {**self.raw, 'код': '2', 'дисциплина': 'пр Криптография',
                 'датаНачала': self.raw['датаНачала'][:10] + 'T14:15:00',
                 'датаОкончания': self.raw['датаНачала'][:10] + 'T15:50:00'}
        self.observe({'1': original}, 1)
        self.observe({'1': original, '2': extra}, 2)
        self.observe({'1': original}, 3)
        final = {'1': original, '3': {**extra, 'код': '3'}}
        self.observe(final, 4)
        before = self.dispatch('/api/changes')
        self.assertEqual(before['total_events'], 4)
        self.assertEqual([e['kind'] for e in before['events']], ['restored', 'removed', 'added', 'changed'])
        self.assertEqual(before['events'][-1]['before']['room'], '1-333А')
        self.assertEqual(before['events'][-1]['after']['room'], '1-500')
        self.assertFalse(self.owner['pending'])
        restarted = ScheduleBot(self.root)
        self.service = MiniAppService(restarted.state_file, restarted.history, restarted.miniapp_queue,
                                      existing_tests.TOKEN, self.teachers)
        self.assertEqual(before['revision'], self.dispatch('/api/changes')['revision'])
        restarted.process_profile_snapshot(restarted.profile_for_chat(42), final,
                                            self.now + timedelta(seconds=5))
        bot.save_state(restarted.state_file, restarted.state)
        after = self.dispatch('/api/changes')
        self.assertEqual(after['total_events'], 4)
        self.assertEqual(before['revision'], after['revision'])
        self.assertEqual({e['id'] for e in before['events']}, {e['id'] for e in after['events']})

    def test_change_cursor_is_stable_when_new_events_arrive(self):
        for i in range(1, 4):
            self.observe({'1': {**self.raw, 'аудитория': str(200 + i)}}, i)
        first = self.dispatch('/api/changes', {'limit': '1'})
        self.assertEqual(first['total_events'], 3)
        cursor = first['next_cursor']
        self.assertEqual(cursor, first['events'][0]['id'])
        self.observe({'1': {**self.raw, 'аудитория': '204'}}, 4)
        second = self.dispatch('/api/changes', {'limit': '1', 'before_id': str(cursor)})
        self.assertEqual(second['total_events'], 4)
        self.assertLess(second['events'][0]['id'], cursor)
        third = self.dispatch('/api/changes', {'limit': '1', 'before_id': str(second['next_cursor'])})
        self.assertIsNone(third['next_cursor'])
        self.assertEqual(len({first['events'][0]['id'], second['events'][0]['id'], third['events'][0]['id']}), 3)

    def test_teacher_revision_and_ids_survive_source_code_churn(self):
        teacher = {'id': 3704, 'name': 'Иванов Иван Иванович', 'avatar_url': None}
        self.teachers.resolve.return_value = [teacher]
        base = {'teacher': teacher, 'fetched_at': self.now.isoformat(), 'last_fetch_failed': False}
        self.teachers.schedule.return_value = {**base, 'snapshot': {'1': {**self.raw, 'group_name': 'ВКБ51'}}}
        params = {'lesson_id': self.uid, 'from': self.raw['датаНачала'][:10], 'to': self.raw['датаНачала'][:10]}
        first = self.dispatch('/api/teacher', params)
        self.teachers.schedule.return_value = {**base, 'fetched_at': (self.now + timedelta(minutes=5)).isoformat(),
                                              'snapshot': {'900': {**self.raw, 'код': '900', 'group_name': 'ВКБ51'}}}
        second = self.dispatch('/api/teacher', params)
        self.assertEqual(first['revision'], second['revision'])
        self.assertEqual(first['day_revisions'], second['day_revisions'])
        self.assertEqual(first['lessons'][0]['id'], second['lessons'][0]['id'])


if __name__ == '__main__':
    unittest.main()
