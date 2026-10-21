from contextlib import nullcontext
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ai_backend
import group_actions as ga
import line_cli as cli
import line_common as lc
import send_message as sender
import watch_messages as watch
from test_memory import message, SELF, OTHER, ROOM, CREATED


class RoutingTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.args = cli.build_parser().parse_args(['ai', '--group-bot', '--cooldown', '0', '--min-interval', '0'])
        self.args.self_mid = SELF
        self.args.schedules_file = os.path.join(root.name, 'schedules.json')
        self.args.state_file = os.path.join(root.name, 'handled.json')
        self.gx = SimpleNamespace(stop=False)
        self.st = SimpleNamespace(last_send_at=0, last_group={}, replies=0)
        for name in ('run', 'run_file'):
            mock = patch.object(sender, name, side_effect=AssertionError('unexpected send path'))
            mock.start()
            self.addCleanup(mock.stop)

    def execute(self, actions):
        return cli._execute_actions(actions, 'command-id', 'group', ROOM,
                                    self.args, self.gx, self.st)

    def test_dm_reply_targets_sender_and_groups_target_room(self):
        self.assertEqual(cli._reply_room(message(to=SELF), SELF), OTHER)
        self.assertEqual(cli._reply_room(message(), SELF), ROOM)

    def test_group_frequency_cannot_identify_self(self):
        self.assertIsNone(cli._confident_self_mid([message(), message(**{'from': SELF})]))

    def test_utf16_mention_offsets_and_malformed_metadata(self):
        text = '🙂 @bot hello'
        meta = json.dumps({'MENTION': json.dumps({'MENTIONEES': [{'M': SELF, 'S': '3', 'E': '7'}]})})
        row = message(text=text, contentMeta=meta)
        self.assertEqual(cli._command_text(row, SELF, ''), '🙂 hello')
        row['contentMeta'] = json.dumps({'MENTION': {'MENTIONEES': 2}})
        self.assertIsNone(cli._mention_path(row, SELF, '@bot'))

    def test_unconfirmed_send_is_handled_and_stops_remaining_actions(self):
        with patch.object(cli, '_send_verified_text', return_value=(False, 0)) as send:
            ok, sent = self.execute([{'type': 'send_text', 'text': 'hello'}, {'type': 'send_text', 'text': 'second'}])
        self.assertTrue(ok)
        self.assertTrue(sent)
        send.assert_called_once()

    def test_safe_pre_send_failure_can_retry_but_stops_actions(self):
        with patch.object(cli, '_send_verified_text', return_value=(False, 4)) as send:
            ok, sent = self.execute([{'type': 'send_text', 'text': 'hello'}, {'type': 'list_schedules'}])
        self.assertFalse(ok)
        self.assertFalse(sent)
        send.assert_called_once()

    def test_partial_success_does_not_replay_first_action(self):
        with patch.object(cli, '_wait_gap', return_value=True), patch.object(
                cli, '_send_verified_text', side_effect=[(True, 0), (False, 4)]) as send:
            _, sent = self.execute([{'type': 'send_text', 'text': 'hello'}, {'type': 'send_text', 'text': 'second'}])
        self.assertTrue(sent)
        self.assertEqual(send.call_count, 2)

    def test_schedule_claim_is_durable_before_send(self):
        store = ga.ScheduleStore(self.args.schedules_file)
        item, _ = store.add(ROOM, 'group', ga.now_taipei() - timedelta(seconds=1), 'hello', 'command-id')
        def send(*args):
            self.assertEqual(store.load()[0]['status'], 'sending')
            return False, 0
        with patch.object(cli, '_send_verified_text', side_effect=send) as mocked:
            cli._dispatch_due_schedules(self.args, self.gx, self.st)
            cli._dispatch_due_schedules(self.args, self.gx, self.st)
        self.assertEqual(store.load()[0]['status'], 'unconfirmed')
        mocked.assert_called_once()

    def test_schedule_retries_only_definite_pre_send_failure(self):
        store = ga.ScheduleStore(self.args.schedules_file)
        store.add(ROOM, '', ga.now_taipei() - timedelta(seconds=1), 'hello', '')
        with patch.object(cli, '_send_verified_text', return_value=(False, 4)) as mocked:
            for _ in range(4):
                cli._dispatch_due_schedules(self.args, self.gx, self.st)
        self.assertEqual(store.load()[0]['status'], 'failed')
        self.assertEqual(mocked.call_count, 3)

    def test_dry_run_does_not_send_or_write_schedules(self):
        self.args.dry_run = True
        with patch.object(cli, '_send_verified_text', side_effect=AssertionError('dry-run send')):
            self.execute([{'type': 'send_text', 'text': 'hello'},
                          {'type': 'schedule_text', 'text': 'later', 'at': ga.iso8(ga.now_taipei() + timedelta(minutes=2))}])
        self.assertFalse(Path(self.args.schedules_file).exists())

    def test_failed_state_save_stops_before_actions(self):
        with patch.object(cli, '_call', return_value='{"actions":[{"type":"send_text","text":"hello"}]}'), patch.object(
                watch, 'save_state', return_value=False), patch.object(cli, '_execute_actions') as execute:
            cli._group_bot_handle({'123456789': message(text='@bot hello')}, {}, set(),
                                  SimpleNamespace(**dict(vars(self.args), mention='@bot', catchup_minutes=0)), {}, self.gx, self.st)
        self.assertTrue(self.gx.stop)
        execute.assert_not_called()

    def test_baseline_uses_one_snapshot_for_catchup(self):
        now = CREATED / 1000
        old = message(id='old-id', createdTime=CREATED - 3600000)
        rows = {'old-id': old, '123456789': message(), 'already': message(id='already')}
        seen = {'already'}
        self.args.catchup_minutes = 15
        with patch.object(watch, 'do_scan', return_value=(rows, {}, 1, 1)) as scan, patch.object(cli.time, 'time', return_value=now):
            cli._group_bot_baseline(123, SimpleNamespace(), seen, self.args)
        self.assertEqual(seen, {'old-id', 'already'})
        scan.assert_called_once()

    def test_fresh_line_late_loaded_recent_command_is_answered_old_is_history(self):
        # Regression 2026-10-21: after a LINE (re)start (pid None -> X) the
        # baseline sees an empty cache; messages then load lazily.  Recent
        # unhandled ones must be answered, older history must not.
        now = CREATED / 1000 + 60
        self.args.catchup_minutes = 15
        seen = set()
        with patch.object(watch, 'do_scan', return_value=({}, {}, 1, 0)), patch.object(cli.time, 'time', return_value=now):
            cli._group_bot_baseline(123, SimpleNamespace(), seen, self.args)
        rows = {'123456789': message(text='@bot hello'),
                'old-id': message(id='old-id', text='@bot old', createdTime=CREATED - 3600000)}
        args = SimpleNamespace(**dict(vars(self.args), mention='@bot'))
        with patch.object(cli, '_call', return_value='{"actions":[{"type":"send_text","text":"hi"}]}') as call, patch.object(
                cli, '_execute_actions', return_value=(True, True)) as execute, patch.object(
                watch, 'save_state', return_value=True), patch.object(cli.time, 'time', return_value=now):
            cli._group_bot_handle(rows, {}, seen, args, {}, self.gx, self.st)
        call.assert_called_once()
        execute.assert_called_once()
        self.assertEqual(seen, {'123456789', 'old-id'})

    def test_command_kicks_watchdog_before_grok(self):
        order = []
        st = SimpleNamespace(**dict(vars(self.st), on_command=lambda: order.append('kick')))
        args = SimpleNamespace(**dict(vars(self.args), mention='@bot', catchup_minutes=0))
        with patch.object(cli, '_call', side_effect=lambda p: order.append('grok') or None):
            cli._group_bot_handle({'123456789': message(text='@bot hello')}, {}, set(), args, {}, self.gx, st)
        self.assertEqual(order, ['kick', 'grok'])

    def test_stamped_stream_prefixes_each_line(self):
        import io
        buf = io.StringIO()
        out = cli._Stamped(buf)
        out.write("a\nb")
        out.write("c\n")
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d\.\d{3} a$")
        self.assertRegex(lines[1], r"^\d\d:\d\d:\d\d\.\d{3} bc$")


