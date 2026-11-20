"""Native-Windows layer: parsing, safety rules and fallbacks. Runs on any OS."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ai_backend  # noqa: E402
import bot_config as bc  # noqa: E402
import group_actions as ga  # noqa: E402
import line_cli  # noqa: E402
import line_common as lc  # noqa: E402
import platform_compat as pc  # noqa: E402
import sync_watchdog as wd  # noqa: E402
import win_line  # noqa: E402

SELF = 'u' + '2' * 32
OWNER = 'u' + '1' * 32
OTHER = 'u' + '5' * 32
ROOM = 'c' + '3' * 32
PLUGIN_ROOM = 'c' + '6' * 32


def message(**kw):
    row = {'id': '100000000000000001', 'from': OWNER, 'to': ROOM, 'text': '@bot hi',
           'contentType': 0, 'createdTime': 1_800_000_000_000,
           'contentMeta': json.dumps({'MENTION': json.dumps({'MENTIONEES': [{'M': SELF, 'S': '0', 'E': '4'}]})})}
    row.update(kw)
    return row


def bot_args(**kw):
    ns = argparse.Namespace(self_mid=SELF, mention='@bot', commander='', include_dm=False,
                            dm_without_mention=False, room_plugins=[], bot_config='missing.json')
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


class LockAndLauncherTests(unittest.TestCase):
    def test_lock_excludes_second_holder_until_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'x.lock')
            with pc.locked_path(path):
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    with pc.locked_path(path, timeout=0.3):
                        pass
                self.assertGreaterEqual(time.monotonic() - started, 0.25)
            with pc.locked_path(path, timeout=0.3):
                pass

    def test_lock_serializes_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, inside, peak = os.path.join(tmp, 'y.lock'), [0], [0]

            def work():
                with pc.locked_path(path, timeout=5):
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                    time.sleep(0.05)
                    inside[0] -= 1
            threads = [threading.Thread(target=work) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(peak[0], 1)

    def test_cmd_shims_never_receive_shell_metacharacters(self):
        with patch.object(pc, '_NT', True):
            with self.assertRaises(OSError):
                pc.safe_argv(['C:\\x\\agent.cmd', '-p', 'hi & del /q *'])
            self.assertEqual(pc.safe_argv(['C:\\x\\agent.cmd', '-p', 'plain']),
                             ['C:\\x\\agent.cmd', '-p', 'plain'])
            # Real executables take any argument; CreateProcess does not re-parse.
            self.assertEqual(pc.safe_argv(['node.exe', 'x.js', 'a & b'])[2], 'a & b')

    def test_known_shims_resolve_to_node_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for ver in ('2026.1.2-aaaa', '2026.10.22-bbbb'):
                (base / 'versions' / ver).mkdir(parents=True)
                (base / 'versions' / ver / 'node.exe').write_text('')
                (base / 'versions' / ver / 'index.js').write_text('')
            entry = pc._windows_node_entry('cursor-agent', str(base / 'cursor-agent.cmd'))
            self.assertEqual(entry, [str(base / 'versions' / '2026.10.22-bbbb' / 'node.exe'),
                                     str(base / 'versions' / '2026.10.22-bbbb' / 'index.js')])
            self.assertIsNone(pc._windows_node_entry('unknown', str(base / 'x.cmd')))

    def test_process_token_of_self_and_missing(self):
        self.assertIsNotNone(pc.process_token(os.getpid()))
        self.assertIsNone(pc.process_token(-5))


class SecretPathTests(unittest.TestCase):
    def test_appdata_is_secret_except_temp(self):
        for path in (r'C:\Users\a\AppData\Local\Google\Chrome\User Data\Default\Cookies',
                     r'C:\Users\a\AppData\Roaming\GitHub CLI\hosts.yml',
                     r'C:\Users\a\AppData\Local\LINE\Data\x.edb',
                     r'C:\Users\a\NTUSER.DAT'):
            self.assertTrue(ga.is_secret_path(path), path)
        self.assertFalse(ga.is_secret_path(r'C:\Users\a\AppData\Local\Temp\line-uploads\a.pdf'))
        self.assertFalse(ga.is_secret_path(r'\\nas\share\notes\會議筆記.docx'))

    def test_wine_path_is_identity_on_windows(self):
        with patch.object(pc, 'IS_WINDOWS', True):
            self.assertEqual(ga.wine_path(os.path.abspath('a.mp4')), os.path.abspath('a.mp4'))


class RowQuirkTests(unittest.TestCase):
    def test_own_outgoing_row_gets_sender(self):
        vals = [None] * 36
        vals[lc.MSG_FROM], vals[lc.MSG_TO] = '', ROOM
        with patch.object(lc, '_SELF_MID_HINT', [SELF]):
            self.assertEqual(lc.normalize_own_row(vals)[lc.MSG_FROM], SELF)
            vals[20] = OTHER
            self.assertEqual(lc.normalize_own_row(vals)[lc.MSG_FROM], OTHER)
        short = ['', ROOM, 1]
        self.assertIs(lc.normalize_own_row(short), short)

    def test_unknown_self_leaves_row_unparsed(self):
        vals = [None] * 36
        vals[lc.MSG_FROM], vals[lc.MSG_TO] = '', ROOM
        with patch.object(lc, '_SELF_MID_HINT', [None]), patch.object(lc, 'self_mid_hint', return_value=None):
            self.assertEqual(lc.normalize_own_row(vals)[lc.MSG_FROM], '')

    def test_contact_profile_json_and_lookup_blob(self):
        vals = [OTHER, 0, 0, 0, '', ''] + [None] * 17 + [json.dumps({'profileName': '小明'})]
        self.assertTrue(lc.looks_like_contact_row(vals))
        self.assertEqual(lc.contact_row_name(vals), '小明')
        blob = [OTHER, 0, 0, 0, 'x', '{"_lkp|abc":1}']
        self.assertFalse(lc.looks_like_contact_row(blob))

    def test_group_row_without_added_time_or_inviter(self):
        now = 1_800_000_000_000
        vals = [0, ROOM, now, 0, 0, '貓貓', '', '', 0, 0, '']
        self.assertTrue(lc.looks_like_group_chat_row(vals))

    def test_profile_name_capture(self):
        name = '新名字'.encode()
        data = (b'xx getProfile\x0c\x00\x1c\x18!' + SELF.encode() + b'\x00\x88'
                + bytes([len(name)]) + name + b'\x28 tail')
        names = []
        lc._collect_profile_names(data, SELF, names)
        self.assertEqual(names, ['新名字'])
        lc._collect_profile_names(data, OTHER, names)
        self.assertEqual(names, ['新名字'])


class NetstatTests(unittest.TestCase):
    OUT = '\n'.join([
        '  TCP    10.0.0.2:5000    147.92.1.1:443   ESTABLISHED   4242',
        '  TCP    10.0.0.2:5001    147.92.1.2:443   CLOSE_WAIT    4242',
        '  TCP    10.0.0.2:5002    3.76.1.1:443     ESTABLISHED   4242',
        '  TCP    10.0.0.2:5003    147.92.1.3:443   ESTABLISHED   999',
        '  TCP    127.0.0.1:5004   127.0.0.1:443    ESTABLISHED   4242'])

    def test_counts_only_this_pid_and_core_nets(self):
        done = subprocess.CompletedProcess([], 0, self.OUT, '')
        with patch.object(wd.subprocess, 'run', return_value=done):
            r = wd._windows_conn_states(4242, [wd.ipaddress.ip_network(n) for n in wd.DEFAULT_NETS])
        self.assertEqual((r['core_estab'], len(r['core_closewait']), r['estab_443']), (1, 1, 2))

    def test_failed_netstat_is_unknown(self):
        with patch.object(wd.subprocess, 'run', side_effect=OSError('x')):
            self.assertIsNone(wd._windows_conn_states(1, []))

    def test_windows_stall_needs_no_tls_at_all(self):
        states = [dict(core_estab=0, core_closewait={('a', 'b')}, core_other=[], estab_443=1),
                  dict(core_estab=0, core_closewait={('a', 'b')}, core_other=[], estab_443=0)]
        dog = wd.SyncWatchdog(stall_secs=1000, check_every=0, ui_nudge=False)
        with patch.object(pc, 'IS_WINDOWS', True), patch.object(wd, 'line_conn_states', side_effect=states):
            dog.tick(4242)
            self.assertIsNone(dog.stall_since)
            dog.tick(4242)
            self.assertIsNotNone(dog.stall_since)

    def test_windows_restart_never_kills(self):
        with patch.object(pc, 'IS_WINDOWS', True), patch.object(lc, 'find_line_pid', return_value=7), \
                patch.object(win_line, 'launch_line', return_value=True) as launch, \
                patch.object(wd.os, 'kill', side_effect=AssertionError('killed')):
            self.assertTrue(wd.restart_line(pid=7))
        launch.assert_called_once_with(7)


class DecisionTests(unittest.TestCase):
    def test_commander_list(self):
        args = bot_args(commander=f'{OWNER},{OTHER}')
        self.assertEqual(line_cli._groupbot_decision(message(), args, {}), (None, 'metadata'))
        self.assertEqual(line_cli._groupbot_decision(message(**{'from': 'u' + '7' * 32}), args, {})[0],
                         'not commander')

    def test_metadata_naming_someone_else_is_not_a_command(self):
        meta = json.dumps({'MENTION': json.dumps({'MENTIONEES': [{'M': OTHER, 'S': '0', 'E': '4'}]})})
        reason, _ = line_cli._groupbot_decision(message(contentMeta=meta, text='@新名字 hi'),
                                                bot_args(), {SELF: '新名字'})
        self.assertEqual(reason, 'not mentioned')

    def test_renamed_bot_text_fallback_without_metadata(self):
        m = message(contentMeta=None, text='@新名字 幫我查')
        self.assertEqual(line_cli._groupbot_decision(m, bot_args(), {SELF: '新名字'}), (None, 'text'))
        self.assertEqual(line_cli._command_text(m, SELF, '@bot', {SELF: '新名字'}), '幫我查')

    def test_dm_without_mention_is_opt_in(self):
        dm = message(to=SELF, contentMeta=None, text='hello')
        self.assertEqual(line_cli._groupbot_decision(dm, bot_args(include_dm=True), {})[0], 'not mentioned')
        self.assertEqual(line_cli._groupbot_decision(dm, bot_args(include_dm=True, dm_without_mention=True), {}),
                         (None, 'dm'))
        group = message(contentMeta=None, text='hello')
        self.assertEqual(line_cli._groupbot_decision(group, bot_args(dm_without_mention=True), {})[0],
                         'not mentioned')

    def test_room_plugin_owns_its_room_and_senders(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / 'plug.py'
            plugin.write_text(
                f"def handles_room(r): return r == {PLUGIN_ROOM!r}\n"
                f"def is_allowed_sender(m): return m == {OTHER!r}\n"
                "def handle_group_command(**kw): return [{'type':'send_text','text':'ok'}], 'plugin', False\n",
                encoding='utf-8')
            args = bot_args(commander=OWNER, room_plugins=[str(plugin)])
            here = message(to=PLUGIN_ROOM, **{'from': OTHER})
            self.assertEqual(line_cli._groupbot_decision(here, args, {}), (None, 'metadata'))
            self.assertEqual(line_cli._groupbot_decision(message(to=PLUGIN_ROOM), args, {})[0], 'not commander')
            untagged = message(to=PLUGIN_ROOM, contentMeta=None, text='@bot hi', **{'from': OTHER})
            self.assertEqual(line_cli._groupbot_decision(untagged, args, {})[0], 'not mentioned')
            # Other rooms keep --commander.
            self.assertEqual(line_cli._groupbot_decision(message(), args, {}), (None, 'metadata'))
            self.assertIsNotNone(line_cli._room_plugin(args, PLUGIN_ROOM))

    def test_broken_plugin_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / 'bad.py'
            plugin.write_text('raise RuntimeError("boom")\n', encoding='utf-8')
            args = bot_args(room_plugins=[str(plugin)])
            self.assertIsNone(line_cli._room_plugin(args, ROOM))
            self.assertEqual(line_cli._groupbot_decision(message(), args, {}), (None, 'metadata'))


class ConfigBackendTests(unittest.TestCase):
    def write(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'bot_config.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return bc.BotConfig(path)

    def test_backend_model_rules(self):
        self.assertEqual(self.write({'backend': 'antigravity', 'model': 'gemini-4-hydrogen-xhigh'}).model,
                         'gemini-4-hydrogen-xhigh')
        self.assertEqual(self.write({'backend': 'antigravity'}).model, 'gemini-4-hydrogen-xhigh')
        with self.assertRaises(bc.ConfigError):
            self.write({'model': 'grok-4.7-low'}).load()  # cursor stays fast-only
        with self.assertRaises(bc.ConfigError):
            self.write({'backend': 'shell'}).load()
        with self.assertRaises(bc.ConfigError):
            self.write({'fallback_backends': ['nope']}).load()
        cfg = self.write({'backend': 'antigravity', 'fallback_backends': ['openai', 'antigravity']})
        self.assertEqual(cfg.backends, ['antigravity', 'openai'])

    def test_set_model_respects_backend(self):
        cfg = self.write({'backend': 'antigravity'})
        cfg.set_model('claude-sonnet-5-5-high')
        self.assertEqual(cfg.model, 'claude-sonnet-5-5-high')
        cursor = self.write({})
        with self.assertRaises(bc.ConfigError):
            cursor.set_model('claude-sonnet-5-5-high')

    def test_fallback_runs_next_backend_and_starts_once(self):
        cfg = self.write({'backend': 'antigravity', 'fallback_backends': ['openai']})
        started = []

        def broken(prompt, model, timeout, on_started, config):
            raise ai_backend.GrokError('down')

        def ok(prompt, model, timeout, on_started, config):
            on_started()
            on_started()
            return 'reply:' + model
        with patch.object(ai_backend, 'CONFIG_PATH', cfg.path), \
                patch.object(ai_backend, '_ask_antigravity', side_effect=broken), \
                patch.object(ai_backend, '_ask_openai', side_effect=ok):
            self.assertEqual(ai_backend.ask_grok('p', on_started=lambda: started.append(1)),
                             'reply:' + bc.DEFAULT_MODELS['openai'])
        self.assertEqual(started, [1])

    def test_antigravity_model_resolution(self):
        catalog = bc.ModelCatalog()
        with patch.object(catalog, '_list_antigravity', return_value=['gemini-4-neon-high', 'claude-opus-5-5-high']):
            self.assertEqual(catalog.resolve('Gemini-4-Neon-High', 'antigravity'), 'gemini-4-neon-high')
            with self.assertRaises(bc.ConfigError):
                catalog.resolve('gemini-4-neon-hig', 'antigravity')
        self.assertEqual(catalog.resolve('anything', 'openai'), 'anything')


class WinLineTests(unittest.TestCase):
    def test_main_window_prefers_titled_visible(self):
        windows = [{'pid': 1, 'window_id': 10, 'title': '', 'bounds': {'width': 900, 'height': 900}},
                   {'pid': 1, 'window_id': 11, 'title': 'LINE', 'bounds': {'width': 900, 'height': 600}},
                   {'pid': 1, 'window_id': 12, 'title': 'LINE', 'minimized': True,
                    'bounds': {'width': 900, 'height': 600}},
                   {'pid': 2, 'window_id': 13, 'title': 'LINE', 'bounds': {'width': 999, 'height': 999}},
                   {'pid': 1, 'window_id': 14, 'title': 'LINE', 'bounds': {'width': 50, 'height': 50}}]
        self.assertEqual(win_line._pick_main(windows, 1)['window_id'], 11)
        self.assertIsNone(win_line._pick_main(windows, 3))

    def test_enter_is_not_repeated_once_composer_empties(self):
        calls = []
        with patch.object(win_line, 'call', side_effect=lambda tool, p=None: calls.append(p) or {}), \
                patch.object(win_line, 'composer_text', return_value=''), \
                patch.object(win_line.time, 'sleep'):
            self.assertEqual(win_line.press_enter(1, 2, {'element_token': 't'}), 'element')
        self.assertEqual(len(calls), 1)

    def test_failures_before_enter_are_retryable_and_after_are_uncertain(self):
        args = argparse.Namespace(text='hi', chat='', dry_run=False, no_search=False)
        with patch.object(lc, 'find_line_pid', return_value=1):
            with patch.object(win_line, '_send_text', side_effect=win_line.UiError('x')):
                self.assertEqual(win_line.send_text(args, ROOM, log=lambda *a, **k: None), 3)

            def after_enter(args, target, pid, state, log):
                state['enter'] = True
                raise win_line.UiError('lost')
            with patch.object(win_line, '_send_text', side_effect=after_enter):
                self.assertEqual(win_line.send_text(args, ROOM, log=lambda *a, **k: None), 6)

    def test_wrong_room_never_types(self):
        args = argparse.Namespace(text='secret', chat='x', dry_run=False, no_search=False)
        with patch.object(lc, 'find_line_pid', return_value=1), \
                patch.object(win_line, 'main_window', return_value=(5, (0, 0, 10, 10))), \
                patch.object(win_line, 'open_chat', return_value=False), \
                patch.object(win_line, 'set_text', side_effect=AssertionError('typed')), \
                patch.object(win_line, 'end_session'), \
                patch.object(lc, 'current_open_chat_mid', return_value='c' + '9' * 32):
            self.assertEqual(win_line.send_text(args, ROOM, log=lambda *a, **k: None), 4)

    def test_stage_copy_keeps_name_and_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / '會議 筆記.docx'
            src.write_bytes(b'abc')
            staged, size, stage = win_line.stage_copy(str(src), 10)
            try:
                self.assertEqual((Path(staged).name, Path(staged).read_bytes(), size),
                                 ('會議 筆記.docx', b'abc', 3))
            finally:
                win_line.shutil.rmtree(stage, ignore_errors=True)
            with self.assertRaises(OSError):
                win_line.stage_copy(str(src), 2)


class NetworkTimeTests(unittest.TestCase):
    def test_note_only_when_clock_is_off(self):
        from datetime import datetime, timedelta
        now = ga.now_taipei()

        class Resp:
            def __init__(self, when):
                self.headers = {'Date': when.astimezone().strftime('%a, %d %b %Y %H:%M:%S %z')}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        for skew, expect in ((timedelta(seconds=5), ''), (timedelta(days=1), '快')):
            line_cli._NET_TIME.update(at=0.0, text='')
            with patch('urllib.request.urlopen', return_value=Resp(now - skew)):
                text = line_cli._network_time_note(now)
            self.assertTrue(expect in text if expect else text == '', (skew, text))
        self.assertIsInstance(datetime.now(), datetime)


if __name__ == '__main__':
    unittest.main()
