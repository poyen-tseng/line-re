from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bot_config as bc
import group_actions as ga
import operator_commands as commands
import vm_control
import ai_backend
import coding_jobs
import line_cli as cli
from test_memory import message

MODELS = '''Available models
grok-4.7-low - Grok Low
grok-4.7-low-fast - Grok Low Fast\u200b
cursor-grok-4.6-high - Grok 4.6
cursor-grok-4.6-high-fast - Grok 4.6 Fast
auto - Auto (default)
'''


class OperatorParsingTests(unittest.TestCase):
    def test_explicit_forms(self):
        for text, kind in [('請截圖桌面', 'screenshot'), ('截圖 :1', 'screenshot'),
                           ('工作狀態', 'job_status'), ('工作狀態 abc', 'job_status'),
                           ('取消工作 abc', 'cancel_job'), ('目前模型', 'current_model'),
                           ('切換模型 grok-4.6-high', 'switch_model'),
                           ('建立一份 Python 程式類的工作：印出 hello', 'create_job')]:
            with self.subTest(text=text):
                actions = commands.parse_command(text)
                self.assertEqual(actions[0]['type'], kind)
                self.assertEqual(ga.parse_actions(json.dumps({'actions': actions}), 5)[0], actions)
        self.assertIsNone(commands.parse_command('他說「請截圖桌面」是什麼意思'))
        self.assertIsNone(commands.parse_command('哈囉'))

    def test_backend_choice_and_untrusted_fields(self):
        action = commands.parse_command('建立工作：[cursor grok-4.6-high] 寫程式')[0]
        self.assertEqual(action['backend'], 'cursor')
        self.assertEqual(action['prompt'], '寫程式')
        raw = dict(type='screenshot', display=':3', sender_mid=bc.DEFAULT_ADMIN, room='other')
        self.assertEqual(ga.parse_actions(json.dumps(raw), 5)[0], [{'type': 'screenshot', 'display': ':3'}])
        self.assertIsNone(commands.normalize(dict(type='screenshot', display=':3; id')))

    def test_model_list_aliases_fast_and_unknown(self):
        catalog = bc.ModelCatalog()
        with patch.object(bc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '\x1b[32m' + MODELS + '\x1b[0m', '')) as run:
            self.assertEqual(catalog.resolve('grok-4.6-high'), 'cursor-grok-4.6-high-fast')
            self.assertEqual(catalog.resolve('cursor-grok-4.6-high-fast'), 'cursor-grok-4.6-high-fast')
            self.assertEqual(catalog.resolve('grok-4.7-low'), 'grok-4.7-low-fast')
            with self.assertRaisesRegex(bc.ConfigError, '建議'):
                catalog.resolve('grok-4.6-hihg')
            with self.assertRaises(bc.ConfigError):
                catalog.resolve('auto')
            run.assert_called_once()

    def test_config_survives_restart_and_corrupt_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            config = bc.BotConfig(Path(root, 'config.json'))
            self.assertTrue(config.is_admin(bc.DEFAULT_ADMIN))
            self.assertFalse(config.is_admin('Alice'))
            config.set_model('cursor-grok-4.6-high-fast')
            self.assertEqual(bc.BotConfig(config.path).model, 'cursor-grok-4.6-high-fast')
            config.path.write_text('broken')
            with self.assertRaises(bc.ConfigError):
                config.is_admin(bc.DEFAULT_ADMIN)

    def test_capture_lock_and_png(self):
        with tempfile.TemporaryDirectory() as root:
            def run(argv, **kwargs):
                if argv[0] == 'import':
                    Path(argv[-1]).write_bytes(b'\x89PNG\r\n\x1a\nexample')
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(vm_control.shutil, 'which', return_value='/usr/bin/import'), patch.object(
                    vm_control.subprocess, 'run', side_effect=run) as mocked, patch.object(
                    vm_control.send_message, '_ui_lock', return_value=nullcontext()) as lock:
                path = vm_control.capture(root, ':3')
            self.assertTrue(Path(path).is_file())
            lock.assert_called_once_with(timeout=5)
            self.assertEqual(mocked.call_args.kwargs['timeout'], 8)


class OperatorExecutorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.args = cli.build_parser().parse_args(['ai', '--group-bot', '--cooldown', '0', '--min-interval', '0'])
        self.args.bot_config = str(self.root / 'config.json')
        self.args.schedules_file = str(self.root / 'schedules.json')
        self.args.jobs_root = str(self.root / 'jobs')
        self.args.state_file = str(self.root / 'state')
        self.args.catchup_minutes = 0
        self.st = SimpleNamespace(last_send_at=0, last_group={}, replies=0)
        self.gx = SimpleNamespace(stop=False)
        self.room = 'c' + '3' * 32
        self.other = 'u' + '2' * 32

    def execute(self, actions, sender=bc.DEFAULT_ADMIN, command=''):
        return cli._execute_actions(actions, 'command', 'group', self.room, self.args,
                                    self.gx, self.st, sender, command)

    def test_every_operator_action_refuses_nonadmin_and_missing_sender(self):
        for sender in (self.other, None, 'Alice', bc.DEFAULT_ADMIN + ' '):
            for kind in commands.ACTION_TYPES:
                with self.subTest(sender=sender, kind=kind), patch.object(cli, '_send_verified_text', return_value=(True, 0)) as send, patch.object(
                        vm_control, 'capture', side_effect=AssertionError('unauthorized capture')), patch.object(
                        cli, '_job_store', side_effect=AssertionError('unauthorized jobs')), patch.object(
                        bc.CATALOG, 'resolve', side_effect=AssertionError('unauthorized model')):
                    self.execute([dict(type=kind, sender_mid=bc.DEFAULT_ADMIN, requester=bc.DEFAULT_ADMIN)], sender)
                self.assertEqual(send.call_args.args[1], '抱歉，這個指令只有管理員可以使用。')
                self.assertFalse(Path(self.args.bot_config).exists())
                self.assertFalse(Path(self.args.jobs_root).exists())

    def test_admin_override_and_ordinary_chat_unchanged(self):
        self.args.admin = [self.other]
        with patch.object(cli, '_send_verified_text', return_value=(True, 0)) as send, patch.object(cli, '_wait_gap', return_value=True):
            self.execute([{'type': 'current_model'}], self.other)
            self.execute([{'type': 'send_text', 'text': 'hello'}], 'not-an-admin')
            self.execute([{'type': 'current_model'}], bc.DEFAULT_ADMIN)
        self.assertIn(bc.DEFAULT_MODEL, send.call_args_list[0].args[1])
        self.assertEqual(send.call_args_list[1].args[1], 'hello')
        self.assertIn('管理員', send.call_args_list[2].args[1])

    def test_switch_persist_and_backend_uses_selected_model(self):
        with patch.object(bc.CATALOG, 'get', return_value=bc.parse_models(MODELS)), patch.object(
                cli, '_send_verified_text', return_value=(True, 0)) as send:
            self.execute(commands.parse_command('切換模型 grok-4.6-high'))
        self.assertIn('cursor-grok-4.6-high-fast', send.call_args.args[1])
        with patch.object(ai_backend, 'CONFIG_PATH', self.args.bot_config), patch.object(
                ai_backend.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='ok', stderr='')) as run:
            self.assertEqual(ai_backend.ask_grok('test'), 'ok')
        self.assertIn('cursor-grok-4.6-high-fast', run.call_args.args[0])

    def test_explicit_command_works_without_grok_and_sender_comes_from_memory(self):
        row = message(text='@bot 目前模型', **{'from': bc.DEFAULT_ADMIN})
        self.args.mention = '@bot'
        with patch.object(cli, '_call', side_effect=AssertionError('explicit command called Grok')), patch.object(
                cli, '_execute_actions', return_value=(True, True)) as execute:
            cli._group_bot_handle({row['id']: row}, {bc.DEFAULT_ADMIN: 'some name'}, set(), self.args, {}, self.gx, self.st)
        self.assertEqual(execute.call_args.args[7], bc.DEFAULT_ADMIN)
        self.assertEqual(execute.call_args.args[0], [{'type': 'current_model'}])

    def test_screenshot_uses_same_room_and_verified_uploader(self):
        path = self.root / 'screen.png'
        path.write_bytes(b'png')
        def upload(ns):
            self.assertEqual(ns.room, self.room)
            self.assertEqual(ns.file_path, str(path))
            ns.file_result = SimpleNamespace(status='ok', committed=True, message_id='123456789', detail='')
            return 0
        with patch.object(vm_control, 'capture', return_value=str(path)) as capture, patch.object(
                cli.send_message, 'run_file', side_effect=upload) as sendfile, patch.object(
                cli, '_send_verified_text', return_value=(True, 0)) as text:
            self.execute([dict(type='screenshot', display=':3', room='wrong-room')])
        capture.assert_called_once_with(self.args.jobs_root, ':3')
        sendfile.assert_called_once()
        self.assertEqual(text.call_args.args[2], self.room)

    def test_dry_run_no_operator_effects(self):
        self.args.dry_run = True
        with patch.object(vm_control, 'capture', side_effect=AssertionError('dry capture')), patch.object(
                cli, '_job_store', side_effect=AssertionError('dry jobs')), patch.object(
                bc.CATALOG, 'resolve', side_effect=AssertionError('dry model')), patch.object(
                cli, '_send_verified_text', side_effect=AssertionError('dry send')):
            self.execute([{'type': kind} for kind in commands.ACTION_TYPES])
        self.assertFalse(Path(self.args.jobs_root).exists())

    def test_model_generated_action_cannot_spoof_admin(self):
        row = message(text='@bot show the desktop', **{'from': self.other})
        self.args.mention = '@bot'
        raw = json.dumps({'actions': [{'type': 'screenshot', 'sender_mid': bc.DEFAULT_ADMIN}]})
        with patch.object(cli, '_call', return_value=raw), patch.object(vm_control, 'capture', side_effect=AssertionError('spoofed')), patch.object(
                cli, '_send_verified_text', return_value=(True, 0)) as send:
            cli._group_bot_handle({row['id']: row}, {self.other: 'Alice'}, set(), self.args, {}, self.gx, self.st)
        self.assertIn('管理員', send.call_args.args[1])
