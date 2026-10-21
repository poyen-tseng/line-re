from contextlib import nullcontext, contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ai_backend
import line_cli as cli
import reaction_memory as rm
import reactions as react
import watch_messages as watch
from test_memory import message, SELF, OTHER, ROOM


def tagged(**changes):
    meta = json.dumps({'MENTION': json.dumps({'MENTIONEES': [{'M': SELF, 'S': '0', 'E': '4'}]})})
    return message(text='@bot hello', contentMeta=meta, **changes)


def update(mid='123456789', room=ROOM, reactor=SELF, kind=2, cancel=False):
    status = json.dumps({'reactionList': [dict(reactor=reactor, type=1,
                                            PredefinedType=kind, cancel=cancel)]})
    return (f"UPDATE _message SET _reactionStatus='{status}' WHERE _id='{mid}' "
            f"AND _chatId='{room}'").encode()


class ReactionFilterTests(unittest.TestCase):
    def test_explicit_display_name_fallback_matches_reply_detection(self):
        row = message(text='@mybot hello')
        self.assertEqual(cli._mention_path(row, SELF, react.DEFAULT_MENTION), 'text')
        self.assertTrue(react.is_tagged(row, SELF))
        self.assertFalse(react.is_tagged(message(text='mybot hello'), SELF))
        self.assertFalse(react.is_tagged(message(text='@someone hello'), SELF))
        row['contentMeta'] = json.dumps({'MENTION': {'MENTIONEES': [{'M': OTHER}]}})
        self.assertFalse(react.is_tagged(row, SELF))

    def test_only_real_metadata_mentions_in_incoming_groups(self):
        self.assertTrue(react.is_tagged(tagged(), SELF))
        for row in (message(text='@bot hello'), tagged(**{'from': SELF}),
                    tagged(to=SELF), tagged(contentType=18), tagged(id='bad-id'),
                    message(contentMeta=json.dumps({'MENTION': {'MENTIONEES': [{'M': OTHER}]}})),
                    message(contentMeta='{"MENTION":{"MENTIONEES":2}}')):
            with self.subTest(row=row):
                self.assertFalse(react.is_tagged(row, SELF))

    def test_cli_and_reaction_share_the_mention_parser(self):
        self.assertIs(cli._mention_info, react.mention_info)
        self.assertIs(cli._mention_path, react.mention_path)

    def test_flag_and_environment_disable_default_on(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cli.build_parser().parse_args(['ai', '--group-bot']).no_react)
        self.assertTrue(cli.build_parser().parse_args(['ai', '--group-bot', '--no-react']).no_react)
        with patch.dict(os.environ, {'LINE_BOT_NO_REACT': '1'}):
            self.assertTrue(cli.build_parser().parse_args(['ai', '--group-bot']).no_react)


