import copy
import hashlib
import hmac
import json
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import bot
from app_bot import ScheduleBot
from miniapp_server import (APIError, CommandQueue, LimitedHTTPServer, MiniAppService,
                            make_handler, validate_command, validate_init_data)
from test_bot import lesson


TOKEN = 'test-token-for-signatures'


def signed(user_id=42, stamp=None, **extra):
    data = {'auth_date': str(int(time.time()) if stamp is None else stamp),
            'user': json.dumps({'id': user_id, 'first_name': 'Test'}, separators=(',', ':')), **extra}
    check = '\n'.join(f'{key}={data[key]}' for key in sorted(data))
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class AuthTests(unittest.TestCase):
    def test_auth_signature_tampering_age_and_duplicate_fields(self):
        self.assertEqual(validate_init_data(signed(), TOKEN), 42)
        self.assertEqual(validate_init_data(signed(signature='signed-by-telegram'), TOKEN), 42)
        for raw in (signed().replace('Test', 'Other'), signed(stamp=int(time.time()) - 90000),
                    signed(stamp=int(time.time()) + 100), signed() + '&user=other', 'user=42',
                    signed(user_id=-42), signed(user_id=True)):
            with self.subTest(raw=raw[:30]), self.assertRaises(APIError) as ctx:
                validate_init_data(raw, TOKEN)
            self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(APIError):
            validate_init_data(signed(), 'another-token')


class MiniAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': TOKEN}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.app = ScheduleBot(self.root)
        self.owner = self.app.new_profile('telegram', 42)
        self.other = self.app.new_profile('telegram', 43)
        self.other.update({'group_id': 99, 'group_name': 'ПИ31'})
        self.app.state['profiles'] = {'telegram:42': self.owner, 'telegram:43': self.other}
        self.now = datetime.now(bot.MOSCOW)
        start = (self.now + timedelta(days=1)).strftime('%Y-%m-%dT08:30:00')
        self.raw = lesson(code=1, start=start, room='1-333А')
        self.snapshot = {'1': self.raw}
        self.app.process_profile_snapshot(self.owner, self.snapshot, self.now)
        self.app.process_profile_snapshot(self.other, {'2': lesson(code=2, start=start)}, self.now)
        self.persist()
        self.teachers = Mock()
        self.service = MiniAppService(self.app.state_file, self.app.history, self.app.miniapp_queue, TOKEN, self.teachers)
        self.uid = self.app.history.schedule(self.owner['group_id'], start[:10], start[:10])[0]['id']

    def persist(self):
        bot.save_state(self.app.state_file, self.app.state)

    def dispatch(self, path, params=None, method='GET', body=None, user=42):
        chat, owner = self.service.authenticate('tma ' + signed(user))
        return self.service.dispatch(method, path, params or {}, body, chat, owner)

    def test_user_isolation_and_onboarding(self):
        bootstrap = self.dispatch('/api/bootstrap')
        self.assertEqual([g['id'] for g in bootstrap['groups']], [self.owner['group_id']])
        self.assertNotIn('pending', bootstrap['profile'])
        for path, params in [('/api/schedule', {'group_id': str(self.owner['group_id'])}), ('/api/lessons/' + self.uid, {})]:
            with self.subTest(path=path), self.assertRaises(APIError):
                self.dispatch(path, params, user=43)
        with self.assertRaises(APIError) as ctx:
            self.service.authenticate('tma ' + signed(100))
        self.assertEqual(ctx.exception.status, 403)
        self.owner['onboarding'] = True; self.persist()
        with self.assertRaises(APIError):
            self.dispatch('/api/bootstrap')

    def test_schedule_and_history_ignore_notification_filters(self):
        self.owner['alert_types'] = []
        key = bot.subject_and_type(self.raw)[0].casefold()
        self.owner['muted_subjects'] = {key: bot.subject_and_type(self.raw)[0]}
        updated = {'9': {**self.raw, 'код': '9', 'аудитория': '1-492Б'}}
        self.app.process_profile_snapshot(self.owner, updated, self.now + timedelta(seconds=1))
        self.persist()
        detail = self.dispatch('/api/lessons/' + self.uid)
        self.assertEqual(detail['lesson']['room'], '1-492Б')
        self.assertEqual(detail['history'][0]['kind'], 'changed')
        self.assertEqual(detail['history'][0]['before']['room'], '1-333А')
        self.assertEqual(detail['history'][0]['after']['room'], '1-492Б')
        self.assertFalse(self.owner['pending'])
        start = self.raw['датаНачала'][:10]
        response = self.dispatch('/api/schedule', {'from': start, 'to': start})
        self.assertEqual(response['lessons'][0]['id'], self.uid)
        self.assertEqual(len(response['slots']), 7)
        self.assertEqual(len(self.dispatch('/api/changes')['events']), 1)

    def test_saved_schedule_survives_upstream_failure(self):
        with patch('bot.fetch_schedule', side_effect=RuntimeError('offline')), patch('bot.find_group_id', return_value=self.owner['group_id']):
            self.app.refresh_profiles([self.owner], self.now)
        self.persist()
        day = self.raw['датаНачала'][:10]
        response = self.dispatch('/api/schedule', {'from': day, 'to': day})
        self.assertEqual(len(response['lessons']), 1)
        self.assertTrue(response['freshness'][0]['last_fetch_failed'])
        self.assertEqual(response['freshness'][0]['last_fetch_at'], self.owner['last_fetch_at'])
        with self.assertRaises(RuntimeError):
            self.app.process_profile_snapshot(self.owner, {}, self.now)
        self.assertFalse(self.app.history.lesson(self.uid)['deleted'])

    def test_jobs_are_owned_persisted_and_applied_in_bot_thread(self):
        job = self.dispatch('/api/settings', method='POST', body={'daily_time': '19:30', 'seasonal_theme': False})
        self.assertNotEqual(self.owner['daily_time'], '19:30')
        with self.assertRaises(APIError):
            self.dispatch('/api/jobs/' + job['job_id'], user=43)
        self.app.process_miniapp_commands()
        self.assertEqual(self.dispatch('/api/jobs/' + job['job_id'])['status'], 'done')
        self.assertEqual(self.owner['daily_time'], '19:30')
        self.assertNotEqual(self.other['daily_time'], '19:30')
        restarted = ScheduleBot(self.root)
        self.assertEqual(restarted.profile_for_chat(42)['daily_time'], '19:30')
        self.assertFalse(restarted.profile_for_chat(42)['seasonal_theme'])

    def test_untrusted_mutations_rejected(self):
        bad = [{'chat_id': 43}, {'daily_time': '24:00'}, {'alert_types': [{}]},
               {'muted_subjects': {'not a subject': 'made up'}}, {'multigroup_enabled': 'true'},
               {'week_layout': 'malicious'}, {}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(APIError):
                self.dispatch('/api/settings', method='POST', body=data)
        with self.assertRaises(APIError):
            self.dispatch('/api/refresh', method='POST', body={'group_id': 99})

    def test_disabled_groups_are_not_accessible_and_addition_is_personal(self):
        self.owner['multigroup_enabled'] = True
        with patch('bot.find_group_id', return_value=123), patch('bot.fetch_schedule', return_value=self.snapshot):
            self.app.apply_miniapp_command(self.owner, 'groups', {'action': 'add', 'name': 'ВКБ52'})
        self.persist()
        self.assertIn('123', self.owner['watched_groups'])
        self.assertFalse(self.other['watched_groups'])
        day = self.raw['датаНачала'][:10]
        self.assertEqual(len(self.dispatch('/api/schedule', {'group_id': 'all', 'from': day, 'to': day})['lessons']), 2)
        job = self.dispatch('/api/settings', method='POST', body={'multigroup_enabled': False})
        self.app.process_miniapp_commands()
        self.assertEqual(self.dispatch('/api/jobs/' + job['job_id'])['status'], 'done')
        with self.assertRaises(APIError):
            self.dispatch('/api/schedule', {'group_id': '123', 'from': day, 'to': day})

    def test_teacher_is_limited_to_a_verified_teacher_of_accessible_lesson(self):
        teacher = {'id': 3704, 'user_id': 5104, 'name': 'Савельев Владимир Александрович', 'avatar_url': None}
        self.teachers.resolve.return_value = [teacher]
        self.teachers.schedule.return_value = {'teacher': teacher, 'snapshot': {'1': {**self.raw, 'group_name': 'ВКБ51, ВКБ52'}},
                                                'fetched_at': self.now.isoformat(), 'last_fetch_failed': False}
        day = self.raw['датаНачала'][:10]
        result = self.dispatch('/api/teacher', {'lesson_id': self.uid, 'from': day, 'to': day})
        self.assertEqual(result['teacher']['id'], 3704)
        self.assertEqual(result['lessons'][0]['group_name'], 'ВКБ51, ВКБ52')
        self.assertFalse(result['lessons'][0]['history_available'])
        with self.assertRaises(APIError):
            self.dispatch('/api/teacher', {'lesson_id': self.uid, 'teacher_id': '999'})
        self.teachers.resolve.return_value = [teacher, {**teacher, 'id': 11}]
        result = self.dispatch('/api/teacher', {'lesson_id': self.uid})
        self.assertIsNone(result['teacher'])
        self.assertEqual(len(result['candidates']), 2)

    def test_restart_does_not_replay_stale_json_after_journal_commit(self):
        self.persist()  # Last successful JSON save.
        updated = {'1': {**self.raw, 'аудитория': '1-500'}}
        self.app.history.observe(self.owner['group_id'], self.owner['group_name'], updated,
                                 (self.now + timedelta(seconds=1)).isoformat())
        restarted = ScheduleBot(self.root)  # Old state file, newer committed journal.
        self.assertEqual(restarted.history.lesson(self.uid)['lesson']['аудитория'], '1-500')
        self.assertEqual(len(restarted.history.history(self.uid)), 2)

    def test_new_directory_id_is_used_before_journal_hook(self):
        with patch('bot.find_group_id', return_value=888), patch('bot.fetch_schedule', return_value=self.snapshot):
            self.app.refresh_profiles([self.owner], self.now + timedelta(seconds=2))
        self.assertEqual(self.owner['group_id'], 888)
        day = self.raw['датаНачала'][:10]
        self.assertEqual(len(self.app.history.schedule(888, day, day)), 1)

    def test_telegram_group_selection_updates_existing_journal(self):
        target = 999
        self.app.history.observe(target, 'ПИ31', self.snapshot, self.now.isoformat())
        updated = {'1': {**self.raw, 'аудитория': '6-402'}}
        self.owner['awaiting'] = 'group'
        with patch('bot.find_group_id', return_value=target), patch('bot.fetch_schedule', return_value=updated), \
                patch.object(self.app, 'send_profile_message'), patch.object(self.app, 'send_profile_card'), \
                patch.object(self.app, 'show_settings'):
            self.app.apply_user_input(self.owner, 'ПИ31', self.now.date().isoformat())
        day = self.raw['датаНачала'][:10]
        rows = self.app.history.schedule(target, day, day)
        self.assertEqual(rows[0]['lesson']['аудитория'], '6-402')
        self.assertEqual(rows[0]['history_count'], 2)

    def test_miniapp_announcement_only_when_configured_and_once(self):
        from app_bot import MINIAPP_RELEASE_VERSION
        self.app.miniapp_url = 'https://rasp.example.ru'
        self.assertFalse(self.app.queue_release_notifications())
        self.app._miniapp_ready = True
        self.assertTrue(self.app.queue_release_notifications())
        self.assertFalse(self.app.queue_release_notifications())
        self.assertEqual(self.owner['pending'][0]['release_version'], MINIAPP_RELEASE_VERSION)
        text, markup = self.app.settings_view(self.owner)
        self.assertEqual(markup['inline_keyboard'][0][0]['web_app']['url'], self.app.miniapp_url)
        with patch.object(self.app, 'send_profile_message'):
            self.app.drain(self.now.date().isoformat())
        self.assertEqual(self.owner['last_release_notified'], MINIAPP_RELEASE_VERSION)
        self.assertFalse(self.app.queue_release_notifications())

    def test_queue_limit_restart_recovery_and_unknown_job(self):
        queue = self.app.miniapp_queue
        ids = [queue.submit(42, 'settings', {'daily_time': '08:30'})['job_id'] for _ in range(5)]
        with self.assertRaises(APIError):
            queue.submit(42, 'settings', {'daily_time': '08:30'})
        job = queue.next()
        self.assertEqual(job['id'], ids[0])
        queue.interrupted()
        self.assertEqual(queue.get(ids[0], 42)['status'], 'failed')
        with self.assertRaises(APIError):
            queue.get(ids[0], 99)

    def test_http_static_auth_and_no_state_exposure(self):
        static = Path(__file__).parent / 'miniapp'
        server = LimitedHTTPServer(('127.0.0.1', 0), make_handler(self.service, static))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/bootstrap'); response = conn.getresponse(); response.read()
        self.assertEqual(response.status, 401)
        conn.request('GET', '/api/bootstrap', headers={'Authorization': 'tma ' + signed()})
        response = conn.getresponse(); body = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(body['profile']['group_name'], self.owner['group_name'])
        for path in ('/data/state.json', '/.env', '/../../bot.py'):
            conn.request('GET', path); response = conn.getresponse(); response.read()
            self.assertEqual(response.status, 404)
        conn.request('GET', '/'); response = conn.getresponse(); data = response.read()
        self.assertEqual(response.status, 200)
        self.assertIn(b'telegram-web-app.js', data)
        self.assertIn("connect-src 'self'", response.getheader('Content-Security-Policy'))


if __name__ == '__main__':
    unittest.main()
