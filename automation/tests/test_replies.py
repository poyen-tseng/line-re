import io
from contextlib import redirect_stdout, redirect_stderr
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import line_common as lc
import line_cli as cli
import read_messages as reader
import reply_context as replies
import watch_messages as watch
import test_memory as fixtures
from test_memory import message, SELF, OTHER, ROOM, CREATED

ORIGINAL_ID = '123456780'
REPLY_ID = '123456789'


def metadata(**changes):
    result = dict(message_relation_server_message_id=ORIGINAL_ID,
                  message_relation_type_code='reply',
                  message_relation_service_code='talk')
    result.update(changes)
    return result


class ReplyParsingTests(unittest.TestCase):
    def test_observed_metadata_strings_and_numeric_columns(self):
        for meta in (metadata(), json.dumps(metadata())):
            for fields in ({}, dict(related_id=ORIGINAL_ID, relation_type=3, service=1)):
                with self.subTest(meta=type(meta), fields=fields):
                    got = lc._relation_fields(meta, **fields)
                    self.assertEqual(got['reply_to_id'], ORIGINAL_ID)
                    self.assertEqual(got['message_relation_type'], 3)
                    self.assertEqual(got['related_message_service_code'], 1)

    def test_malformed_other_relations_and_conflicts_are_not_quotes(self):
        for meta, fields in ((None, {}), ('bad', {}), ('[]', {}),
                (metadata(message_relation_type_code='forward'), {}),
                (metadata(), dict(relation_type=1)),
                (metadata(), dict(related_id='123456781', relation_type=3)),
                (metadata(), dict(related_id='123456780\n', relation_type=3)),
                (metadata(message_relation_server_message_id=True), {}),
                (metadata(message_relation_server_message_id='000000'), {})):
            with self.subTest(meta=meta, fields=fields):
                self.assertIsNone(lc._relation_fields(meta, **fields)['reply_to_id'])

    def test_serializer_json_relation_fields(self):
        obj = dict(message(), relatedMessageId=int(ORIGINAL_ID),
                   messageRelationType=3, relatedMessageServiceCode=1)
        got = {}
        lc._collect_json_messages(json.dumps(obj).encode(), got)
        self.assertEqual(got[REPLY_ID]['reply_to_id'], ORIGINAL_ID)
        self.assertEqual(got[REPLY_ID]['source'], 'heap-json')

    def test_sql_named_columns_are_not_assumed_to_be_schema_order(self):
        sql = (f"INSERT OR REPLACE INTO _message (_messageRelationType,_relatedMessageId,"
               f"_relatedMessageServiceCode,_id,_from,_to,_createdTime,_text) "
               f"VALUES (3,'{ORIGINAL_ID}',1,'{REPLY_ID}','{OTHER}','{ROOM}',{CREATED},'reply')")
        got = {}
        lc._collect_sql_messages(sql.encode(), got)
        self.assertEqual(got[REPLY_ID]['reply_to_id'], ORIGINAL_ID)
        self.assertEqual(got[REPLY_ID]['source'], 'heap-sql')

    def test_real_schema_leaf_column_offsets_and_scan_wiring(self):
        vals = [OTHER, ROOM, 2, REPLY_ID, CREATED, None, 'reply', None,
                0, 0, json.dumps(metadata()), None, 0, None, ORIGINAL_ID,
                3, 0, 1] + [None] * 18
        page = fixtures.MemoryTests().page_for(vals)
        with patch('builtins.open', return_value=io.BytesIO(page)), patch.object(
                lc, 'iter_readable_regions', return_value=[(0, len(page), '')]), patch.object(
                lc, 'persist_group_roster', return_value={}):
            got, _, _, _ = lc.scan_memory(123)
        self.assertEqual(got[REPLY_ID]['reply_to_id'], ORIGINAL_ID)
        self.assertEqual(got[REPLY_ID]['related_message_service_code'], 1)
        self.assertEqual(got[REPLY_ID]['source'], 'sqlite')

    def test_relation_survives_richer_copy_merge_only_for_same_identity(self):
        original = lc._message_record(OTHER, ROOM, REPLY_ID, CREATED, None, 0, None)
        quote = lc._message_record(OTHER, ROOM, REPLY_ID, CREATED, 'reply', 0, metadata())
        got = {REPLY_ID: original}
        lc._merge_message(got, dict(quote, to=SELF))
        self.assertIsNone(got[REPLY_ID]['reply_to_id'])
        lc._merge_message(got, quote)
        self.assertEqual(got[REPLY_ID]['reply_to_id'], ORIGINAL_ID)