class SendTests(unittest.TestCase):
    def setUp(self):
        self.ctx = dict(display=':test', wid='fake', pid=123, target=ROOM, msg_input=(1, 1))
        self.args = SimpleNamespace(text='hello', dry_run=False)
        self.mocks = {}
        for name in ('_prepare_chat', '_clear_composer', 'set_clipboard', 'paste', 'key', '_ui_lock'):
            mock = patch.object(sender, name)
            self.mocks[name] = mock.start()
            self.addCleanup(mock.stop)
        self.mocks['_prepare_chat'].return_value = (self.ctx, None)
        self.mocks['_ui_lock'].side_effect = nullcontext
        sleep = patch.object(sender.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)
        unexpected = patch.object(sender.subprocess, 'run', side_effect=AssertionError('unmocked X11'))
        unexpected.start()
        self.addCleanup(unexpected.stop)

    def test_wrong_room_aborts_before_paste(self):
        with patch.object(lc, 'current_open_chat_mid', return_value=OTHER):
            self.assertEqual(sender.run(self.args), 4)
        self.mocks['paste'].assert_not_called()
        self.mocks['key'].assert_not_called()

    def test_room_switch_after_paste_aborts_enter(self):
        with patch.object(lc, 'current_open_chat_mid', side_effect=[ROOM, ROOM, OTHER]):
            self.assertEqual(sender.run(self.args), 4)
        self.mocks['paste'].assert_called_once()
        self.mocks['key'].assert_not_called()

    def test_clipboard_failure_does_not_paste_stale_selection(self):
        self.mocks['set_clipboard'].side_effect = OSError('clipboard failed')
        with patch.object(lc, 'current_open_chat_mid', return_value=ROOM):
            self.assertEqual(sender.run(self.args), sender.SEND_UNCERTAIN)
        self.mocks['paste'].assert_not_called()
        self.mocks['key'].assert_not_called()

    def test_success_is_one_mocked_enter(self):
        with patch.object(lc, 'current_open_chat_mid', return_value=ROOM):
            self.assertEqual(sender.run(self.args), 0)
        self.mocks['key'].assert_called_once_with(':test', 'Return')


