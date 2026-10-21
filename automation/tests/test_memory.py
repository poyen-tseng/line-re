import json
import io
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import line_common as lc
import read_messages as reader

SELF = 'u' + '1' * 32
OTHER = 'u' + '2' * 32
ROOM = 'c' + '3' * 32
CREATED = 1792548000000


def message(**changes):
    row = dict(id='123456789', **{'from': OTHER, 'to': ROOM},
               createdTime=CREATED, text='hello', contentType=0, contentMeta=None)
    row.update(changes)
    return row


class MemoryTests(unittest.TestCase):
    def test_open_chat_discovery_requires_one_object_for_each_anchor(self):
        base, address = 0x140000000, 0x20000
        vtables = [base + anchor[2] for anchor in lc._OPEN_CHAT_ANCHORS]
        blob = b''.join(struct.pack('<Q', vt) + b'\0' * 8 for vt in vtables)
        for duplicate in (False, True):
            data = blob + (blob[:16] if duplicate else b'')
            with patch.object(lc, '_image_base', return_value=base), patch.object(
                    lc, 'iter_readable_regions', return_value=[(address, address + len(data), '')]), patch.object(
                    lc, '_mem_read', return_value=data), patch.object(
                    lc, '_read_mid_field', return_value=ROOM):
                result = lc._locate_open_chat(123, None)
            if duplicate:
                self.assertIsNone(result)
            else:
                self.assertEqual([item[0] for item in result[1]], [address, address + 16])

    def test_partial_scan_reuses_persisted_group_names_for_routing_and_discovery(self):
        with tempfile.TemporaryDirectory() as root:
            roster = Path(root, 'groups.json')
            absent = 'c' + '4' * 32
            roster.write_text(json.dumps({ROOM: 'Known group', absent: 'Not in this scan'}))
            blob = f"INSERT OR REPLACE INTO _groupChat (_chatMid,_chatName) VALUES ('{ROOM}','')".encode()
            original_open = open
            def opened(path, *args, **kwargs):
                if path == '/proc/123/mem':
                    return io.BytesIO(blob)
                return original_open(path, *args, **kwargs)
            with patch('builtins.open', side_effect=opened), patch.object(
                    lc, 'iter_readable_regions', return_value=[(0, len(blob), '')]), patch.object(
                    lc, 'GROUPS_JSON', str(roster)):
                _, contacts, _, _ = lc.scan_memory(123)
            self.assertEqual(contacts[ROOM], 'Known group')
            self.assertEqual(lc.scan_memory.last_groups, {ROOM: 'Known group'})
            self.assertEqual(json.loads(roster.read_text())[ROOM], 'Known group')

    def page_for(self, values):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'test.sqlite')
            with closing(sqlite3.connect(path)) as db:
                with db:
                    db.execute('PRAGMA page_size=4096')
                    db.execute('CREATE TABLE sample (' + ','.join('c' + str(i) for i in range(len(values))) + ')')
                    db.execute('INSERT INTO sample VALUES (' + ','.join('?' for _ in values) + ')', values)
            return Path(path).read_bytes()[4096:8192]

    def test_sqlite_parser_round_trip_and_mid_types(self):
        values = [OTHER, ROOM, 2, '123456789', CREATED, None, '中文🙂', None,
                  0, 0, '{"MENTION":"test"}', -32769, 1.25, b'abc']
        rows = lc.parse_leaf_table_page(self.page_for(values))
        self.assertEqual(rows, [(1, values)])
        self.assertTrue(lc.looks_like_message_row(values))
        for prefix in ('u', 'c', 'r'):
            self.assertIsNotNone(lc.MID_RE.fullmatch(prefix + 'a' * 32))

    def test_truncated_varints_and_fields_are_rejected(self):
        for raw, offset in ((b'', 0), (b'\x80', 0), (b'abc', -1)):
            with self.assertRaises(ValueError):
                lc.read_varint(raw, offset)
        page = bytearray(self.page_for(['abc']))
        cell = struct.unpack('>H', page[8:10])[0]
        page[cell] = 1  # payload too short for its record header/column value
        self.assertEqual(lc.parse_leaf_table_page(page), [])
        self.assertEqual(lc.parse_leaf_table_page(page[:200]), [])

    def test_overflow_record_is_not_partially_executed(self):
        self.assertEqual(lc.parse_leaf_table_page(self.page_for(['x' * 10000])), [])

    def test_json_nested_prefix_whitespace_and_escaped_braces(self):
        obj = dict(meta={'nested': '{"odd":true}'}, **{'from': OTHER, 'to': ROOM},
                   id='123456789', createdTime=CREATED, text='hello } 🙂',
                   contentMetadata={'MENTION': 'mentions'})
        messages = {}
        lc._collect_json_messages(b'noise\x00' + json.dumps(obj, ensure_ascii=False).encode(), messages)
        self.assertEqual(messages['123456789']['text'], obj['text'])
        self.assertIn('MENTION', messages['123456789']['contentMeta'])

    def test_sql_insert_or_replace_and_escaped_quote(self):
        data = (f"INSERT OR REPLACE INTO _message (_id,_from,_to,_createdTime,_text,_contentMeta) "
                f"VALUES ('123456789','{OTHER}','{ROOM}',{CREATED},'it''s ok','{{}}')").encode()
        messages = {}
        lc._collect_sql_messages(data, messages)
        self.assertEqual(messages['123456789']['text'], "it's ok")
        self.assertIsNone(lc._sql_values(b"('a',)", 0))

    def test_conflicting_identity_cannot_donate_text_or_mention(self):
        messages = {'123456789': message()}
        lc._merge_message(messages, message(to=SELF, text='a longer command', contentMeta='MENTION'))
        self.assertEqual(messages['123456789']['text'], 'hello')
        self.assertIsNone(messages['123456789']['contentMeta'])

    def test_process_detection_matches_argv_not_source(self):
        self.assertTrue(lc.is_line_cmdline(b'\0'.join([b'C:\\apps\\LINE.exe', b'run', b'-t', b'12345'])))
        self.assertTrue(lc.is_line_cmdline(b'/usr/bin/wine64\0C:\\apps\\LINE.exe\0run\0-t\0abc\0'))
        for raw in (b'bash\0-c\0LINE.exe run -t 123', b'python3\0script.py\0LINE.exe\0run\0-t', b'LINE.exe\0helper'):
            self.assertFalse(lc.is_line_cmdline(raw))

    def test_unreadable_memory_is_error(self):
        with patch('builtins.open', side_effect=PermissionError('sandbox')):
            with self.assertRaises(PermissionError):
                lc.scan_memory(123)

    def test_outgoing_confirmation_only_uses_complete_records(self):
        with patch.object(lc, 'scan_memory', return_value=({'123456789': message(**{'from': SELF})}, {}, 1, 1)), patch('builtins.open') as opened:
            copies = lc.find_outgoing_copies(123, 'hello', SELF)
        self.assertEqual(copies[0]['to'], ROOM)
        opened.assert_not_called()

    def test_read_mid_filter_is_exact(self):
        messages = {'123456789': message(to=SELF), 'other-id': message(to=OTHER)}
        with patch.object(lc, 'scan_memory', return_value=(messages, {}, 1, 1)):
            rows, _, _ = reader.fetch_recent(123, 20, mid=SELF)
            self.assertEqual(len(rows), 1)
            rows, _, _ = reader.fetch_recent(123, 20, mid=SELF[1:])
            self.assertEqual(rows, [])

    def test_file_parse_prefers_active_wal_over_large_stale_database(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'qwstale.edb').write_bytes(b'x' * 9000)
            Path(root, 'qwactive.edb').write_bytes(b'x' * 100)
            Path(root, 'qwactive.edb-wal').write_bytes(b'')
            with patch.object(reader, 'LINE_DB_DIR', root):
                results = reader.try_file_parse()
            self.assertIn('qwactive.edb', results[0][1])


if __name__ == '__main__':
    unittest.main()