class EvidenceTests(unittest.TestCase):
    def test_binary_noise_is_not_scanned_with_the_log_regex(self):
        blob = b'L1234567890 invalid timestamp ' * 100000
        with patch.object(rm, '_CLICK_LOG') as click, patch.object(rm, '_DONE_LOG') as done:
            self.assertEqual(rm.completion_logs(blob), ([], []))
        click.search.assert_not_called()
        done.search.assert_not_called()

    def test_completion_success_is_bound_to_exact_click_and_reaction_timestamp(self):
        stamp = 1792561173946
        local = react.time.localtime(stamp / 1000)
        base = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
        clicks = [((local.tm_mday, base + .5), '123456789', 1, False)]
        done = [(local.tm_mday, base + 1.0)]
        records = [dict(updatedAt=stamp)]
        self.assertTrue(rm.correlated_completion(records, clicks, done, '123456789'))
        self.assertFalse(rm.correlated_completion(records, clicks, done, '999999999'))
        self.assertFalse(rm.correlated_completion(records, clicks,
                                                [(local.tm_mday, base + 20)], '123456789'))
        interrupted = clicks + [((local.tm_mday, base + .8), '999999999', 1, False)]
        self.assertFalse(rm.correlated_completion(records, interrupted, done, '123456789'))

    def test_completion_log_parser_ignores_unstamped_generic_success(self):
        blob = (b'#L0015190, 21, 13:39:33.500000 M_00000336, lv_3, append , '
                b'[ChatMessageView][Reaction] reaction item clicked, icon type: 1 , '
                b'targetId: "123456789" , cancel: false\n\0'
                b'#L0015191, 21, 13:39:34.000000 M_00000336, lv_3, append , '
                b'[ChatMessageView][Reaction] requestReactMessage done: SUCCESS , dump: ****\n\0')
        clicks, done = rm.completion_logs(blob)
        self.assertEqual(len(clicks), 1)
        self.assertEqual(len(done), 1)
        self.assertEqual(rm.completion_logs(b'requestReactMessage done: SUCCESS'), ([], []))

    def test_id_only_updates_require_an_independent_complete_room_record(self):
        blob = update().split(b' AND _chatId=')[0] + b'\0'
        blob += b'response react message: success=1, msgId=123456789, type=****\0'
        for room, expected in ((ROOM, True), ('c' + '4' * 32, False)):
            identity = json.dumps(dict(message(to=room), contentMetadata={})).encode()
            with patch('builtins.open', return_value=io.BytesIO()), patch.object(
                    rm, 'chunks', return_value=iter([(0x10000, blob), (0x20000, identity)])):
                result = rm.reaction_evidence(123, '123456789', ROOM, SELF)
            self.assertEqual(result['confirmed'], expected)

    def test_complete_statement_binds_message_room_self_and_like(self):
        blob = update() + b'\0response react message: success=1, msgId=123456789, type=****'
        records, ack = rm.evidence_from_blob(blob, '123456789', ROOM, SELF)
        self.assertEqual(len(records), 1)
        self.assertTrue(ack)
        self.assertEqual(records[0]['predefined'], 2)

    def test_adjacent_messages_wrong_room_user_type_and_cancel_are_rejected(self):
        for blob in (update(mid='987654321'), update(room='c' + '4' * 32),
                     update(reactor=OTHER), update(kind=3), update(cancel=True),
                     update()[:-8], b'{"reactionList":[]}' + b'123456789' + SELF.encode()):
            with self.subTest(blob=blob):
                self.assertEqual(rm.evidence_from_blob(blob, '123456789', ROOM, SELF)[0], [])

    def test_ack_cannot_match_message_id_prefix(self):
        self.assertFalse(rm.evidence_from_blob(
            b'response react message: success=1, msgId=1234567890,',
            '123456789', ROOM, SELF)[1])


