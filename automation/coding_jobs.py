"""Durable, detached coding jobs. Worker lifetime is independent of the bot.

Agents run with the owner's privileges, not an OS sandbox. Control records
live outside the agent cwd. Only explicit owner text permits repo access.
"""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

import bot_config
import group_actions as ga
import line_common as lc

DEFAULT_ROOT = '/workspace/line-bot-jobs'
DEFAULT_CODEX_MODEL = 'gpt-6.1-sol'  # gpt-6-luna-max is rejected on this ChatGPT account
DEFAULT_BACKEND = 'cursor'
DEFAULT_CURSOR_JOB_MODEL = 'grok-4.7-high-fast'
TERMINAL = {'completed', 'failed', 'timed_out', 'cancelled'}
JOB_ID = re.compile(r'\d{8}-\d{6}-[0-9a-f]{8}\Z')
SECRET = re.compile(r'(?i)(?:-----BEGIN .{0,30}PRIVATE KEY|\b(?:sk-|ghp_|github_pat_)[a-z0-9_-]{16,}|'
                    r'\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)\s*[=:]\s*\S+|'
                    r'authorization\s*:\s*bearer\s+\S+)')


class JobError(ValueError):
    pass


def safe_text(text, limit=1000):
    lines = [line for line in text.splitlines() if not SECRET.search(line) and not ga._OTP_RE.search(line)]
    return ' '.join(lines)[-limit:]


def repo_access_allowed(owner_command):
    # A mere path mention, a negation, or a model-invented permission is insufficient.
    if re.search(r'(?:不要|禁止|不得|勿|不可|do not|never|don.t)[^\n。.!]{0,60}/workspace/line-re', owner_command, re.I):
        return False
    return bool(re.search(r'(?:允許修改|允許編輯|allow edits to|modify|edit|fix)\s*/workspace/line-re(?=$|[\s/，,。:：])',
                          owner_command, re.I))


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, data):
    lc.atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def process_token(pid):
    try:
        # comm may contain spaces or parentheses; fields after its final ')'.
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, ValueError, TypeError, IndexError):
        return None


