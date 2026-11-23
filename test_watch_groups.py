"""Multi-group reports, opt-in state and delivery choices."""
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bot
from bot import MultiBot
from test_bot import lesson
from watch_groups import WatchGroupsMixin, notification_subject, subject_token


class WatchApp(WatchGroupsMixin, MultiBot):
    pass


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 30, 10, 0, tzinfo=tz)


class WatchGroupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test-token'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock1 = patch('watch_groups.datetime', Clock)
        self.clock2 = patch('bot.datetime', Clock)
        self.clock1.start(); self.clock2.start()
        self.addCleanup(self.clock1.stop); self.addCleanup(self.clock2.stop)
        self.root = Path(self.temp.name)
        self.app = WatchApp(self.root)
        self.owner = self.app.new_profile('telegram', 42)
        self.owner.update({'group_checked_on': '2026-10-30', 'daily_time': '23:59', 'keyboard_version': 3})
        self.app.state['profiles'] = {'telegram:42': self.owner}
        self.now = Clock.now(bot.MOSCOW)

    def snapshot(self, room='101', code=1):
        return {str(code): lesson(code=code, room=room, start='2026-10-31T08:30:00')}

    def test_full_lecture_military_alias_and_legacy_mute_survive_restart(self):
        label = 'Лекция 1 Военная кафедра (ЦВПР с.запаса чт)'
        raw = {**self.snapshot()['1'], 'дисциплина': label}
        algebra = {**raw, 'дисциплина': 'Лекция 2 Алгебра'}
        legacy_key = label.casefold()
        unrelated = {'криптография': 'Криптография', 'лек 1 курс': 'лек 1 курс',
                     'произвольный-ключ': 'Лекция 3 Сети'}
        self.owner['muted_subjects'] = {legacy_key: label, 'лекция 2 алгебра': 'Лекция 2 Алгебра', **unrelated}
        bot.save_state(self.app.state_file, self.app.state)
        restarted = WatchApp(self.root)
        owner = restarted.profile_for_chat(42)
        self.assertEqual(notification_subject(raw), ('военная кафедра', 'Военная кафедра'))
        self.assertEqual(owner['muted_subjects'], {'военная кафедра': 'Военная кафедра', 'алгебра': 'Алгебра', **unrelated})
        self.assertFalse(restarted.subject_change_allowed(owner, raw, {**raw, 'аудитория': '102'}))
        self.assertFalse(restarted.subject_change_allowed(owner, algebra, {**algebra, 'аудитория': '102'}))
        again = WatchApp(self.root)
        self.assertEqual(again.profile_for_chat(42)['muted_subjects'], owner['muted_subjects'])

    def test_legacy_full_type_mutes_do_not_duplicate_subject_menu_entries(self):
        labels = ['Лекция 1 Военная кафедра (ЦВПР с.запаса чт)', 'Лекция 2 Алгебра']
        self.owner['muted_subjects'] = {label.casefold(): label for label in labels}
        self.owner['snapshot'] = {str(i): {**self.snapshot(code=i)[str(i)], 'дисциплина': label}
                                  for i, label in enumerate(labels, 1)}
        bot.save_state(self.app.state_file, self.app.state)
        restarted = WatchApp(self.root)
        owner = restarted.profile_for_chat(42)
        subjects = restarted.notification_subjects(owner)
        self.assertEqual(subjects, [('алгебра', 'Алгебра'), ('военная кафедра', 'Военная кафедра')])
        self.assertEqual(set(owner['muted_subjects']), {key for key, name in subjects})

    def test_legacy_mute_alias_collision_preserves_existing_canonical_entry(self):
        self.owner['muted_subjects'] = {'алгебра': 'АЛГЕБРА', 'лекция 2 алгебра': 'Лекция 2 Алгебра'}
        self.app._watch_defaults(self.owner)
        self.assertEqual(self.owner['muted_subjects'], {'алгебра': 'АЛГЕБРА'})

    def child(self, group_id=123, name='ПИ31'):
        child = self.app.new_profile('telegram', 42)
        child.update({'group_id': group_id, 'group_name': name, 'owner_chat_id': 42,
                      'group_checked_on': '2026-10-30'})
        self.owner['watched_groups'][str(group_id)] = child
        return child

    def cycle(self, responses):
        with patch.object(self.app, 'telegram_commands'), patch.object(self.app, 'drain'), \
             patch.object(self.app, 'ensure_telegram_keyboards'), \
             patch('bot.fetch_schedule', side_effect=responses) as fetch:
            self.app.cycle()
        return fetch

    def test_opt_in_and_personal_settings_survive_reload(self):
        child = self.child()
        self.assertFalse(self.owner['multigroup_enabled'])
        self.assertFalse(self.owner['send_schedule_on_change'])
        self.assertEqual(self.app.monitored_schedules(self.owner), [self.owner])
        self.owner.update({'multigroup_enabled': True, 'selected_group_id': 123,
                           'display_format': 'text', 'week_layout': 'list'})
        self.assertIs(self.app.selected_schedule(self.owner), child)
        self.assertEqual(child['display_format'], 'text')
        self.assertEqual(child['week_layout'], 'list')
        bot.save_state(self.app.state_file, self.app.state)
        restored = WatchApp(self.root)
        owner = restored.profile_for_chat(42)
        self.assertEqual(restored.selected_schedule(owner)['group_name'], 'ПИ31')
        self.assertEqual(owner['watched_groups']['123']['chat_id'], 42)

    def test_one_report_contains_two_groups_and_no_automatic_cards_by_default(self):
        child = self.child()
        self.owner['multigroup_enabled'] = True
        self.owner['snapshot'] = self.snapshot()
        child['snapshot'] = self.snapshot(code=2)
        self.cycle([self.snapshot('102'), self.snapshot('202', 2)])
        pending = self.owner['pending']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['kind'], 'combined_change')
        self.assertIn('ВКБ51', pending[0]['text'])
        self.assertIn('ПИ31', pending[0]['text'])
        self.assertIn('101 → 102', pending[0]['text'])
        self.assertIn('101 → 202', pending[0]['text'])
        batch = self.owner['change_batches'][pending[0]['batch_id']]
        self.assertEqual(len(batch['groups']), 2)
        with patch.object(self.app, '_send_group_change_card') as send:
            self.app.show_profile_change_batch(self.owner, pending[0]['batch_id'])
        self.assertEqual(send.call_count, 2)
        self.assertEqual({call.args[1]['group_name'] for call in send.call_args_list}, {'ВКБ51', 'ПИ31'})
        self.assertEqual(child['pending'], [])

    def test_initial_snapshots_are_not_changes_and_same_group_fetch_is_shared(self):
        child = self.child()
        self.owner['multigroup_enabled'] = True
        other = self.app.new_profile('telegram', 77)
        other.update({'group_name': 'ПИ31', 'group_id': 123, 'group_checked_on': '2026-10-30',
                      'daily_time': '23:59', 'keyboard_version': 3})
        self.app.state['profiles']['telegram:77'] = other
        fetch = self.cycle([self.snapshot(), self.snapshot(code=2)])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(child['snapshot'], other['snapshot'])
        self.assertEqual(self.owner['pending'], [])
        self.assertEqual(other['pending'], [])

    def test_auto_cards_apply_to_single_group_and_skip_disabled_alerts(self):
        self.owner.update({'send_schedule_on_change': True, 'snapshot': self.snapshot()})
        self.cycle([self.snapshot('103')])
        self.assertEqual([item['kind'] for item in self.owner['pending']], ['combined_change', 'change_card'])
        self.assertEqual(self.owner['pending'][1]['source_group_id'], self.owner['group_id'])
        self.owner['pending'] = []
        self.owner['alert_types'] = ['teacher']
        self.app.process_profile_snapshot(self.owner, self.snapshot('104'), self.now)
        self.app.flush_group_reports()
        self.assertEqual(self.owner['pending'], [])
        self.assertEqual(self.owner['snapshot'], self.snapshot('104'))

    def test_disabling_prunes_additional_groups_from_queued_reports(self):
        child = self.child()
        self.owner.update({'multigroup_enabled': True, 'send_schedule_on_change': True,
                           'snapshot': self.snapshot(), 'selected_group_id': 123})
        child['snapshot'] = self.snapshot(code=2)
        self.cycle([self.snapshot('102'), self.snapshot('202', 2)])
        with patch.object(self.app, 'answer_callback'), patch.object(self.app, 'show_watch_settings'):
            self.app.handle_profile_callback(self.owner, {'id': 'cb', 'data': 'watch:off'})
        self.assertFalse(self.owner['multigroup_enabled'])
        self.assertIn('123', self.owner['watched_groups'])
        self.assertIs(self.app.selected_schedule(self.owner), self.owner)
        self.assertEqual([item['kind'] for item in self.owner['pending']], ['combined_change', 'change_card'])
        self.assertNotIn('ПИ31', self.owner['pending'][0]['text'])
        self.assertEqual(self.owner['pending'][1]['source_group_id'], self.owner['group_id'])

    def test_failed_group_keeps_snapshot_while_other_group_reports_changes(self):
        child = self.child()
        self.owner.update({'multigroup_enabled': True, 'snapshot': self.snapshot()})
        child['snapshot'] = self.snapshot(code=2)
        self.cycle([self.snapshot('102'), RuntimeError('offline')])
        self.assertTrue(child['last_fetch_failed'])
        self.assertEqual(child['snapshot'], self.snapshot(code=2))
        self.assertEqual(len(self.owner['pending']), 1)
        self.assertIn('ВКБ51', self.owner['pending'][0]['text'])
        self.assertNotIn('ПИ31', self.owner['pending'][0]['text'])

    def test_adding_group_validates_before_save_and_never_notifies_existing_lessons(self):
        self.owner.update({'multigroup_enabled': True, 'awaiting': 'watch_group'})
        with patch('bot.find_group_id', return_value=123), \
             patch('bot.fetch_schedule', return_value=self.snapshot(code=2)), \
             patch.object(self.app, 'send_profile_message'), patch.object(self.app, 'show_watch_settings'):
            self.app.apply_user_input(self.owner, 'ПИ31', '2026-10-30')
        self.assertEqual(self.owner['watched_groups']['123']['snapshot'], self.snapshot(code=2))
        self.assertEqual(self.owner['pending'], [])
        restored = WatchApp(self.root).profile_for_chat(42)
        self.assertEqual(restored['watched_groups']['123']['group_name'], 'ПИ31')
        self.owner['awaiting'] = 'watch_group'
        with patch('bot.find_group_id') as search, patch.object(self.app, 'send_profile_message'):
            self.app.apply_user_input(self.owner, 'пи-31', '2026-10-30')
        search.assert_not_called()

    def test_manual_refresh_updates_selected_group_and_persists_report(self):
        child = self.child()
        self.owner.update({'multigroup_enabled': True, 'selected_group_id': 123,
                           'snapshot': self.snapshot()})
        child['snapshot'] = self.snapshot(code=2)
        callback = {'id': 'cb', 'data': 'refresh:week:2026-10-26', 'message': {'message_id': 9}}
        with patch('bot.fetch_schedule', return_value=self.snapshot('222', 2)) as fetch, \
             patch.object(self.app, 'answer_callback'), patch.object(self.app, 'show_schedule') as show:
            self.app.handle_refresh_callback(self.owner, callback)
            self.app.handle_refresh_callback(self.owner, callback)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0], 123)
        self.assertEqual(self.owner['snapshot'], self.snapshot())
        self.assertEqual(show.call_args.kwargs['message_id'], 9)
        self.assertEqual(len(self.owner['pending']), 1)
        saved = WatchApp(self.root).profile_for_chat(42)
        self.assertEqual(saved['pending'][0]['text'], self.owner['pending'][0]['text'])

    def test_release_marker_only_written_after_successful_send(self):
        self.owner['pending'] = [{'kind': 'release', 'text': 'News', 'release_version': 'v-next'}]
        with patch.object(self.app, 'send_profile_message', side_effect=RuntimeError('offline')):
            self.app.drain('2026-10-30')
        self.assertNotIn('last_release_notified', self.owner)
        self.assertEqual(len(self.owner['pending']), 1)
        with patch.object(self.app, 'send_profile_message'):
            self.app.drain('2026-10-30')
        self.assertEqual(self.owner['last_release_notified'], 'v-next')
        self.assertEqual(self.owner['pending'], [])

    def test_existing_cards_refresh_with_new_cards_off_even_when_alert_type_disabled(self):
        child = self.child()
        self.owner.update({'multigroup_enabled': True, 'alert_types': ['teacher']})
        child['snapshot'] = self.snapshot(code=2)
        child['cards'] = {'day:2026-10-31': [99], 'week:2026-10-26': [100], 'month:2026-10-01': [101]}
        self.app.process_profile_snapshot(child, self.snapshot('222', 2), self.now)
        self.app.flush_group_reports()
        self.assertEqual(len(self.owner['pending']), 3)
        self.assertEqual({item['kind'] for item in self.owner['pending']}, {'refresh'})
        self.assertEqual({item['source_group_id'] for item in self.owner['pending']}, {123})
        self.assertFalse(self.owner['send_schedule_on_change'])
        with patch.object(self.app, 'send_profile_card') as send:
            self.app.drain('2026-10-30')
        self.assertEqual(send.call_count, 3)
        self.assertTrue(all(call.args[0] is child and call.kwargs['replace'] for call in send.call_args_list))
        # A refreshed API identifier is not a real lesson change and must not edit messages.
        new_code = self.snapshot('222', 5)
        self.app.process_profile_snapshot(child, new_code, self.now)
        self.assertEqual(self.owner['pending'], [])

    def test_refresh_callback_keeps_its_group_even_after_selection_changes(self):
        child = self.child()
        self.owner.update({'multigroup_enabled': True, 'selected_group_id': 123,
                           'snapshot': self.snapshot()})
        child['snapshot'] = self.snapshot(code=2)
        callback = {'id': 'cb', 'data': f"refresh:{self.owner['group_id']}:week:2026-10-26",
                    'message': {'message_id': 9}}
        with patch('bot.fetch_schedule', return_value=self.snapshot('111')) as fetch, \
             patch.object(self.app, 'answer_callback'), patch.object(self.app, 'show_schedule'):
            self.app.handle_refresh_callback(self.owner, callback)
        self.assertEqual(fetch.call_args.args[0], self.owner['group_id'])
        self.assertEqual(self.owner['selected_group_id'], self.owner['group_id'])
        self.assertEqual(child['snapshot'], self.snapshot(code=2))

    def test_muted_military_changes_update_all_snapshots_and_cards_without_alerts(self):
        child = self.child()
        military = self.snapshot()['1']
        military['дисциплина'] = 'лек Военная кафедра (ЦВПР с.запаса чт)'
        second = {**military, 'код': '2', 'дисциплина': 'пр Военная кафедра (ЦВПР запаса пт)'}
        ordinary = self.snapshot(code=3)['3']
        self.owner.update({'multigroup_enabled': True, 'send_schedule_on_change': True,
                           'muted_subjects': {'военная кафедра': 'Военная кафедра'},
                           'snapshot': {'1': military}, 'cards': {'day:2026-10-31': [91]}})
        child.update({'snapshot': {'2': second, '3': ordinary},
                      'cards': {'week:2026-10-26': [92]}, 'last_fetch_failed': True})
        self.app.process_profile_snapshot(self.owner, {'1': {**military, 'аудитория': '333'}}, self.now)
        self.app.process_profile_snapshot(child, {'2': {**second, 'аудитория': '444'},
                                                 '3': {**ordinary, 'аудитория': '555'}}, self.now)
        self.app.flush_group_reports()
        reports = [item for item in self.owner['pending'] if item['kind'] == 'combined_change']
        self.assertEqual(len(reports), 1)
        self.assertNotIn('Военная кафедра', reports[0]['text'])
        self.assertIn('Тестовый предмет', reports[0]['text'])
        self.assertEqual(self.owner['snapshot']['1']['аудитория'], '333')
        self.assertEqual(child['snapshot']['2']['аудитория'], '444')
        self.assertEqual(child['last_fetch_at'], self.now.isoformat(timespec='seconds'))
        self.assertFalse(child['last_fetch_failed'])
        batch = self.owner['change_batches'][reports[0]['batch_id']]
        self.assertEqual(len(batch['groups']), 1)
        markers = batch['groups'][0]['weeks']['2026-10-26']['changed']
        self.assertEqual(set(markers), {'3'})
        automatic = [item for item in self.owner['pending'] if item['kind'] == 'change_card']
        self.assertEqual(len(automatic), 1)
        self.assertEqual(automatic[0]['source_group_id'], child['group_id'])
        refreshes = [item for item in self.owner['pending'] if item['kind'] == 'refresh']
        self.assertEqual({item['source_group_id'] for item in refreshes}, {self.owner['group_id'], 123})

    def test_subject_switch_persists_covers_child_and_can_be_restored_without_new_data(self):
        child = self.child()
        child['snapshot'] = self.snapshot(code=2)
        child['snapshot']['2']['дисциплина'] = 'лаб   Тестовый   предмет'
        self.owner.update({'snapshot': self.snapshot(), 'multigroup_enabled': True})
        subjects = self.app.notification_subjects(self.owner)
        self.assertEqual(len(subjects), 1)
        key, name = subjects[0]
        callback = {'id': 'cb', 'data': f'subjects:toggle:{subject_token(key)}:0'}
        with patch.object(self.app, 'answer_callback'), patch.object(self.app, 'show_subject_settings'):
            self.app.handle_profile_callback(self.owner, callback)
        self.assertEqual(self.owner['muted_subjects'], {key: name})
        self.assertEqual(child['muted_subjects'], {key: name})
        restored = WatchApp(self.root)
        owner = restored.profile_for_chat(42)
        self.assertFalse(restored.subject_change_allowed(owner, None, child['snapshot']['2']))
        owner['snapshot'] = {}
        owner['watched_groups']['123']['snapshot'] = {}
        self.assertEqual(restored.notification_subjects(owner), [(key, name)])
        with patch.object(restored, 'answer_callback'), patch.object(restored, 'show_subject_settings'):
            restored.handle_profile_callback(owner, callback)
        self.assertEqual(owner['muted_subjects'], {})

    def test_muting_subject_is_personal_and_existing_type_filters_still_apply(self):
        other = self.app.new_profile('telegram', 77)
        other['snapshot'] = self.snapshot()
        self.app.state['profiles']['telegram:77'] = other
        key, name = notification_subject(self.snapshot()['1'])
        self.owner.update({'snapshot': self.snapshot(), 'muted_subjects': {key: name},
                           'send_schedule_on_change': True})
        self.app.process_profile_snapshot(self.owner, self.snapshot('222'), self.now)
        self.app.process_profile_snapshot(other, self.snapshot('222'), self.now)
        self.app.flush_group_reports()
        self.assertEqual(self.owner['pending'], [])
        self.assertEqual(len(other['pending']), 1)
        # Re-enabling starts from the current snapshot; it does not replay muted changes.
        self.owner['muted_subjects'] = {}
        self.owner['alert_types'] = ['teacher']
        self.app.process_profile_snapshot(self.owner, self.snapshot('333'), self.now)
        self.app.flush_group_reports()
        self.assertEqual(self.owner['pending'], [])

    def test_subject_filter_covers_add_remove_and_rename_on_either_side(self):
        before = self.snapshot()['1']
        self.owner['muted_subjects'] = {'тестовый предмет': 'Тестовый предмет'}
        renamed = {**before, 'дисциплина': 'лек Другой предмет'}
        variant = {**before, 'дисциплина': 'лаб ТЕСТОВЫЙ   ПРЕДМЕТ'}
        for old, new in ((None, before), (before, None), (before, renamed), (renamed, before),
                         (before, variant)):
            with self.subTest(old=old, new=new):
                self.assertFalse(self.app.subject_change_allowed(self.owner, old, new))
        self.assertTrue(self.app.subject_change_allowed(self.owner, None, renamed))

    def test_subject_menu_uses_all_enabled_groups_and_paginates_long_names(self):
        child = self.child()
        self.owner['snapshot'] = self.snapshot()
        long_name = 'Предмет дополнительной группы с очень длинным полным названием дисциплины'
        child['snapshot'] = {str(i): {**self.snapshot(code=i)[str(i)],
                                     'дисциплина': f'пр {long_name} {i}'} for i in range(2, 9)}
        self.assertEqual(len(self.app.notification_subjects(self.owner)), 1)
        self.owner['multigroup_enabled'] = True
        text, markup = self.app.subject_settings_view(self.owner)
        self.assertIn(long_name, text)
        self.assertIn('Страница 1 из 2', text)
        toggles = [button for row in markup['inline_keyboard'] for button in row
                   if button['callback_data'].startswith('subjects:toggle:')]
        self.assertEqual(len(toggles), 6)
        self.assertTrue(all(len(button['callback_data'].encode('utf-8')) <= 64 for button in toggles))
        second_text, second_markup = self.app.subject_settings_view(self.owner, 1)
        self.assertIn('Страница 2 из 2', second_text)
        self.assertIn('Тестовый предмет', second_text)

    def test_production_reload_skips_sunday_daily_and_expired_week_but_sends_changes(self):
        from app_bot import ScheduleBot

        self.owner['pending'] = [
            {'kind': 'daily', 'format': 'card', 'view': 'day', 'date': '2026-11-01'},
            {'kind': 'daily', 'text': 'Воскресное расписание', 'date': '2026-11-01'},
            {'kind': 'daily', 'text': 'Старая очередь без даты'},
            {'kind': 'weekly', 'format': 'card', 'view': 'week', 'date': '2026-10-26'},
            {'kind': 'refresh', 'format': 'card', 'view': 'week', 'date': '2026-10-26'},
            {'kind': 'combined_change', 'text': 'Аудитория на понедельник изменена'},
        ]
        bot.save_state(self.app.state_file, self.app.state)
        restored = ScheduleBot(self.root)
        with patch.object(restored, 'send_profile_message') as send, \
             patch.object(restored, 'send_profile_card') as send_card:
            restored.drain('2026-11-01')
        send_card.assert_not_called()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], 'Аудитория на понедельник изменена')
        self.assertEqual(restored.profile_for_chat(42)['pending'], [])
        self.assertEqual(ScheduleBot(self.root).profile_for_chat(42)['pending'], [])


if __name__ == '__main__':
    unittest.main()
