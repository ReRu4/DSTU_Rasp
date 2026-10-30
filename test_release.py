import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app_bot import ScheduleBot, RELEASE_VERSION
from bot import MOSCOW, save_state


class ReleaseTests(unittest.TestCase):
    def test_existing_user_receives_release_once_across_restart(self):
        with TemporaryDirectory() as temp, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test'}, clear=True):
            root = Path(temp)
            app = ScheduleBot(root)
            profile = app.new_profile('telegram', 42)
            profile.pop('last_release_notified')  # Existing state written before this release.
            app.state['profiles'] = {'telegram:42': profile}
            save_state(app.state_file, app.state)
            self.assertTrue(app.queue_release_notifications())
            self.assertFalse(app.queue_release_notifications())
            self.assertEqual(len(profile['pending']), 1)
            app = ScheduleBot(root)
            profile = app.profile_for_chat(42)
            self.assertFalse(app.queue_release_notifications())
            with patch.object(app, 'send_profile_message') as send:
                app.drain(datetime.now(MOSCOW).date().isoformat())
            self.assertEqual(send.call_count, 1)
            self.assertEqual(profile['last_release_notified'], RELEASE_VERSION)
            self.assertEqual(profile['pending'], [])
            restarted = ScheduleBot(root)
            self.assertFalse(restarted.queue_release_notifications())

    def test_failed_announcement_remains_queued_until_success(self):
        with TemporaryDirectory() as temp, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test'}, clear=True):
            app = ScheduleBot(Path(temp))
            profile = app.new_profile('telegram', 42)
            profile.pop('last_release_notified')
            app.state['profiles'] = {'telegram:42': profile}
            app.queue_release_notifications()
            with patch.object(app, 'send_profile_message', side_effect=RuntimeError('offline')):
                app.drain('2026-10-30')
            restored = ScheduleBot(Path(temp))
            profile = restored.profile_for_chat(42)
            self.assertNotEqual(profile.get('last_release_notified'), RELEASE_VERSION)
            self.assertEqual(len(profile['pending']), 1)
            with patch.object(restored, 'send_profile_message') as send:
                restored.drain('2026-10-30')
                restored.drain('2026-10-30')
            self.assertEqual(send.call_count, 1)
            self.assertEqual(profile['last_release_notified'], RELEASE_VERSION)

    def test_new_users_and_incomplete_onboarding_do_not_get_release(self):
        with TemporaryDirectory() as temp, patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test'}, clear=True):
            app = ScheduleBot(Path(temp))
            new = app.new_profile('telegram', 42)
            pending = app.new_profile('telegram', 43)
            pending.pop('last_release_notified')
            pending['onboarding'] = True
            app.state['profiles'] = {'telegram:42': new, 'telegram:43': pending}
            self.assertFalse(app.queue_release_notifications())
            self.assertEqual(new['pending'], [])
            self.assertEqual(pending['pending'], [])


if __name__ == '__main__':
    unittest.main()
