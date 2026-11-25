"""Additional visible history fields retain real journal data and isolation."""
import unittest
from datetime import timedelta

import bot
from app_bot import ScheduleBot
from miniapp_server import APIError, MiniAppService
import test_miniapp as existing_tests


class AdditionalHistoryFieldsTests(unittest.TestCase):
    setUp = existing_tests.MiniAppTests.setUp
    persist = existing_tests.MiniAppTests.persist
    dispatch = existing_tests.MiniAppTests.dispatch

    def schedule(self):
        day = self.raw['датаНачала'][:10]
        return self.dispatch('/api/schedule', {'from': day, 'to': day})

    def test_extra_fields_are_visible_before_after_and_survive_rechecks_restart(self):
        initial = self.schedule()
        for key, value in [('replacement', False), ('topic', ''), ('link', '')]:
            self.assertEqual(initial['lessons'][0][key], value)
        self.assertIs(initial['lessons'][0]['replacement'], False)

        # Preserve text verbatim; frontend decides its safe text presentation.
        topic = '<img src=x onerror=alert(1)> Тема занятия'
        link = 'javascript:alert(1)'
        updated = {**self.raw, 'замена': True, 'тема': topic, 'ссылка': link}
        self.app.process_profile_snapshot(self.owner, {'1': updated}, self.now + timedelta(seconds=1))
        self.persist()
        detail = self.dispatch('/api/lessons/' + self.uid)
        latest = detail['history'][0]
        self.assertEqual(latest['kind'], 'changed')
        self.assertEqual(set(latest['types']), {'replacement', 'other'})
        self.assertEqual((latest['before']['replacement'], latest['before']['topic'], latest['before']['link']),
                         (False, '', ''))
        expected = (True, topic, link)
        for value in (latest['after'], detail['lesson'], self.schedule()['lessons'][0]):
            self.assertEqual((value['replacement'], value['topic'], value['link']), expected)
            self.assertIs(value['replacement'], True)
        changed = self.schedule()
        self.assertNotEqual(initial['revision'], changed['revision'])
        before = self.dispatch('/api/changes')
        self.assertEqual(before['total_events'], 1)
        self.assertEqual(before['events'][0]['after']['topic'], topic)

        # The same visible content with another source code is still one event.
        repeated = {'9': {**updated, 'код': '9'}}
        self.app.process_profile_snapshot(self.owner, repeated, self.now + timedelta(seconds=2))
        self.persist()
        self.assertEqual(changed['revision'], self.schedule()['revision'])
        self.assertEqual(before['revision'], self.dispatch('/api/changes')['revision'])

        restarted = ScheduleBot(self.root)
        self.service = MiniAppService(restarted.state_file, restarted.history, restarted.miniapp_queue,
                                      existing_tests.TOKEN, self.teachers)
        again = self.dispatch('/api/lessons/' + self.uid)
        self.assertEqual(len(again['history']), 2)  # one change and the baseline
        self.assertEqual((again['lesson']['replacement'], again['lesson']['topic'], again['lesson']['link']),
                         expected)
        self.assertEqual(again['history'][0]['before']['topic'], '')
        self.assertEqual(before['revision'], self.dispatch('/api/changes')['revision'])
        restarted.process_profile_snapshot(restarted.profile_for_chat(42), repeated,
                                            self.now + timedelta(seconds=3))
        bot.save_state(restarted.state_file, restarted.state)
        self.assertEqual(self.dispatch('/api/changes')['total_events'], 1)

    def test_new_history_fields_remain_scoped_to_the_users_groups(self):
        private_topic = 'Только для ПИ31'
        other_raw = {**self.raw, 'код': '2', 'замена': True,
                     'тема': private_topic, 'ссылка': 'https://example.invalid/private'}
        self.app.process_profile_snapshot(self.other, {'2': other_raw}, self.now + timedelta(seconds=1))
        self.persist()
        other_uid = self.app.history.schedule(self.other['group_id'], self.raw['датаНачала'][:10],
                                              self.raw['датаНачала'][:10])[0]['id']
        self.assertEqual(self.dispatch('/api/changes')['events'], [])
        self.assertEqual(self.dispatch('/api/changes', user=43)['events'][0]['after']['topic'], private_topic)
        for uid, user in ((other_uid, 42), (self.uid, 43)):
            with self.subTest(uid=uid, user=user), self.assertRaises(APIError) as error:
                self.dispatch('/api/lessons/' + uid, user=user)
            self.assertEqual(error.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
