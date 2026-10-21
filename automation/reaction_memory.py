"""Read-only, build-checked LINE 26.0.2.3828 reaction inspection.

Offsets and evidence are documented in re-reaction/REPORT.md. Never writes
process memory, executes native code, or prints unrelated heap contents.
"""
import json
import re
import struct
import time

import line_common as lc

DOCK_VT = 0x2866360
MODEL_VT = 0x26F6D90
DATA_VT = 0x26F5698
VIEW_VT = 0x281DE98
ENUM_VALUES = 0x394EB60
ENUM_NAMES = 0x394EB78
REACTION_NAMES = ('NICE', 'LOVE', 'FUN', 'AMAZING', 'SAD', 'OMG')


def chunks(pid, mem):
    for start, end, _ in lc.iter_readable_regions(pid, writable_only=True):
        for addr in range(start, end, 4 * 1024 * 1024):
            blob = lc._mem_read(mem, addr, min(4 * 1024 * 1024 + 65536, end - addr))
            if blob:
                yield addr, blob


def qstring(mem, addr, max_length=100):
    raw = lc._mem_read(mem, addr, 24)
    if len(raw) != 24:
        return None
    header, ptr, size = struct.unpack('<QQq', raw)
    if not 0 < size <= max_length or header != ptr - 16:
        return None
    data = lc._mem_read(mem, ptr, size * 2)
    if len(data) != size * 2:
        return None
    try:
        return data.decode('utf-16le')
    except UnicodeError:
        return None


def enum_table(mem, base):
    values = lc._mem_read(mem, base + ENUM_VALUES, 24)
    names = lc._mem_read(mem, base + ENUM_NAMES, 48)
    if len(values) != 24 or len(names) != 48:
        raise ValueError('reaction enum table is unreadable')
    result = [(value, lc._mem_read(mem, ptr, 16).split(b'\0')[0].decode('ascii'))
              for value, ptr in zip(struct.unpack('<6i', values),
                                    struct.unpack('<6Q', names))]
    if result != list(zip(range(2, 8), REACTION_NAMES)):
        raise ValueError('unsupported LINE reaction enum table')
    return result


def widget_rect(mem, obj):
    """QWidgetData.crect is an inclusive QRect at data+0x14."""
    data = lc._mem_u64(mem, obj + 0x20)
    raw = lc._mem_read(mem, (data or 0) + 0x14, 16)
    if len(raw) != 16:
        raise ValueError('missing widget rectangle')
    left, top, right, bottom = struct.unpack('<4i', raw)
    if not (0 < right - left + 1 <= 8192 and 0 < bottom - top + 1 <= 8192):
        raise ValueError('invalid widget rectangle')
    return left, top, right - left + 1, bottom - top + 1


def widget_global(mem, obj):
    x = y = 0
    seen = set()
    width = height = None
    while obj:
        if obj in seen or len(seen) >= 16:
            raise ValueError('invalid QWidget parent chain')
        seen.add(obj)
        left, top, w, h = widget_rect(mem, obj)
        if width is None:
            width, height = w, h
        x += left
        y += top
        private = lc._mem_u64(mem, obj + 8)
        obj = lc._mem_u64(mem, (private or 0) + 0x10)
        if obj is None:
            raise ValueError('unreadable QWidget parent')
    return x, y, width, height


