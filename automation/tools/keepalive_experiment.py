#!/usr/bin/env python3
"""Outside-sandbox opt-in test: restart LINE with core-only TCP keepalive.

Never sends LINE messages; leaves the bot and its restart fallback running.
No global sysctls, Wine registry or persistent launch settings are changed.
The ordinary watchdog/launcher removes the shim on the next normal restart.
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import line_common as lc
import sync_watchdog as wd


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--restart-line-with-shim', action='store_true', required=True,
                    help='explicitly restart visible LINE; never use inside the PID sandbox')
    ap.add_argument('--display', default=':3')
    args = ap.parse_args(argv)
    pid = lc.find_line_pid()
    if pid is None:
        ap.error('no visible LINE PID; run outside the Codex PID sandbox')
    library = HERE / 'libline_keepalive.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror',
                    '-o', str(library), str(HERE / 'line_keepalive.c'), '-ldl'],
                   check=True, timeout=30)
    # Flatpak removes host LD_PRELOAD. Set it explicitly inside Flatpak and
    # mount only this tools directory read-only so the runtime can find it.
    cmd = ['flatpak', 'run', f'--filesystem={HERE}:ro',
           f'--env=LD_PRELOAD={library}', '--command=bottles-cli',
           'com.usebottles.bottles', 'run', '-b', 'LINE', '-p', 'LINE']
    script = ('#!/bin/bash\nset -e\n. /workspace/line-bottles/env.sh\n'
              'nohup ' + shlex.join(cmd)
              + ' >> /workspace/line-bottles/line-run.log 2>&1 &\n')
    fd, path = tempfile.mkstemp(prefix='line-keepalive-launch-', suffix='.sh')
    try:
        with os.fdopen(fd, 'w') as launcher:
            launcher.write(script)
        original = wd.OPEN_LINE
        wd.OPEN_LINE = path
        try:
            if not wd.restart_line(args.display, pid=pid):
                raise RuntimeError('LINE launch failed; use the ordinary open-line.sh')
        finally:
            wd.OPEN_LINE = original
    finally:
        os.unlink(path)
    print('Launcher invoked. Verify actual injection before drawing conclusions:')
    print('  [line-keepalive] configured lines in /workspace/line-bottles/line-run.log')
    print('  libline_keepalive.so in the actual LINE/Wine /proc/<pid>/maps')
    print('  timer:(keepalive,...) on 147.92/43.223:443 in ss -tnoie')
    print('Then run tools/socket_experiment.py --duration 900 --interval 2;')
    print('check sustained core ESTAB and new incoming memory timestamps without sending.')


if __name__ == '__main__':
    main()
