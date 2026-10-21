#!/usr/bin/env python3
"""
read_messages.py — Read recent messages from the user's own LINE Desktop history.

Two strategies are attempted, in order:

  Strategy 1 (instructed, FAILS): copy the live `.edb` to a temp file and parse
  it with dissect.esedb / libesedb / sqlite3. This fails because LINE Desktop
  stores its message database as a **SQLite** file (NOT ESE/JET Blue as the
  earlier REPORT.md guessed) with **full-content encryption** applied by LINE.
  The standard SQLite magic `SQLite format 3\0` is replaced with a custom
  8-byte marker (`1a c6 04 c9 14 d5 2c 3a` + 8 bytes that vary), and every
  page (including the header page) is encrypted. dissect.esedb rejects it
  ("invalid file header signature"); libesedb rejects it ("unsupported file
  signature"); sqlite3, even after restoring the magic, rejects it
  ("unsupported file format" / "database disk image is malformed") because
  the schema-format and page-count fields are also encrypted. The encryption
  key is held only inside the Themida-protected LINE.exe process, so this
  path is a hard blocker without reverse-engineering the codec.

  Strategy 2 (WORKS): while LINE is running, SQLite holds the **decrypted**
  page cache in its process heap. We scan `/proc/<pid>/mem` for 4096-byte
  SQLite leaf-table b-tree pages (page type 0x0d) that look like `_message`
  table pages, parse the cells with a minimal SQLite record parser, and print
  the most recent messages sorted by `_createdTime`. We also pick up
  `_contact` pages to map mids to display names. Read-only; does not touch
  LINE.exe.

  The shared memory-scan / PID-autodetect / SQLite-parser / formatting
  code lives in `line_common.py`; this script keeps only the on-disk
  file-parse demo (strategy 1) and the read-specific CLI / output shaping.

Usage:
  ./read_messages.py                       # print 20 most recent messages
  ./read_messages.py --limit 50            # print 50 most recent
  ./read_messages.py --chat "Alice"         # filter by chat display name
  ./read_messages.py --mid <user-mid>      # filter by chat mid
  ./read_messages.py --json                # one JSON object per message
  ./read_messages.py --try-file            # also run strategy 1 to show it fails
  ./read_messages.py --pid 1373692         # explicit LINE PID

Requirements: Python 3.8+, `dissect.esedb` (in `.venv`) for strategy 1.
Strategy 2 only needs the Python stdlib + read access to `/proc/<pid>/mem`
(same user as LINE).
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from contextlib import closing

import line_common as lc
import reply_context


# ---------------------------------------------------------------------------
# Strategy 1: try to parse the on-disk .edb (documents the blocker)
# ---------------------------------------------------------------------------

def _default_db_dir():
    override = os.environ.get("LINE_DB_DIR", "").strip()
    if override:
        return override
    user = os.environ.get("USER") or "user"
    return os.path.join(
        os.path.expanduser("~"),
        ".var/app/com.usebottles.bottles/data/bottles/bottles/LINE",
        "drive_c", "users", user, "AppData", "Local", "LINE", "Data", "db")


LINE_DB_DIR = _default_db_dir()


def try_file_parse(verbose=False):
    """Copy the active .edb (+ -wal + -shm) to temp and try every parser we
    have. Returns a list of (parser_name, result_string). All currently fail
    because LINE encrypts the SQLite content; we record the exact errors."""
    results = []
    if not os.path.isdir(LINE_DB_DIR):
        results.append(("ls", f"DB dir not found: {LINE_DB_DIR}"))
        return results
    # Prefer a WAL-backed/current database to a larger stale account DB.
    edbs = []
    for f in os.listdir(LINE_DB_DIR):
        if f.startswith("qw") and f.endswith(".edb"):
            p = os.path.join(LINE_DB_DIR, f)
            try:
                edbs.append((os.path.exists(p + "-wal"), os.path.getmtime(p), os.path.getsize(p), p))
            except OSError:
                pass
    if not edbs:
        results.append(("ls", "no qw*.edb files"))
        return results
    edbs.sort(reverse=True)
    main_edb = edbs[0][3]
    tmpdir = tempfile.mkdtemp(prefix="line_edb_")
    try:
        base = os.path.basename(main_edb)
        shutil.copy2(main_edb, os.path.join(tmpdir, base))
        for ext in (".edb-wal", ".edb-shm"):
            src = main_edb + ext.replace(".edb", "")
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmpdir, base + ext.replace(".edb", "")))
        target = os.path.join(tmpdir, base)
        results.append(("copy", f"copied {main_edb} -> {target}"))

        # 1a. dissect.esedb
        try:
            from dissect.esedb import EseDB
            with open(target, "rb") as stream:
                EseDB(stream)
            results.append(("dissect.esedb", "opened (unexpected)"))
        except Exception as e:
            results.append(("dissect.esedb", f"{type(e).__name__}: {e}"))

        # 1b. libesedb (pyesedb)
        try:
            import pyesedb
            f = pyesedb.file()
            try:
                f.open(target)
            finally:
                f.close()
            results.append(("pyesedb/libesedb", "opened (unexpected)"))
        except Exception as e:
            results.append(("pyesedb/libesedb", f"{type(e).__name__}: {e}"))

        # 1c. sqlite3 raw
        import sqlite3
        try:
            with closing(sqlite3.connect(target)) as con:
                con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            results.append(("sqlite3 raw", "opened (unexpected)"))
        except Exception as e:
            results.append(("sqlite3 raw", f"{type(e).__name__}: {e}"))

        # 1d. sqlite3 with restored magic
        try:
            fixed = os.path.join(tmpdir, base + ".fixed.sqlite")
            with open(target, "rb") as stream:
                d = bytearray(stream.read())
            d[0:16] = b"SQLite format 3\x00"
            with open(fixed, "wb") as stream:
                stream.write(d)
            with closing(sqlite3.connect(fixed)) as con:
                con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            results.append(("sqlite3 magic-restored", "opened (unexpected)"))
        except Exception as e:
            results.append(("sqlite3 magic-restored", f"{type(e).__name__}: {e}"))
    except OSError as exc:
        results.append(("copy", f"{type(exc).__name__}: {exc}"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------

def message_to_json_obj(m, contacts):
    """Build the JSON object for one message, matching watch_messages.py."""
    return {
        "id": m["id"],
        "time": lc.fmt_time(m["createdTime"]),
        "createdTime": m["createdTime"],
        "from": m["from"],
        "from_name": lc.display_name(m["from"], contacts),
        "to": m["to"],
        "to_name": lc.display_name(m["to"], contacts),
        "type": lc.fmt_content_type(m["contentType"]),
        "contentType": m["contentType"],
        "text": m["text"],
        **reply_context.json_fields(m, contacts),
    }


def emit_text(m, contacts):
    """Print one message in the human-readable text format."""
    frm = lc.display_name(m["from"], contacts)
    to = lc.display_name(m["to"], contacts)
    ct = m["contentType"]
    text = lc.truncate(m["text"], 200)
    if text is None:
        text = f"[{lc.fmt_content_type(ct)}]"
    print(f"[{lc.fmt_time(m['createdTime'])}] {frm} → {to} "
          f"({lc.fmt_content_type(ct)})")
    print(f"    {text}")
    quote = reply_context.text_line(m, contacts)
    if quote:
        print(f"    {quote}")


def emit_json(m, contacts):
    """Print one message as a single-line JSON object."""
    sys.stdout.write(json.dumps(message_to_json_obj(m, contacts),
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Read recent messages from the user's own LINE Desktop "
                    "history by scanning the live LINE process memory.")
    ap.add_argument("--limit", type=int, default=20,
                    help="Number of recent messages to print (default 20)")
    ap.add_argument("--chat", default=None,
                    help="Filter by chat display name (substring match)")
    ap.add_argument("--mid", default=None,
                    help="Filter by chat mid (_to column, exact match)")
    ap.add_argument("--pid", type=int, default=None,
                    help="LINE.exe PID (auto-detected if omitted)")
    ap.add_argument("--try-file", action="store_true",
                    help="Also run strategy 1 (file parse) to show it fails")
    ap.add_argument("--json", action="store_true",
                    help="Emit one JSON object per message per line "
                         "(machine-readable; matches watch_messages.py --json)")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


def fetch_recent(pid, limit, chat=None, mid=None):
    """Scan LINE memory and return the newest messages.

    Returns (msgs, contacts, stats). `msgs` is newest-first, already
    filtered by `chat` (display-name substring) and/or `mid` (`_to`),
    and truncated to `limit`. `stats` has scanned/matched/n_messages/
    n_contacts from the raw scan (before the limit). Callers that only
    need the rows should use this instead of reimplementing the scan.
    """
    messages, contacts, scanned, matched = lc.scan_memory(pid)
    stats = {
        "scanned": scanned,
        "matched": matched,
        "n_messages": len(messages),
        "n_contacts": len(contacts),
    }
    msgs = list(messages.values())
    if mid:
        msgs = [x for x in msgs if x["to"] == mid]
    if chat:
        wanted = {cmid for cmid, name in contacts.items()
                  if chat in (name or "")}
        msgs = [x for x in msgs
                if x["to"] in wanted or x["from"] in wanted]
    stats["n_filtered"] = len(msgs)
    msgs.sort(key=lambda x: x["createdTime"], reverse=True)
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")
    if limit is not None:
        msgs = msgs[:limit]
    return msgs, contacts, stats


def run(args):
    """Run the read operation with a parsed argparse Namespace. Returns
    an exit code (0 success, 1 no messages, 2 LINE not running)."""
    if args.limit is not None and args.limit < 0:
        print("ERROR: --limit must be >= 0.", file=sys.stderr)
        return 2
    if args.try_file:
        stream = sys.stderr if args.json else sys.stdout
        print("=== Strategy 1: parse on-disk .edb (expected to FAIL) ===", file=stream)
        for name, res in try_file_parse():
            print(f"  [{name}] {res}", file=stream)
        print(file=stream)

    pid = args.pid or lc.find_line_pid()
    if not pid:
        print("ERROR: LINE.exe not running (no /proc/<pid>/cmdline matched "
              "'LINE.exe run -t'). Pass --pid or start LINE first.",
              file=sys.stderr)
        return 2
    print(f"=== Strategy 2: read decrypted pages from LINE PID {pid} ===",
          file=sys.stderr)
    print("Scanning process memory for SQLite leaf-table pages...",
          file=sys.stderr)
    try:
        msgs, contacts, stats = fetch_recent(
            pid, args.limit, chat=args.chat, mid=args.mid)
    except OSError as exc:
        print(f"ERROR: cannot read LINE pid {pid}: {exc}", file=sys.stderr)
        return 1
    print(f"  pages scanned (type 0x0d, plausible): {stats['scanned']}",
          file=sys.stderr)
    print(f"  pages with parseable rows: {stats['matched']}", file=sys.stderr)
    print(f"  distinct messages recovered: {stats['n_messages']}",
          file=sys.stderr)
    print(f"  distinct contacts recovered: {stats['n_contacts']}",
          file=sys.stderr)
    print(file=sys.stderr)

    if not stats["n_messages"]:
        print("No messages recovered. Tips:", file=sys.stderr)
        print("  - Make sure LINE is running and you've opened a chat recently",
              file=sys.stderr)
        print("    (SQLite only caches pages that have been touched).",
              file=sys.stderr)
        print("  - Run as the same user that owns the LINE process.",
              file=sys.stderr)
        return 1

    if args.mid:
        print(f"Filtered to chat mid {args.mid}: {stats['n_filtered']} messages",
              file=sys.stderr)
    if args.chat:
        print(f"Filtered to chat name containing {args.chat!r}: "
              f"{stats['n_filtered']} messages", file=sys.stderr)
    if args.mid or args.chat:
        print(file=sys.stderr)

    if args.json:
        for m in msgs:
            emit_json(m, contacts)
    else:
        print(f"=== {len(msgs)} most recent messages (UTC+8) ===")
        for m in msgs:
            emit_text(m, contacts)
    return 0


def main():
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