class WorkerTests(unittest.TestCase):
    def test_fresh_mention_runs_while_an_older_retry_waits_for_the_batch(self):
        first, fresh = threading.Event(), threading.Event()
        calls = []
        def backend(row, *a):
            calls.append(row['id'])
            if len(calls) == 1:
                first.set()
                return react.Result()
            if row['id'] == '987654321':
                fresh.set()
            return react.Result(True, True)
        worker = self.worker(backend)
        with worker.command_batch():
            worker.submit(tagged())
            self.assertTrue(first.wait(1))
            worker.reply_ready('123456789')
            worker.reply_finished('123456789')
            worker.submit(tagged(id='987654321'))
            self.assertTrue(fresh.wait(1))
            self.assertEqual(calls, ['123456789', '987654321'])
        worker.jobs.join()
        self.assertEqual(calls, ['123456789', '987654321', '123456789'])

    def test_tag_rejected_by_commander_filter_is_not_queued(self):
        calls = []
        worker = self.worker(lambda *a: calls.append(a) or react.Result(True, True))
        args = cli.build_parser().parse_args(['ai', '--group-bot', '--commander', 'u' + '4' * 32])
        args.self_mid = SELF
        args.catchup_minutes = 0
        st = SimpleNamespace(reactions=worker, replies=0, last_group={}, last_send_at=0)
        with patch.object(cli, '_call') as grok:
            cli._group_bot_handle({'123456789': tagged()}, {}, self.state, args, {},
                                  SimpleNamespace(stop=False), st)
        worker.jobs.join()
        self.assertEqual(calls, [])
        grok.assert_not_called()

    def test_retry_waits_until_all_commands_in_the_batch_finish(self):
        first, second = threading.Event(), threading.Event()
        calls = []
        def backend(*a):
            calls.append(a)
            (first if len(calls) == 1 else second).set()
            return react.Result(ok=len(calls)>1, committed=len(calls)>1)
        worker = self.worker(backend)
        with worker.command_batch():
            worker.submit(tagged())
            self.assertTrue(first.wait(1))
            worker.reply_ready('123456789')
            worker.reply_finished('123456789')
            self.assertFalse(second.wait(.7))
        self.assertTrue(second.wait(2))
        worker.jobs.join()
        self.assertEqual(len(calls), 2)

    def test_persisted_safe_failure_resumes_only_remaining_attempt_budget(self):
        self.state.reactions['123456789'] = dict(status='failed', attempts=2, room=ROOM)
        calls = []
        worker = self.worker(lambda *a: calls.append(a) or react.Result())
        worker.submit(tagged())
        worker.jobs.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state.reactions['123456789']['attempts'], 3)
        self.assertFalse(worker.submit(tagged()))

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = str(Path(self.root.name, 'state.json'))
        self.state = watch.HandledState({'history'})
        self.logs = []

    def worker(self, backend):
        worker = react.ReactionWorker(self.state, self.path, SELF, backend=backend, log=self.logs.append)
        self.addCleanup(worker.close)
        return worker

    def test_durable_claim_before_backend_and_once_per_id(self):
        calls = []
        def backend(*args):
            state = watch.load_state(self.path)
            self.assertEqual(state.reactions['123456789']['status'], 'pending')
            calls.append(args)
            return react.Result(True, True, 'verified')
        worker = self.worker(backend)
        self.assertTrue(worker.submit(tagged()))
        self.assertFalse(worker.submit(tagged()))
        worker.jobs.join()
        loaded = watch.load_state(self.path)
        self.assertEqual(loaded, {'history'})
        self.assertEqual(loaded.reactions['123456789']['status'], 'ok')
        worker.close()
        restarted = react.ReactionWorker(loaded, self.path, SELF, backend=backend, log=self.logs.append)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.submit(tagged()))
        self.assertEqual(len(calls), 1)

    def test_failures_have_three_safe_attempts_and_do_not_retry_on_restart(self):
        calls = []
        worker = self.worker(lambda *a: calls.append(a) or react.Result(detail='not clicked'))
        worker.submit(tagged())
        worker.jobs.join()
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.state.reactions['123456789']['attempts'], 3)
        self.assertEqual(self.state.reactions['123456789']['status'], 'failed')
        self.assertFalse(worker.submit(tagged()))
        self.assertEqual(sum('[react] fail' in line for line in self.logs), 3)

    def test_uncertain_click_and_unexpected_exception_never_replay(self):
        for backend in (lambda *a: react.Result(committed=True, detail='timeout'),
                        lambda *a: (_ for _ in ()).throw(RuntimeError('unexpected'))):
            self.state.reactions.clear()
            calls = []
            def wrapped(*a):
                calls.append(a)
                return backend(*a)
            worker = self.worker(wrapped)
            worker.submit(tagged())
            worker.jobs.join()
            worker.close()
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.state.reactions['123456789']['status'], 'uncertain')

    def test_interrupted_pending_claim_is_never_clicked_on_restart(self):
        self.state.reactions['123456789'] = dict(status='pending', attempts=1, room=ROOM)
        worker = self.worker(lambda *a: self.fail('replayed interrupted click'))
        self.assertEqual(self.state.reactions['123456789']['status'], 'uncertain')
        self.assertFalse(worker.submit(tagged()))

    def test_state_failure_disables_actions_without_raising_into_reply(self):
        calls = []
        worker = self.worker(lambda *a: calls.append(a))
        with patch.object(watch, 'save_state', return_value=False):
            worker.submit(tagged())
            worker.jobs.join()
        self.assertEqual(calls, [])
        self.assertTrue(worker.disabled)

    def test_handled_save_preserves_worker_reaction_records(self):
        worker = self.worker(lambda *a: react.Result(True, True))
        worker.submit(tagged())
        worker.jobs.join()
        self.state.add('new-handled')
        self.assertTrue(watch.save_state(self.path, self.state))
        loaded = watch.load_state(self.path)
        self.assertIn('new-handled', loaded)
        self.assertIn('123456789', loaded.reactions)

    def test_legacy_migration_and_corrupt_v2_state(self):
        Path(self.path).write_text('a\nb\n')
        loaded = watch.load_state(self.path)
        self.assertEqual(loaded, {'a', 'b'})
        loaded.structured = True
        self.assertTrue(watch.save_state(self.path, loaded))
        self.assertEqual(json.loads(Path(self.path).read_text())['version'], 2)
        for raw in ('{"version":2}', '{bad', '{"version":2,"handled":[],"reactions":{"x":{}}}'):
            Path(self.path).write_text(raw)
            with self.assertRaises(watch.StateError):
                watch.load_state(self.path)

    def test_submit_returns_while_reaction_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        def backend(*a):
            entered.set()
            release.wait(2)
            return react.Result(True, True)
        worker = self.worker(backend)
        try:
            self.assertTrue(worker.submit(tagged()))
            self.assertTrue(entered.wait(1))
            self.assertFalse(release.is_set())
            # Main-thread state flush still succeeds while the backend waits.
            self.assertTrue(watch.save_state(self.path, self.state))
        finally:
            release.set()
        worker.jobs.join()


