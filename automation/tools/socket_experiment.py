#!/usr/bin/env python3
"""Observe socket state + memory metadata; never sends LINE messages.

Run outside the Codex PID sandbox for PID ownership/memory and optional
--nudge-closewait. Does not restart LINE or the bot. Default is read-only.
Output is JSONL (no message bodies); redirect it to a private, ignored log.
"""
import argparse
from datetime import datetime
import ipaddress
import json
import math
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import line_common as lc
import sync_watchdog as wd

FILTER = '( dst 147.92.0.0/16 or dst 43.223.0.0/16 )'


def proc_core_tcp():
    """Raw kernel state/inode cross-check, filtered to LINE's IPv4 core nets."""
    try:
        rows = []
        nets = [ipaddress.ip_network(net) for net in wd.DEFAULT_NETS]
        for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
            peer = line.split()[2].split(':')[0]
            ip = ipaddress.ip_address(bytes.fromhex(peer)[::-1])
            if any(ip in net for net in nets):
                rows.append(line.strip())
        return rows
    except (OSError, ValueError, IndexError) as exc:
        return {'error': str(exc)}


def capture(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return dict(rc=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return dict(error=str(exc))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--duration', type=float, default=900)
    ap.add_argument('--interval', type=float, default=2)
    ap.add_argument('--pid', type=int)
    ap.add_argument('--memory-every', type=float, default=30,
                    help='memory metadata interval; 0 disables')
    ap.add_argument('--nudge-closewait', action='store_true',
                    help='outside sandbox only: one sudo -n ss -K nudge per PID')
    ap.add_argument('--nudge-after', type=float, default=40,
                    help='seconds without core ESTAB before a nudge')
    args = ap.parse_args(argv)
    for name in ('duration', 'interval', 'memory_every', 'nudge_after'):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name == 'interval' and value == 0):
            ap.error(f'{name} must be finite and nonnegative (interval > 0)')
    print(json.dumps({'environment': {
        name: Path('/proc/sys/' + name.replace('.', '/')).read_text().strip()
        for name in ('net.ipv4.tcp_keepalive_time', 'net.ipv4.tcp_keepalive_intvl',
                     'net.ipv4.tcp_keepalive_probes')},
        'route': capture(['ip', '-4', 'route']),
        'note': 'No LINE message sending or process restart; memory summary only.'}), flush=True)
    nets = [ipaddress.ip_network(n) for n in wd.DEFAULT_NETS]
    start = time.monotonic()
    last_mem = -float('inf')
    last_pid = None
    stalled_at = None
    nudged = set()
    previous_ids = set()
    while time.monotonic() - start <= args.duration:
        now = time.monotonic()
        pid = lc.find_line_pid(args.pid or last_pid)
        row = {'time': datetime.now().astimezone().isoformat(), 'pid': pid,
               'sockets': capture(['ss', '-tnoie', 'state', 'all', FILTER]),
               'proc_net_tcp': proc_core_tcp()}
        if pid != last_pid:
            stalled_at = None
            last_mem = -float('inf')
            last_pid = pid
        if args.nudge_closewait and pid is None:
            ap.error('nudge needs a visible LINE PID; run outside the PID sandbox')
        if args.nudge_closewait:
            states = wd.line_conn_states(pid, nets)
            if states is None or states['core_estab']:
                stalled_at = None
            else:
                if stalled_at is None:
                    stalled_at = now
                if states['core_closewait'] and pid not in nudged and now - stalled_at >= args.nudge_after:
                    nudged.add(pid)
                    row['nudge_command_attempted'] = wd.destroy_core_closewait(pid, nets)
                    row['note'] = 'Socket kill is not proof of recovery; check subsequent sockets and memory.'
        if pid and args.memory_every and now - last_mem >= args.memory_every:
            last_mem = now
            try:
                messages, contacts, scanned, matched = lc.scan_memory(pid)
                ids = set(messages)
                row['memory'] = dict(messages=len(ids), new_ids=len(ids - previous_ids),
                                     newest_created_ms=max((m['createdTime'] for m in messages.values()), default=None),
                                     pages=scanned, matched=matched, contacts=len(contacts))
                previous_ids.update(ids)
            except OSError as exc:
                row['memory'] = {'error': str(exc)}
        print(json.dumps(row), flush=True)
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
