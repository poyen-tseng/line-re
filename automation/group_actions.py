"""group_actions.py — fixed action set for `line_cli.py ai --group-bot`.

Grok returns one JSON object. This module parses it, checks schedule
times, and searches file names. It does not send, does not run a shell,
and does not read file contents.

Every path the bot may name must sit under an allowed root and must not
be a secret location (hidden components, keys, browser profiles, Bottles
prefixes, and similar).
"""
import fnmatch
import fcntl
from functools import wraps
import json
import math
import os
import re
import secrets
import stat
import time
from datetime import datetime, timedelta

import line_common as lc
import operator_commands

ACTION_TYPES = (
    "send_text",
    "schedule_text",
    "schedule_file",
    "send_file",
    "find_files",
    "list_schedules",
    "cancel_schedule",
) + operator_commands.ACTION_TYPES
PENDING_CAP = 20
SCHEDULE_HORIZON = timedelta(days=30)
TEXT_MAX = 300
FIND_MAX = 10

# Substrings that mark a path as off-limits. Hidden components (a path
# segment starting with ".") are rejected separately, which covers
# `/.ssh`, `/.config`, `/.claude`, `/.codex`, and `/.local/share/cursor*`.
_SECRET_BITS = (
    "credentials",
    "keyring",
    "cookies",
    "token",
    "secret",
    "bottles",
    "drive_c",
    "google-chrome",
    "chromium",
    "mozilla",
    "firefox",
    "brave",
    "microsoft-edge",
)
_SECRET_SUFFIXES = (".env", ".pem", ".key")

_OTP_RE = re.compile(
    r"(verification code|authentication code|account transfer code|"
    r"one[- ]time|驗證碼|認證碼|確認碼|\botp\b)",
    re.IGNORECASE,
)


def now_taipei():
    return datetime.now(lc.TZ8)


def iso8(dt):
    return dt.astimezone(lc.TZ8).isoformat(timespec="seconds")


def collapse_text(text, limit=TEXT_MAX):
    """One LINE message. Enter sends, so newlines become spaces."""
    if not isinstance(text, str):
        return None
    text = text.replace("\r", " ")
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = "".join(ch for ch in text if ch >= " ")
    text = text.strip()
    if not text or _OTP_RE.search(text):
        return None
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text or None


