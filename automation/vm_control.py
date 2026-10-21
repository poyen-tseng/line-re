"""Bounded desktop capture; the verified uploader owns its own UI lock."""
from pathlib import Path
import os
import re
import secrets
import shutil
import subprocess
import time

import send_message


def live_displays(timeout=2):
    displays = []
    deadline = time.monotonic() + timeout
    for socket in sorted(Path('/tmp/.X11-unix').glob('X*')):
        if not re.fullmatch(r'X\d{1,3}', socket.name):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        display = ':' + socket.name[1:]
        try:
            proc = subprocess.run(['xdpyinfo', '-display', display], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=min(.25, remaining))
            if proc.returncode == 0:
                displays.append(display)
        except (OSError, subprocess.SubprocessError):
            pass
    return displays


def capture(root, display=':3'):
    if not isinstance(display, str) or not re.fullmatch(r':\d{1,3}(?:\.\d)?', display):
        raise ValueError('顯示器格式應為 :3 或 :1')
    # Validate the requested screen directly as enumeration has a global budget.
    probe = subprocess.run(['xdpyinfo', '-display', display], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2)
    if probe.returncode:
        raise ValueError('顯示器無法連線；可用：' + '、'.join(live_displays()))
    if not shutil.which('import'):
        raise ValueError('找不到 ImageMagick import')
    directory = Path(root) / 'screenshots'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (time.strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(4) + '.png')
    try:
        with send_message._ui_lock(timeout=5):
            subprocess.run(['import', '-display', display, '-window', 'root', str(path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=8, check=True,
                           env=dict(os.environ, DISPLAY=display))
        if path.stat().st_size < 8 or path.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError('擷取結果不是 PNG')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return str(path)
