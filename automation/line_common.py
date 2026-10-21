"""line_common.py — shared logic for the LINE Desktop automation tools.

This module is the single home for code that more than one of
`send_message.py`, `read_messages.py`, `watch_messages.py`, and
`line_cli.py` need:

  * LINE.exe PID autodetection via `/proc` (`find_line_pid`).
  * X display autodetection via `xdotool` (`find_line_display`,
    `find_line_windows`).
  * The minimal in-memory SQLite leaf-table b-tree page parser
    (`read_varint`, `parse_leaf_table_page`) plus the LINE schema
    column-index constants and row heuristics
    (`looks_like_message_row`, `looks_like_contact_row`).
  * The decrypted-page-cache memory scanner (`iter_readable_regions`,
    `scan_memory`) used by `read_messages.py` and `watch_messages.py`.
    Leaf pages are merged with the live message JSON LINE keeps beside
    them and with plaintext `INSERT`/`REPLACE INTO _message` text,
    deduped by message id.
  * Formatting helpers shared by the read and watch paths
    (`fmt_time`, `fmt_content_type`, `display_name`, `truncate`).
    Joined groups (`_groupChat`, `_chat` midType 2, and plaintext
    `INSERT INTO _groupChat`) are a room-mid → name map. The name may
    be empty. Each successful scan merges that map into `groups.json`.
    Non-empty names are also copied onto the contact map. The visible
    chat-list order (pinned, then recent) is `chat_list_mids`.
  * The open-chat mid (`current_open_chat_mid`) read from the
    LineApplication and AllInOneWindow UTF-16 fields, plus
    `find_outgoing_copies` for the post-send `to` check.

The older scripts import from this module so their internals are not
duplicated; they continue to work standalone exactly as before.
"""

import json
import os
import re
import struct
import subprocess
import tempfile
import time
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PGSZ = 4096  # LINE's SQLite page size (header bytes 0x10-0x11 = 0x1000)
TZ8 = timezone(timedelta(hours=8))  # display timezone for timestamps
MID_RE = re.compile(r"(?:[ucr][0-9a-fA-F]{32}|[0-9a-fA-F]{30,40})")

# Common X displays to probe when looking for the LINE window, in order.
CANDIDATE_DISPLAYS = [":3", ":2", ":10", ":1", ":0"]

# ---------------------------------------------------------------------------
# LINE schema column indices (recovered from LINE.exe process memory)
# ---------------------------------------------------------------------------

# CREATE TABLE _message(_from TEXT, _to TEXT, _toType INTEGER, _id TEXT,
#   _createdTime INTEGER, _deliveredTime INTEGER, _text TEXT, _location TEXT,
#   _hasContent INTEGER, _contentType INTEGER, _contentMeta TEXT, ...)
MSG_FROM = 0
MSG_TO = 1
MSG_TOTYPE = 2
MSG_ID = 3
MSG_CREATED = 4
MSG_DELIVERED = 5
MSG_TEXT = 6
MSG_LOCATION = 7
MSG_HASCONTENT = 8
MSG_CONTENTTYPE = 9
MSG_CONTENTMETA = 10
MSG_RELATED_ID = 14
MSG_RELATION_TYPE = 15
MSG_RELATED_SERVICE = 17

# CREATE TABLE _contact(_mid TEXT, _createdTime INTEGER, _type INTEGER,
#   _status INTEGER, _displayName TEXT, _displayNameOverridden TEXT, ...)
CT_MID = 0
CT_DISPLAYNAME = 4
CT_DISPLAYNAME_OVERRIDDEN = 5

# CREATE TABLE _chat(_id TEXT, _midType INTEGER, _lastMessage TEXT,
#   _lastUpdatedTime INTEGER, ...). midType 2 is a group (id starts
#   with c). midType 0 is a 1:1 user chat.
CHAT_ID = 0
CHAT_MIDTYPE = 1
CHAT_UPDATED = 3
CHAT_UNREAD = 6
GROUP_MIDTYPE = 2

# CREATE TABLE _chatOption(_id TEXT, _hidden INTEGER, _notification INTEGER,
#   _pinnedTime INTEGER, _utilityData TEXT, PRIMARY KEY (_id))
OPT_ID = 0
OPT_HIDDEN = 1
OPT_PINNED = 3

# Joined-group roster written by every successful scan_memory.
GROUPS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "groups.json")

# Content type codes used by LINE.
CT_TEXT = 0
CT_STICKER = 1
CT_FLEX = 22

CONTENT_TYPE_NAMES = {
    0: "text", 1: "image", 2: "video", 3: "audio", 4: "html",
    5: "pdf", 6: "call", 7: "sticker", 8: "presence", 9: "gift",
    10: "groupboard", 11: "applink", 12: "link", 13: "contact",
    14: "file", 15: "location", 16: "postnotification", 17: "rich",
    18: "chatevent", 19: "music", 20: "payment", 21: "extimage", 22: "flex",
}


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def is_line_cmdline(raw):
    """Match executable argv, never shell/python source mentioning LINE.exe."""
    argv = raw.decode("utf-8", "replace").split("\0")
    names = [arg.replace("\\", "/").rsplit("/", 1)[-1].lower() for arg in argv]
    if not names or names[0] not in ("line.exe", "wine", "wine64", "wine-preloader", "wine64-preloader"):
        return False
    return "line.exe" in names and "run" in argv and "-t" in argv


def find_line_pid(preferred=None):
    """Find the LINE.exe PID by scanning `/proc`.

    Matches processes whose cmdline contains both `LINE.exe` and
    `run -t` (the LINE Desktop launcher form). If `preferred` is given
    and still alive + matches, returns it without a full scan.

    Returns int PID, or None if not found.
    """
    if preferred:
        try:
            with open(f"/proc/{preferred}/cmdline", "rb") as f:
                cmd = f.read()
            if is_line_cmdline(cmd):
                return preferred
        except OSError:
            pass
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                cmd = f.read()
        except OSError:
            continue
        if is_line_cmdline(cmd):
            return int(name)
    return None


