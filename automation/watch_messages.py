#!/usr/bin/env python3
"""watch_messages.py — Real-time new-message watcher for the user's own LINE
Desktop history. Reuses the WORKING memory-scan approach from `line_common.py`
(scan /proc/<LINE_pid>/mem for decrypted SQLite _message leaf pages; mids via
_contact). The on-disk .edb is encrypted SQLite (key inside Themida LINE.exe),
so we do NOT touch the file — only the live process memory.

The shared memory-scan / PID-autodetect / SQLite-parser / formatting code
lives in `line_common.py`; this script keeps only the watch-specific
state management, --exec runner, and the polling loop.

Usage (see also --help):
  ./watch_messages.py                              # silent baseline, then watch
  ./watch_messages.py --interval 2 --backfill 3    # show 3 baseline msgs first
  ./watch_messages.py --json                       # one JSON object per new msg
  ./watch_messages.py --state-file ~/.line-seen    # persist seen ids
  ./watch_messages.py --exec 'notify-send "$LINE_MSG_FROM: $LINE_MSG_TEXT"'
  ./watch_messages.py --once                       # single scan, debug
"""
import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
import threading

import line_common as lc
import reply_context


# ---------------------------------------------------------------------------
# State persistence (seen ids)
# ---------------------------------------------------------------------------

class StateError(OSError):
    """Stop startup instead of treating unreadable handled IDs as new commands."""


class HandledState(set):
    """Handled IDs plus reaction claims, saved together under one thread lock.

    Legacy newline files remain readable. The group bot opts into version 2
    before starting its reaction worker; other watchers retain their format.
    """
    def __init__(self, ids=(), reactions=None, structured=False):
        super().__init__(ids)
        self.reactions = reactions or {}
        self.structured = structured
        self.lock = threading.RLock()


def load_state(path):
    """Load seen ids from state file. Returns a set of ids, or empty set."""
    seen = HandledState()
    if not path:
        return seen
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
            if raw.lstrip().startswith('{'):
                try:
                    obj = json.loads(raw)
                    ids = obj['handled']
                    reactions = obj['reactions']
                    if (obj.get('version') != 2 or not isinstance(ids, list)
                            or any(not isinstance(i, str) or not i for i in ids)
                            or not isinstance(reactions, dict)):
                        raise ValueError('invalid state envelope')
                    for mid, item in reactions.items():
                        if (not isinstance(mid, str) or not mid
                                or not isinstance(item, dict)
                                or item.get('status') not in
                                ('pending', 'ok', 'failed', 'uncertain')
                                or type(item.get('attempts')) is not int
                                or not 1 <= item['attempts'] <= 3
                                or not isinstance(item.get('room'), str)):
                            raise ValueError('invalid reaction claim')
                except (ValueError, KeyError, TypeError) as exc:
                    raise StateError(f'invalid state file {path}: {exc}') from exc
                return HandledState(ids, reactions, structured=True)
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    seen.add(line)
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as e:
        raise StateError(f"could not read state file {path}: {e}") from e
    return seen