class OrderingTests(unittest.TestCase):
    def test_model_process_started_before_callback_and_timeout_reaps(self):
        events = []
        proc = SimpleNamespace(returncode=0,
                               communicate=lambda **kw: events.append('communicate') or ('reply', ''))
        class Process:
            def __enter__(self): return proc
            def __exit__(self, *a): pass
        def popen(*a, **kw):
            events.append('process-started')
            return Process()
        with patch.object(ai_backend.subprocess, 'Popen', side_effect=popen):
            self.assertEqual(ai_backend.ask_grok('prompt', on_started=lambda: events.append('queued')), 'reply')
        self.assertEqual(events, ['process-started', 'queued', 'communicate'])
        events.clear()
        proc.communicate = lambda **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired('fake', 1)) if kw else ('', '')
        proc.kill = lambda: events.append('killed')
        with patch.object(ai_backend.subprocess, 'Popen', side_effect=popen):
            with self.assertRaises(ai_backend.GrokError):
                ai_backend.ask_grok('prompt', on_started=lambda: None)
        self.assertIn('killed', events)

    def test_blocked_reaction_does_not_block_grok_or_reply_actions(self):
        with tempfile.TemporaryDirectory() as root:
            args = cli.build_parser().parse_args(['ai', '--group-bot', '--cooldown', '0', '--min-interval', '0'])
            args.self_mid = SELF
            args.catchup_minutes = 0
            args.state_file = str(Path(root, 'state.json'))
            state = watch.HandledState()
            events = []
            entered, release = threading.Event(), threading.Event()
            def backend(*a):
                events.append('reaction')
                entered.set()
                release.wait(2)
                return react.Result(True, True)
            worker = react.ReactionWorker(state, args.state_file, SELF, backend=backend, log=lambda s: None)
            st = SimpleNamespace(last_send_at=0, last_group={}, replies=0, reactions=worker)
            def grok(prompt, on_started=None):
                events.append('grok-started')
                on_started()
                self.assertTrue(entered.wait(1))
                return '{"actions":[{"type":"send_text","text":"reply"}]}'
            def actions(*a):
                self.assertFalse(release.is_set())
                events.append('reply-actions')
                return True, True
            try:
                with patch.object(cli, '_call', side_effect=grok), patch.object(cli, '_execute_actions', side_effect=actions), patch('sys.stderr', new=io.StringIO()), patch('sys.stdout', new=io.StringIO()):
                    cli._group_bot_handle({'123456789': tagged()}, {}, state, args, {}, SimpleNamespace(stop=False), st)
                self.assertEqual(events, ['grok-started', 'reaction', 'reply-actions'])
            finally:
                release.set()
                worker.jobs.join()
                worker.close()


class UIGuardTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('DISPLAY'), 'DISPLAY is not set')
    def test_read_only_capture_on_available_display(self):
        display = os.environ['DISPLAY']
        try:
            picked = react.sm.pick_main_window(display)
        except (OSError, subprocess.SubprocessError):
            self.skipTest('X display is unavailable')
        if not picked:
            self.skipTest('no LINE window on the configured display')
        wid, geom = picked
        try:
            rgb = react._capture(display, wid, geom[2], geom[3])
        except (OSError, subprocess.SubprocessError):
            self.skipTest('X capture is unavailable')
        self.assertEqual(len(rgb), geom[2] * geom[3] * 3)

    def test_short_room_starts_at_top_and_full_room_scrolls_to_end(self):
        rows = [('date', 45), ('target', 72), ('reply', 71)]
        self.assertEqual(react.row_bounds(rows, 'target', 108, 576), (153, 225))
        rows = [('history', 900), ('target', 72), ('reply', 71)]
        self.assertEqual(react.row_bounds(rows, 'target', 108, 576), (541, 613))

    def test_nonmentioned_message_never_reaches_process_or_ui(self):
        with patch.object(react.lc, 'find_line_pid') as pid:
            result = react.like_message(message(text='@bot hello'), '', SELF, threading.Event())
        self.assertFalse(result.ok)
        pid.assert_not_called()

    def test_verification_runs_after_ui_lock_is_released(self):
        held = False
        picker = dict(id='123456789', room=ROOM, rect=(462, 580, 272, 62))
        memory = SimpleNamespace(mem=None, view=1, rows=lambda mid: [('123456789', 72)],
                                 picker=lambda: picker, validate=lambda: None, close=lambda: None)
        @contextmanager
        def locked(**kw):
            nonlocal held
            held = True
            try: yield
            finally: held = False
        checks = []
        def evidence(*a):
            self.assertFalse(held)
            checks.append(True)
            return dict(records=[] if len(checks) == 1 else [dict(address='0x1234')],
                        confirmed=len(checks) > 1, server_ack=True, client_success=True)
        with patch.object(react.lc, 'find_line_pid', return_value=123), patch.object(
                react.lc, 'current_open_chat_mid', return_value=ROOM), patch.object(
                react.lc, 'chat_list_mids', return_value=[ROOM]), patch.object(
                rm, 'reaction_evidence', side_effect=evidence), patch.object(
                rm, 'ReactionMemory', return_value=memory), patch.object(
                rm, 'widget_global', return_value=(365, 108, 377, 576)), patch.object(
                react.sm, '_ui_lock', side_effect=locked), patch.object(
                react.sm, 'pick_main_window', return_value=('1', (0, 0, 742, 806))), patch.object(
                react.sm, 'activate'), patch.object(react.sm, '_move'), patch.object(
                react.sm, 'run_cmd'), patch.object(react.sm, 'click_relative'), patch.object(
                react.sm, 'key'), patch.object(react, '_capture', return_value=b''), patch.object(
                react, '_bubble', return_value=(574, 650)), patch.object(react.time, 'sleep'):
            result = react.like_message(tagged(), '', SELF, threading.Event())
        self.assertTrue(result.ok)
        self.assertTrue(result.committed)
        self.assertEqual(len(checks), 2)

    def test_wrong_picker_message_never_clicks_a_reaction_and_cleans_up(self):
        memory = SimpleNamespace(mem=None, view=1, rows=lambda mid: [('123456789', 72)],
                                 picker=lambda: dict(id='987654321', room=ROOM,
                                                     rect=(470, 600, 272, 62)),
                                 validate=lambda: None, close=lambda: None)
        with patch.object(react.lc, 'find_line_pid', return_value=123), patch.object(
                react.lc, 'current_open_chat_mid', return_value=ROOM), patch.object(
                react.lc, 'chat_list_mids', return_value=[ROOM]), patch.object(
                rm, 'reaction_evidence', return_value=dict(records=[], confirmed=False)), patch.object(
                rm, 'ReactionMemory', return_value=memory), patch.object(
                rm, 'widget_global', return_value=(365, 108, 377, 576)), patch.object(
                react.sm, '_ui_lock', return_value=nullcontext()), patch.object(
                react.sm, 'pick_main_window', return_value=('1', (0, 0, 742, 806))), patch.object(
                react.sm, 'activate'), patch.object(react.sm, '_move'), patch.object(
                react.sm, 'run_cmd') as command, patch.object(react.sm, 'click_relative') as click, patch.object(
                react.sm, 'key') as key, patch.object(react, '_capture', return_value=b''), patch.object(
                react, '_bubble', return_value=(574, 650)), patch.object(react.time, 'sleep'):
            result = react.like_message(tagged(), '', SELF, threading.Event())
        self.assertFalse(result.ok)
        self.assertFalse(result.committed)
        self.assertIn('picker mismatch', result.detail)
        self.assertEqual(click.call_count, 1)  # opens picker only
        self.assertFalse(any(c.args[0] == ['xdotool', 'click', '1'] for c in command.call_args_list))
        key.assert_called_once_with(os.environ.get('DISPLAY') or ':3', 'Escape')


if __name__ == '__main__':
    unittest.main()