def is_secret_path(path):
    """True when this path must not be searched or sent.

    A hit is enough: hidden segment (`/.`), a secret suffix, or a
    denied substring anywhere in the path. Does not open the file.
    """
    if not isinstance(path, str) or not path.strip():
        return True
    raw = path.replace("\\", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    for part in parts:
        if part.startswith("."):
            return True
        low = part.lower()
        if low.endswith(_SECRET_SUFFIXES):
            return True
    folded = "/".join(parts).lower()
    return any(bit in folded for bit in _SECRET_BITS)


def under_roots(path, roots):
    """True when `path` resolves inside one of `roots`."""
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            base = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        try:
            if os.path.commonpath((real, base)) == base:
                return True
        except ValueError:
            continue
    return False


def path_allowed(path, roots):
    """Existing regular file under a root, not secret, not a symlink."""
    if is_secret_path(path) or not under_roots(path, roots):
        return False
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    if is_secret_path(real) or not under_roots(real, roots):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return True


def _name_match(filename, query):
    return fnmatch.fnmatch(filename.casefold(), query.casefold())


def _matches(path, query):
    name = os.path.basename(path)
    if "/" in query or "\\" in query:
        folded = path.replace("\\", "/").casefold()
        if fnmatch.fnmatch(folded, query.replace("\\", "/").casefold()):
            return True
        return _name_match(name, os.path.basename(query))
    return _name_match(name, query)


class FileSearchTimeout(OSError):
    pass


def _walk_files(roots, budget_seconds=5):
    """Yield regular files under roots. Skips secret dirs and symlinks.

    Never reads file contents. `lstat` is only used to skip symlinks.
    """
    deadline = time.monotonic() + budget_seconds
    seen_dirs = set()
    for root in roots:
        if not isinstance(root, str) or not root:
            continue
        base = os.path.abspath(root)
        if is_secret_path(base) or not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            if time.monotonic() >= deadline:
                raise FileSearchTimeout("file search timed out; specify an absolute path")
            real_dir = os.path.realpath(dirpath)
            if real_dir in seen_dirs or is_secret_path(dirpath) or is_secret_path(real_dir):
                dirnames[:] = []
                continue
            if not under_roots(real_dir, roots):
                dirnames[:] = []
                continue
            seen_dirs.add(real_dir)
            kept = []
            for name in dirnames:
                child = os.path.join(dirpath, name)
                if os.path.islink(child) or is_secret_path(child):
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                if time.monotonic() >= deadline:
                    raise FileSearchTimeout("file search timed out; specify an absolute path")
                path = os.path.join(dirpath, name)
                if os.path.islink(path) or is_secret_path(path):
                    continue
                if not under_roots(path, roots):
                    continue
                yield path


def _file_size(path):
    try:
        return os.lstat(path).st_size
    except OSError:
        return None


def fmt_size(n):
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def find_files(roots, query, limit=FIND_MAX):
    """Up to `limit` (path, size) pairs whose name matches `query`."""
    if limit <= 0 or not isinstance(query, str) or not query.strip():
        return []
    query = query.strip()
    if len(query) > 200 or "\x00" in query:
        return []
    hits = []
    try:
        for path in _walk_files(roots):
            if not _matches(path, query):
                continue
            if not path_allowed(path, roots):
                continue
            size = _file_size(path)
            if size is None:
                continue
            hits.append((path, size))
            if len(hits) >= limit:
                break
    except FileSearchTimeout:
        pass  # finding may return a bounded partial list; resolution may not
    return hits


def format_find(query, hits):
    if not hits:
        shown = collapse_text(query, limit=80) or "檔案"
        return f"找不到符合「{shown}」的檔案"
    parts = [f"{path} ({fmt_size(size)})" for path, size in hits]
    text = f"找到 {len(hits)} 個：" + "，".join(parts)
    return collapse_text(text, limit=500) or "找到檔案但清單太長"


def resolve_send_file(raw_path, roots, max_mb):
    """Return (linux_path, None) or (None, error_text).

    An absolute allowed file is used as-is. A bare name is searched
    under the roots; zero matches or more than one match does not send.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "沒有檔案路徑"
    raw_path = raw_path.strip()
    if "\x00" in raw_path or "\n" in raw_path or "\r" in raw_path:
        return None, "不能傳送這個路徑"
    if is_secret_path(raw_path):
        return None, "不能傳送這個路徑"
    if not math.isfinite(max_mb) or max_mb <= 0:
        return None, "檔案大小限制不正確"
    max_bytes = int(max_mb * 1024 * 1024)
    candidate = raw_path
    if not os.path.isabs(candidate):
        name = os.path.basename(candidate.replace("\\", "/"))
        if not name or is_secret_path(name):
            return None, "不能傳送這個路徑"
        matches = []
        try:
            for path in _walk_files(roots):
                if os.path.basename(path).casefold() != name.casefold():
                    continue
                if not path_allowed(path, roots):
                    continue
                matches.append(path)
                if len(matches) > 1:
                    break
        except FileSearchTimeout:
            return None, "檔案搜尋逾時，請指定完整絕對路徑"
        if not matches:
            return None, f"找不到檔案：{collapse_text(name, limit=80) or '檔案'}"
        if len(matches) > 1:
            shown = "，".join(matches[:5])
            return None, collapse_text(
                f"檔案有多個，請指定路徑：{shown}", limit=400)
        candidate = matches[0]
    if not under_roots(candidate, roots):
        return None, "路徑不在允許的目錄內"
    if is_secret_path(candidate):
        return None, "不能傳送這個路徑"
    if not path_allowed(candidate, roots):
        if os.path.lexists(candidate):
            return None, "不能傳送這個路徑"
        return None, "檔案不存在"
    size = _file_size(candidate)
    if size is None:
        return None, "檔案不存在"
    if size > max_bytes:
        return None, f"檔案超過 {max_mb:g} MB"
    return candidate, None


def wine_path(linux_path):
    """Logical Z: mapping; Flatpak may hide the host path. Uploads stage on C:."""
    ap = os.path.abspath(linux_path)
    return "Z:" + ap.replace("/", "\\")


def fallback_text(raw):
    """One sendable line from a reply that was not valid action JSON."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    text = re.split(r"\n\s*\n", text, maxsplit=1)[0].strip()
    changed = True
    pairs = (("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"),
             ("“", "”"), ("‘", "’"))
    while changed and len(text) >= 2:
        changed = False
        for a, b in pairs:
            if text.startswith(a) and text.endswith(b):
                text = text[len(a):-len(b)].strip()
                changed = True
                break
    text = re.sub(r"^(回覆|草稿|reply|response)\s*[:：]\s*", "", text,
                  flags=re.IGNORECASE).strip()
    return collapse_text(text)


def _extract_json(text):
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json|JSON)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    brace = s.find("{")
    bracket = s.find("[")
    if brace < 0 and bracket < 0:
        return None
    if brace < 0 or (bracket >= 0 and bracket < brace):
        start = bracket
    else:
        start = brace
    try:
        obj, _end = json.JSONDecoder().raw_decode(s[start:])
    except json.JSONDecodeError:
        return None
    return obj


