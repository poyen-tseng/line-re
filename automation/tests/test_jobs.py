import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bot_config as bc
import coding_jobs as jobs
import line_cli as cli
import line_common as lc

ROOM = 'c' + '3' * 32
OTHER_ROOM = 'c' + '4' * 32


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = jobs.JobStore(self.temp.name, timeout=5)

    def start(self, code="from pathlib import Path; Path('result.py').write_text('print(42)'); print('done')", **kw):
        return self.store.start(ROOM, 'group', bc.DEFAULT_ADMIN, 'write a Python program',
                                command=[sys.executable, '-c', code], **kw)

    def wait(self, row, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.store.refresh()[0]
            if current['status'] in jobs.TERMINAL:
                return current
            time.sleep(.05)
        self.fail('job failed to finish')

    def test_detached_persist_complete_files_and_notify_once_after_restart(self):
        row = self.start("import time; from pathlib import Path; time.sleep(.2); Path('result.py').write_text('print(42)'); print('done')", command_id='message-1')
        self.assertNotEqual(os.getsid(row['worker_pid']), os.getsid(0))
        self.assertTrue(Path(row['workdir'], '.git').is_dir())
        self.store = jobs.JobStore(self.temp.name)  # simulate bot restart
        done = self.wait(row)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['exit_code'], 0)
        self.assertIsNotNone(done['pgid'])
        self.assertEqual([Path(x).name for x in self.store.outputs(done)], ['result.py'])
        self.assertIn('done', self.store.summary(done, self.store.outputs(done)))
        claimed = self.store.claim_notification()
        self.store.finish_notification(claimed['id'], True)
        self.assertIsNone(jobs.JobStore(self.temp.name).claim_notification())
        with patch.object(jobs.subprocess, 'Popen', side_effect=AssertionError('duplicate spawn')):
            same = self.start(command_id='message-1')
        self.assertEqual(same['id'], row['id'])

    def test_worker_survives_actual_parent_exit(self):
        starter = """
import sys, json
from coding_jobs import JobStore
s = JobStore(sys.argv[1], timeout=3)
r = s.start(sys.argv[2], '', sys.argv[3], 'test detached', command=[sys.executable, '-c', 'import time; time.sleep(.4); print(123)'])
print(json.dumps(r))
"""
        result = subprocess.run([sys.executable, '-c', starter, self.temp.name, ROOM, bc.DEFAULT_ADMIN],
                                cwd=str(Path(jobs.__file__).parent), capture_output=True, text=True, timeout=3, check=True)
        row = json.loads(result.stdout)
        self.assertEqual(self.wait(row)['status'], 'completed')

    def test_failure_timeout_and_process_group_cleanup(self):
        self.assertEqual(self.wait(self.start('raise SystemExit(7)'))['exit_code'], 7)
        self.store = jobs.JobStore(self.temp.name, timeout=.4)
        row = self.start("import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path('child.pid').write_text(str(p.pid)); time.sleep(60)")
        # Find this row specifically (there is also the earlier failed row).
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            self.store.refresh()
            done = self.store.load(row['id'])
            if done['status'] in jobs.TERMINAL:
                break
            time.sleep(.05)
        self.assertEqual(done['status'], 'timed_out')
        self.assertIsNone(jobs.process_token(done['pid']))
        child = int(Path(row['workdir'], 'child.pid').read_text())
        self.assertIsNone(jobs.process_token(child))

    def test_cancel_is_room_scoped_and_survives_restart(self):
        row = self.start('import time; time.sleep(60)')
        with self.assertRaises(jobs.JobError):
            self.store.cancel(OTHER_ROOM, row['id'])
        self.assertIn('已取消', self.store.cancel(ROOM, row['id']))
        current = jobs.JobStore(self.temp.name).refresh()[0]
        self.assertEqual(current['status'], 'cancelled')
        deadline = time.monotonic() + 4
        exit_path = self.store.control(row['id']) / 'exit.json'
        while not exit_path.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(exit_path.exists())
        self.assertEqual(json.loads(exit_path.read_text())['status'], 'cancelled')

    def test_concurrency_cap_and_crashed_claim_never_replayed(self):
        self.store = jobs.JobStore(self.temp.name, timeout=1, max_concurrent=1)
        row = self.start('import time; time.sleep(.4)')
        with self.assertRaisesRegex(jobs.JobError, '最多'):
            self.start()
        self.wait(row)
        self.assertIsNotNone(self.store.claim_notification())
        self.assertIsNone(jobs.JobStore(self.temp.name).claim_notification())
        self.assertEqual(self.store.load(row['id'])['notification'], 'unconfirmed')

    def test_secrets_and_output_symlinks_rejected_and_repo_permission_trusted(self):
        with self.assertRaisesRegex(jobs.JobError, '憑證'):
            self.store.start(ROOM, '', bc.DEFAULT_ADMIN, 'api_key=sk-example-12345678901234567890')
        for command in ('read /workspace/line-re', '不要修改 /workspace/line-re',
                        'do not edit /workspace/line-re'):
            self.assertFalse(jobs.repo_access_allowed(command))
        self.assertTrue(jobs.repo_access_allowed('允許修改 /workspace/line-re 修正錯誤'))
        row = self.start()
        row = self.wait(row)
        work = Path(row['workdir'])
        (work / 'unsafe.py').write_text('api_key="sk-example-12345678901234567890"')
        (work / 'link.py').symlink_to(work / 'result.py')
        (work / 'secret.txt').write_text('private')
        (work / '.env').write_text('private')
        self.assertEqual([Path(x).name for x in self.store.outputs(row)], ['result.py'])
        self.assertNotIn('sk-example', jobs.safe_text('done\napi_key=sk-example-12345678901234567890'))

    def test_completion_dispatch_uses_original_room_and_never_repeats(self):
        row = self.wait(self.start())
        args = cli.build_parser().parse_args(['ai', '--group-bot', '--jobs-root', self.temp.name])
        args.bot_config = str(Path(self.temp.name, 'bot-config'))
        st = SimpleNamespace(last_send_at=0, last_group={})
        gx = SimpleNamespace(stop=False)
        def upload(ns, *a):
            self.assertEqual(ns.room, ROOM)
            ns.file_result = SimpleNamespace(status='ok')
            return 0
        with patch.object(cli, '_send_verified_text', return_value=(True, 0)) as send, patch.object(
                cli, '_bot_upload', side_effect=upload) as attach:
            cli._dispatch_completed_jobs(args, gx, st)
            cli._dispatch_completed_jobs(args, gx, st)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], ROOM)
        attach.assert_called_once()
        self.assertEqual(self.store.load(row['id'])['notification'], 'notified')

    def test_dead_supervisor_fails_and_cleans_up_orphan_agent(self):
        row = self.start('import time; time.sleep(60)')
        running_path = self.store.control(row['id']) / 'running.json'
        deadline = time.monotonic() + 3
        while not running_path.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        running = json.loads(running_path.read_text())
        os.kill(row['worker_pid'], signal.SIGKILL)
        row['start_time'] -= 4
        jobs._write(self.store.path(row['id']), row)
        time.sleep(.05)
        refreshed = self.store.refresh()[0]
        self.assertEqual(refreshed['status'], 'failed')
        self.assertIsNone(jobs.process_token(running['pid']))

    def test_corrupt_records_fail_closed_and_job_root_must_be_outside_repo(self):
        row = self.wait(self.start())
        path = self.store.path(row['id'])
        path.write_text('broken')
        with self.assertRaises(jobs.JobError):
            self.start()
        self.assertEqual(path.read_text(), 'broken')
        with self.assertRaises(jobs.JobError):
            jobs.JobStore(Path(jobs.__file__).parent / 'jobs')