def save_state(path, seen):
    """Persist seen ids to state file (atomic-ish: write tmp + rename)."""
    if not path:
        return True
    try:
        if isinstance(seen, HandledState):
            with seen.lock:
                if seen.structured:
                    data = json.dumps(dict(version=2, handled=sorted(seen),
                                           reactions=seen.reactions),
                                      ensure_ascii=False, sort_keys=True) + '\n'
                else:
                    data = "".join(mid + "\n" for mid in sorted(seen))
                lc.atomic_write_text(path, data)
        else:
            lc.atomic_write_text(path, "".join(mid + "\n" for mid in sorted(seen)))
        return True
    except OSError as e:
        print(f"[watch] WARN: could not write state file {path}: {e}",
              file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def emit_message(m, contacts, args):
    """Emit one new message in either text or --json mode, and run --exec
    if configured."""
    frm = lc.display_name(m["from"], contacts)
    to = lc.display_name(m["to"], contacts)
    ct = m["contentType"]
    text = lc.truncate(m["text"], 200)
    if text is None:
        text = f"[{lc.fmt_content_type(ct)}]"
    ts = lc.fmt_time(m["createdTime"])

    if args.json:
        obj = {
            "id": m["id"],
            "time": ts,
            "createdTime": m["createdTime"],
            "from": m["from"],
            "from_name": frm,
            "to": m["to"],
            "to_name": to,
            "type": lc.fmt_content_type(ct),
            "contentType": ct,
            "text": m["text"],
            **reply_context.json_fields(m, contacts),
        }
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(
            f"[{ts}] {frm} → {to} ({lc.fmt_content_type(ct)})\n"
            f"    {text}\n"
        )
        quote = reply_context.text_line(m, contacts)
        if quote:
            sys.stdout.write(f"    {quote}\n")
    sys.stdout.flush()

    if args.exec_cmd:
        run_exec(args.exec_cmd, m, frm, to, ts, ct, args.exec_timeout)


def run_exec(cmd, m, frm, to, ts, ct, timeout):
    """Run the user-supplied shell command with LINE_MSG_* env vars.
    Command is run via /bin/sh -c (shell=True). Killed after `timeout`
    seconds. Failures are logged to stderr but do not stop the watcher."""
    env = os.environ.copy()
    env["LINE_MSG_ID"] = m["id"] or ""
    env["LINE_MSG_TEXT"] = m["text"] or ""
    env["LINE_MSG_FROM"] = frm or ""
    env["LINE_MSG_TO"] = to or ""
    env["LINE_MSG_TIME"] = ts or ""
    env["LINE_MSG_TYPE"] = lc.fmt_content_type(ct)
    try:
        result = subprocess.run(
            cmd, shell=True, env=env, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode:
            print(f"[watch] WARN: --exec exited {result.returncode}: {cmd!r}",
                  file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"[watch] WARN: --exec timed out after {timeout}s: {cmd!r}",
              file=sys.stderr)
    except Exception as e:
        print(f"[watch] WARN: --exec failed: {type(e).__name__}: {e}",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

class GracefulExit:
    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGINT, self._h)
        signal.signal(signal.SIGTERM, self._h)

    def _h(self, signum, frame):
        self.stop = True


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Watch LINE Desktop memory for new messages and emit them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--interval", type=float, default=3.0,
                    help="Poll interval in seconds (default 3)")
    ap.add_argument("--backfill", type=int, default=0,
                    help="On startup, print the N most-recent baseline "
                         "messages before watching. Default 0 (silent "
                         "baseline — only genuinely new messages are shown).")
    ap.add_argument("--state-file", default=None,
                    help="Path to persist seen message _ids across restarts. "
                         "One id per line. If set, ids already in the file at "
                         "startup are treated as already-seen (no backfill of "
                         "those).")
    ap.add_argument("--json", action="store_true",
                    help="Emit one JSON object per new message per line.")
    ap.add_argument("--exec", dest="exec_cmd", default=None,
                    help='Shell command to run per new message. Env vars '
                         'LINE_MSG_TEXT, LINE_MSG_FROM, LINE_MSG_TO, '
                         'LINE_MSG_TIME, LINE_MSG_ID, LINE_MSG_TYPE are set. '
                         'Example: --exec \'notify-send "$LINE_MSG_FROM: '
                         '$LINE_MSG_TEXT"\'')
    ap.add_argument("--exec-timeout", type=float, default=30.0,
                    help="Timeout in seconds for --exec commands (default 30)")
    ap.add_argument("--pid", type=int, default=None,
                    help="LINE.exe PID (auto-detected if omitted). If the pid "
                         "disappears, the watcher re-detects.")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress the per-poll status line on stderr.")
    ap.add_argument("--once", action="store_true",
                    help="Run a single scan and exit (debugging).")
    return ap.parse_args(argv)


def log(args, msg):
    """Log to stderr (so stdout stays clean for piping)."""
    if not args.quiet:
        print(msg, file=sys.stderr)


def do_scan(pid, args):
    """Run one scan; return (messages, contacts, scanned, matched) or
    ({}, {}, 0, 0) if pid unreadable."""
    try:
        args._scan_failed = False
        return lc.scan_memory(pid)
    except OSError as e:
        args._scan_failed = True
        log(args, f"[watch] scan failed for pid {pid}: {e}")
        return {}, {}, 0, 0


def establish_baseline(pid, args, seen):
    """Scan once, populate `seen` with all current message ids, optionally
    print the N most-recent as backfill. Returns contacts dict."""
    log(args, f"[watch] establishing baseline from pid {pid} ...")
    messages, contacts, scanned, matched = do_scan(pid, args)
    args._baseline_pending = getattr(args, '_scan_failed', False)
    log(args, f"[watch]   pages scanned={scanned} matched={matched} "
              f"messages={len(messages)} contacts={len(contacts)}")
    if not messages:
        log(args, "[watch]   no messages recovered yet — open a chat in LINE "
                  "to warm the cache; will keep polling.")
        return contacts
    # backfill: print N most-recent
    if args.backfill > 0:
        msgs = sorted(messages.values(),
                       key=lambda x: x["createdTime"], reverse=True)
        backfill = [m for m in msgs[:args.backfill] if m['id'] not in seen]
        # oldest first, so the stream reads chronologically
        backfill.reverse()
        log(args, f"[watch]   backfilling {len(backfill)} most-recent "
                  f"baseline messages:")
        for m in backfill:
            emit_message(m, contacts, args)
    # mark all as seen
    seen.update(messages.keys())
    return contacts


def poll_once(pid, args, seen, contacts):
    """Run one poll. Emit any new messages. Update seen + contacts.
    Returns number of new messages emitted."""
    messages, new_contacts, scanned, matched = do_scan(pid, args)
    if new_contacts:
        contacts.update(new_contacts)
    new = 0
    if not messages:
        return 0
    # only consider ids we haven't seen
    new_ids = [mid for mid in messages if mid not in seen]
    if not new_ids:
        return 0
    # sort by createdTime ascending so emission is chronological
    new_ids.sort(key=lambda mid: messages[mid]["createdTime"])
    for mid in new_ids:
        emit_message(messages[mid], contacts, args)
        seen.add(mid)
        new += 1
    return new


def run(args):
    """Run the watcher with a parsed argparse Namespace. Returns exit code."""
    if (not math.isfinite(args.interval) or args.interval < 0 or args.backfill < 0
            or not math.isfinite(args.exec_timeout) or args.exec_timeout <= 0):
        print("ERROR: interval/backfill must be nonnegative; exec timeout must be positive and finite.", file=sys.stderr)
        return 2
    gx = GracefulExit()

    seen = load_state(args.state_file)
    if args.state_file and seen:
        log(args, f"[watch] loaded {len(seen)} seen-ids from {args.state_file}")

    pid = lc.find_line_pid(args.pid)
    if pid is None:
        log(args, "[watch] LINE.exe not running; waiting for it to start...")
    else:
        log(args, f"[watch] watching LINE pid {pid} "
                  f"(interval={args.interval}s)")

    # establish baseline (silent unless --backfill)
    if pid is not None:
        contacts = establish_baseline(pid, args, seen)
        save_state(args.state_file, seen)
    else:
        contacts = {}

    if args.once:
        return 0

    last_pid = pid
    while not gx.stop:
        # sleep first (we just baseline-scanned), but check stop flag
        slept = 0.0
        while slept < args.interval and not gx.stop:
            time.sleep(min(0.25, args.interval - slept))
            slept += 0.25
        if gx.stop:
            break

        # re-detect pid if needed
        pid = lc.find_line_pid(last_pid)
        if pid is None:
            if last_pid is not None:
                log(args, "[watch] LINE pid gone; re-detecting...")
                last_pid = None
            # wait for LINE to come back
            continue
        if pid != last_pid:
            log(args, f"[watch] LINE pid changed {last_pid} -> {pid}; "
                      "re-baselining (silent).")
            last_pid = pid
            # re-baseline silently so we don't dump the entire existing
            # history as "new"
            restart_args = argparse.Namespace(**vars(args))
            restart_args.backfill = 0
            contacts = establish_baseline(pid, restart_args, seen)
            args._baseline_pending = restart_args._baseline_pending
            save_state(args.state_file, seen)
            continue

        # normal poll
        if getattr(args, '_baseline_pending', False):
            contacts = establish_baseline(pid, args, seen)
            save_state(args.state_file, seen)
            continue
        try:
            new = poll_once(pid, args, seen, contacts)
        except OSError as e:
            log(args, f"[watch] poll error: {e}; will re-detect pid")
            last_pid = None
            continue
        if new:
            save_state(args.state_file, seen)

    log(args, "[watch] exiting; saving state...")
    save_state(args.state_file, seen)
    return 0


def main():
    try:
        return run(parse_args())
    except StateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
