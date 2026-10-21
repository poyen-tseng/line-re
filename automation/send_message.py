#!/usr/bin/env python3
"""
send_message.py — Send a text message to a named LINE Desktop chat via X11 UI automation.

Strategy (Path A from REPORT.md):
  1. Find the LINE main window on the chosen DISPLAY (xdotool search --class line.exe).
  2. Activate & focus it.
  3. Resolve the chat name to one room id, or use the caller's `--room` mid.
  4. If a display name was given, click the chat search box, clear it,
     paste the name, and click result rows.
  5. If there is no name, or the search did not open the room, clear the
     search box and click chat-list rows. The row index comes from the
     in-memory `_chat` order (pinned, then recent). Each click is checked
     against `current_open_chat_mid`.
  6. Enter is not used to open a chat: in this client it leaves the
     previously open chat focused.
  7. If the open chat is unknown or not the target, abort. Nothing is pasted
     and Enter is not pressed.
  8. Click the message input, paste the body, press Enter to send.
     --dry-run stops before that Enter.

Usage:
  ./send_message.py --chat "Alice" --text "hello" [--display :3]
  ./send_message.py --chat "Keep Memo" --text "automation test" --dry-run

Notes:
  * LINE Desktop is Qt6 Quick/QML rendered via DXVK on Wine. X11 only sees the
    top-level Qt window, not the QML controls — so we drive it by clicking at
    window-relative coordinates derived from the standard LINE layout
    (742×806 client area, sidebar ~52px, chat-list ~250px, search bar near top).
  * Coordinates are window-relative (xdotool mousemove --window), so they
    survive window movement.
  * UTF-8 Chinese is delivered via xclip + Ctrl+V (typing via xdotool type
    would go through XIM which is unreliable under Wine).
  * Conservative sleeps everywhere; --dry-run skips the final Enter.

The shared X-display autodetect / window-find logic lives in
`line_common.py`; this script keeps the X11 UI-automation helpers and
the send-specific CLI.
"""
import argparse
from contextlib import contextmanager
import fcntl
import os
import re
import subprocess
import sys
import time
import tempfile

import line_common as lc

# Only these return codes prove the send never reached its commit step.
SAFE_RETRY_CODES = frozenset((2, 3, 4))
SEND_UNCERTAIN = 6


@contextmanager
def _ui_lock(timeout=15, name="ui"):
    """Serialize our senders' shared clipboard/focus/composer operations."""
    path = os.path.join(tempfile.gettempdir(), f"line-{name}-{os.getuid()}.lock")
    with open(path, "a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another LINE sender holds the {name} lock")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

# Window-relative coordinates for the standard LINE 26.x desktop layout
# (742x806 AllInOneWindow). These are centers of the target widgets.
# The search field's text sits around y=80; y=58 is the padding above it
# and does not focus the field.
SEARCH_BAR_REL = (180, 80)    # search box in chat-list pane
MSG_INPUT_REL  = (480, 765)   # message input box at bottom of chat pane
# First "Chats" hit under the search header, then each following row.
RESULT_ROW_X = 180
RESULT_ROW_Y0 = 166
RESULT_ROW_PITCH = 71
RESULT_ROW_TRIES = 4
# Unfiltered chat list (search box empty), measured by clicking the
# 742×806 window and reading current_open_chat_mid. Row 0 center is
# y=144; each next row is 72px down. That order is `_chat` sorted
# pinned-then-recent (line_common.chat_list_mids).
CHAT_LIST_X = 180
CHAT_LIST_Y0 = 144
CHAT_LIST_PITCH = 72
CHAT_LIST_VISIBLE = 8
# Paperclip center in the 742×806 LINE window, measured on DISPLAY=:3.
ATTACH_REL = (390, 783)
# Wine GetOpenFileName frame (~670×522, title bar included). The client
# window is inset by about (5, 29). Coordinates are the filename field,
# the Open button, and Cancel, measured on that frame.
DIALOG_FRAME_REF = (670, 522)
DIALOG_NAME_REL = (385, 474)
DIALOG_OPEN_REL = (566, 501)
DIALOG_CANCEL_REL = (630, 501)