class ReactionMemory:
    def __init__(self, pid):
        self.pid = pid
        self.mem = open(f'/proc/{pid}/mem', 'rb', buffering=0)
        try:
            cached = lc._open_chat_cache.get(pid)
            self.base = cached[0] if cached else lc._image_base(pid, self.mem)
            if self.base is None:
                raise ValueError('unrecognized LINE image')
            enum_table(self.mem, self.base)
            token = b'.?AVReactionDockWidget@@\0'
            if lc._mem_read(self.mem, self.base + 0x398FEF0, len(token)) != token:
                raise ValueError('reaction picker RTTI changed')
            hits = {DOCK_VT: set(), MODEL_VT: set()}
            for addr, blob in chunks(pid, self.mem):
                for vt, found in hits.items():
                    token = struct.pack('<Q', self.base + vt)
                    for match in re.finditer(re.escape(token), blob):
                        if (addr + match.start()) % 8 == 0:
                            found.add(addr + match.start())
            if len(hits[DOCK_VT]) != 1:
                raise ValueError('reaction picker object is ambiguous')
            self.dock = hits[DOCK_VT].pop()
            self.models = hits[MODEL_VT]
            private = lc._mem_u64(self.mem, self.dock + 8)
            self.view = lc._mem_u64(self.mem, (private or 0) + 0x10)
            self.validate()
        except Exception:
            self.close()
            raise

    def close(self):
        self.mem.close()

    def validate(self):
        if (lc.find_line_pid(self.pid) != self.pid
                or lc._mem_u64(self.mem, self.dock) != self.base + DOCK_VT
                or lc._mem_u64(self.mem, self.view) != self.base + VIEW_VT):
            raise ValueError('LINE process or reaction widgets changed')

    def picker(self):
        self.validate()
        return dict(id=qstring(self.mem, self.dock + 0x458),
                    room=qstring(self.mem, self.dock + 0x470),
                    rect=widget_global(self.mem, self.dock))

    def rows(self, message_id):
        self.validate()
        candidates = []
        for model in self.models:
            if lc._mem_u64(self.mem, model) != self.base + MODEL_VT:
                continue
            array = lc._mem_u64(self.mem, model + 0x60)
            count = lc._mem_u64(self.mem, model + 0x68)
            if not array or not count or count > 2000:
                continue
            raw = lc._mem_read(self.mem, array, count * 16)
            if len(raw) != count * 16:
                continue
            rows = []
            for i in range(count):
                obj = struct.unpack_from('<Q', raw, i * 16)[0]
                if lc._mem_u64(self.mem, obj) != self.base + DATA_VT:
                    break
                size = lc._mem_read(self.mem, obj + 0x3C, 4)
                height = struct.unpack('<i', size)[0] if len(size) == 4 else -1
                mid = qstring(self.mem, obj + 0x1E0)
                if not mid or not 0 < height < 10000:
                    break
                rows.append((mid, height))
            else:
                if sum(mid == message_id for mid, _ in rows) == 1:
                    candidates.append(rows)
        if len(candidates) != 1:
            raise ValueError('message is not in one unambiguous loaded model')
        return candidates[0]


_UPDATE = re.compile(
    rb"UPDATE\s+_message\s+SET\s+_reactionStatus\s*=\s*'((?:[^'\x00]|''){1,32768})'"
    rb"\s+WHERE\s+_id\s*=\s*'([0-9]{1,24})'"
    rb"(?:\s+AND\s+_chatId\s*=\s*'([cr][0-9a-f]{32})')?"
    rb"(?=\s*(?:;|\x00|$))")


