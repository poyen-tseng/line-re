"""Read-only quote lookup. No LINE UI, network requests or persistent message cache."""
from collections import OrderedDict
import threading
import time

import line_common as lc


class MessageCache:
    """Per-process cache: 2048 records / 8 MiB text budget / one hour unseen."""
    def __init__(self, max_entries=2048, max_bytes=8 * 1024 * 1024, ttl=3600):
        self.max_entries, self.max_bytes, self.ttl = max_entries, max_bytes, ttl
        self.pid = None
        self.rows = OrderedDict()
        self.size = 0
        self.lock = threading.RLock()

    def observe(self, pid, messages):
        now = time.monotonic()
        with self.lock:
            if pid != self.pid:
                self.pid, self.rows, self.size = pid, OrderedDict(), 0
            for mid, row in messages.items():
                snapshot = {k: row.get(k) for k in
                            ('id', 'from', 'to', 'createdTime', 'text', 'contentType', 'source')}
                cost = len((snapshot.get('text') or '').encode('utf-8')) + 512
                if cost > self.max_bytes:
                    continue
                old = self.rows.pop(mid, None)
                if old:
                    self.size -= old[2]
                    # A later partial copy must not erase a recovered original.
                    lc._merge_message({mid: snapshot}, old[1])
                    cost = len((snapshot.get('text') or '').encode('utf-8')) + 512
                self.rows[mid] = (now, snapshot, cost)
                self.size += cost
            while self.rows:
                first = next(iter(self.rows))
                if (len(self.rows) <= self.max_entries and self.size <= self.max_bytes
                        and now - self.rows[first][0] <= self.ttl):
                    break
                self.size -= self.rows.pop(first)[2]

    def get(self, pid, mid):
        with self.lock:
            if pid != self.pid:
                return None
            entry = self.rows.get(mid)
            if not entry or time.monotonic() - entry[0] > self.ttl:
                return None
            return dict(entry[1])


CACHE = MessageCache()
_MISSES = OrderedDict()
_MISS_LOCK = threading.Lock()


def _valid_original(original, reply):
    return bool(original and original.get('id') == reply.get('reply_to_id')
                and original.get('id') != reply.get('id')
                and original.get('to') == reply.get('to')
                and isinstance(original.get('from'), str)
                and lc.MID_RE.fullmatch(original['from'])
                and type(original.get('createdTime')) is int
                and 1262304000000 < original['createdTime'] <= reply.get('createdTime', 0)
                and (original.get('text') or original.get('contentType') not in (None, 0)))


def scan_message_id(pid, mid, timeout=0.75, max_bytes=64 * 1024 * 1024):
    """Bounded best-effort heap/page fallback; never call a second full scan.

    Read <=64 MiB in 256 KiB chunks with 16 KiB overlap. Stop at the first
    complete matching record or the 0.75s deadline (one chunk can overrun).
    """
    deadline = time.monotonic() + timeout
    consumed = 0
    needle = mid.encode('ascii')
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for lo, hi, _ in lc.iter_readable_regions(pid, writable_only=True):
            pos, overlap = lo, b''
            while pos < hi:
                if time.monotonic() >= deadline or consumed >= max_bytes:
                    return None, 'budget-exhausted'
                size = min(256 * 1024, hi - pos, max_bytes - consumed)
                try:
                    mem.seek(pos)
                    chunk = mem.read(size)
                except OSError:
                    break
                if not chunk:
                    break
                consumed += len(chunk)
                pos += len(chunk)
                data = overlap + chunk
                overlap = data[-16384:]
                if needle not in data:
                    continue
                found = {}
                idx = 0
                while time.monotonic() < deadline:
                    idx = data.find(b'\x0d\x00\x00', idx)
                    if idx < 0 or idx + lc.PGSZ > len(data):
                        break
                    page = data[idx:idx + lc.PGSZ]
                    idx += 1
                    if needle not in page:
                        continue
                    for _, vals in lc.parse_leaf_table_page(page):
                        if lc.looks_like_message_row(vals) and vals[lc.MSG_ID] == mid:
                            row = lc._sqlite_message_record(vals)
                            if row:
                                lc._merge_message(found, row)
                if time.monotonic() >= deadline:
                    return None, 'budget-exhausted'
                lc._collect_json_messages(data, found)
                lc._collect_sql_messages(data, found)
                row = found.get(mid)
                if row and (row.get('text') or row.get('contentType') not in (None, 0)):
                    return row, 'lookup/' + row.get('source', 'unknown')
    return None, 'not-found'


def resolve_reply(reply, messages=None, pid=None, allow_scan=False):
    rid = reply.get('reply_to_id')
    if not rid:
        return None
    result = dict(id=rid, found=False, source='not-found', message=None)
    if reply.get('related_message_service_code') not in (None, 1):
        return dict(result, source='unsupported-service')
    original = (messages or {}).get(rid)
    if _valid_original(original, reply):
        return dict(result, found=True, source=original.get('source', 'scan'), message=original)
    attached = reply.get('reply_context') or {}
    if attached.get('found') and _valid_original(attached.get('message'), reply):
        return attached
    if pid is None:
        return result
    original = CACHE.get(pid, rid)
    if _valid_original(original, reply):
        return dict(result, found=True, source='cache/' + (original.get('source') or 'unknown'),
                    message=original)
    if not allow_scan:
        return result
    key = (pid, rid, reply.get('to'))
    with _MISS_LOCK:
        now = time.monotonic()
        if now - _MISSES.get(key, float('-inf')) < 30:
            return dict(result, source='negative-cache')
        _MISSES[key] = now
        _MISSES.move_to_end(key)
        while len(_MISSES) > 512:
            _MISSES.popitem(last=False)
    try:
        original, source = scan_message_id(pid, rid)
    except OSError:
        return dict(result, source='scan-error')
    if _valid_original(original, reply):
        CACHE.observe(pid, {rid: original})
        return dict(result, found=True, source=source, message=original)
    return dict(result, source=source if not original else 'identity-mismatch')


def unavailable(rid):
    return f'原訊息無法取得 (id={rid})'


def text_line(message, contacts):
    rid = message.get('reply_to_id')
    if not rid:
        return None
    context = resolve_reply(message) or {}
    original = context.get('message')
    if not context.get('found'):
        return f'↪ reply to {rid}: {unavailable(rid)}'
    body = original.get('text') or f"[{lc.fmt_content_type(original.get('contentType'))}]"
    return (f'↪ reply to {rid} {lc.display_name(original["from"], contacts)} '
            f'{lc.fmt_time(original["createdTime"])} Asia/Taipei: {lc.truncate(body, 200)}')


def json_fields(message, contacts):
    context = resolve_reply(message)
    quote = None
    if context:
        quote = {k: context[k] for k in ('id', 'found', 'source')}
        original = context.get('message')
        if context['found']:
            quote.update({k: original.get(k) for k in
                          ('from', 'createdTime', 'text', 'contentType')})
            quote['from_name'] = lc.display_name(original['from'], contacts)
            quote['time'] = lc.fmt_time(original['createdTime'])
            quote['timezone'] = 'Asia/Taipei'
        else:
            quote['unavailable'] = unavailable(context['id'])
    return dict(reply_to_id=message.get('reply_to_id'),
                related_message_id=message.get('related_message_id'),
                message_relation_type=message.get('message_relation_type'),
                related_message_service_code=message.get('related_message_service_code'),
                reply=quote)