def run_cmd(cmd, env=None, check=False, capture=True):
    """Run a command, return CompletedProcess."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    if isinstance(cmd, str):
        shell = True
    else:
        shell = False
    return subprocess.run(cmd, env=full_env, shell=shell, check=check,
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None,
                           text=True, timeout=15)


def set_clipboard(text: str, display: str):
    """Put UTF-8 text into the X CLIPBOARD selection via xclip.

    Default xclip forks a selection owner. Reap its parent with output
    directed to DEVNULL so inherited pipes cannot keep communicate open.
    Failure aborts the send rather than pasting an old clipboard selection.
    """
    env = dict(os.environ, DISPLAY=display)
    p = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-i"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True,
    )
    try:
        # Default xclip forks a child to hold the selection. With stdout/stderr
        # directed to DEVNULL, communicate waits only for the short-lived parent.
        p.communicate(text.encode("utf-8"), timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    if p.returncode:
        raise OSError(f"xclip exited {p.returncode}; refusing to paste stale clipboard")
    # Give the X server a moment to register the new selection owner.
    time.sleep(0.25)


def window_name(display: str, wid: str) -> str:
    r = run_cmd(["xdotool", "getwindowname", wid], env={"DISPLAY": display})
    return r.stdout.strip() if r.returncode == 0 else ""


def window_geom(display: str, wid: str):
    """Return (x, y, w, h) of the window."""
    r = run_cmd(["xwininfo", "-id", wid], env={"DISPLAY": display})
    m = re.search(r"^\s*Absolute upper-left X:\s*(-?\d+)\s*$", r.stdout, re.M)
    my = re.search(r"^\s*Absolute upper-left Y:\s*(-?\d+)\s*$", r.stdout, re.M)
    mw = re.search(r"^\s*Width:\s*(\d+)\s*$", r.stdout, re.M)
    mh = re.search(r"^\s*Height:\s*(\d+)\s*$", r.stdout, re.M)
    if not (m and my and mw and mh):
        return None
    return (int(m.group(1)), int(my.group(1)), int(mw.group(1)), int(mh.group(1)))


def pick_main_window(display: str):
    """Pick the main LINE client window: prefer the one named 'LINE' with
    the 742x806 client geometry; fall back to the largest unnamed line.exe
    window. Returns (wid, geom) or None."""
    wids = lc.find_line_windows(display)
    if not wids:
        return None
    candidates = []
    for w in wids:
        name = window_name(display, w)
        g = window_geom(display, w)
        if g is None:
            continue
        candidates.append((w, name, g))
    # Prefer named "LINE" windows with width ~742 and height ~806
    for w, name, g in candidates:
        if name == "LINE" and 700 < g[2] < 800 and 780 < g[3] < 820:
            return (w, g)
    # Fall back: largest line.exe window
    candidates.sort(key=lambda c: c[2][2] * c[2][3], reverse=True)
    if candidates:
        return (candidates[0][0], candidates[0][2])
    return None


def activate(display: str, wid: str, sync=True):
    """Activate + focus the window. Wine windows sometimes reject
    XSetInputFocus on the invisible root windows; we retry on the named
    'LINE' window and fall back to a simple activate.

    `sync=False` skips xdotool --sync. The file dialog's frame does not
    always report activation, and --sync then blocks for a long time.
    """
    flag = ["--sync"] if sync else []
    activated = False
    for cmd in (["xdotool", "windowactivate", *flag, wid],
                ["xdotool", "windowfocus", *flag, wid]):
        r = run_cmd(cmd, env={"DISPLAY": display})
        activated = activated or r.returncode == 0
        # windowfocus can return BadMatch for unmapped windows; tolerate
    if not activated:
        raise OSError("could not activate/focus LINE window")
    time.sleep(0.4)


def click_relative(display: str, wid: str, rel_xy, button=1):
    """Move to window-relative (x,y), click."""
    x, y = rel_xy
    run_cmd(["xdotool", "mousemove", "--window", wid, str(x), str(y)],
            env={"DISPLAY": display}, check=True)
    time.sleep(0.15)
    run_cmd(["xdotool", "click", str(button)], env={"DISPLAY": display}, check=True)
    time.sleep(0.2)


def key(display: str, keysym):
    """Send a key combo, e.g. 'ctrl+a' or 'Return'."""
    run_cmd(["xdotool", "key", keysym], env={"DISPLAY": display}, check=True)
    time.sleep(0.15)


def paste(display: str):
    """Ctrl+V to paste from clipboard."""
    key(display, "ctrl+v")


def _xwin_field(display, wid, label):
    r = run_cmd(["xwininfo", "-id", str(wid)], env={"DISPLAY": display})
    if r.returncode != 0:
        return None
    m = re.search(rf"^\s*{label}:\s*(.+)$", r.stdout, re.M)
    return m.group(1).strip() if m else None


def _window_mapped(display, wid):
    state = _xwin_field(display, wid, "Map State")
    return state == "IsViewable"


def _parent_wid(display, wid):
    r = run_cmd(["xwininfo", "-id", str(wid), "-tree"], env={"DISPLAY": display})
    match = re.search(r"Parent window id:\s*(0x[0-9a-fA-F]+)", r.stdout or "")
    raw = match.group(1) if match else None
    if not raw:
        return None
    m = re.search(r"0x[0-9a-fA-F]+", raw)
    if not m:
        return None
    return str(int(m.group(0), 16))


def _line_class_windows(display):
    """Viewable line.exe windows as (wid, geom)."""
    r = run_cmd(["xdotool", "search", "--class", "line.exe"],
                env={"DISPLAY": display})
    out = []
    for wid in (r.stdout or "").split():
        geom = window_geom(display, wid)
        if not geom or not _window_mapped(display, wid):
            continue
        out.append((wid, geom))
    return out


def _dialog_candidates(display, main_wid):
    """File-dialog sized line.exe windows, not the main LINE window."""
    found = []
    for wid, geom in _line_class_windows(display):
        if wid == str(main_wid):
            continue
        _x, _y, w, h = geom
        # Fail closed on unsupported layouts; do not mistake arbitrary LINE
        # windows for the measured 660x488 Wine picker.
        if not (640 <= w <= 680 and 470 <= h <= 510):
            continue
        found.append((wid, geom))
    return found


def _attachment_modals(display, main_wid, before):
    """New LINE-owned modal confirmations/errors, excluding existing windows."""
    found = []
    for wid, geom in _line_class_windows(display):
        if wid == str(main_wid) or wid in before or geom[2] < 150 or geom[3] < 80:
            continue
        props = run_cmd(['xprop', '-id', wid, 'WM_TRANSIENT_FOR', '_NET_WM_STATE'],
                        env={'DISPLAY': display}).stdout or ''
        if '_NET_WM_STATE_MODAL' in props or re.search(r'WM_TRANSIENT_FOR.*0x[0-9a-f]+', props):
            found.append(wid)
    return found


def _cancel_attachment_modals(display, wids):
    # Unknown send/original/limit prompts are never accepted with a blind Enter.
    # A possible selection remains uncertain even after cancellation.
    for wid in wids:
        if _window_mapped(display, wid):
            activate(display, wid, sync=False)
            key(display, 'Escape')
            if _window_mapped(display, wid):
                raise OSError('attachment confirmation could not be dismissed')


def _dialog_click_target(display, client_wid, client_geom):
    """Return (wid, name_xy, open_xy, cancel_xy) in that window's space.

    Prefer the outer frame: the measured points include the title bar.
    """
    parent = _parent_wid(display, client_wid)
    parent_geom = window_geom(display, parent) if parent else None
    cx, cy, cw, ch = client_geom
    if parent_geom:
        px, py, pw, ph = parent_geom
        if pw >= cw and ph >= ch and (pw - cw) < 40 and (ph - ch) < 80:
            sx = pw / DIALOG_FRAME_REF[0]
            sy = ph / DIALOG_FRAME_REF[1]
            def pt(rel):
                return (max(0, int(rel[0] * sx)), max(0, int(rel[1] * sy)))
            return parent, pt(DIALOG_NAME_REL), pt(DIALOG_OPEN_REL), pt(DIALOG_CANCEL_REL)
    # Client only: the frame's title bar is not part of this window.
    fw = cw + 10
    fh = ch + 34
    sx = fw / DIALOG_FRAME_REF[0]
    sy = fh / DIALOG_FRAME_REF[1]
    def pt(rel):
        return (max(0, int(rel[0] * sx) - 5), max(0, int(rel[1] * sy) - 29))
    return client_wid, pt(DIALOG_NAME_REL), pt(DIALOG_OPEN_REL), pt(DIALOG_CANCEL_REL)


def _dismiss_file_dialogs(display, main_wid):
    """Escape any file dialog left open by an earlier attempt."""
    for wid, geom in _dialog_candidates(display, main_wid):
        target, _name, _open, cancel = _dialog_click_target(display, wid, geom)
        print(f"[*] closing leftover file dialog wid={target}")
        activate(display, target, sync=False)
        # A path error may be modal above the picker: Escape dismisses that
        # first, then the picker. Never send Return while dismissing errors.
        for _ in range(3):
            key(display, "Escape")
            if not _window_mapped(display, wid):
                break
        if _window_mapped(display, wid):
            click_relative(display, target, cancel)
        if _window_mapped(display, wid):
            raise OSError("could not dismiss LINE file picker")


def _fill_file_dialog(display, main_wid, wine_path, commit, pid=None, room=None,
                      on_commit=None):
    """Type `wine_path` into the open dialog.

    `commit` True clicks Open. False clicks Cancel (no file is chosen).
    Returns True when a dialog was found and the click was issued.
    """
    deadline = time.monotonic() + 5
    picked = None
    while time.monotonic() < deadline:
        cands = _dialog_candidates(display, main_wid)
        if len(cands) == 1:
            picked = cands[0]
            break
        if len(cands) > 1:
            return False
        time.sleep(0.3)
    if not picked:
        print("ERROR: file dialog did not open.", file=sys.stderr)
        return False
    client_wid, geom = picked
    target, name_xy, open_xy, cancel_xy = _dialog_click_target(
        display, client_wid, geom)
    print(f"[*] file dialog wid={target} geom={window_geom(display, target)} "
          f"name_at={name_xy}")
    activate(display, target, sync=False)
    click_relative(display, target, name_xy)
    time.sleep(0.15)
    key(display, "ctrl+a")
    time.sleep(0.1)
    set_clipboard(wine_path, display)
    time.sleep(0.15)
    paste(display)
    time.sleep(0.35)
    if not commit:
        print("[*] file dialog probe: clicking Cancel, not Open.")
        click_relative(display, target, cancel_xy)
        time.sleep(0.4)
        return True
    if not pid or lc.current_open_chat_mid(pid) != room:
        print("ERROR: room changed before file commit; cancelling.", file=sys.stderr)
        click_relative(display, target, cancel_xy)
        return False
    print(f"[*] clicking Open at rel {open_xy}")
    if on_commit:
        on_commit()  # a failed X call can still have issued its click
    click_relative(display, target, open_xy)
    time.sleep(0.8)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if not _window_mapped(display, client_wid):
            print("[*] file dialog closed.")
            return True
        time.sleep(0.4)
    print("ERROR: file dialog still open after Open (path rejected?).",
          file=sys.stderr)
    activate(display, target, sync=False)
    click_relative(display, target, cancel_xy)
    key(display, "Escape")
    return False


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Send a text message to a named LINE Desktop chat via "
                    "X11 UI automation.")
    ap.add_argument("--chat", default="",
                    help="Target chat display name (UTF-8 OK). Optional "
                         "when --room is set; used only to search the list.")
    ap.add_argument("--room", default=None,
                    help="Target room mid. The open chat must match this "
                         "before anything is sent.")
    ap.add_argument("--text", required=True, help="Message body (UTF-8 OK)")
    ap.add_argument("--display", default=None,
                    help="X display, e.g. :3. Auto-detects if omitted.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do everything except the final Enter (no send).")
    ap.add_argument("--no-search", action="store_true",
                    help="Skip search; assume the target chat is already open.")
    ap.add_argument("--search-bar-rel", default=None,
                    help="Override search bar coords 'x,y' (window-relative)")
    ap.add_argument("--msg-input-rel", default=None,
                    help="Override msg input coords 'x,y' (window-relative)")
    return ap.parse_args(argv)


def _target_room(pid, args):
    """Room id that must be open before anything is sent. None aborts."""
    room = getattr(args, "room", None)
    if room:
        if not isinstance(room, str) or not lc.MID_RE.fullmatch(room):
            print("ERROR: room id is not a LINE mid; aborting without sending.",
                  file=sys.stderr)
            return None
        print(f"[*] target room {room}")
        return room
    if not pid:
        print("ERROR: LINE.exe not running; cannot resolve the chat id. "
              "Aborting without sending.", file=sys.stderr)
        return None
    if not (getattr(args, "chat", None) or "").strip():
        print("ERROR: no --room and no --chat; aborting without sending.",
              file=sys.stderr)
        return None
    print(f"[*] resolving {args.chat!r} to a room id ...")
    _messages, contacts, _scanned, _matched = lc.scan_memory(pid)
    mid = lc.resolve_chat_mid(contacts, args.chat)
    if not mid:
        print(f"ERROR: {args.chat!r} does not match exactly one chat in "
              "memory; aborting without sending.", file=sys.stderr)
        return None
    print(f"[*] target room {mid} name={args.chat!r}")
    return mid


def _open_search_result(display, wid, chat, pid, target, search_xy, max_rows=RESULT_ROW_TRIES,
                        deadline=None):
    """Type `chat` in search and click result rows until the open mid matches.

    Returns True only when the memory indicator reads `target`.
    """
    print(f"[*] Clicking search bar at rel {search_xy}")
    click_relative(display, wid, search_xy)
    key(display, "ctrl+a")
    time.sleep(0.1)
    key(display, "BackSpace")
    time.sleep(0.1)
    set_clipboard(chat, display)
    time.sleep(0.15)
    paste(display)
    time.sleep(0.8)
    for i in range(max_rows):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        y = RESULT_ROW_Y0 + i * RESULT_ROW_PITCH
        print(f"[*] Clicking search result row {i} at rel ({RESULT_ROW_X}, {y})")
        click_relative(display, wid, (RESULT_ROW_X, y))
        time.sleep(0.55)
        open_mid = lc.current_open_chat_mid(pid)
        print(f"[*] open chat mid={open_mid}")
        if open_mid == target:
            return True
    return False


def _move(display, wid, rel_xy):
    x, y = rel_xy
    run_cmd(["xdotool", "mousemove", "--window", wid, str(x), str(y)],
            env={"DISPLAY": display})


def _scroll_chat_list(display, wid, button, notches):
    """Wheel over the chat list. button 4 is up, 5 is down. Does not click."""
    _move(display, wid, (CHAT_LIST_X, CHAT_LIST_Y0 + CHAT_LIST_PITCH))
    time.sleep(0.05)
    for _ in range(notches):
        run_cmd(["xdotool", "click", str(button)], env={"DISPLAY": display})
        time.sleep(0.04)


def _clear_search(display, wid, search_xy):
    """Empty the search box so the full recent list is showing.

    Escape is not pressed: in this client it closes the open chat.
    """
    click_relative(display, wid, search_xy)
    key(display, "ctrl+a")
    time.sleep(0.05)
    key(display, "BackSpace")
    time.sleep(0.35)


def _read_open_mid(pid):
    mid = lc.current_open_chat_mid(pid)
    if mid:
        return mid
    time.sleep(0.4)
    return lc.current_open_chat_mid(pid)


def _click_chat_list_row(display, wid, pid, index):
    y = CHAT_LIST_Y0 + index * CHAT_LIST_PITCH
    print(f"[*] Clicking chat list row {index} at rel ({CHAT_LIST_X}, {y})")
    click_relative(display, wid, (CHAT_LIST_X, y))
    time.sleep(0.4)
    open_mid = _read_open_mid(pid)
    print(f"[*] open chat mid={open_mid}")
    return open_mid


def _open_from_chat_list(display, wid, pid, target, search_xy, deadline=None):
    """Open `target` by clicking the recent-chat list. No display name.

    The row index is the in-memory `_chat` order. A click counts only
    when `current_open_chat_mid` equals `target`. Returns False if the
    list never shows that room.
    """
    print(f"[*] opening {target} from the chat list (no display name)")
    _clear_search(display, wid, search_xy)
    _scroll_chat_list(display, wid, 4, 20)
    order = lc.chat_list_mids(pid)
    print(f"[*] chat list order ({len(order)}): "
          + ", ".join(order) if order else "[*] chat list order is empty")
    if target in order:
        idx = order.index(target)
        if idx < CHAT_LIST_VISIBLE:
            if _click_chat_list_row(display, wid, pid, idx) == target:
                return True
            print("[*] indexed chat-list click did not match; walking rows")
        else:
            print(f"[*] room index {idx} is below the first screen")
    else:
        print(f"[*] {target} is not in the in-memory chat list")
    seen = set()
    for page in range(6):
        page_new = False
        for i in range(CHAT_LIST_VISIBLE):
            if deadline is not None and time.monotonic() >= deadline:
                return False
            mid = _click_chat_list_row(display, wid, pid, i)
            if mid == target:
                return True
            if mid and mid not in seen:
                seen.add(mid)
                page_new = True
        if page and not page_new:
            break
        _scroll_chat_list(display, wid, 5, 4)
        time.sleep(0.2)
    return False


def _prepare_chat(args):
    """Open the named chat and require the memory mid to match.

    Returns (ctx, None) or (None, exit code). Nothing has been pasted.
    ctx is display, wid, pid, target, msg_input_rel.
    """
    display = args.display or lc.find_line_display()
    if display is None:
        print("ERROR: could not find LINE window on any common display. "
              "Pass --display :N explicitly.", file=sys.stderr)
        return None, 2
    print(f"[*] Using DISPLAY={display}")

    sb_rel = SEARCH_BAR_REL
    mi_rel = MSG_INPUT_REL
    try:
        if getattr(args, "search_bar_rel", None):
            x, y = args.search_bar_rel.split(",")
            sb_rel = (int(x), int(y))
        if getattr(args, "msg_input_rel", None):
            x, y = args.msg_input_rel.split(",")
            mi_rel = (int(x), int(y))
    except ValueError:
        print("ERROR: coordinates must be x,y integers.", file=sys.stderr)
        return None, 4

    picked = pick_main_window(display)
    if not picked:
        print("ERROR: no LINE window found.", file=sys.stderr)
        return None, 3
    wid, g = picked
    print(f"[*] LINE window: wid={wid} geom={g} name={window_name(display, wid)!r}")

    pid = lc.find_line_pid()
    if not pid:
        print("ERROR: LINE.exe not running; cannot verify the open chat. "
              "Aborting without sending.", file=sys.stderr)
        return None, 4
    target = _target_room(pid, args)
    if not target:
        return None, 4

    activate(display, wid, sync=not getattr(args, 'reuse_open', False))

    name = (getattr(args, "chat", None) or "").strip()
    opened = bool(getattr(args, 'reuse_open', False) and _read_open_mid(pid) == target)
    route_seconds = getattr(args, 'route_timeout', None)
    deadline = time.monotonic() + route_seconds if route_seconds else None
    if not args.no_search and name and not opened:
        opened = _open_search_result(display, wid, name, pid, target, sb_rel, deadline=deadline)
        if not opened:
            print("[*] name search did not open the room; trying the chat list.")
    if not args.no_search and not opened:
        opened = _open_from_chat_list(display, wid, pid, target, sb_rel, deadline=deadline)
    if not args.no_search:
        if not opened:
            open_mid = lc.current_open_chat_mid(pid)
            print(f"ERROR: open chat mid={open_mid} is not target {target}. "
                  "Aborting without sending.", file=sys.stderr)
            return None, 4
    else:
        open_mid = lc.current_open_chat_mid(pid)
        print(f"[*] open chat mid={open_mid}")
        if open_mid != target:
            print(f"ERROR: open chat mid={open_mid} is not target {target}. "
                  "Aborting without sending.", file=sys.stderr)
            return None, 4
    print(f"[*] open chat verified {target}")
    return {
        "display": display,
        "wid": wid,
        "pid": pid,
        "target": target,
        "msg_input": mi_rel,
    }, None


def _clear_composer(display, wid, mi_rel):
    print(f"[*] Clicking message input at rel {mi_rel}")
    click_relative(display, wid, mi_rel)
    key(display, "ctrl+a")
    time.sleep(0.1)
    key(display, "BackSpace")
    time.sleep(0.1)


def run(args):
    try:
        with _ui_lock():
            return _run_text(args)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: send failed; delivery uncertain, do not auto-retry: {exc}", file=sys.stderr)
        return SEND_UNCERTAIN


def _route_matches(ctx):
    return lc.current_open_chat_mid(ctx['pid']) == ctx['target']


def _run_text(args):
    """Run the send operation with a parsed argparse Namespace. Returns
    an exit code (0 success, 2 no display, 3 no window, 4 route check
    failed — nothing was sent)."""
    ctx, err = _prepare_chat(args)
    if err:
        return err
    display = ctx["display"]
    wid = ctx["wid"]
    mi_rel = ctx["msg_input"]
    if not _route_matches(ctx):
        print("ERROR: room changed before paste; aborting.", file=sys.stderr)
        return 4
    _clear_composer(display, wid, mi_rel)

    set_clipboard(args.text, display)
    time.sleep(0.15)
    if not _route_matches(ctx):
        print("ERROR: room changed before paste; aborting.", file=sys.stderr)
        return 4
    paste(display)
    time.sleep(0.4)

    if args.dry_run:
        print("[*] --dry-run: NOT pressing Enter. Message is in the input box.")
    else:
        if not _route_matches(ctx):
            print("ERROR: room changed before Enter; draft left unsent.", file=sys.stderr)
            return 4
        key(display, "Return")
        print("[*] Sent.")
    return 0


def run_file(args):
    import file_upload
    result = file_upload.send(args)
    args.file_result = result
    return result.rc


def _run_file(args):
    """Compatibility entry point; staging, locking and verification are shared."""
    return run_file(args)


def main():
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
