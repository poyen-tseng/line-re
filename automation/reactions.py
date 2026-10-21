"""Bounded, durable, asynchronous likes for accepted mentions of this bot."""
from dataclasses import dataclass
from contextlib import contextmanager
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

import bot_config
import line_common as lc
import reaction_memory as rm
import send_message as sm
import watch_messages as watch

DEFAULT_MENTION = bot_config.DEFAULT_MENTION
MAX_ATTEMPTS = 3


def mention_info(content_meta):
    """The bot's shared authoritative MENTION parser (S/E are UTF-16 units)."""
    if not isinstance(content_meta, str) or not content_meta.strip():
        return None
    try:
        meta = json.loads(content_meta)
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict) or 'MENTION' not in meta:
        return None
    raw = meta.get('MENTION')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    mentionees = raw.get('MENTIONEES', [])
    if not isinstance(mentionees, list):
        return []
    out = []
    for item in mentionees:
        if not isinstance(item, dict):
            continue
        mid = item.get('M') or item.get('mid')
        if not isinstance(mid, str) or not mid:
            continue
        start = end = None
        try:
            if item.get('S') is not None:
                start = int(item['S'])
            if item.get('E') is not None:
                end = int(item['E'])
        except (TypeError, ValueError, OverflowError):
            start = end = None
        out.append((mid, start, end))
    return out


def mention_path(message, self_mid, token):
    """Identical acceptance rule for reply and reaction; metadata wins."""
    info = mention_info(message.get('contentMeta'))
    if info is not None:
        return 'metadata' if any(mid.lower() == self_mid.lower() for mid, _, _ in info) else None
    text = message.get('text') or ''
    return 'text' if isinstance(text, str) and token and token in text else None


def is_tagged(message, self_mid, mention_token=DEFAULT_MENTION):
    sender = message.get('from') or ''
    room = message.get('to') or ''
    mid = message.get('id') or ''
    if not all(isinstance(value, str) for value in (sender, room, mid, self_mid)):
        return False
    return (bool(re.fullmatch(r'[0-9]{1,24}', mid))
            and bool(re.fullmatch(r'u[0-9a-f]{32}', sender))
            and sender.lower() != self_mid.lower()
            and bool(re.fullmatch(r'c[0-9a-f]{32}', room))
            and message.get('contentType') != 18
            and bool(mention_path(message, self_mid, mention_token)))


@dataclass
class Result:
    ok: bool = False
    committed: bool = False
    detail: str = ''


def _bubble(rgb, width, height, row_top, row_bottom, view_left):
    """Grey incoming text bubble in the memory-selected row, standard theme."""
    spans = []
    for y in range(max(110, int(row_top)), min(683, int(row_bottom))):
        xs = [x for x in range(int(view_left) + 63, min(width - 10, int(view_left) + 365))
              if rgb[(y * width + x) * 3:(y * width + x) * 3 + 3] == b'\xef\xef\xef']
        if xs and xs[-1] - xs[0] >= 35:
            spans.append((y, xs[0], xs[-1]))
    if not spans or spans[-1][0] - spans[0][0] < 16:
        raise ValueError('unsupported or invisible incoming bubble')
    right = max(row[2] for row in spans)
    bottom = spans[-1][0] + 1
    return right + 1 + 14, bottom - 15


def row_bounds(rows, message_id, view_top, view_height):
    """Qt lists align short contents at the top; long contents scroll to end."""
    index = next(i for i, row in enumerate(rows) if row[0] == message_id)
    content_height = sum(h for _, h in rows)
    bottom = view_top + min(view_height, content_height) - sum(h for _, h in rows[index + 1:])
    return bottom - rows[index][1], bottom


def _capture(display, wid, width, height):
    env = os.environ.copy()
    env['DISPLAY'] = display
    proc = subprocess.run(['import', '-window', wid, '-depth', '8', 'rgb:-'],
                          env=env, capture_output=True, timeout=3, check=True)
    if len(proc.stdout) != width * height * 3:
        raise ValueError('LINE screenshot dimensions changed')
    return proc.stdout


