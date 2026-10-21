#!/usr/bin/env python3
"""Read-only LINE quote evidence; never print message bodies or participant MIDs."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'automation'))
import line_common as lc


def main():
    pid = lc.find_line_pid()
    if not pid:
        raise SystemExit('LINE not running')
    started = time.monotonic()
    rows = {}
    schemas = set()
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for lo, hi, _ in lc.iter_readable_regions(pid):
            try:
                mem.seek(lo)
                data = mem.read(hi - lo)
            except OSError:
                continue
            pos = 0
            while True:
                pos = data.find(b'CREATE TABLE _message(', pos)
                if pos < 0:
                    break
                suffix = b'PRIMARY KEY ( _id ) )'
                end = data.find(suffix, pos, pos + 4000)
                if end >= 0:
                    schemas.add(data[pos:end + len(suffix)].decode('ascii'))
                pos += 1
            pos = 0
            while True:
                pos = data.find(b'\x0d\x00\x00', pos)
                if pos < 0 or pos + lc.PGSZ > len(data):
                    break
                for _, vals in lc.parse_leaf_table_page(data[pos:pos + lc.PGSZ]):
                    if lc.looks_like_message_row(vals):
                        row = lc._sqlite_message_record(vals)
                        if row:
                            lc._merge_message(rows, row)
                pos += 1
            lc._collect_json_messages(data, rows)
            lc._collect_sql_messages(data, rows)
    print(json.dumps(dict(pid=pid, elapsed=round(time.monotonic() - started, 3),
                          messages=len(rows))))
    for schema in sorted(schemas):
        print(schema)
    for row in sorted(rows.values(), key=lambda m: m['createdTime']):
        rid = row.get('reply_to_id')
        if not rid:
            continue
        meta = json.loads(row.get('contentMeta') or '{}')
        obj = dict(id=row['id'], reply_to_id=rid,
                   relation=row['message_relation_type'],
                   service=row['related_message_service_code'],
                   createdTime=row['createdTime'], source=row['source'],
                   text=f"[redacted; {len(row.get('text') or '')} characters]",
                   metadata={k: v for k, v in meta.items()
                             if k.startswith('message_relation_')})
        original = rows.get(rid)
        if original:
            obj['original'] = dict(
                createdTime=original['createdTime'], source=original['source'],
                sender_present=bool(original.get('from')),
                same_room=original['to'] == row['to'],
                text=f"[redacted; {len(original.get('text') or '')} characters]",
                age_seconds=(row['createdTime'] - original['createdTime']) / 1000,
                later_same_room=sum(1 for m in rows.values()
                    if m['to'] == row['to'] and original['createdTime'] <
                    m['createdTime'] <= row['createdTime']))
        print(json.dumps(obj, ensure_ascii=False))


if __name__ == '__main__':
    main()