def quote(**changes):
    return message(reply_to_id=ORIGINAL_ID, related_message_id=ORIGINAL_ID,
                   message_relation_type=3, related_message_service_code=1, **changes)


def original(**changes):
    row = message(id=ORIGINAL_ID, createdTime=CREATED - 20000000,
                  text='An older quoted message', source='sqlite')
    row.update(changes)
    return row


class ReplyLookupTests(unittest.TestCase):
    def setUp(self):
        cache = patch.object(replies, 'CACHE', replies.MessageCache())
        cache.start()
        self.addCleanup(cache.stop)
        misses = patch.object(replies, '_MISSES', replies.OrderedDict())
        misses.start()
        self.addCleanup(misses.stop)

    def test_current_scan_then_cached_original_without_another_scan(self):
        old = original()
        replies.CACHE.observe(123, {ORIGINAL_ID: old})
        with patch.object(replies, 'scan_message_id', side_effect=AssertionError('unnecessary scan')):
            context = replies.resolve_reply(quote(), {ORIGINAL_ID: old}, pid=123, allow_scan=True)
            self.assertEqual(context['source'], 'sqlite')
            context = replies.resolve_reply(quote(), {}, pid=123, allow_scan=True)
            self.assertTrue(context['found'])
            self.assertEqual(context['source'], 'cache/sqlite')

    def test_wrong_room_sender_time_or_id_is_not_accepted(self):
        for old in (original(to=SELF), original(id='123456781'),
                    original(createdTime=CREATED + 1), original(**{'from': 'bad'}),
                    original(text=None, contentType=0)):
            with self.subTest(old=old):
                got = replies.resolve_reply(quote(), {ORIGINAL_ID: old})
                self.assertFalse(got['found'])

    def test_supported_media_has_a_content_type_placeholder(self):
        row = quote()
        row['reply_context'] = replies.resolve_reply(row, {ORIGINAL_ID: original(text=None, contentType=1)})
        self.assertIn('[image]', replies.text_line(row, {}))

    def test_missing_error_and_negative_cache_do_not_block_or_retry_repeatedly(self):
        with patch.object(replies, 'scan_message_id', side_effect=OSError('gone')) as scan:
            self.assertEqual(replies.resolve_reply(quote(), pid=123, allow_scan=True)['source'], 'scan-error')
            self.assertEqual(replies.resolve_reply(quote(), pid=123, allow_scan=True)['source'], 'negative-cache')
            scan.assert_called_once()
        row = quote()
        self.assertIn(f'原訊息無法取得 (id={ORIGINAL_ID})', replies.text_line(row, {}))

    def test_bounded_fallback_populates_cache_and_validates_room(self):
        with patch.object(replies, 'scan_message_id', return_value=(original(), 'lookup/sqlite')) as scan:
            got = replies.resolve_reply(quote(), pid=123, allow_scan=True)
            self.assertTrue(got['found'])
            self.assertEqual(got['source'], 'lookup/sqlite')
            self.assertEqual(replies.resolve_reply(quote(), pid=123)['source'], 'cache/sqlite')
            scan.assert_called_once_with(123, ORIGINAL_ID)
        with patch.object(replies, 'scan_message_id', return_value=(original(to=SELF), 'lookup/sqlite')):
            got = replies.resolve_reply(quote(), pid=124, allow_scan=True)
            self.assertFalse(got['found'])
            self.assertEqual(got['source'], 'identity-mismatch')

    def test_unsupported_services_never_scan(self):
        for service in (2, 'square'):
            row = quote()
            row['related_message_service_code'] = service
            with patch.object(replies, 'scan_message_id') as scan:
                self.assertEqual(replies.resolve_reply(row, pid=123, allow_scan=True)['source'], 'unsupported-service')
                scan.assert_not_called()
        self.assertEqual(lc._relation_fields(metadata(message_relation_service_code='square'))[
            'related_message_service_code'], 'square')

    def test_cache_pid_ttl_entry_and_byte_limits_and_partial_updates(self):
        cache = replies.MessageCache(max_entries=1)
        with patch.object(replies.time, 'monotonic', return_value=100):
            cache.observe(123, {ORIGINAL_ID: original()})
            cache.observe(123, {ORIGINAL_ID: original(text=None)})
            self.assertEqual(cache.get(123, ORIGINAL_ID)['text'], original()['text'])
            self.assertIsNone(cache.get(124, ORIGINAL_ID))
            cache.observe(123, {REPLY_ID: message()})
            self.assertIsNone(cache.get(123, ORIGINAL_ID))
            self.assertEqual(len(cache.rows), 1)
            cache.observe(124, {ORIGINAL_ID: original()})
            self.assertEqual(cache.pid, 124)
            self.assertNotIn(REPLY_ID, cache.rows)
        with patch.object(replies.time, 'monotonic', return_value=4000):
            self.assertIsNone(cache.get(124, ORIGINAL_ID))
        cache = replies.MessageCache(max_bytes=1024)
        cache.observe(123, {ORIGINAL_ID: original(text='x' * 2048)})
        self.assertEqual(cache.size, 0)
        self.assertFalse(cache.rows)

    def test_fallback_reads_complete_json_sql_and_sqlite_including_chunk_boundary(self):
        obj = dict(original(), contentMetadata={})
        sql = (f"INSERT INTO _message (_id,_from,_to,_createdTime,_text) VALUES "
               f"('{ORIGINAL_ID}','{OTHER}','{ROOM}',{CREATED-20000000},'old')").encode()
        vals = [OTHER, ROOM, 2, ORIGINAL_ID, CREATED-20000000, None, 'old', None, 0, 0, None]
        page = fixtures.MemoryTests().page_for(vals)
        raw_json = json.dumps(obj).encode()
        for data in (raw_json, sql, page, b'x' * (256 * 1024 - 20) + raw_json):
            with self.subTest(length=len(data)), patch('builtins.open', return_value=io.BytesIO(data)), patch.object(
                    lc, 'iter_readable_regions', return_value=[(0, len(data), '')]):
                row, source = replies.scan_message_id(123, ORIGINAL_ID)
                self.assertEqual(row['id'], ORIGINAL_ID)
                self.assertTrue(source.startswith('lookup/'))

    def test_fallback_deadline_and_byte_cap_stop_before_full_scan(self):
        with patch('builtins.open', return_value=io.BytesIO(b'noise')) as opened, patch.object(
                lc, 'iter_readable_regions', return_value=[(0, 1000000000, '')]), patch.object(
                replies.time, 'monotonic', side_effect=[0, 1]):
            self.assertEqual(replies.scan_message_id(123, ORIGINAL_ID), (None, 'budget-exhausted'))
            self.assertTrue(opened.return_value.closed)
        data = io.BytesIO(b'x' * 1024)
        with patch('builtins.open', return_value=data), patch.object(
                lc, 'iter_readable_regions', return_value=[(0, 1000000000, '')]):
            self.assertEqual(replies.scan_message_id(123, ORIGINAL_ID, max_bytes=32),
                             (None, 'budget-exhausted'))