def like_message(message, chat_name, self_mid, preempt, mention_token=DEFAULT_MENTION):
    """UI fallback with live picker id/room checks, then memory+server ACK.

    The lock covers only UI work. All heap discovery and delivery verification
    occur outside it. A post-click exception is uncertain and never retried.
    """
    if not is_tagged(message, self_mid, mention_token):
        return Result(detail='message is not an incoming tagged group command')
    mid, room = message['id'], message['to']
    pid = lc.find_line_pid()
    if not pid:
        return Result(detail='LINE is not running')
    committed = False
    memory = None
    display = os.environ.get('DISPLAY') or ':3'
    wid = None
    timings = {}
    def measure(label, call):
        started = time.monotonic()
        try:
            return call()
        finally:
            timings[label] = round(time.monotonic() - started, 3)
    try:
        existing = measure('evidence', lambda: rm.reaction_evidence(pid, mid, room, self_mid))
        if existing['records']:
            return Result(ok=True, committed=True,
                          detail='existing self NICE in memory; no click')
        # Warm the open-room anchors before taking the UI lock.
        measure('anchors', lambda: lc.current_open_chat_mid(pid))
        memory = measure('discovery', lambda: rm.ReactionMemory(pid))
        order = measure('room_order', lambda:
                        lc.chat_list_mids(pid) if lc.current_open_chat_mid(pid) != room else [])
        deadline = time.monotonic() + 6

        def guard():
            if preempt.is_set() or time.monotonic() > deadline:
                raise TimeoutError('yielding UI to reply')
            memory.validate()
            if lc.current_open_chat_mid(pid) != room:
                raise ValueError('open room changed or is unknown')

        with sm._ui_lock(timeout=0.5):
            ui_started = time.monotonic()
            try:
                if preempt.is_set():
                    raise TimeoutError('reply is ready')
                picked = sm.pick_main_window(display)
                if not picked:
                    raise ValueError('LINE window is unavailable')
                wid, geom = picked
                if geom[2:] != (742, 806):
                    raise ValueError('unsupported LINE window size')
                sm.activate(display, wid, sync=False)
                if lc.current_open_chat_mid(pid) != room:
                    # A new group's _chat row may not be cached yet. Use the
                    # sender's name-search path, bounded to one result, and
                    # always require its observed mid; never trust the label.
                    opened = False
                    if chat_name:
                        opened = sm._open_search_result(display, wid, chat_name, pid,
                                                        room, sm.SEARCH_BAR_REL, max_rows=1)
                    if not opened:
                        if room not in order or order.index(room) >= sm.CHAT_LIST_VISIBLE:
                            raise ValueError('verified room is unavailable in search/recent chats')
                        if preempt.is_set():
                            raise TimeoutError('yielding UI to reply')
                        sm._clear_search(display, wid, sm.SEARCH_BAR_REL)
                        sm._scroll_chat_list(display, wid, 4, 20)
                        if sm._click_chat_list_row(display, wid, pid, order.index(room)) != room:
                            raise ValueError('recent-chat click did not match room')
                guard()
                # Wheel, never Return/Ctrl+End in a possibly focused composer.
                vx, vy, vw, vh = rm.widget_global(memory.mem, memory.view)
                vx -= geom[0]
                vy -= geom[1]
                sm._move(display, wid, (vx + vw // 2, vy + vh // 2))
                sm.run_cmd(['xdotool', 'click', '--repeat', '25', '--delay', '0', '5'],
                           env={'DISPLAY': display}, check=True)
                time.sleep(0.12)
                rows = memory.rows(mid)
                top, bottom = row_bounds(rows, mid, vy, vh)
                if top < vy or bottom > vy + vh:
                    raise ValueError('target message is outside the visible viewport')
                rgb = _capture(display, wid, geom[2], geom[3])
                x, y = _bubble(rgb, geom[2], geom[3], top, bottom, vx)
                if not (vx + 60 < x < vx + vw - 4 and vy < y < vy + vh):
                    raise ValueError('reaction button is outside the message view')
                sm._move(display, wid, (x - 30, y))
                time.sleep(0.08)
                guard()
                sm.click_relative(display, wid, (x, y))
                picker = memory.picker()
                if picker['id'] != mid or picker['room'] != room:
                    raise ValueError(f'picker mismatch id={picker["id"]} room={picker["room"]}')
                px, py, pw, ph = picker['rect']
                if not (260 <= pw <= 290 and 55 <= ph <= 70):
                    raise ValueError('unsupported reaction picker geometry')
                point = (px - geom[0] + 26, py - geom[1] + 28)
                if not (365 < point[0] < 730 and 110 < point[1] < 683):
                    raise ValueError('like button is outside the verified chat')
                # Move first, then re-read identity immediately before mousedown.
                sm._move(display, wid, point)
                guard()
                again = memory.picker()
                if again != picker:
                    raise ValueError('reaction picker changed before click')
                committed = True  # even an X11 error may have delivered the click
                sm.run_cmd(['xdotool', 'click', '1'], env={'DISPLAY': display}, check=True)
            finally:
                if wid:
                    # Escape only with an open picker; otherwise it closes chat.
                    if memory.picker()['id']:
                        sm.key(display, 'Escape')
                    sm._move(display, wid, (380, 80))
                timings['ui'] = round(time.monotonic() - ui_started, 3)
        # Verification deliberately releases the lock before scanning memory.
        until = time.monotonic() + 6
        while True:
            evidence = measure('verify', lambda: rm.reaction_evidence(pid, mid, room, self_mid))
            if evidence['confirmed']:
                return Result(True, True, 'NICE=2 self=' + self_mid + ' room=' + room
                              + ' evidence=' + evidence['records'][0]['address']
                              + f' server_ack={int(evidence["server_ack"])}'
                              + f' client_success={int(evidence["client_success"])}')
            if time.monotonic() >= until:
                return Result(committed=True, detail='click sent; memory/server confirmation unavailable')
            time.sleep(0.25)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return Result(committed=committed, detail=str(exc))
    finally:
        if memory:
            memory.close()
        print(f'[react] timing id={mid} method=ui-picker ' +
              ' '.join(f'{name}={value:.3f}s' for name, value in timings.items()),
              file=sys.stderr, flush=True)


@dataclass
class _Ticket:
    message: dict
    chat: str
    ready: threading.Event
    finished: threading.Event
    attempts: int = 0
    retry_at: float = 0


class _Preempt:
    def __init__(self, worker, ticket, retry):
        self.worker, self.ticket, self.retry = worker, ticket, retry

    def is_set(self):
        with self.worker.lock:
            return (self.worker.stopping.is_set()
                    or self.worker._send_pending()
                    or (self.retry and self.worker.batch_depth > 0))


class ReactionWorker:
    """Fresh mentions first; bounded safe retries only when the send queue is idle."""
    def __init__(self, state, path, self_mid, backend=None, log=None,
                 mention_token=DEFAULT_MENTION):
        if not isinstance(state, watch.HandledState) or not path:
            raise ValueError('reactions require a durable HandledState and path')
        self.state, self.path, self.self_mid = state, path, self_mid
        self.mention_token = mention_token
        state.structured = True
        self.backend = backend or (lambda m, chat, own, stop:
                                  like_message(m, chat, own, stop, mention_token=mention_token))
        self.warm_anchors = backend is None
        self.log = log or (lambda s: print(s, file=sys.stderr, flush=True))
        self.jobs = queue.Queue(maxsize=64)
        self.tickets = {}
        self.lock = threading.RLock()
        self.batch_depth = 0
        self.stopping = threading.Event()
        self.disabled = False
        for item in state.reactions.values():
            if item['status'] == 'pending':
                item['status'] = 'uncertain'
        self.thread = threading.Thread(target=self._run, name='line-reactions', daemon=True)
        self.thread.start()

    @contextmanager
    def command_batch(self):
        """Keep retries out of gaps between multiple commands and schedule sends."""
        with self.lock:
            self.batch_depth += 1
        try:
            yield
        finally:
            with self.lock:
                self.batch_depth -= 1

    def _send_pending(self):
        return any(t.ready.is_set() and not t.finished.is_set() for t in self.tickets.values())

    def submit(self, message, chat_name=''):
        if (not is_tagged(message, self.self_mid, self.mention_token)
                or self.disabled or self.stopping.is_set()):
            return False
        mid = message['id']
        with self.lock:
            prior = self.state.reactions.get(mid)
            if (mid in self.tickets or (prior and (prior['status'] != 'failed'
                                                   or prior['attempts'] >= MAX_ATTEMPTS))):
                return False
            if len(self.tickets) >= 64:
                self.log(f'[react] fail id={mid} method=ui-picker dur=0s reason=queue-full')
                return False
            ticket = _Ticket(dict(message), chat_name, threading.Event(), threading.Event(),
                             attempts=prior['attempts'] if prior else 0)
            self.tickets[mid] = ticket
            try:
                self.jobs.put_nowait(ticket)
            except queue.Full:
                del self.tickets[mid]
                self.log(f'[react] fail id={mid} method=ui-picker dur=0s reason=queue-full')
                return False
        return True

    def reply_ready(self, mid):
        with self.lock:
            ticket = self.tickets.get(mid)
            if ticket:
                ticket.ready.set()

    def reply_finished(self, mid):
        with self.lock:
            ticket = self.tickets.get(mid)
            if ticket:
                ticket.finished.set()

    def _save(self):
        if not watch.save_state(self.path, self.state):
            self.disabled = True
            raise OSError('cannot persist reaction state; worker disabled')

    def _perform(self, ticket):
        message, chat_name = ticket.message, ticket.chat
        mid, room = message['id'], message['to']
        attempt = ticket.attempts + 1
        with self.state.lock:
            self.state.reactions[mid] = dict(status='pending', attempts=attempt, room=room)
            self._save()
        ticket.attempts = attempt
        start = time.monotonic()
        self.log(f'[react] start id={mid} method=ui-picker attempt={attempt}')
        try:
            result = self.backend(message, chat_name, self.self_mid,
                                  _Preempt(self, ticket, attempt > 1))
        except Exception as exc:
            result = Result(committed=True, detail=f'backend exception: {exc}')
        status = 'ok' if result.ok else ('uncertain' if result.committed else 'failed')
        with self.state.lock:
            self.state.reactions[mid].update(status=status, detail=result.detail[:500])
            self._save()
        self.log(f'[react] {"ok" if result.ok else "fail"} id={mid} method=ui-picker '
                 f'attempt={attempt} status={status} dur={time.monotonic() - start:.2f}s '
                 f'{result.detail}')
        if not (result.ok or result.committed) and attempt < MAX_ATTEMPTS:
            ticket.retry_at = time.monotonic() + 0.5 * (2 ** (attempt - 1))
            return True
        return False

    def _run(self):
        if self.warm_anchors:
            # Read-only discovery overlaps the startup baseline, rather than
            # delaying the first mention. The sender shares these cached anchors.
            started = time.monotonic()
            try:
                pid = lc.find_line_pid()
                if pid:
                    lc.current_open_chat_mid(pid)
                self.log(f'[react] ready method=memory-anchors pid={pid} '
                         f'dur={time.monotonic() - started:.2f}s')
            except OSError as exc:
                self.log(f'[react] warmup unavailable: {exc}')
        while not self.stopping.is_set():
            try:
                ticket = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            retry = False
            try:
                if self.disabled:
                    continue
                with self.lock:
                    blocked = (self._send_pending() or
                               (ticket.attempts > 0 and self.batch_depth > 0))
                if blocked or time.monotonic() < ticket.retry_at:
                    retry = True
                    self.stopping.wait(0.05)
                else:
                    retry = self._perform(ticket)
            except Exception as exc:
                self.log(f'[react] fail id={ticket.message["id"]} method=ui-picker dur=0s reason={exc}')
            finally:
                # Requeue before task_done so jobs.join includes bounded retries.
                if retry and not self.stopping.is_set():
                    self.jobs.put_nowait(ticket)
                else:
                    with self.lock:
                        self.tickets.pop(ticket.message['id'], None)
                self.jobs.task_done()

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=8)
