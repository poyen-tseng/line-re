#!/usr/bin/env python3
"""Read-only reaction RE probe; no X11, native calls, writes, or message bodies."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'automation'))
import bot_config
import line_common as lc
import reaction_memory as rm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--message-id')
    parser.add_argument('--room')
    parser.add_argument('--self-mid', default=None, help='bot mid; default from config or LINE_BOT_MID')
    args = parser.parse_args()
    if not args.self_mid:
        args.self_mid = bot_config.BotConfig().bot_mid()
    if bool(args.message_id) != bool(args.room):
        parser.error('--message-id and --room must be supplied together')
    pid = lc.find_line_pid(args.pid)
    if pid is None:
        parser.error('LINE is not running')
    memory = rm.ReactionMemory(pid)
    try:
        result = dict(pid=pid, image_base=hex(memory.base),
                      enum_values_rva=hex(rm.ENUM_VALUES),
                      enum_names_rva=hex(rm.ENUM_NAMES),
                      predefined_reactions=rm.enum_table(memory.mem, memory.base),
                      dock=hex(memory.dock), dock_vtable_rva=hex(rm.DOCK_VT),
                      picker=memory.picker(), open_room=lc.current_open_chat_mid(pid))
        if args.message_id:
            result['evidence'] = rm.reaction_evidence(
                pid, args.message_id, args.room, args.self_mid)
            try:
                result['loaded_rows'] = memory.rows(args.message_id)
            except ValueError as exc:
                result['loaded_rows_error'] = str(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        memory.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