class ReplyPresentationTests(unittest.TestCase):
    def test_group_bot_passes_reply_pid_and_quote_to_model_before_mocked_actions(self):
        target = quote(text='@bot explain this')
        args = cli.build_parser().parse_args(['ai', '--group-bot', '--dry-run',
                                             '--mention', '@bot', '--cooldown', '0'])
        args.self_mid, args.catchup_minutes = SELF, 0
        state = SimpleNamespace(replies=0, last_send_at=0, last_group={}, line_pid=123)
        gx = SimpleNamespace(stop=False)
        with patch.object(cli, '_call', return_value='{"actions":[{"type":"send_text","text":"ok"}]}') as model, patch.object(
                cli, '_execute_actions', return_value=(True, False)), patch.object(
                cli, '_group_roster_text', return_value=''), patch.object(
                replies, 'scan_message_id', return_value=(original(), 'lookup/sqlite')) as lookup, patch.object(
                replies, 'CACHE', replies.MessageCache()), patch.object(
                replies, '_MISSES', replies.OrderedDict()), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli._group_bot_handle({REPLY_ID: target}, {}, set(), args, {}, gx, state)
        lookup.assert_called_once_with(123, ORIGINAL_ID)
        self.assertIn(original()['text'], model.call_args.args[0])

    def test_prompt_includes_quote_older_than_eight_messages_and_logs_lookup(self):
        target, old = quote(), original()
        rows = {ORIGINAL_ID: old, REPLY_ID: target}
        for i in range(12):
            row = message(id=str(200000000 + i), createdTime=CREATED - 1000 * (i+1), text='recent')
            rows[row['id']] = row
        self.assertNotIn(old['text'], cli._group_transcript(rows, target, {}, SELF, 8))
        log = io.StringIO()
        with redirect_stderr(log), patch.object(cli, '_group_roster_text', return_value=''):
            prompt = cli._group_prompt('explain the quote', target, rows, {OTHER: 'Alice'}, SELF, '', 8)
        self.assertIn(f"使用者回覆的原訊息: Alice {lc.fmt_time(old['createdTime'])} Asia/Taipei: {old['text']}", prompt)
        self.assertIn(f'[reply] msg={REPLY_ID} reply_to={ORIGINAL_ID} found=yes source=sqlite', log.getvalue())

    def test_prompt_missing_original_and_otp_redaction(self):
        for old, expected in ((None, f'原訊息無法取得 (id={ORIGINAL_ID})'),
                (original(text='Your verification code is 123456'), '已略過驗證碼')):
            with self.subTest(old=old), redirect_stderr(io.StringIO()):
                section = cli._reply_prompt_section(quote(), {ORIGINAL_ID: old} if old else {}, {})
                self.assertIn(expected, section)
                self.assertNotIn('123456', section.replace(ORIGINAL_ID, 'id'))

    def test_read_watch_text_json_share_context_and_null_for_nonreply(self):
        for found in (True, False):
            target = quote()
            target['reply_context'] = replies.resolve_reply(target, {ORIGINAL_ID: original()} if found else {})
            contacts = {OTHER: 'Alice'}
            read_output, watch_output = io.StringIO(), io.StringIO()
            with redirect_stdout(read_output):
                reader.emit_text(target, contacts)
            with redirect_stdout(watch_output):
                watch.emit_message(target, contacts, SimpleNamespace(json=False, exec_cmd=None))
            self.assertEqual(read_output.getvalue(), watch_output.getvalue())
            self.assertIn(f'↪ reply to {ORIGINAL_ID}', read_output.getvalue())
            with redirect_stdout(watch_output := io.StringIO()):
                watch.emit_message(target, contacts, SimpleNamespace(json=True, exec_cmd=None))
            got = json.loads(watch_output.getvalue())
            self.assertEqual(got, reader.message_to_json_obj(target, contacts))
            self.assertEqual(got['reply_to_id'], ORIGINAL_ID)
            self.assertEqual(got['reply']['found'], found)
            if found:
                self.assertEqual(got['reply']['from_name'], 'Alice')
                self.assertEqual(got['reply']['text'], original()['text'])
            else:
                self.assertIn('原訊息無法取得', got['reply']['unavailable'])
        self.assertIsNone(reader.message_to_json_obj(message(), {})['reply'])


if __name__ == '__main__':
    unittest.main()