class WatchAndBackendTests(unittest.TestCase):
    def test_backfill_does_not_reemit_persisted_ids(self):
        rows = {'123456789': message(), 'new-id': message(id='new-id')}
        args = SimpleNamespace(backfill=2, quiet=True)
        with patch.object(watch, 'do_scan', return_value=(rows, {}, 1, 1)), patch.object(watch, 'emit_message') as emit:
            seen = {'123456789'}
            watch.establish_baseline(123, args, seen)
        self.assertEqual(emit.call_count, 1)
        self.assertEqual(emit.call_args.args[0]['id'], 'new-id')

    def test_state_write_uses_nested_parent_and_no_shared_temp(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'nested', 'state')
            self.assertTrue(watch.save_state(path, {'b', 'a'}))
            self.assertEqual(watch.load_state(path), {'a', 'b'})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_corrupt_handled_ids_do_not_become_empty_history(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'state')
            path.write_bytes(b'\xff')
            with self.assertRaises(watch.StateError):
                watch.load_state(path)

    def test_failed_baseline_is_retried_without_emitting_history(self):
        args = SimpleNamespace(backfill=0, quiet=True)
        seen = set()
        with patch.object(lc, 'scan_memory', side_effect=[PermissionError('not ready'), ({'123456789': message()}, {}, 1, 1)]), patch.object(watch, 'emit_message') as emit:
            watch.establish_baseline(123, args, seen)
            self.assertTrue(args._baseline_pending)
            watch.establish_baseline(123, args, seen)
            self.assertFalse(args._baseline_pending)
        self.assertIn('123456789', seen)
        emit.assert_not_called()

    def test_exec_nonzero_is_reported(self):
        with patch.object(watch.subprocess, 'run', return_value=SimpleNamespace(returncode=7)), patch('sys.stderr') as err:
            watch.run_exec('fake', message(), '', '', '', 0, 1)
        self.assertTrue(any('exited 7' in str(c) for c in err.write.call_args_list))

    def test_backend_errors_are_wrapped_and_tempdir_removed(self):
        for error in (PermissionError('denied'), ValueError('embedded null byte'), subprocess.TimeoutExpired('fake', 1)):
            with patch.object(ai_backend.subprocess, 'run', side_effect=error) as run:
                with self.assertRaises(ai_backend.GrokError):
                    ai_backend.ask_grok('prompt', timeout=1)
                self.assertFalse(Path(run.call_args.kwargs['cwd']).exists())

    def test_backend_tempdir_failure_is_wrapped(self):
        with patch.object(ai_backend.tempfile, 'mkdtemp', side_effect=OSError('disk full')):
            with self.assertRaises(ai_backend.GrokError):
                ai_backend.ask_grok('prompt')

    def test_backend_model_and_nonzero_result(self):
        with patch.object(ai_backend.subprocess, 'run', return_value=SimpleNamespace(returncode=3, stdout='', stderr='failure')) as run:
            with self.assertRaises(ai_backend.GrokError):
                ai_backend.ask_grok('prompt')
        self.assertIn(ai_backend.MODEL, run.call_args.args[0])

    def test_cli_rejects_nonfinite_interval_before_work(self):
        with self.assertRaises(SystemExit):
            cli.main(['ai', '--group-bot', '--interval', 'nan'])


if __name__ == '__main__':
    unittest.main()