def _normalize_action(item):
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind not in ACTION_TYPES:
        return None
    if kind in operator_commands.ACTION_TYPES:
        return operator_commands.normalize(item)
    if kind == "send_text":
        text = collapse_text(item.get("text"))
        if not text:
            return None
        return {"type": kind, "text": text}
    if kind == "schedule_text":
        text = collapse_text(item.get("text"))
        at = item.get("at")
        if not text or not isinstance(at, str) or not at.strip():
            return None
        return {"type": kind, "text": text, "at": at.strip()}
    if kind in ("send_file", "schedule_file"):
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        if kind == "schedule_file":
            at = item.get('at')
            if not isinstance(at, str) or not at.strip():
                return None
            return {"type": kind, "path": path.strip(), "at": at.strip()}
        return {"type": kind, "path": path.strip()}
    if kind == "find_files":
        query = item.get("query")
        if not isinstance(query, str) or not query.strip():
            return None
        return {"type": kind, "query": query.strip()}
    if kind == "cancel_schedule":
        sid = item.get("id")
        if not isinstance(sid, str) or not sid.strip():
            return None
        return {"type": kind, "id": sid.strip().lstrip("#")}
    return {"type": kind}


def parse_actions(raw, max_actions):
    """Return (actions, source, truncated).

    `source` is `json` when an object parsed, else `fallback` (one
    send_text of the cleaned prose) or `empty`.
    """
    cap = max_actions if max_actions and max_actions > 0 else 1
    obj = _extract_json(raw)
    if obj is None:
        if isinstance(raw, str) and ("{" in raw or "[" in raw):
            return [], "empty", False  # never paste malformed action JSON as prose
        text = fallback_text(raw)
        if not text:
            return [], "empty", False
        return [{"type": "send_text", "text": text}], "fallback", False
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict) and "actions" in obj:
        items = obj.get("actions")
        if isinstance(items, dict):
            items = [items]
    elif isinstance(obj, dict) and obj.get("type"):
        items = [obj]
    else:
        return [], "empty", False
    if not isinstance(items, list):
        return [], "empty", False
    actions = []
    for item in items:
        norm = _normalize_action(item)
        if norm:
            actions.append(norm)
    truncated = len(actions) > cap
    if truncated:
        actions = actions[:cap]
    if not actions:
        return [], "empty", False
    return actions, "json", truncated


def parse_at(value, now=None):
    """Return (aware datetime, error_text). Error is None when it is in range."""
    now = now or now_taipei()
    if not isinstance(value, str):
        return None, "無法排程：時間格式不對"
    try:
        dt = datetime.fromisoformat(value.strip())
    except (ValueError, OverflowError):
        return None, "無法排程：時間格式不對"
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=lc.TZ8)
        dt = dt.astimezone(lc.TZ8)
    except (ValueError, OverflowError):
        return None, "無法排程：時間格式不對"
    if dt <= now:
        return None, "無法排程：時間已過"
    if dt > now + SCHEDULE_HORIZON:
        return None, "無法排程：超過 30 天"
    return dt, None


def confirm_text(item):
    dt = datetime.fromisoformat(item["at"])
    return f"已排程 #{item['id']} 於 {dt.strftime('%H:%M')} 發送"


def format_pending(items):
    pending = [i for i in items if i.get("status") == "pending"]
    pending.sort(key=lambda i: i.get("at") or "")
    if not pending:
        return "沒有待發送的排程"
    parts = []
    for item in pending:
        try:
            dt = datetime.fromisoformat(item["at"])
            when = dt.strftime("%m-%d %H:%M")
        except ValueError:
            when = item.get("at") or "?"
        parts.append(f"#{item['id']} {when} {item.get('path') or item.get('text') or ''}")
    return collapse_text("待發送：" + "；".join(parts), limit=500) or "待發送清單太長"


class ScheduleError(RuntimeError):
    """Unusable schedule data must not be overwritten or sent."""


