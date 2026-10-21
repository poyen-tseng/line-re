from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import group_actions as ga

ROOM = 'c' + '3' * 32
OTHER_ROOM = 'c' + '4' * 32


class ActionTests(unittest.TestCase):
    def test_invalid_json_is_not_sent_as_text(self):
        for raw in ('{"actions":', 'Here is JSON: {"actions":', '{"actions":null}', '{"unexpected":123}',
                    '```json\n{"actions":', '{"actions":[{"type":"shell","cmd":"no"}]}'):
            self.assertEqual(ga.parse_actions(raw, 5)[0], [], raw)
        self.assertEqual(ga.parse_actions('你好', 5)[0], [{'type': 'send_text', 'text': '你好'}])

    def test_action_normalization_and_cap(self):
        raw = json.dumps({'actions': [{'type': 'send_text', 'text': 'one\ntwo'},
                                      {'type': 'send_text', 'text': 'verification code 123'},
                                      {'type': 'send_text', 'text': 'three'}]})
        actions, source, truncated = ga.parse_actions(raw, 1)
        self.assertEqual(actions, [{'type': 'send_text', 'text': 'one two'}])
        self.assertEqual(source, 'json')
        self.assertTrue(truncated)

    def test_time_range_and_timezone(self):
        now = ga.now_taipei()
        for at in (now - timedelta(seconds=1), now + timedelta(days=31)):
            self.assertIsNotNone(ga.parse_at(at.isoformat(), now)[1])
        self.assertEqual(ga.parse_at((now + timedelta(minutes=1)).isoformat(), now)[1], None)
        self.assertIsNotNone(ga.parse_at('9999-12-31T23:59:59-23:59', now)[1])

    def test_file_search_and_resolve_reject_symlinks_and_ambiguous_names(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'a').mkdir()
            Path(root, 'b').mkdir()
            Path(root, 'a', 'video.mp4').write_bytes(b'a')
            Path(root, 'b', 'video.mp4').write_bytes(b'b')
            Path(root, 'credentials.mp4').write_bytes(b'private')
            Path(root, 'link.mp4').symlink_to(Path(root, 'a', 'video.mp4'))
            hits = ga.find_files([root], '*.mp4')
            self.assertEqual(len(hits), 2)
            self.assertIsNone(ga.resolve_send_file('video.mp4', [root], 200)[0])
            self.assertIsNone(ga.resolve_send_file(str(Path(root, 'link.mp4')), [root], 200)[0])
            self.assertIsNone(ga.resolve_send_file(str(Path(root, 'a', 'video.mp4')), [root], float('nan'))[0])
            self.assertEqual(ga.find_files([root], '*', limit=0), [])
            self.assertTrue(ga.under_roots(root, ['/']))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name, 'schedules.json')
        self.store = ga.ScheduleStore(self.path)

    def add(self, room=ROOM, offset=1):
        item, err = self.store.add(room, 'group', ga.now_taipei() + timedelta(seconds=offset), 'hello', 'command-id')
        self.assertIsNone(err)
        return item

    def test_cancel_and_list_are_room_scoped(self):
        first = self.add()
        second = self.add(OTHER_ROOM)
        self.assertIsNone(self.store.cancel(first['id'], OTHER_ROOM))
        self.assertEqual(len(self.store.list_room(ROOM)), 1)
        self.store.cancel(first['id'], ROOM)
        self.assertEqual(self.store.list_room(ROOM), [])
        self.assertEqual(self.store.list_room(OTHER_ROOM)[0]['id'], second['id'])

    def test_corrupt_store_is_preserved(self):
        for content in ('broken json', '{"items":{}}', '{"items":[{"id":"abc","status":"pending","at":9}]}'):
            self.path.write_text(content)
            with self.assertRaises(ga.ScheduleError):
                self.add()
            self.assertEqual(self.path.read_text(), content)

    def test_pending_cap(self):
        for _ in range(ga.PENDING_CAP):
            self.add()
        item, err = self.store.add(ROOM, '', ga.now_taipei(), 'hello', '')
        self.assertIsNone(item)
        self.assertIsNotNone(err)

    def test_parallel_add_does_not_lose_updates(self):
        def add(_):
            return ga.ScheduleStore(self.path).add(ROOM, '', ga.now_taipei(), 'hello', '')[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(add, range(12)))
        self.assertEqual(len(self.store.load()), len(rows))

    def test_only_one_claim_and_crash_does_not_requeue(self):
        item = self.add(offset=-1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: ga.ScheduleStore(self.path).claim(item['id']), range(2)))
        self.assertEqual(sum(row is not None for row in claims), 1)
        self.assertEqual(self.store.due(), [])
        self.assertEqual(self.store.load()[0]['status'], 'sending')


if __name__ == '__main__':
    unittest.main()
