import ipaddress
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sync_watchdog as wd

R = dict(restart=True, stall_secs=90, cooldown_secs=300, ui_nudge=False)
NETS = [ipaddress.ip_network(n) for n in wd.DEFAULT_NETS]


def states(estab=0, cw=()):
    return dict(core_estab=estab, core_closewait=set(cw), core_other=[], estab_443=0)


class SocketParsingTests(unittest.TestCase):
    def inspect(self, output, inodes=None, rc=0):
        result = SimpleNamespace(returncode=rc, stdout=output)
        with patch.object(wd, "_socket_inodes", return_value=inodes), patch.object(
                wd.subprocess, "run", return_value=result):
            return wd.line_conn_states(42, NETS)

    def test_all_owners_are_checked_and_noncore_does_not_count(self):
        out = '\n'.join([
            'ESTAB 0 0 192.0.2.10:5000 147.92.1.2:443 users:(("LINE.exe",pid=99,fd=1),("LINE.exe",pid=42,fd=3))',
            'CLOSE-WAIT 0 0 192.0.2.10:5001 43.223.1.2:443 users:(("LINE.exe",pid=42,fd=4))',
            'ESTAB 0 0 192.0.2.10:5002 203.0.113.10:443 users:(("LINE.exe",pid=42,fd=5))',
            'ESTAB 0 0 192.0.2.10:5003 147.92.1.2:80 users:(("LINE.exe",pid=42,fd=6))',
            'ESTAB 0 0 192.0.2.10:5004 147.92.1.2:443 users:(("LINE.exe",pid=99,fd=1))'])
        result = self.inspect(out)
        self.assertEqual(result['core_estab'], 1)
        self.assertEqual(result['estab_443'], 2)
        self.assertEqual(len(result['core_closewait']), 1)

    def test_inode_ownership_without_process_info(self):
        out = 'ESTAB 0 0 192.0.2.10:5000 147.92.1.2:443 uid:1000 ino:123 sk:a'
        self.assertEqual(self.inspect(out, {'123'})['core_estab'], 1)
        self.assertEqual(self.inspect(out, set())['core_estab'], 0)
        self.assertIsNone(self.inspect(out))

    def test_failed_ss_is_unknown(self):
        self.assertIsNone(self.inspect('', set(), rc=1))
        self.assertIsNone(self.inspect(''))

    def test_nudge_is_exact_tuple_and_uses_noninteractive_sudo(self):
        cw = [('192.0.2.10:5000', '147.92.1.2:443')]
        with patch.object(wd, 'line_conn_states', return_value=states(cw=cw)), patch.object(
                wd.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='denied')) as run:
            self.assertTrue(wd.destroy_core_closewait(42, NETS))
        self.assertEqual(run.call_args.args[0], [
            'sudo', '-n', 'ss', '-K', '-t', 'state', 'close-wait',
            'src', '192.0.2.10', 'sport', '=', '5000',
            'dst', '147.92.1.2', 'dport', '=', '443'])

    def test_nudge_handles_missing_sudo(self):
        with patch.object(wd, 'line_conn_states', return_value=states(cw=[('10.0.0.1:5', '43.223.1.2:443')])), patch.object(
                wd.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertFalse(wd.destroy_core_closewait(42, NETS))

    def test_restart_scope_excludes_other_wine_bottles_and_shells(self):
        names = {'42': 'LINE.exe', '43': 'winedevice.exe', '44': 'wineserver', '45': 'bash', '46': 'linemediaplayer', '47': 'linecall.exe', '48': 'services.exe', '49': 'python3'}
        def prefix(pid):
            return b'/bottles/OTHER' if str(pid) == '43' else b'/bottles/LINE'
        with patch.object(wd.os, 'listdir', return_value=list(names)), patch.object(
                wd, '_wine_prefix', side_effect=prefix), patch.object(wd, '_process_identity', return_value='start'), patch(
                'builtins.open', side_effect=lambda path: io.StringIO(names[path.split('/')[2]])):
            self.assertEqual(set(wd._bottles_line_pids(42)), {42, 44, 46, 47, 48})

    def test_stale_pid_cannot_be_restarted(self):
        with patch('line_common.find_line_pid', return_value=43), patch.object(wd.os, 'kill') as kill:
            self.assertFalse(wd.restart_line(pid=42))
        kill.assert_not_called()


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(wd, '_read_stamp', return_value=0),
                        patch.object(wd, '_write_stamp'), patch.object(wd, '_log'),
                        patch.object(wd.time, 'monotonic'), patch.object(wd.time, 'time'),
                        patch.object(wd, 'line_conn_states', return_value=states()),
                        patch.object(wd, 'restart_line', return_value=True),
                        patch.object(wd, 'destroy_core_closewait', return_value=True),
                        patch.object(wd, 'ui_nudge', return_value=True)]
        values = [p.start() for p in self.patches]
        self.mono, self.wall, self.inspect, self.restart, self.nudge, self.ui = values[3:]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def tick(self, watchdog, now, pid=42):
        self.mono.return_value = now
        self.wall.return_value = 10000 + now
        return watchdog.tick(pid)

    def test_stall_restart_and_persisted_cooldown(self):
        dog = wd.SyncWatchdog(**R)
        for now in (0, 30, 89):
            self.assertFalse(self.tick(dog, now))
        self.assertTrue(self.tick(dog, 100))
        self.restart.assert_called_once_with(':3', pid=42)
        self.tick(dog, 110)
        self.tick(dog, 210)
        self.restart.assert_called_once()
        self.assertTrue(self.tick(dog, 401))

    def test_healthy_unknown_and_pid_change_reset_continuity(self):
        dog = wd.SyncWatchdog(**R)
        self.tick(dog, 0)
        self.inspect.return_value = states(estab=1)
        self.tick(dog, 50)
        self.inspect.return_value = states()
        self.tick(dog, 90)
        self.inspect.return_value = None
        self.tick(dog, 170)
        self.inspect.return_value = states()
        self.tick(dog, 180)
        self.tick(dog, 260, pid=43)
        self.tick(dog, 340, pid=43)
        self.restart.assert_not_called()

    def test_nudge_once_then_restart_fallback(self):
        self.inspect.return_value = states(cw=[('10.0.0.1:5', '147.92.1.2:443')])
        dog = wd.SyncWatchdog(socket_nudge=True, **R)
        for now in (0, 30, 40, 60):
            self.assertFalse(self.tick(dog, now))
        self.nudge.assert_called_once()
        self.assertTrue(self.tick(dog, 90))

    def test_recovery_after_nudge_avoids_restart(self):
        self.inspect.return_value = states(cw=[('10.0.0.1:5', '147.92.1.2:443')])
        dog = wd.SyncWatchdog(socket_nudge=True, **R)
        self.tick(dog, 0)
        self.tick(dog, 30)
        self.inspect.return_value = states(estab=1)
        self.tick(dog, 40)
        self.tick(dog, 200)
        self.restart.assert_not_called()

    def test_nudge_off_by_default_and_failed_launcher_not_success(self):
        self.inspect.return_value = states(cw=[('10.0.0.1:5', '147.92.1.2:443')])
        self.restart.return_value = False
        dog = wd.SyncWatchdog(**R)
        self.tick(dog, 0)
        self.assertFalse(self.tick(dog, 90))
        self.nudge.assert_not_called()
        self.restart.assert_called_once()

    def test_invalid_duration(self):
        for value in (-1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                wd.SyncWatchdog(stall_secs=value)

    def test_wall_clock_jump_does_not_advance_stall_timer(self):
        dog = wd.SyncWatchdog(**R)
        self.tick(dog, 0)
        self.mono.return_value = 10
        self.wall.return_value = 10000000000
        self.assertFalse(dog.tick(42))
        self.restart.assert_not_called()

    def test_nudge_failure_still_restarts_after_grace(self):
        self.inspect.return_value = states(cw=[('10.0.0.1:5', '147.92.1.2:443')])
        self.nudge.return_value = False
        dog = wd.SyncWatchdog(socket_nudge=True, **R)
        self.tick(dog, 0)
        self.tick(dog, 30)
        self.assertTrue(self.tick(dog, 90))

    def test_default_is_log_only_and_never_restarts(self):
        dog = wd.SyncWatchdog()
        self.assertEqual(dog.stall_secs, 900)
        for now in range(0, 4000, 10):
            self.assertFalse(self.tick(dog, now))
        self.restart.assert_not_called()

    def test_new_messages_reset_stall_timer(self):
        dog = wd.SyncWatchdog(**R)
        self.tick(dog, 0)
        self.tick(dog, 80)
        self.mono.return_value = 85
        dog.activity()
        self.assertFalse(self.tick(dog, 100))
        self.assertFalse(self.tick(dog, 170))
        self.assertTrue(self.tick(dog, 180))

    def test_ui_nudge_first_then_restart_fallback(self):
        dog = wd.SyncWatchdog(restart=True, stall_secs=300, cooldown_secs=600)
        for now in range(0, 45, 10):
            self.assertFalse(self.tick(dog, now))
        self.ui.assert_not_called()
        self.assertFalse(self.tick(dog, 50))
        self.ui.assert_called_once_with(':3')
        # nudge did not help: repeated every 60s, restart at 300s
        for now in range(55, 300, 5):
            self.assertFalse(self.tick(dog, now))
        self.assertGreaterEqual(self.ui.call_count, 4)
        self.restart.assert_not_called()
        self.assertTrue(self.tick(dog, 305))

    def test_successful_ui_nudge_resets_stall_and_avoids_restart(self):
        dog = wd.SyncWatchdog(restart=True, stall_secs=300)
        t = 0
        for cycle in range(6):
            self.inspect.return_value = states()
            for _ in range(6):
                self.tick(dog, t); t += 10
            self.inspect.return_value = states(estab=1)
            self.tick(dog, t); t += 10
        self.assertGreaterEqual(self.ui.call_count, 6)
        self.restart.assert_not_called()

    def test_ui_nudge_can_be_disabled(self):
        dog = wd.SyncWatchdog(restart=False, ui_nudge=False)
        for now in range(0, 400, 10):
            self.tick(dog, now)
        self.ui.assert_not_called()

    def test_kick_nudges_immediately_when_stalled(self):
        dog = wd.SyncWatchdog(restart=True, stall_secs=300)
        self.tick(dog, 0)
        self.ui.assert_not_called()
        dog.kick()
        self.tick(dog, 1)
        self.ui.assert_called_once()
        dog.kick()          # rate-limited: at most one urgent nudge per 8s
        self.tick(dog, 3)
        self.ui.assert_called_once()

    def test_kick_while_healthy_does_not_nudge_later(self):
        dog = wd.SyncWatchdog(restart=True, stall_secs=300)
        self.inspect.return_value = states(estab=1)
        dog.kick()
        self.tick(dog, 0)
        self.inspect.return_value = states()
        self.tick(dog, 20)
        self.ui.assert_not_called()

    def test_keepalive_settings_nudge_every_25s(self):
        dog = wd.SyncWatchdog(restart=True, stall_secs=300, check_every=5,
                              ui_nudge_after=5, ui_nudge_every=25)
        for now in range(0, 100, 1):
            self.tick(dog, now)
        self.assertEqual(self.ui.call_count, 4)   # ~5, 30, 55, 80
        self.restart.assert_not_called()


class ThreadTests(unittest.TestCase):
    def test_thread_ticks_and_stops(self):
        import threading
        ticked = threading.Event()
        with patch.object(wd, '_log'), patch.object(wd, '_read_stamp', return_value=0):
            dog = wd.SyncWatchdog()
            with patch.object(dog, 'tick', side_effect=lambda pid: ticked.set()):
                dog.start(lambda last: 42, poll=0.01)
                self.assertTrue(ticked.wait(2))
                dog.stop()
        self.assertIsNone(dog._thread)


class UiNudgeTests(unittest.TestCase):
    def test_click_only_under_ui_lock_no_typing(self):
        import contextlib
        import send_message as sm
        events = []
        @contextlib.contextmanager
        def lock():
            events.append('lock'); yield; events.append('unlock')
        with patch.object(sm, 'pick_main_window', return_value=('77', (0, 0, 742, 806))), patch.object(
                sm, '_ui_lock', lock), patch.object(wd.subprocess, 'run',
                side_effect=lambda cmd, **kw: events.append(cmd[1])):
            self.assertTrue(wd.ui_nudge(':3'))
        self.assertEqual(events, ['lock', 'windowactivate', 'mousemove', 'click', 'unlock'])

    def test_lock_timeout_is_a_failed_nudge(self):
        import send_message as sm
        with patch.object(sm, 'pick_main_window', return_value=('77', None)), patch.object(
                sm, '_ui_lock', side_effect=TimeoutError('busy')), patch.object(wd, '_log'):
            self.assertFalse(wd.ui_nudge(':3'))


if __name__ == '__main__':
    unittest.main()