def _transaction(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        path = os.fspath(self.path) + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return method(self, *args, **kwargs)
    return locked


class ScheduleStore:
    """`schedules.json`. Pending rows survive a process restart."""

    def __init__(self, path):
        self.path = path

    def load(self):
        if not self.path:
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise ScheduleError(f"cannot load {self.path}: {exc}") from exc
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ScheduleError(f"{self.path}: expected an items list")
        out = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ScheduleError(f"{self.path}: invalid schedule id")
            if item.get("status") not in {"pending", "sending", "sent", "cancelled", "failed", "unconfirmed"}:
                raise ScheduleError(f"{self.path}: invalid schedule status")
            if item.get("status") == "pending":
                room = item.get("room")
                if not (isinstance(room, str) and lc.MID_RE.fullmatch(room) and room.startswith(("u", "c", "r"))):
                    raise ScheduleError(f"{self.path}: invalid schedule room")
                kind = item.get('type', 'send_text')
                if kind == 'send_file':
                    if not isinstance(item.get('path'), str) or not item['path'].strip():
                        raise ScheduleError(f"{self.path}: invalid schedule file")
                elif kind != 'send_text' or not collapse_text(item.get("text")):
                    raise ScheduleError(f"{self.path}: invalid schedule action")
                try:
                    at = datetime.fromisoformat(item.get("at"))
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=lc.TZ8)
                    item['at'] = iso8(at)
                except (TypeError, ValueError, OverflowError):
                    raise ScheduleError(f"{self.path}: invalid schedule time") from None
                if not isinstance(item.get('group') or '', str):
                    raise ScheduleError(f"{self.path}: invalid group name")
                if not isinstance(item.get("attempts", 0), int) or item.get("attempts", 0) < 0:
                    raise ScheduleError(f"{self.path}: invalid attempts")
            out.append(item)
        if len({i['id'] for i in out}) != len(out):
            raise ScheduleError(f"{self.path}: duplicate schedule ids")
        return out

    def save(self, items):
        blob = json.dumps({"items": items}, ensure_ascii=False, indent=2)
        lc.atomic_write_text(self.path, blob + "\n")

    def pending_count(self, items=None):
        rows = self.load() if items is None else items
        return sum(1 for i in rows if i.get("status") == "pending")

    @_transaction
    def add(self, room, group, at_dt, text, created_by, file_path=None):
        items = self.load()
        if self.pending_count(items) >= PENDING_CAP:
            return None, "無法排程：待發送已達 20 則"
        used = {i.get("id") for i in items}
        sid = None
        for _ in range(8):
            cand = secrets.token_hex(3)
            if cand not in used:
                sid = cand
                break
        if not sid:
            return None, "無法排程：請稍後再試"
        item = {
            "id": sid,
            "room": room,
            "group": group,
            "at": iso8(at_dt),
            "text": text,
            "created_by": created_by or "",
            "status": "pending",
        }
        if file_path is not None:
            item['type'] = 'send_file'
            item['path'] = file_path
            item.pop('text')
        items.append(item)
        self.save(items)
        return item, None

    def list_room(self, room):
        return [i for i in self.load()
                if i.get("room") == room and i.get("status") == "pending"]

    @_transaction
    def cancel(self, sid, room):
        items = self.load()
        for item in items:
            if item.get("id") == sid and item.get("room") == room and item.get("status") == "pending":
                item["status"] = "cancelled"
                self.save(items)
                return item
        return None

    def due(self, now=None):
        now = now or now_taipei()
        ready = []
        for item in self.load():
            if item.get("status") != "pending":
                continue
            try:
                at = datetime.fromisoformat(item.get("at") or "")
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=lc.TZ8)
            if at.astimezone(lc.TZ8) <= now:
                ready.append(item)
        ready.sort(key=lambda i: i.get("at") or "")
        return ready

    @_transaction
    def claim(self, sid):
        """Persist sending before the irreversible UI action. Claim once only."""
        items = self.load()
        for item in items:
            if item.get("id") == sid and item.get("status") == "pending":
                item["status"] = "sending"
                self.save(items)
                return item
        return None

    @_transaction
    def mark(self, sid, status, attempts=None, message_id=None):
        items = self.load()
        found = None
        for item in items:
            if item.get("id") == sid:
                item["status"] = status
                if attempts is not None:
                    item["attempts"] = attempts
                if message_id:
                    item['message_id'] = message_id
                found = item
                break
        if found is not None:
            self.save(items)
        return found


def describe_action(action):
    kind = action.get("type")
    if kind == "send_text":
        return f"send_text text={action.get('text')!r}"
    if kind == "schedule_text":
        return f"schedule_text at={action.get('at')} text={action.get('text')!r}"
    if kind == "schedule_file":
        return f"schedule_file at={action.get('at')} path={action.get('path')!r}"
    if kind == "send_file":
        return f"send_file path={action.get('path')!r}"
    if kind == "find_files":
        return f"find_files query={action.get('query')!r}"
    if kind == "cancel_schedule":
        return f"cancel_schedule id={action.get('id')}"
    if kind == "list_schedules":
        return "list_schedules"
    return kind or "?"
