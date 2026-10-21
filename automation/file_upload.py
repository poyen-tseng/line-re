"""Bounded file upload through LINE's own E2EE/media pipeline.

Only the picker selection holds the shared UI lock. Copies are staged on C:
inside the existing Wine prefix because the Flatpak cannot see /workspace.
Nothing injects code, extracts credentials, or replays authenticated traffic.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time

import bot_config
import group_actions as ga
import line_common as lc
import send_message as sm

DEFAULT_SELF = bot_config.DEFAULT_BOT_MID


def default_file_roots():
    """Search roots: /workspace and the current user's home directory."""
    home = os.path.expanduser('~')
    roots = ['/workspace']
    if home and home not in roots:
        roots.append(home)
    return roots


def configured_self_mid():
    """Bot mid from bot_config.json / LINE_BOT_MID, or a placeholder."""
    try:
        return bot_config.BotConfig().bot_mid()
    except bot_config.ConfigError:
        return DEFAULT_SELF
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 60.0

@dataclass
class Result:
    status: str = 'fail'
    detail: str = ''
    committed: bool = False
    message_id: str = ''
    content_type: int = 0
    size: int = 0
    path: str = ''
    uploaded_name: str = ''

    @property
    def rc(self):
        return 0 if self.status in ('ok', 'probe') else (6 if self.committed else 4)


def prefix_for_pid(pid):
    with open(f'/proc/{pid}/environ', 'rb') as f:
        for entry in f.read().split(b'\0'):
            if entry.startswith(b'WINEPREFIX='):
                prefix = os.fsdecode(entry.split(b'=', 1)[1])
                if os.path.isabs(prefix) and os.path.isdir(os.path.join(prefix, 'drive_c')):
                    return prefix
    raise OSError('LINE Wine prefix unavailable')


def windows_path(path, prefix):
    """Map a host path on C: or Z: using actual Windows separators."""
    path = os.path.abspath(path)
    drive = os.path.join(os.path.abspath(prefix), 'drive_c')
    if os.path.commonpath((path, drive)) == drive:
        return 'C:\\' + os.path.relpath(path, drive).replace('/', '\\')
    return 'Z:' + path.replace('/', '\\')