def kill_agent(running):
    pid = running.get('pid')
    if not isinstance(pid, int) or pid <= 1 or running.get('pgid') != pid:
        return
    if process_token(pid) != running.get('token'):
        return  # do not signal a reused PID
    try:
        os.killpg(pid, signal.SIGTERM)
        # Also kill surviving children if the leader exits during the grace.
        time.sleep(.2)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class JobStore:
    def __init__(self, root=DEFAULT_ROOT, timeout=1800, max_concurrent=2):
        self.root = Path(root).resolve()
        repo = Path(__file__).resolve().parent.parent
        if self.root == repo or repo in self.root.parents:
            raise JobError('工作根目錄必須在 /workspace/line-re 之外')
        self.timeout = float(timeout)
        self.max_concurrent = int(max_concurrent)
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 86400 or not 1 <= self.max_concurrent <= 10:
            raise JobError('工作限制不正確')

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / '.jobs.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def control(self, jid):
        if not JOB_ID.fullmatch(jid):
            raise JobError('工作 ID 格式不正確')
        return self.root / '.control' / jid

    def path(self, jid):
        self.control(jid)
        return self.root / (jid + '.json')

    def load(self, jid):
        try:
            row = _read(self.path(jid))
        except FileNotFoundError:
            raise JobError('找不到這個工作') from None
        except (OSError, ValueError) as exc:
            raise JobError('工作紀錄無法讀取') from exc
        if (not isinstance(row, dict) or row.get('id') != jid
                or not isinstance(row.get('room'), str) or not re.fullmatch(r'c[0-9a-f]{32}', row['room'])
                or not isinstance(row.get('requester'), str) or not re.fullmatch(r'u[0-9a-f]{32}', row['requester'])
                or row.get('workdir') != str(self.root / jid)
                or row.get('status') not in TERMINAL | {'running'}
                or not isinstance(row.get('start_time'), (int, float))
                or not isinstance(row.get('timeout'), (int, float))
                or not math.isfinite(row['timeout']) or not 0 < row['timeout'] <= 86400):
            raise JobError('工作紀錄不正確')
        return row

    def rows(self):
        rows = []
        for path in sorted(self.root.glob('*.json'), reverse=True):
            if JOB_ID.fullmatch(path.stem):
                # Do not lose a running job from the concurrency calculation.
                rows.append(self.load(path.stem))
        return rows

    def _refresh(self, row):
        before = dict(row)
        control = self.control(row['id'])
        if row['status'] not in TERMINAL:
            try:
                running = _read(control / 'running.json')
                row.update(pid=running['pid'], pgid=running['pgid'])
            except FileNotFoundError:
                pass
            try:
                result = _read(control / 'exit.json')
                row.update(status=result['status'], end_time=result['end_time'], exit_code=result['exit_code'])
            except FileNotFoundError:
                if time.time() - row['start_time'] > row['timeout'] + 10:
                    if (control / 'running.json').exists():
                        kill_agent(_read(control / 'running.json'))
                    row.update(status='timed_out', end_time=time.time(), exit_code=-9)
                elif (time.time() - row['start_time'] > 3 and
                      (not row.get('worker_pid') or process_token(row['worker_pid']) != row.get('worker_token'))):
                    if (control / 'running.json').exists():
                        kill_agent(_read(control / 'running.json'))
                    row.update(status='failed', end_time=time.time(), exit_code=-1)
        if row.get('notification') == 'sending':
            row['notification'] = 'unconfirmed'  # crash after a durable delivery claim
        if row != before:
            _write(self.path(row['id']), row)
        return row

    def refresh(self):
        with self.locked():
            return [self._refresh(row) for row in self.rows()]

    def start(self, room, chat, requester, prompt, backend=DEFAULT_BACKEND, model='',
              command_id='', owner_command='', command=None):
        # command is an internal test hook, never accepted by the action parser.
        if not re.fullmatch(r'c[0-9a-f]{32}', room) or not re.fullmatch(r'u[0-9a-f]{32}', requester):
            raise JobError('工作必須來自群組及有效的使用者 MID')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise JobError('工作描述必須為 1–12000 字')
        if SECRET.search(prompt) or ga._OTP_RE.search(prompt):
            raise JobError('工作描述疑似包含憑證或驗證資訊，請移除後重試')
        if backend not in ('codex', 'cursor'):
            raise JobError('後端必須為 codex 或 cursor')
        model = model or (DEFAULT_CODEX_MODEL if backend == 'codex' else DEFAULT_CURSOR_JOB_MODEL)
        if not bot_config.MODEL_RE.fullmatch(model) or (backend == 'cursor' and not model.endswith('-fast')):
            raise JobError('工作模型格式不正確；Cursor 必須使用 fast 模型')
        # Permission comes only from the original owner command, never the Grok action.
        repo_allowed = repo_access_allowed(owner_command)
        instructions = (
            f'Work in {self.root}/<job-id> (your current directory), a new git repository. '
            'Create and test the requested deliverables there. Never read, print, or include credentials, '
            'tokens, authentication files or private keys in prompts, logs, replies or output files. '
            'Do not send LINE messages, control the desktop, or modify bot job control files. '
            + ('The owner explicitly requested work on /workspace/line-re; only the requested repo edits are allowed. '
               if repo_allowed else 'Do not read or modify /workspace/line-re or its runtime data. Keep all project changes inside your cwd. ')
            + 'End with a short summary, tests and output filenames.\n\nOwner task:\n' + prompt)
        with self.locked():
            rows = [self._refresh(row) for row in self.rows()]
            for row in rows:
                if command_id and row.get('command_id') == command_id and row['room'] == room:
                    return row
            if sum(row['status'] not in TERMINAL for row in rows) >= self.max_concurrent:
                raise JobError(f'同時最多 {self.max_concurrent} 個工作，請稍後再試')
            jid = time.strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(4)
            work = self.root / jid
            control = self.control(jid)
            work.mkdir(mode=0o700)
            control.mkdir(parents=True, mode=0o700)
            subprocess.run(['git', 'init', '-q', str(work)], timeout=5, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            instructions = instructions.replace(str(self.root) + '/<job-id>', str(work))
            (work / 'AGENTS.md').write_text(instructions.split('\n\nOwner task:')[0] + '\n')
            argv = command or (['codex', 'exec', '--dangerously-bypass-approvals-and-sandbox', '-m', model,
                                '-o', str(control / 'final.txt'), instructions] if backend == 'codex' else
                               ['cursor-agent', '-p', '--force', '--model', model, '--output-format', 'text', instructions])
            _write(control / 'command.json', dict(argv=argv, timeout=self.timeout, workdir=str(work)))
            row = dict(id=jid, room=room, chat=chat, requester=requester, prompt=prompt,
                       backend=backend, model=model, workdir=str(work), status='running',
                       start_time=time.time(), end_time=None, exit_code=None, pid=None, pgid=None,
                       timeout=self.timeout, notification='pending', command_id=command_id)
            # Write before spawn, under the store lock; a spawn failure remains inspectable.
            _write(self.path(jid), row)
            try:
                with (work / 'worker.log').open('ab', buffering=0) as log:
                    worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(control)],
                                              cwd=work, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                              start_new_session=True, close_fds=True)
                row.update(worker_pid=worker.pid, worker_token=process_token(worker.pid),
                           pid=worker.pid, pgid=worker.pid)
                _write(self.path(jid), row)
                # Reap after exit without tying worker lifetime to this thread/process.
                import threading
                threading.Thread(target=worker.wait, daemon=True).start()
            except OSError:
                row.update(status='failed', end_time=time.time(), exit_code=-1)
                _write(self.path(jid), row)
                raise
            return row

    def status(self, room, jid=''):
        rows = [row for row in self.refresh() if row['room'] == room and (not jid or row['id'] == jid)]
        if not rows:
            return '找不到這個工作' if jid else '目前沒有工作'
        return '；'.join(f"{row['id']} {row['status']} {row['backend']}/{row['model']} "
                        f"{int((row.get('end_time') or time.time()) - row['start_time'])}s" for row in rows[:5])

    def cancel(self, room, jid):
        with self.locked():
            row = self.load(jid)
            if row['room'] != room:
                raise JobError('找不到這個工作')
            row = self._refresh(row)
            if row['status'] in TERMINAL:
                return f"工作 {jid} 已是 {row['status']}"
            (self.control(jid) / 'cancel').touch()
            try:
                kill_agent(_read(self.control(jid) / 'running.json'))
            except FileNotFoundError:
                pass  # worker checks cancel before spawning and throughout supervision
            row.update(status='cancelled', end_time=time.time(), exit_code=-15)
            _write(self.path(jid), row)
            return f'已取消工作 {jid}'

    def claim_notification(self):
        with self.locked():
            for row in self.rows():
                row = self._refresh(row)
                if row['status'] in TERMINAL and row.get('notification') == 'pending':
                    row['notification'] = 'sending'
                    _write(self.path(row['id']), row)
                    return row
        return None

    def finish_notification(self, jid, ok):
        with self.locked():
            row = self.load(jid)
            row.update(notification='notified' if ok else 'unconfirmed', notified_at=time.time())
            _write(self.path(jid), row)

    def outputs(self, row):
        work = Path(row['workdir'])
        files = []
        count = 0
        for directory, dirs, names in os.walk(work, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(directory) / d).is_symlink())
            for name in sorted(names):
                count += 1
                if count > 500:
                    return files
                path = Path(directory) / name
                if (name == 'AGENTS.md' or not ga.path_allowed(str(path), [str(work)])
                        or not 0 < path.stat().st_size <= 1024 * 1024):
                    continue
                if path.suffix.lower() not in {'.py', '.md', '.txt', '.csv', '.json', '.html', '.css', '.js', '.svg'}:
                    continue
                content = path.read_text(errors='replace')
                if SECRET.search(content) or ga._OTP_RE.search(content):
                    continue
                files.append(str(path))
                if len(files) >= 8:
                    return files
        return files

    def created_files(self, row):
        """Bounded filename inventory also includes outputs too large to attach."""
        work = Path(row['workdir'])
        found, count = [], 0
        for directory, dirs, names in os.walk(work, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(directory) / d).is_symlink())
            for name in sorted(names):
                count += 1
                if count > 500:
                    return found
                path = Path(directory) / name
                if name in {'AGENTS.md', 'agent.log', 'worker.log'} or not ga.path_allowed(str(path), [str(work)]):
                    continue
                found.append(str(path.relative_to(work)))
                if len(found) >= 8:
                    return found
        return found

    def summary(self, row, files):
        control = self.control(row['id'])
        final = control / 'final.txt'
        if not final.exists():
            final = Path(row['workdir']) / 'agent.log'
        try:
            with final.open('rb') as log:
                log.seek(max(0, final.stat().st_size - 8192))
                tail = safe_text(log.read().decode(errors='replace'), 220)
        except OSError:
            tail = ''
        names = ', '.join(self.created_files(row)) or '無'
        return (f"工作 {row['id']} {row['status']}，耗時 {int((row.get('end_time') or time.time()) - row['start_time'])}s，"
                f"exit={row.get('exit_code')}。檔案：{names[:130]}。{tail}")