def evidence_from_blob(blob, message_id, room, self_mid):
    """Bind JSON to the SAME complete SQL statement, never nearby heap bytes."""
    if message_id.encode() not in blob:
        return [], False
    found = []
    for match in _UPDATE.finditer(blob):
        if match[2].decode() != message_id or (match[3] and match[3].decode() != room):
            continue
        try:
            status = json.loads(match[1].replace(b"''", b"'").decode())
        except (ValueError, UnicodeError):
            continue
        items = status.get('reactionList') if isinstance(status, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if (isinstance(item, dict) and item.get('reactor') == self_mid
                    and item.get('type') == 1 and item.get('cancel', False) is False
                    and item.get('PredefinedType') == 2):
                found.append(dict(id=message_id, room=room, reactor=self_mid,
                                  predefined=2, updatedAt=item.get('updatedAt'),
                                  source='SQL:_reactionStatus', offset=match.start(),
                                  room_bound=bool(match[3])))
    ack = re.search(rb'response react message: success=1, msgId='
                    + re.escape(message_id.encode()) + rb'(?=[,\s\x00])', blob)
    return found, bool(ack)


def message_identity_from_blob(blob, message_id, room):
    """Room for an id-only UPDATE must come from a complete message record."""
    if message_id.encode() not in blob:
        return False
    messages = {}
    lc._collect_json_messages(blob, messages)
    lc._collect_sql_messages(blob, messages)
    if messages.get(message_id, {}).get('to') == room:
        return True
    for match in re.finditer(b'\x0d\x00\x00', blob):
        page = blob[match.start():match.start() + lc.PGSZ]
        if message_id.encode() not in page:
            continue
        for _, values in lc.parse_leaf_table_page(page):
            if (lc.looks_like_message_row(values) and values[lc.MSG_ID] == message_id
                    and values[lc.MSG_TO] == room):
                return True
    return False


_LOG_PREFIX = (rb'#?L\d+,\s*(\d{1,2}),\s*(\d{2}):(\d{2}):(\d{2}\.\d{3,6})'
               rb'[^\x00\r\n]{0,220}\[ChatMessageView\]\[Reaction\]\s*')
_CLICK_LOG = re.compile(_LOG_PREFIX + rb'reaction item clicked, icon type:\s*(\d+)'
                        rb'\s*, targetId:\s*"(\d+)"\s*, cancel:\s*(true|false)')
_DONE_LOG = re.compile(_LOG_PREFIX + rb'requestReactMessage done:\s*SUCCESS')


def completion_logs(blob):
    def timestamp(match):
        return (int(match[1]), int(match[2]) * 3600 + int(match[3]) * 60 + float(match[4]))
    # Searching the timestamp regex across hundreds of MB of binary data cost
    # ~11 seconds per attempt. Locate literal markers in C first, then parse
    # only their complete, bounded log lines (including other message clicks).
    clicks, done = [], []
    marker = b'[ChatMessageView][Reaction]'
    offset = 0
    while True:
        index = blob.find(marker, offset)
        if index < 0:
            break
        offset = index + len(marker)
        start = max(0, index - 260)
        prefix = blob[start:index]
        start += max(prefix.rfind(b'\0'), prefix.rfind(b'\n')) + 1
        line = blob[start:index + 400].split(b'\0', 1)[0].split(b'\n', 1)[0]
        match = _CLICK_LOG.search(line)
        if match:
            clicks.append((timestamp(match), match[6].decode(), int(match[5]), match[7] == b'true'))
        match = _DONE_LOG.search(line)
        if match:
            done.append(timestamp(match))
    return clicks, done


def correlated_completion(records, clicks, completions, message_id):
    """A generic SUCCESS line alone cannot confirm an arbitrary message.

    Require the same day's exact-id NICE click, matching reaction updatedAt,
    followed within 10s by SUCCESS, with no intervening reaction click.
    """
    clicks = set(clicks)
    for record in records:
        stamp = record.get('updatedAt')
        if not isinstance(stamp, (int, float)) or not 1e12 < stamp < 1e14:
            continue
        local = time.localtime(stamp / 1000)
        seconds = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec + (stamp % 1000) / 1000
        for (day, when), mid, icon, cancel in clicks:
            if (mid != message_id or icon != 1 or cancel or day != local.tm_mday
                    or abs(seconds - when) > 2):
                continue
            for done_day, finished in completions:
                if done_day != day or not 0 <= finished - when <= 10:
                    continue
                if any(other_day == day and (when < other_time <= finished
                                            or (other_time == when and other_mid != message_id))
                       for (other_day, other_time), other_mid, _, _ in clicks):
                    continue
                return True
    return False


def reaction_evidence(pid, message_id, room, self_mid):
    found = []
    ack = False
    identity = False
    clicks, completions = [], []
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for addr, blob in chunks(pid, mem):
            rows, ok = evidence_from_blob(blob, message_id, room, self_mid)
            for row in rows:
                row['address'] = hex(addr + row.pop('offset'))
            found.extend(rows)
            ack = ack or ok
            identity = identity or message_identity_from_blob(blob, message_id, room)
            click_events, done_events = completion_logs(blob)
            clicks.extend(click_events)
            completions.extend(done_events)
    found = [row for row in found if row['room_bound'] or identity]
    for row in found:
        if not row.pop('room_bound'):
            row['source'] += '+message-record:room'
    completed = correlated_completion(found, clicks, completions, message_id)
    return dict(confirmed=bool(found and (ack or completed)), records=found,
                server_ack=ack, client_success=completed)
