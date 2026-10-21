#!/usr/bin/env python3
"""Read-only upload diagnostics without printing object IDs, headers or tokens."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'automation'))
import bot_config
import file_upload as fu
import line_common as lc
from scan_file_send import content_types


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--room', required=True, help='group or room mid')
    parser.add_argument('--self-mid', default=None, help='bot mid; default from config or LINE_BOT_MID')
    parser.add_argument('--message-id', action='append', default=[])
    args = parser.parse_args()
    self_mid = args.self_mid or bot_config.BotConfig().bot_mid()
    pid = lc.find_line_pid()
    if not pid:
        parser.error('LINE is not running')
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        types = {value: name for _, value, _, name in content_types(mem)}
    print(json.dumps(dict(pid=pid, contentTypes=types,
        flatpak_workspace_visible=os.path.isdir(f'/proc/{pid}/root/workspace'),
        c_drive_visible=os.path.isdir(f'/proc/{pid}/root{fu.prefix_for_pid(pid)}/drive_c'))))
    rows, _, _, _ = lc.scan_memory(pid)
    selected = set(args.message_id)
    found = set()
    for mid, row in sorted(rows.items()):
        if (row.get('from') != self_mid or row.get('to') != args.room
                or row.get('contentType') not in lc.MEDIA_CONTENT_TYPES
                or (selected and mid not in selected)):
            continue
        try:
            meta = json.loads(row.get('contentMeta') or '{}')
        except ValueError:
            continue
        if not isinstance(meta, dict):
            continue
        found.add(mid)
        print(json.dumps(dict(id=mid, room=row['to'], sender=row['from'],
            contentType=row['contentType'], filename=row.get('text') or None,
            size=meta.get('FILE_SIZE'), service=meta.get('SID'),
            object_present=bool(meta.get('OID')), e2eeMark=meta.get('e2eeMark'),
            createdTime=row.get('createdTime')), ensure_ascii=False))
    if selected - found:
        print('not in current heap cache: ' + ', '.join(sorted(selected - found)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