def find_line_windows(display):
    """Return list of window IDs (hex strings) for LINE top-level windows
    on the given X display, via `xdotool search --class line.exe`."""
    try:
        r = subprocess.run(
            ["xdotool", "search", "--class", "line.exe"],
            env=dict(os.environ, DISPLAY=display), timeout=5,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [w for w in r.stdout.split() if w.strip()]


def find_line_display():
    """Auto-detect the X display that has a LINE window on it.

    Probes the common displays in `CANDIDATE_DISPLAYS` and returns the
    first one where `xdotool search --class line.exe` finds at least one
    window. Returns None if no LINE window is found on any of them.
    """
    for d in CANDIDATE_DISPLAYS:
        if find_line_windows(d):
            return d
    return None


# ---------------------------------------------------------------------------
# SQLite b-tree leaf-table page parser (minimal, in-memory only)
# ---------------------------------------------------------------------------

def read_varint(buf, off):
    """SQLite varint: up to 9 bytes, big-endian, 7 bits per byte
    (8 bits in the 9th). Returns (value, next_offset)."""
    val = 0
    if off < 0:
        raise ValueError("negative varint offset")
    for i in range(9):
        if off + i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[off + i]
        if i == 8:
            val = (val << 8) | b
            return val, off + 9
        val = (val << 7) | (b & 0x7f)
        if not (b & 0x80):
            return val, off + i + 1
    return val, off + 9


def parse_leaf_table_page(page):
    """Parse a SQLite leaf-table b-tree page (type 0x0d). Return list of
    (rowid, [column_values]). Reject incomplete/overflow records: bytes of
    another cell must never become text or mention metadata."""
    if len(page) < 8 or page[0] != 0x0d:
        return []
    ncells = struct.unpack(">H", page[3:5])[0]
    if ncells == 0 or ncells > 500 or 8 + 2 * ncells > len(page):
        return []
    rows = []
    for i in range(ncells):
        ptr_off = 8 + i * 2
        if ptr_off + 2 > len(page):
            break
        cell_off = struct.unpack(">H", page[ptr_off:ptr_off + 2])[0]
        if cell_off < 8 + 2 * ncells or cell_off >= len(page):
            continue
        try:
            payload_len, o = read_varint(page, cell_off)
            rowid, o = read_varint(page, o)
            if payload_len > PGSZ - 35 or payload_len > len(page) - o:
                continue  # overflow pages are not followed
            payload = page[o:o + payload_len]
            hdr_len, ho = read_varint(payload, 0)
            if not ho <= hdr_len <= len(payload):
                continue
            serial_types = []
            p = ho
            while p < hdr_len:
                st, p = read_varint(payload, p)
                serial_types.append(st)
            if p != hdr_len or any(st in (10, 11) for st in serial_types):
                continue
            vals = []
            vp = hdr_len
            for st in serial_types:
                width = ({0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6,
                          6: 8, 7: 8, 8: 0, 9: 0}.get(st)
                         if st < 12 else (st - 12) // 2)
                if width is None or vp + width > len(payload):
                    raise ValueError("truncated SQLite field")
                if st == 0:
                    vals.append(None)
                elif st == 1:
                    vals.append(int.from_bytes(payload[vp:vp + 1], 'big', signed=True)); vp += 1
                elif st == 2:
                    vals.append(int.from_bytes(payload[vp:vp + 2], 'big', signed=True)); vp += 2
                elif st == 3:
                    vals.append(int.from_bytes(payload[vp:vp + 3], 'big', signed=True)); vp += 3
                elif st == 4:
                    vals.append(int.from_bytes(payload[vp:vp + 4], 'big', signed=True)); vp += 4
                elif st == 5:
                    vals.append(int.from_bytes(payload[vp:vp + 6], 'big', signed=True)); vp += 6
                elif st == 6:
                    vals.append(int.from_bytes(payload[vp:vp + 8], 'big', signed=True)); vp += 8
                elif st == 7:
                    vals.append(struct.unpack('>d', payload[vp:vp + 8])[0]); vp += 8
                elif st == 8:
                    vals.append(0)
                elif st == 9:
                    vals.append(1)
                elif st >= 12 and st % 2 == 0:
                    n = (st - 12) // 2
                    vals.append(payload[vp:vp + n]); vp += n
                elif st >= 13 and st % 2 == 1:
                    n = (st - 13) // 2
                    vals.append(payload[vp:vp + n].decode('utf-8', 'replace')); vp += n
                else:
                    vals.append(None)
            rows.append((rowid, vals))
        except (ValueError, struct.error):
            continue
    return rows


# ---------------------------------------------------------------------------
# Row heuristics
# ---------------------------------------------------------------------------

def looks_like_message_row(vals):
    """Heuristic: does this parsed row look like a `_message` record?
    Require both `_from` and `_to` to be LINE mids, `_id` to be a
    plausible message id, and `_createdTime` to be a sane ms epoch."""
    if len(vals) < 7:
        return False
    frm = vals[MSG_FROM]; to = vals[MSG_TO]; mid = vals[MSG_ID]
    ct = vals[MSG_CREATED]
    if not isinstance(frm, str) or not MID_RE.fullmatch(frm):
        return False
    if not isinstance(to, str) or not MID_RE.fullmatch(to):
        return False
    if not isinstance(mid, str) or len(mid) < 6:
        return False
    if not isinstance(ct, int):
        return False
    # createdTime is a ms epoch; sanity range 2010-01-01 .. 2035-01-01
    if not (1262304000000 < ct < 2050000000000):
        return False
    return True


def looks_like_contact_row(vals):
    """Heuristic: does this parsed row look like a `_contact` record?"""
    if len(vals) <= CT_DISPLAYNAME_OVERRIDDEN:
        return False
    mid = vals[CT_MID]
    if not isinstance(mid, str) or not MID_RE.fullmatch(mid):
        return False
    return True


def looks_like_group_chat_row(vals):
    """Heuristic: does this parsed row look like a `_groupChat` record?

    Schema (recovered from process memory):
      _type, _chatMid, _createdTime, _notificationDisabled,
      _favoriteTimestamp, _chatName, _picturePath, _extra, _status,
      _addedTime, _inviterMId
    Group/room ids start with `c`. The display name is `_chatName`.
    """
    if len(vals) < 11:
        return False
    typ, mid, created = vals[0], vals[1], vals[2]
    notif, fav, name = vals[3], vals[4], vals[5]
    status, added, inviter = vals[8], vals[9], vals[10]
    if not isinstance(typ, int) or typ not in (0, 1, 2):
        return False
    if not isinstance(mid, str) or not mid.startswith("c"):
        return False
    if not MID_RE.fullmatch(mid):
        return False
    if not isinstance(created, int) or not (1262304000000 < created < 2050000000000):
        return False
    if not isinstance(notif, int) or notif not in (0, 1):
        return False
    if not isinstance(fav, int):
        return False
    if not isinstance(name, str):
        return False
    name = name.strip()
    # A joined group can be in this table before it has a display name.
    if len(name) > 80 or (name and name[0] in "{[/"):
        return False
    if name and MID_RE.fullmatch(name):
        return False
    if not isinstance(status, int) or not (0 <= status <= 10):
        return False
    if not isinstance(added, int) or not (1262304000000 < added < 2050000000000):
        return False
    if not (isinstance(inviter, str) and inviter.startswith("u")
            and MID_RE.fullmatch(inviter)):
        return False
    return True


def looks_like_chat_row(vals):
    """Heuristic: a `_chat` row (the recent-chat list, groups included).

    `_id` is the room mid, `_midType` 2 is a group, `_lastUpdatedTime`
    is what the chat list sorts by when nothing is pinned.
    """
    if len(vals) < 17:
        return False
    mid = vals[CHAT_ID]
    mid_type = vals[CHAT_MIDTYPE]
    updated = vals[CHAT_UPDATED]
    unread = vals[CHAT_UNREAD]
    if not isinstance(mid, str) or not MID_RE.fullmatch(mid):
        return False
    if mid[0] not in ("u", "c", "r"):
        return False
    if not isinstance(mid_type, int) or mid_type not in (0, 1, 2, 3):
        return False
    if not isinstance(updated, int) or not (1262304000000 < updated < 2050000000000):
        return False
    if not isinstance(unread, int) or not (0 <= unread <= 1000000):
        return False
    return True


def looks_like_chat_option_row(vals):
    """Heuristic: a `_chatOption` row (`_hidden`, `_pinnedTime`)."""
    if len(vals) != 5:
        return False
    mid, hidden, pinned = vals[OPT_ID], vals[OPT_HIDDEN], vals[OPT_PINNED]
    if not isinstance(mid, str) or not MID_RE.fullmatch(mid):
        return False
    if mid[0] not in ("u", "c", "r"):
        return False
    if not isinstance(hidden, int) or hidden not in (0, 1):
        return False
    if not isinstance(vals[2], int) or vals[2] not in (0, 1):
        return False
    if not isinstance(pinned, int) or pinned < 0:
        return False
    if pinned and not (1262304000000 < pinned < 2050000000000):
        return False
    util = vals[4]
    if not isinstance(util, (str, type(None))):
        return False
    return True


def _remember_group(groups, mid, name):
    """Record one joined group. An empty name does not erase a known one."""
    if not (isinstance(mid, str) and mid.startswith("c") and MID_RE.fullmatch(mid)):
        return
    if not isinstance(name, str):
        name = ""
    name = name.strip()
    if len(name) > 80 or (name[:1] in "{[/") or (name and MID_RE.fullmatch(name)):
        name = ""
    cur = groups.get(mid)
    if cur is None or (name and name != cur):
        groups[mid] = name


def _ordered_chats(chats, options):
    """Visible chat-list order: pinned first, then recently updated.

    `chats` is mid → lastUpdated ms. `options` is mid → (hidden, pinned).
    Hidden rows are left out. This matched the on-screen list top to
    bottom on the 742×806 window (no pins).
    """
    rows = []
    for mid, updated in chats.items():
        hidden, pinned = options.get(mid, (0, 0))
        if hidden:
            continue
        rows.append((0 if pinned else 1, -(pinned or 0), -(updated or 0), mid))
    rows.sort()
    return [row[3] for row in rows]


def _load_group_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, val in data.items():
        if (isinstance(key, str) and key.startswith("c")
                and MID_RE.fullmatch(key)):
            out[key] = val.strip() if isinstance(val, str) else ""
    return out


def persist_group_roster(groups, path=None):
    """Merge `groups` (room mid → name) into `groups.json`.

    A scan that finds nothing does not wipe the file. A non-empty name
    replaces a blank one. Returns the merged map.
    """
    path = path or GROUPS_JSON
    prev = _load_group_file(path)
    if not groups:
        return prev
    for mid, name in groups.items():
        if mid not in prev:
            prev[mid] = name or ""
        elif name and prev[mid] != name:
            prev[mid] = name
    ordered = {k: prev[k] for k in sorted(prev)}
    try:
        atomic_write_text(path, json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        return ordered
    return ordered


def atomic_write_text(path, text):
    """Unique sibling temp avoids cross-process .tmp collisions; flush before rename."""
    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Process memory scanning
# ---------------------------------------------------------------------------

def iter_readable_regions(pid, writable_only=False):
    """Yield (start, end, desc) for readable, non-code regions of pid."""
    try:
        with open(f"/proc/{pid}/maps") as f:
            maps = f.read().splitlines()
    except OSError:
        return
    for line in maps:
        m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S+) \S+ \S+ \S+\s*(.*)", line)
        if not m:
            continue
        start = int(m.group(1), 16); end = int(m.group(2), 16)
        perms = m.group(3); desc = m.group(4)
        if 'r' not in perms:
            continue
        if writable_only and 'w' not in perms:
            continue
        if end - start > 256 * 1024 * 1024:
            continue
        low = desc.lower()
        if '.dll' in low or '.so' in low or low.endswith('line.exe'):
            continue
        yield start, end, desc


def _merge_message(messages, row):
    """Insert `row` or fill gaps on the same message id.

    A decrypted leaf page often keeps an older copy, while the live JSON
    or the INSERT text still has `_text` and `_contentMetadata`. The copy
    that carries MENTION, or the longer text, wins that field.
    """
    mid = row.get("id")
    if not mid:
        return
    prev = messages.get(mid)
    if prev is None:
        messages[mid] = row
        return
    if any(prev.get(key) != row.get(key) for key in ("from", "to", "createdTime")):
        return  # conflicting heap fragments cannot donate command text/mentions
    new_text = row.get("text")
    if isinstance(new_text, str) and new_text and (
            not prev.get("text") or len(new_text) > len(prev.get("text") or "")):
        prev["text"] = new_text
        prev["source"] = row.get("source", "unknown")
    new_meta = row.get("contentMeta")
    old_meta = prev.get("contentMeta")
    if isinstance(new_meta, str) and new_meta:
        old_s = old_meta if isinstance(old_meta, str) else ""
        if not old_s or ("MENTION" in new_meta and "MENTION" not in old_s):
            prev["contentMeta"] = new_meta
    if prev.get("contentType") in (None, 0) and row.get("contentType") not in (None, 0):
        prev["contentType"] = row["contentType"]
    if prev.get("deliveredTime") is None and row.get("deliveredTime") is not None:
        prev["deliveredTime"] = row["deliveredTime"]
    # Keep the relation together; never donate it across conflicting identities.
    if not prev.get("related_message_id") and row.get("related_message_id"):
        for key in ("related_message_id", "message_relation_type",
                    "related_message_service_code", "reply_to_id"):
            prev[key] = row.get(key)


def _relation_fields(meta, related_id=None, relation_type=None, service=None):
    """LINE 26 quote relation: row fields or the observed metadata strings.

    The real metadata uses server_message_id / 'reply' / 'talk', whereas
    SQLite and serializer fields use an ID string / 3 / 1. Other relations
    are retained but must not be interpreted as quotes.
    """
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta_id = meta.get("message_relation_server_message_id")
    meta_type = meta.get("message_relation_type_code")
    meta_service = meta.get("message_relation_service_code")

    def number(value, name=None, code=None):
        if name is not None and value == name:
            return code
        if type(value) is int:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}", value):
            return int(value)
        return None

    rid = related_id if related_id not in (None, "") else meta_id
    if type(rid) is int:
        rid = str(rid)
    if not isinstance(rid, str) or not re.fullmatch(r"[0-9]{6,24}", rid) or int(rid) == 0:
        rid = None
    typ = number(relation_type) if relation_type is not None else number(meta_type, "reply", 3)
    svc = number(service) if service is not None else number(meta_service, "talk", 1)
    raw_service = service if service is not None else meta_service
    if svc is None and raw_service not in (None, ""):
        svc = str(raw_service)[:64]  # unknown services must not look like absent fields
    conflict = (meta_id not in (None, "") and rid != str(meta_id))
    if meta_type is not None and relation_type is not None:
        conflict |= number(meta_type, "reply", 3) != typ
    if meta_service is not None and service is not None:
        conflict |= number(meta_service, "talk", 1) != svc
    return dict(related_message_id=rid, message_relation_type=typ,
                related_message_service_code=svc,
                reply_to_id=rid if typ == 3 and not conflict else None)


def _message_record(frm, to, mid, created, text, content_type, content_meta,
                    to_type=None, delivered=None, related_id=None,
                    relation_type=None, related_service=None, source="unknown"):
    """Return a message dict, or None when the identity fields are not a row."""
    if not isinstance(frm, str) or not MID_RE.fullmatch(frm):
        return None
    if not isinstance(to, str) or not MID_RE.fullmatch(to):
        return None
    if not isinstance(mid, str) or len(mid) < 6:
        return None
    if isinstance(created, str) and created.isdigit():
        created = int(created)
    if not isinstance(created, int) or not (1262304000000 < created < 2050000000000):
        return None
    if isinstance(content_meta, dict):
        content_meta = json.dumps(content_meta, ensure_ascii=False, separators=(",", ":"))
    elif not isinstance(content_meta, str) or not content_meta.strip():
        content_meta = None
    if not isinstance(text, str):
        text = None
    if not isinstance(content_type, int):
        content_type = 0
    if not isinstance(delivered, int):
        delivered = None
    return {
        "from": frm,
        "to": to,
        "toType": to_type,
        "id": mid,
        "createdTime": created,
        "deliveredTime": delivered,
        "text": text,
        "contentType": content_type,
        "contentMeta": content_meta,
        "source": source,
        **_relation_fields(content_meta, related_id, relation_type, related_service),
    }


def _sqlite_message_record(vals):
    """Decode a validated _message row using the recovered 36-column schema."""
    def at(index):
        return vals[index] if len(vals) > index else None
    return _message_record(
        at(MSG_FROM), at(MSG_TO), at(MSG_ID), at(MSG_CREATED), at(MSG_TEXT),
        at(MSG_CONTENTTYPE), at(MSG_CONTENTMETA), to_type=at(MSG_TOTYPE),
        delivered=at(MSG_DELIVERED), related_id=at(MSG_RELATED_ID),
        relation_type=at(MSG_RELATION_TYPE), related_service=at(MSG_RELATED_SERVICE),
        source="sqlite")


def _json_object_end(buf, start, limit=12000):
    """Index just past the JSON object at `start`, or None."""
    depth = 0
    in_str = False
    esc = False
    end = min(len(buf), start + limit)
    for j in range(start, end):
        c = buf[j]
        if in_str:
            if esc:
                esc = False
            elif c == 0x5c:
                esc = True
            elif c == 0x22:
                in_str = False
            continue
        if c == 0x22:
            in_str = True
        elif c == 0x7b:
            depth += 1
        elif c == 0x7d:
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def _collect_json_messages(data, messages):
    """Message objects LINE keeps as JSON (`"from"`, `"to"`, `"text"`, MENTION).

    The page cache drops rows that were just inserted. The same row is
    still in the chat `_lastMessage` blob and in the serializer output.
    """
    seen = set()
    for match in re.finditer(rb'"from"\s*:\s*"', data):
        i = match.start()
        start = i + 1
        obj = None
        while True:
            start = data.rfind(b"{", max(0, i - 800), start)
            if start < 0:
                break
            if start in seen:
                continue
            seen.add(start)
            end = _json_object_end(data, start)
            if end is None:
                continue
            try:
                candidate = json.loads(data[start:end])
            except (ValueError, TypeError):
                continue
            if isinstance(candidate, dict) and "from" in candidate:
                obj = candidate
                break
        if not isinstance(obj, dict):
            continue
        mid = obj.get("id")
        if isinstance(mid, int):
            mid = str(mid)
        row = _message_record(
            obj.get("from"), obj.get("to"), mid, obj.get("createdTime"),
            obj.get("text"), obj.get("contentType"), obj.get("contentMetadata"),
            to_type=obj.get("toType"), delivered=obj.get("deliveredTime"),
            related_id=obj.get("relatedMessageId"),
            relation_type=obj.get("messageRelationType"),
            related_service=obj.get("relatedMessageServiceCode"), source="heap-json",
        )
        if row:
            _merge_message(messages, row)


_SQL_MARKS = (
    b"INSERT INTO _message(",
    b"INSERT INTO _message (",
    b"REPLACE INTO _message(",
    b"REPLACE INTO _message (",
    b"INSERT OR REPLACE INTO _message(",
    b"INSERT OR REPLACE INTO _message (",
)
_SQL_COL = {
    "_from": "from",
    "_to": "to",
    "_id": "id",
    "_createdTime": "createdTime",
    "_deliveredTime": "deliveredTime",
    "_text": "text",
    "_contentType": "contentType",
    "_contentMetadata": "contentMeta",
    "_contentMeta": "contentMeta",
    "_toType": "toType",
    "_relatedMessageId": "relatedId",
    "_messageRelationType": "relationType",
    "_relatedMessageServiceCode": "relatedService",
}


def _sql_columns(buf, i):
    """Parse `_message` column list starting at `(`. Returns (names, next)."""
    if i >= len(buf) or buf[i] != 0x28:
        return None
    j = buf.find(b")", i + 1, i + 2000)
    if j < 0:
        return None
    try:
        inner = buf[i + 1:j].decode("utf-8")
    except UnicodeDecodeError:
        return None
    cols = [c.strip() for c in inner.split(",")]
    if not cols or any(not c or " " in c for c in cols):
        return None
    return cols, j + 1


def _sql_values(buf, i):
    """Parse one parenthesized SQL value list. Returns (values, next) or None."""
    if i >= len(buf) or buf[i] != 0x28:
        return None
    vals = []
    i += 1
    n = len(buf)
    while i < n:
        while i < n and buf[i] in b" \t\r\n":
            i += 1
        if i >= n:
            return None
        if buf[i] == 0x29:
            return vals, i + 1
        if buf[i] == 0x27:
            i += 1
            raw = bytearray()
            while i < n:
                if buf[i] == 0x27:
                    if i + 1 < n and buf[i + 1] == 0x27:
                        raw.append(0x27)
                        i += 2
                        continue
                    i += 1
                    break
                raw.append(buf[i])
                i += 1
            else:
                return None
            vals.append(raw.decode("utf-8", "replace"))
        elif buf[i:i + 4].upper() == b"NULL" and (
                i + 4 >= n or buf[i + 4] in b" \t\r\n,)"):
            vals.append(None)
            i += 4
        elif buf[i] == 0x2d or 48 <= buf[i] <= 57:
            j = i + 1
            while j < n and 48 <= buf[j] <= 57:
                j += 1
            try:
                vals.append(int(buf[i:j]))
            except ValueError:
                return None
            i = j
        else:
            return None
        while i < n and buf[i] in b" \t\r\n":
            i += 1
        if i < n and buf[i] == 0x2c:
            i += 1
            if not buf[i:].lstrip() or buf[i:].lstrip().startswith(b")"):
                return None
            continue
        if i < n and buf[i] == 0x29:
            return vals, i + 1
        return None
    return None


def _collect_sql_messages(data, messages):
    """Rows still sitting in plaintext INSERT/REPLACE `_message` text.

    The decrypted leaf page for a just-written row is not in the cache
    the leaf scan reads. The statement that inserted it is still in the
    heap, with `_contentMetadata` (including MENTION) and `_text`.
    """
    for mark in _SQL_MARKS:
        idx = 0
        while True:
            i = data.find(mark, idx)
            if i < 0:
                break
            idx = i + len(mark)
            window = data[i:i + 8000]
            paren = window.find(b"(")
            if paren < 0:
                continue
            parsed = _sql_columns(window, paren)
            if not parsed:
                continue
            cols, after = parsed
            vpos = window.find(b"VALUES", after, after + 40)
            if vpos < 0:
                continue
            vparen = window.find(b"(", vpos, vpos + 20)
            if vparen < 0:
                continue
            got = _sql_values(window, vparen)
            if not got:
                continue
            vals, _next = got
            if len(vals) != len(cols):
                continue
            fields = {}
            for col, val in zip(cols, vals):
                key = _SQL_COL.get(col)
                if key:
                    fields[key] = val
            row = _message_record(
                fields.get("from"), fields.get("to"), fields.get("id"),
                fields.get("createdTime"), fields.get("text"),
                fields.get("contentType"), fields.get("contentMeta"),
                to_type=fields.get("toType"),
                delivered=fields.get("deliveredTime"),
                related_id=fields.get("relatedId"),
                relation_type=fields.get("relationType"),
                related_service=fields.get("relatedService"), source="heap-sql",
            )
            if row:
                _merge_message(messages, row)


_GROUP_SQL_MARKS = (
    b"INSERT INTO _groupChat(",
    b"INSERT INTO _groupChat (",
    b"REPLACE INTO _groupChat(",
    b"REPLACE INTO _groupChat (",
    b"INSERT OR REPLACE INTO _groupChat(",
    b"INSERT OR REPLACE INTO _groupChat (",
)


def _collect_sql_groups(data, groups):
    """Joined groups still sitting in plaintext `_groupChat` statements.

    A just-written row is often in the heap before its decrypted leaf
    page is. Column order is whatever the statement lists (`_chatMid`,
    `_chatName`), not the table order.
    """
    for mark in _GROUP_SQL_MARKS:
        idx = 0
        while True:
            i = data.find(mark, idx)
            if i < 0:
                break
            idx = i + len(mark)
            window = data[i:i + 16000]
            paren = window.find(b"(")
            if paren < 0:
                continue
            parsed = _sql_columns(window, paren)
            if not parsed:
                continue
            cols, after = parsed
            vpos = window.find(b"VALUES", after, after + 40)
            if vpos < 0:
                continue
            vparen = window.find(b"(", vpos, vpos + 20)
            if vparen < 0:
                continue
            got = _sql_values(window, vparen)
            if not got:
                continue
            vals, _next = got
            if len(vals) != len(cols):
                continue
            fields = dict(zip(cols, vals))
            _remember_group(groups, fields.get("_chatMid"), fields.get("_chatName") or "")


def scan_memory(pid):
    """Scan process memory for messages and contacts.

    Returns (messages, contacts, pages_scanned, pages_matched) where
    `messages` is a dict keyed by message `_id` and `contacts` is a dict
    mid -> displayName. Sources, merged by id:

      * decrypted SQLite leaf pages (`_message`, `_contact`, `_groupChat`,
        `_chat`, `_chatOption`)
      * live message JSON (`"from"` / `"to"` / `"text"` / contentMetadata)
      * plaintext `INSERT`/`REPLACE INTO _message` statements
      * plaintext `INSERT`/`REPLACE INTO _groupChat` statements

    Joined groups are room mid → `_chatName` (the name may be empty).
    They come from `_groupChat` rows, from `_chat` rows whose `_midType`
    is 2, and from those INSERT statements. The map is stashed on
    `scan_memory.last_groups` and merged into `groups.json`. A non-empty
    name is also copied onto `contacts`. `scan_memory.last_chat_order`
    is the visible chat list (pinned, then recent). Read-only toward
    LINE; the only write is `groups.json`.
    """
    messages = {}
    contacts = {}
    groups = {}
    chats = {}
    options = {}
    pages_scanned = 0
    pages_matched = 0
    try:
        mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
    except OSError:
        scan_memory.last_groups = {}
        scan_memory.last_chat_order = []
        scan_memory.last_pid = pid
        scan_memory.last_at = time.monotonic()
        raise
    try:
        for start, end, desc in iter_readable_regions(pid):
            size = end - start
            try:
                mem.seek(start)
                data = mem.read(size)
            except OSError:
                continue
            idx = 0
            sig = b"\x0d\x00\x00"
            while True:
                idx = data.find(sig, idx)
                if idx < 0 or idx + PGSZ > len(data):
                    break
                page = data[idx:idx + PGSZ]
                ncells = struct.unpack(">H", page[3:5])[0]
                ccs = struct.unpack(">H", page[5:7])[0]
                idx += 1
                if not (1 <= ncells <= 200 and 8 <= ccs < PGSZ):
                    continue
                pages_scanned += 1
                rows = parse_leaf_table_page(page)
                if not rows:
                    continue
                pages_matched += 1
                for rowid, vals in rows:
                    if looks_like_message_row(vals):
                        mid = vals[MSG_ID]
                        if mid:
                            row = _sqlite_message_record(vals)
                            if row:
                                _merge_message(messages, row)
                    elif looks_like_chat_row(vals):
                        chats[vals[CHAT_ID]] = vals[CHAT_UPDATED]
                        if (vals[CHAT_MIDTYPE] == GROUP_MIDTYPE
                                and isinstance(vals[CHAT_ID], str)
                                and vals[CHAT_ID].startswith("c")):
                            _remember_group(groups, vals[CHAT_ID], "")
                    elif looks_like_chat_option_row(vals):
                        options[vals[OPT_ID]] = (vals[OPT_HIDDEN], vals[OPT_PINNED])
                    elif looks_like_contact_row(vals):
                        cmid = vals[CT_MID]
                        name = vals[CT_DISPLAYNAME_OVERRIDDEN] or vals[CT_DISPLAYNAME]
                        if cmid and isinstance(name, str) and name:
                            contacts.setdefault(cmid, name)
                    elif looks_like_group_chat_row(vals):
                        _remember_group(
                            groups, vals[1],
                            vals[5] if isinstance(vals[5], str) else "")
            _collect_json_messages(data, messages)
            _collect_sql_messages(data, messages)
            _collect_sql_groups(data, groups)
    finally:
        mem.close()
    # A restarted/partial cache can identify a joined room without loading its
    # _groupChat name. Reuse its persisted name for both routing and discovery.
    # Only enrich rooms actually found in this scan; do not resurrect old rooms.
    roster = persist_group_roster(groups)
    for mid in groups:
        if not groups[mid]:
            groups[mid] = roster.get(mid) or ""
    # A real group name wins over a stray contact hit on the same room id.
    for mid, name in groups.items():
        if name:
            contacts[mid] = name
    scan_memory.last_groups = dict(groups)
    scan_memory.last_chat_order = _ordered_chats(chats, options)
    scan_memory.last_pid = pid
    scan_memory.last_at = time.monotonic()
    # Import here because reply_context uses this module's parsers. Keep the
    # cache ephemeral and attach only one level of quote context to each row.
    import reply_context
    reply_context.CACHE.observe(pid, messages)
    for row in messages.values():
        if row.get("reply_to_id"):
            context = reply_context.resolve_reply(row, messages, pid=pid)
            if context.get("found"):
                context = dict(context, message=dict(context["message"]))
                context["message"].pop("reply_context", None)
            row["reply_context"] = context
    return messages, contacts, pages_scanned, pages_matched


def chat_list_mids(pid, max_age=20):
    """Mids in on-screen chat-list order, top to bottom.

    Pinned chats first, then most recently updated. Hidden chats are
    omitted. Reuses the last `scan_memory` for this pid when it is fresh.
    """
    if not pid:
        return []
    order = getattr(scan_memory, "last_chat_order", None)
    last_at = getattr(scan_memory, "last_at", 0)
    if (getattr(scan_memory, "last_pid", None) == pid
            and order is not None
            and (time.monotonic() - last_at) < max_age):
        return list(order)
    scan_memory(pid)
    return list(getattr(scan_memory, "last_chat_order", None) or [])


# ---------------------------------------------------------------------------
# Currently open chat (Qt objects inside LINE.exe)
# ---------------------------------------------------------------------------
#
# LINE 26.0.2 keeps the open chat's mid in two UTF-16 string fields that
# move together when the user switches chats:
#   * LineApplication + 0xad8  (the activeChatId QString; length qword is
#     33 and the previous qword points 16 bytes before the characters)
#   * AllInOneWindow + 0x1048  (the same mid, same layout)
# RVAs are relative to the PE image that contains the RTTI names. Both
# fields must agree; otherwise the open chat is reported as unknown.

_OPEN_CHAT_ANCHORS = (
    # rtti name, name RVA, primary vtable RVA, mid-pointer offset
    (b".?AVLineApplication@@", 0x3993E88, 0x281D188, 0xAD8),
    (b".?AVAllInOneWindow@@", 0x3964758, 0x26DCEE8, 0x1048),
)
_open_chat_cache = {}  # pid -> (image_base, [(obj, offset, vtable), ...])


def _mem_read(mem, addr, n):
    if not isinstance(addr, int) or addr < 0x10000 or addr > 0x7FFFFFFFFFFF:
        return b""
    try:
        mem.seek(addr)
        return mem.read(n)
    except OSError:
        return b""


def _mem_u64(mem, addr):
    raw = _mem_read(mem, addr, 8)
    if len(raw) != 8:
        return None
    return struct.unpack("<Q", raw)[0]


def _image_base(pid, mem):
    """PE base of the LINE image that holds the RTTI names, or None."""
    # This Wine build normally maps at its PE preferred base. Check all build
    # anchors directly before scanning the whole process (formerly ~4 seconds).
    preferred = 0x140000000
    if all(_mem_read(mem, preferred + rva, len(name)) == name
           for name, rva, _vt, _off in _OPEN_CHAT_ANCHORS):
        return preferred
    token, rva, _vt, _off = _OPEN_CHAT_ANCHORS[0]
    for start, end, _desc in iter_readable_regions(pid):
        if end <= 0x100000000:
            continue
        try:
            mem.seek(start)
            data = mem.read(end - start)
        except OSError:
            continue
        idx = 0
        while True:
            i = data.find(token, idx)
            if i < 0:
                break
            base = (start + i) - rva
            if base > 0 and all(
                _mem_read(mem, base + name_rva, len(name)) == name
                for name, name_rva, _v, _o in _OPEN_CHAT_ANCHORS
            ):
                return base
            idx = i + 1
    return None


def _read_mid_field(mem, obj, offset):
    """UTF-16 mid at obj+offset, or None if the layout does not match."""
    hdr = _mem_u64(mem, obj + offset - 8)
    ptr = _mem_u64(mem, obj + offset)
    length = _mem_u64(mem, obj + offset + 8)
    if hdr is None or ptr is None or length != 33 or hdr != ptr - 16:
        return None
    raw = _mem_read(mem, ptr, 66)
    if len(raw) != 66:
        return None
    try:
        text = raw.decode("utf-16le")
    except UnicodeDecodeError:
        return None
    if text[0] not in ("u", "c") or not MID_RE.fullmatch(text):
        return None
    return text


def _locate_open_chat(pid, mem):
    base = _image_base(pid, mem)
    if base is None:
        return None
    # Find both anchors in one pass. bytes.find on the pointer patterns across
    # two full heap passes cost ~4s; a literal regex scan takes a fraction of it.
    anchors = [(base + vt_rva, offset) for _n, _r, vt_rva, offset in _OPEN_CHAT_ANCHORS]
    needles = {struct.pack('<Q', vt): (vt, offset) for vt, offset in anchors}
    pattern = re.compile(b'|'.join(re.escape(needle) for needle in needles))
    objects = {vt: set() for vt, _offset in anchors}
    for start, end, _desc in iter_readable_regions(pid, writable_only=True):
        data = _mem_read(mem, start, end - start)
        for match in pattern.finditer(data):
            vt, offset = needles[match[0]]
            obj = start + match.start()
            if obj % 8 == 0 and _read_mid_field(mem, obj, offset):
                objects[vt].add(obj)
    if any(len(objects[vt]) != 1 for vt, _offset in anchors):
        return None
    return base, [(next(iter(objects[vt])), offset, vt) for vt, offset in anchors]


def current_open_chat_mid(pid):
    """Mid of the chat LINE is showing now, or None if it cannot be read.

    Both the LineApplication and AllInOneWindow fields must be present
    and equal. A miss is unknown, not a guess.
    """
    if not pid:
        return None
    try:
        mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
    except OSError:
        return None
    try:
        cached = _open_chat_cache.get(pid)
        if cached is not None:
            _base, anchors = cached
            if any(_mem_u64(mem, obj) != vt for obj, _off, vt in anchors):
                cached = None
                _open_chat_cache.pop(pid, None)
        if cached is None:
            cached = _locate_open_chat(pid, mem)
            if cached is None:
                return None
            _open_chat_cache[pid] = cached
        mids = []
        for obj, offset, _vt in cached[1]:
            mid = _read_mid_field(mem, obj, offset)
            if not mid:
                _open_chat_cache.pop(pid, None)
                return None
            mids.append(mid)
        if len(mids) == 2 and mids[0] == mids[1]:
            return mids[0]
        return None
    finally:
        mem.close()


def resolve_chat_mid(contacts, name):
    """Exact display-name → one mid. None when it is missing or ambiguous."""
    if not isinstance(name, str) or not name.strip():
        return None
    want = name.strip()
    hits = [mid for mid, label in contacts.items()
            if isinstance(label, str) and label.strip() == want
            and isinstance(mid, str) and MID_RE.fullmatch(mid)
            and mid[0] in ("u", "c")]
    if len(hits) != 1:
        return None
    return hits[0]


def find_outgoing_copies(pid, text, self_mid=None):
    """In-memory copies of an outgoing message with this exact text.

    Returns dicts with from, to, id, createdTime, text. Looks at decrypted
    `_message` pages and at the live message JSON LINE keeps beside them
    (`"from"`, `"to"`, `"text"`). The page cache drops older rows; the
    JSON copy is what still showed the misrouted send after that.
    """
    if not pid or not isinstance(text, str) or not text:
        return []
    found = {}

    def add(frm, to, mid, created):
        if not (isinstance(frm, str) and isinstance(to, str)):
            return
        if self_mid and frm != self_mid:
            return
        if not MID_RE.fullmatch(frm) or not MID_RE.fullmatch(to):
            return
        key = (frm, to, mid or "", created or 0, text)
        found[key] = {
            "from": frm,
            "to": to,
            "id": mid,
            "createdTime": created,
            "text": text,
        }

    messages, _contacts, _sc, _ma = scan_memory(pid)
    for mid, row in messages.items():
        if row.get("text") != text:
            continue
        add(row.get("from"), row.get("to"), mid, row.get("createdTime"))

    # Only complete parsed records can establish a route. Combining nearby
    # regex hits could join the sender/target of two unrelated heap objects.
    return list(found.values())


# Recovered from this build's thrift::Talk::ContentType table.
MEDIA_CONTENT_TYPES = {1, 2, 3, 5, 14}


def find_outgoing_media(pid, self_mid, since_ms, filename=None):
    """Outgoing attachment-like rows from `self_mid` at or after `since_ms`.

    A row counts when its contentType is image, video, or file, or when
    the filename appears in the text or content metadata of a non-text
    row. Does not read the file on disk. Scans the same message cache
    as `scan_memory`.
    """
    if not pid or not self_mid:
        return []
    base = os.path.basename(filename).lower() if filename else ""
    messages, _contacts, _sc, _ma = scan_memory(pid)
    hits = []
    for mid, row in messages.items():
        if row.get("from") != self_mid:
            continue
        created = row.get("createdTime") or 0
        if created < since_ms:
            continue
        ct = row.get("contentType") or 0
        meta = (row.get("contentMeta") or "").lower()
        if ct in MEDIA_CONTENT_TYPES and (not base or base in meta
                                         or base == (row.get("text") or "").lower()):
            hits.append(row)
    hits.sort(key=lambda r: r.get("createdTime") or 0)
    return hits


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_time(ms):
    """Format a LINE ms-epoch timestamp as a UTC+8 string."""
    if not ms:
        return "?"
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(TZ8)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms)


def fmt_content_type(ct):
    """Map a LINE contentType int to a human-readable name."""
    return CONTENT_TYPE_NAMES.get(ct, f"type{ct}")


def display_name(mid, contacts):
    """Resolve a mid to a display name via the contacts dict, falling
    back to a truncated mid."""
    if not mid:
        return "?"
    return (contacts.get(mid)
            or contacts.get(mid.lstrip("u"))
            or mid[:10] + "…")


def truncate(text, n=200):
    """Truncate text to n chars, replacing newlines with ⏎."""
    if text is None:
        return None
    text = text.replace("\n", " ⏎ ")
    if len(text) > n:
        return text[:n] + "…"
    return text