def run_worker(control):
    control = Path(control)
    spec = _read(control / 'command.json')
    started = time.monotonic()
    status, code = 'failed', -1
    proc = None
    try:
        if (control / 'cancel').exists():
            status, code = 'cancelled', -15
        else:
            # Authentication remains available via CLI's home files, not inherited secrets.
            env = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'TZ', 'TERM'} or k.startswith('LC_')}
            with (Path(spec['workdir']) / 'agent.log').open('ab', buffering=0) as log:
                proc = subprocess.Popen(spec['argv'], cwd=spec['workdir'], stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log, start_new_session=True, close_fds=True, env=env)
                running = dict(pid=proc.pid, pgid=proc.pid, token=process_token(proc.pid))
                _write(control / 'running.json', running)
                while True:
                    if (control / 'cancel').exists():
                        status = 'cancelled'
                        kill_agent(running)
                        code = proc.wait()
                        break
                    if time.monotonic() - started >= spec['timeout']:
                        status = 'timed_out'
                        kill_agent(running)
                        code = proc.wait()
                        break
                    code = proc.poll()
                    if code is not None:
                        status = 'completed' if code == 0 else 'failed'
                        # Clean up processes the agent left in its group.
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        break
                    time.sleep(.1)
    except Exception as exc:
        print(f'job supervisor failure: {type(exc).__name__}', flush=True)
        if proc is not None:
            kill_agent(running)
            proc.wait()
    finally:
        _write(control / 'exit.json', dict(status=status, exit_code=code, end_time=time.time()))


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--worker':
    run_worker(sys.argv[2])