@contextmanager
def staged_file(path, pid, max_bytes):
    """Immutable bounded copy on the drive visible inside LINE's mount namespace."""
    prefix = prefix_for_pid(pid)
    root = Path(prefix, 'drive_c', 'line-uploads')
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise OSError('upload staging directory is a symlink')
    stage = Path(tempfile.mkdtemp(prefix='upload-', dir=root))
    item = None
    try:
        name = Path(path).name
        reserved = {'con','prn','aux','nul'} | {f'{p}{n}' for p in ('com','lpt') for n in range(1,10)}
        if (len(name.encode('utf-16-le')) > 240 or name.split('.',1)[0].casefold() in reserved
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((' ', '.'))):
            name = 'upload' + re.sub(r'[^a-zA-Z0-9.]', '', Path(path).suffix)[:20]
        dest = stage / name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as src:
            info = os.fstat(src.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise OSError('file changed or exceeds configured size limit')
            with dest.open('xb') as out:
                copied = 0
                while True:
                    chunk = src.read(min(1024 * 1024, max_bytes - copied + 1))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise OSError('file grew beyond configured size limit')
                    out.write(chunk)
            after = os.fstat(src.fileno())
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError('source file changed while staging')
        # The caller retains uncertain uploads: LINE may still read this copy.
        item = dict(path=str(dest), wine=windows_path(str(dest), prefix), size=copied, retain=False)
        yield item
    finally:
        if not item or not item['retain']:
            shutil.rmtree(stage, ignore_errors=True)


def media_candidates(messages, self_mid, room, since_ms, before, filename, size):
    """Only complete new, server-ID rows; filename/size correlation is mandatory.

    IMAGE/VIDEO may omit FILE_NAME. Original content must still match its
    size and service ID. Transformed/ambiguous records remain uncertain.
    """
    suffix = Path(filename).suffix.lower()
    # This picker sends MP4 as original FILE=14, but PNG as IMAGE=1.
    expected = {14, 5, 3} | ({1} if suffix in {'.png','.jpg','.jpeg','.gif','.bmp','.webp',
                                           '.heic','.heif','.avif','.tif','.tiff'} else
                            {2} if suffix in {'.mp4','.mov'} else set())
    candidates = []
    for mid, row in messages.items():
        if (mid in before or not re.fullmatch(r'[0-9]{6,24}', str(mid))
                or row.get('from') != self_mid or row.get('to') != room
                or (row.get('createdTime') or 0) < since_ms
                or row.get('contentType') not in expected):
            continue
        try:
            meta = json.loads(row.get('contentMeta') or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(meta, dict):
            continue
        service = {1:'emi',2:'emv',3:'ema',5:'emf',14:'emf'}.get(row.get('contentType'))
        if not meta.get('OID') or meta.get('SID') != service:
            continue
        name = meta.get('FILE_NAME') or meta.get('fileName') or row.get('text')
        if name and name != Path(filename).name:
            continue
        if row.get('contentType') in {14,5,3}:
            if name != Path(filename).name:
                continue
            try:
                if int(meta.get('FILE_SIZE', -1)) != size:
                    continue
            except (ValueError, TypeError):
                continue
        else:
            # Native media records have no filename. Bind original content
            # through its size too; never accept an arbitrary new image.
            try:
                if int(meta.get('FILE_SIZE', -1)) != size:
                    continue
            except (ValueError, TypeError):
                continue
        # Local pending UUIDs cannot satisfy the numeric server ID check above.
        candidates.append(row)
    return candidates


def verify(pid, self_mid, room, since_ms, before, filename, size, timeout):
    deadline = time.monotonic() + timeout
    while True:
        if lc.find_line_pid(pid) != pid:
            return None
        messages, _, _, _ = lc.scan_memory(pid)
        hits = media_candidates(messages, self_mid, room, since_ms, before, filename, size)
        if len(hits) == 1:
            return hits[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(1.0, max(0, deadline - time.monotonic())))


def _log(event, result, room, started):
    print(f'[file] {event} room={room} path={result.path!r} size={result.size} '
          f'method=ui-staged dur={time.monotonic()-started:.2f}s '
          f'msgid={result.message_id or "-"} status={result.status} '
          f'detail={result.detail!r}', file=sys.stderr, flush=True)


def send(args):
    started = time.monotonic()
    room = getattr(args, 'room', '')
    result = Result(path=getattr(args, 'file_path', '') or '')
    ctx = None
    try:
        timeout = float(getattr(args, 'file_timeout', DEFAULT_TIMEOUT))
        if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError(f'upload timeout must be >0 and <= {MAX_TIMEOUT:g}s')
        max_mb = getattr(args, 'max_file_mb', 200)
        path, err = ga.resolve_send_file(result.path, getattr(args, 'file_roots', None) or default_file_roots(), max_mb)
        if err:
            result.detail = err
            return result
        result.path = path
        result.size = os.stat(path).st_size
        _log('start', result, room, started)
        # One in-flight file verifier per account, without excluding text/scans.
        with sm._ui_lock(timeout=2, name='upload'):
            pid = lc.find_line_pid()
            if not pid:
                raise OSError('LINE is not running')
            with staged_file(path, pid, int(max_mb * 1024 * 1024)) as staged:
                result.size = staged['size']
                result.uploaded_name = Path(staged['path']).name
                before, _, _, _ = lc.scan_memory(pid)
                with sm._ui_lock():
                    # Dismiss previous pickers before search/focus work.
                    display = getattr(args, 'display', None) or lc.find_line_display()
                    picked = sm.pick_main_window(display) if display else None
                    if picked:
                        sm._dismiss_file_dialogs(display, picked[0])
                    args.reuse_open = True
                    args.route_timeout = 15
                    ctx, error = sm._prepare_chat(args)
                    if error:
                        result.detail = f'cannot open verified room (rc={error})'
                        return result
                    if ctx['pid'] != pid:
                        raise OSError('LINE changed before file selection')
                    room = ctx['target']
                    if args.dry_run:
                        result.status = 'probe'
                        result.detail = 'dry-run; picker not opened'
                        return result
                    try:
                        if not sm._route_matches(ctx):
                            raise OSError('room changed before attach')
                        windows_before = {w for w, _ in sm._line_class_windows(ctx['display'])}
                        sm.click_relative(ctx['display'], ctx['wid'], sm.ATTACH_REL)
                        # The callback marks the last reversible boundary.
                        def commit():
                            result.committed = True
                            staged['retain'] = True
                        since = int(time.time() * 1000)
                        accepted = sm._fill_file_dialog(ctx['display'], ctx['wid'], staged['wine'],
                            getattr(args,'file_commit',True), pid, room, on_commit=commit)
                        if not accepted:
                            result.status = 'uncertain' if result.committed else 'fail'
                            result.detail = 'picker rejected file or did not close; no automatic resend'
                            return result
                        if not getattr(args, 'file_commit', True):
                            result.status = 'probe'
                            result.detail = 'picker cancelled without sending'
                            return result
                        prompts = sm._attachment_modals(ctx['display'], ctx['wid'], windows_before)
                        if prompts:
                            sm._cancel_attachment_modals(ctx['display'], prompts)
                            result.status = 'uncertain'
                            result.detail = 'unsupported attachment confirmation/limit prompt cancelled; no automatic resend'
                            return result
                    finally:
                        sm._dismiss_file_dialogs(ctx['display'], ctx['wid'])
                row = verify(pid, getattr(args, 'self_mid', None) or configured_self_mid(), room, since,
                             set(before), staged['path'], staged['size'], timeout)
                if row:
                    result.status = 'ok'
                    result.message_id = row['id']
                    result.content_type = row['contentType']
                    result.detail = 'new self attachment in requested room with server message ID'
                    staged['retain'] = False
                else:
                    result.status = 'uncertain'
                    result.detail = 'upload not confirmed before timeout; do not resend automatically'
    except (OSError, ValueError, TimeoutError, sm.subprocess.SubprocessError) as exc:
        result.status = 'uncertain' if result.committed else 'fail'
        result.detail = str(exc)
    finally:
        _log('ok' if result.status in ('ok','probe') else 'fail', result, room, started)
    return result
