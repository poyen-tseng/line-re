"""win_line.py — drive native Windows LINE Desktop through UI Automation.

The Linux path clicks fixed X11 coordinates. On Windows the Qt client
exposes a UI Automation tree, so controls are found by role + label and
acted on through `cua-driver` (https://github.com/trycua/cua), which
delivers clicks/keys to the LINE window in the background when it can,
and only falls back to the foreground when it must.

Safety rules are the same as on Linux:
  * the open chat's mid is read from LINE memory
    (`line_common.current_open_chat_mid`) and must equal the target room
    before text is placed and again before Enter / Open;
  * a mismatch aborts without sending;
  * nothing writes LINE's memory, injects code or talks to LINE servers.

Labels are the Traditional-Chinese UI strings of LINE 26.6.0.4004
(`LABELS`). Another UI language needs its labels added there.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import line_common as lc
import platform_compat as pc

# UIA names in the zh-TW client. Tuples accept any of the given labels.
LABELS = {
    "composer": ("輸入訊息", "Enter a message"),
    "search": ("以姓名搜尋", "Search by name"),
    "friends_tab": ("好友", "Friends"),
    "chats_tab": ("聊天", "Chats"),
    "keep": ("Keep筆記", "Keep Memo"),
    "attach": ("傳送檔案", "Send file"),
    "open_chat": ("聊天", "Chat"),
}
DIALOG_OPEN = {"開啟(O)", "開啟舊檔(O)", "Open(O)", "Open", "開啟", "開啟舊檔"}
DIALOG_CANCEL = {"取消", "取消(C)", "Cancel", "Cancel(C)"}

_SESSION = [None]


class UiError(RuntimeError):
    """cua-driver failed or the LINE UI is not in the expected state."""


# ---------------------------------------------------------------------------
# cua-driver plumbing
# ---------------------------------------------------------------------------

def _driver():
    exe = shutil.which("cua-driver")
    if not exe:
        raise UiError("cua-driver not found on PATH (https://github.com/trycua/cua)")
    return exe


def new_session():
    _SESSION[0] = f"line_{os.getpid()}_{int(time.time() * 1000)}"
    return _SESSION[0]


def end_session():
    """Best-effort cleanup of the transient cua-driver session."""
    sid, _SESSION[0] = _SESSION[0], None
    if not sid:
        return
    try:
        subprocess.run([_driver(), "call", "end_session"], input=json.dumps({"session": sid}),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                       encoding="utf-8", timeout=10, creationflags=pc.no_window_flags())
    except (OSError, subprocess.SubprocessError, UiError):
        pass


class session:
    """Context manager: one cua-driver session per UI operation.

    A long-lived named session expires after idling; a fresh one per
    send/upload/like avoids 'session has ended' failures in a daemon.
    """

    def __enter__(self):
        new_session()
        return self

    def __exit__(self, *exc):
        end_session()


def call(tool, payload=None):
    """`cua-driver call <tool>` with JSON on stdin; returns parsed JSON."""
    body = dict(payload or {})
    explicit = "session" in body
    for attempt in range(2):
        if not explicit:
            body["session"] = _SESSION[0] or new_session()
        try:
            r = subprocess.run([_driver(), "call", tool],
                               input=json.dumps(body, ensure_ascii=False),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace", timeout=25,
                               creationflags=pc.no_window_flags())
        except (OSError, subprocess.SubprocessError) as exc:
            raise UiError(f"cua-driver {tool} could not run: {exc}") from exc
        if r.returncode == 0:
            raw = (r.stdout or "").strip()
            try:
                return json.loads(raw) if raw else {}
            except ValueError as exc:
                raise UiError(f"cua-driver {tool} returned non-JSON output") from exc
        err = (r.stderr or r.stdout or "").strip()
        if attempt == 0 and not explicit and "session has ended" in err.lower():
            new_session()
            continue
        raise UiError(f"cua-driver {tool} failed (rc={r.returncode}): {err[:300]}")
    raise UiError(f"cua-driver {tool} failed")


# ---------------------------------------------------------------------------
# Window discovery
# ---------------------------------------------------------------------------

def find_line_exe(pid=None):
    """Path of the running or installed LINE.exe."""
    if pid:
        try:
            import ctypes
            import ctypes.wintypes as wt
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x1000, False, int(pid))
            if handle:
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wt.DWORD(len(buf))
                    if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        if os.path.isfile(buf.value):
                            return buf.value
                finally:
                    k32.CloseHandle(handle)
        except (OSError, AttributeError):
            pass
    root = os.path.join(os.environ.get("LOCALAPPDATA") or
                        os.path.join(os.path.expanduser("~"), "AppData", "Local"), "LINE", "bin")
    current = os.path.join(root, "current", "LINE.exe")
    if os.path.isfile(current):
        return current
    try:
        versions = sorted(os.listdir(root), reverse=True)
    except OSError:
        return None
    for name in versions:
        exe = os.path.join(root, name, "LINE.exe")
        if os.path.isfile(exe):
            return exe
    return None


def launch_line(pid=None):
    """Start LINE.exe. With LINE running this only re-shows its window
    (single-instance IPC); it never kills or restarts the client, which
    would log out an account without Auto-Login and drop the memory cache.
    """
    exe = find_line_exe(pid)
    if not exe:
        return False
    try:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=pc.no_window_flags())
        return True
    except OSError:
        return False


def _area(w):
    b = w.get("bounds") or {}
    return b.get("width", 0) * b.get("height", 0)


def _pick_main(windows, pid):
    mine = [w for w in windows if w.get("pid") == pid
            and (w.get("bounds") or {}).get("width", 0) >= 200
            and (w.get("bounds") or {}).get("height", 0) >= 200]
    pool = [w for w in mine if (w.get("title") or "") == "LINE"] or mine
    if not pool:
        return None
    pool.sort(key=lambda w: (not w.get("minimized"), _area(w)), reverse=True)
    return pool[0]


def main_window(pid, restore=True):
    """(hwnd, (left, top, right, bottom)) of LINE's main window, or None.

    A tray-hidden window is re-shown by launching LINE.exe again; a
    minimized one is restored. Neither steals focus when avoidable.
    """
    win = _pick_main(call("list_windows").get("windows") or [], pid)
    if win is None and restore and launch_line(pid):
        deadline = time.monotonic() + 6
        while win is None and time.monotonic() < deadline:
            time.sleep(0.5)
            win = _pick_main(call("list_windows").get("windows") or [], pid)
    if win is None:
        return None
    hwnd = int(win["window_id"])
    if restore and win.get("minimized"):
        try:
            call("bring_to_front", {"pid": pid, "window_id": hwnd})
        except UiError:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            except (OSError, AttributeError):
                pass
        time.sleep(0.35)
        for w in call("list_windows").get("windows") or []:
            if w.get("window_id") == hwnd:
                win = w
                break
    b = win.get("bounds") or {}
    x, y = int(b.get("x", 0)), int(b.get("y", 0))
    return hwnd, (x, y, x + int(b.get("width", 0)), y + int(b.get("height", 0)))


def elements(pid, hwnd, max_elements=None, screenshot=False):
    payload = {"pid": pid, "window_id": hwnd, "include_screenshot": screenshot,
               "tree_format": "elements"}
    if max_elements:
        payload["max_elements"] = max_elements
    state = call("get_window_state", payload)
    return state.get("elements") or [], state


def origin(els, rect):
    if els and isinstance(els[0].get("frame"), dict):
        f = els[0]["frame"]
        return f.get("x", rect[0]), f.get("y", rect[1])
    return rect[0], rect[1]


def find(els, role=None, labels=None, prefix=None, where=None):
    """First element matching role / exact label(s) / label prefix / predicate."""
    if isinstance(labels, str):
        labels = (labels,)
    for el in els:
        if role and el.get("role") != role:
            continue
        label = el.get("label") or ""
        if labels is not None and label not in labels:
            continue
        if prefix is not None and not label.startswith(prefix):
            continue
        if where and not where(el):
            continue
        return el
    return None


def rel_x(el, org):
    f = el.get("frame")
    return f["x"] - org[0] if isinstance(f, dict) else None


def rel_center(el, org):
    f = el.get("frame")
    if not isinstance(f, dict):
        return None
    return f["x"] - org[0] + f["w"] / 2.0, f["y"] - org[1] + f["h"] / 2.0


def click(payload):
    """Background click first so a locked desktop does not fail at once."""
    try:
        call("click", dict(payload, delivery_mode="background"))
    except UiError:
        call("click", dict(payload, delivery_mode="foreground"))


def set_text(pid, hwnd, el, text):
    """Replace an Edit's text: UIA ValuePattern, else clipboard paste."""
    token = el.get("element_token")
    if not token:
        raise UiError(f"element {el.get('label')!r} has no element_token")
    try:
        call("set_value", {"element_token": token, "value": text or ""})
        return
    except UiError:
        pass
    steps = [
        {"tool": "click", "args": {"element_token": token, "delivery_mode": "foreground"}},
        {"tool": "hotkey", "args": {"pid": pid, "window_id": hwnd, "keys": ["ctrl", "a"],
                                    "delivery_mode": "foreground"}},
        {"tool": "press_key", "args": {"pid": pid, "window_id": hwnd, "key": "backspace",
                                       "delivery_mode": "foreground"}},
    ]
    if text:
        call("clipboard_write", {"text": text})
        steps.append({"tool": "hotkey", "args": {"pid": pid, "window_id": hwnd,
                                                 "keys": ["ctrl", "v"],
                                                 "delivery_mode": "foreground"}})
    call("run_steps", {"pid": pid, "window_id": hwnd, "steps": steps, "delay_ms": 50})


def composer_text(pid, hwnd):
    """Current composer text, or None when the composer cannot be read."""
    try:
        els, _ = elements(pid, hwnd)
    except UiError:
        return None
    comp = find(els, role="Edit", labels=LABELS["composer"])
    return (comp.get("value") or "") if comp else None


def press_enter(pid, hwnd, composer):
    """Submit the composer. Background routes first (lock-screen safe).

    Each route is tried only while the composer still holds text, so a
    delivered Enter is never repeated.
    """
    def pending():
        text = composer_text(pid, hwnd)
        return text is None or bool(text.strip())

    token = (composer or {}).get("element_token")
    if token:
        try:
            call("press_key", {"element_token": token, "key": "return",
                               "delivery_mode": "background"})
        except UiError:
            pass
        time.sleep(0.3)
        if not pending():
            return "element"
    try:
        call("press_key", {"pid": pid, "window_id": hwnd, "key": "return",
                           "delivery_mode": "background"})
    except UiError:
        pass
    time.sleep(0.3)
    if not pending():
        return "window"
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.PostMessageW(hwnd, 0x0100, 0x0D, 0x001C0001)  # WM_KEYDOWN VK_RETURN
        user32.PostMessageW(hwnd, 0x0101, 0x0D, 0xC01C0001)  # WM_KEYUP
    except (OSError, AttributeError):
        pass
    time.sleep(0.3)
    if not pending():
        return "postmessage"
    call("press_key", {"pid": pid, "window_id": hwnd, "key": "return",
                       "delivery_mode": "foreground"})
    return "foreground"


def wait_open(pid, target, timeout=2.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lc.current_open_chat_mid(pid) == target:
            return True
        time.sleep(0.2)
    return lc.current_open_chat_mid(pid) == target


# ---------------------------------------------------------------------------
# Opening a chat
# ---------------------------------------------------------------------------

def _room_name(pid, target, chat_name):
    want = (chat_name or "").strip()
    if want:
        return want
    _msgs, contacts, _s, _m = lc.scan_memory(pid)
    return (contacts.get(target) or lc._load_group_file(lc.GROUPS_JSON).get(target) or "").strip()


def open_chat(pid, hwnd, rect, target, chat_name="", no_search=False, log=print):
    """Open `target`; True only when LINE memory reports it as the open chat."""
    if lc.current_open_chat_mid(pid) == target:
        return True
    if no_search:
        return False
    els, _ = elements(pid, hwnd)
    org = origin(els, rect)

    if target == lc.self_mid_hint() or (chat_name or "") in LABELS["keep"]:
        keep = find(els, role="Button", labels=LABELS["keep"],
                    where=lambda e: (rel_x(e, org) or 999) < 100)
        if keep:
            click({"element_token": keep["element_token"]})
            return wait_open(pid, target)

    # The friends list (好友) lists groups and friends with a name search.
    search = find(els, role="Edit", labels=LABELS["search"])
    if not search:
        # The sidebar 好友 tab is leftmost; a chat-list filter has the same name.
        tabs = [e for e in els if e.get("role") == "TabItem"
                and (e.get("label") or "") in LABELS["friends_tab"]]
        tab = min(tabs, key=lambda e: rel_x(e, org) or 0) if tabs else None
        if tab and tab.get("element_token"):
            click({"element_token": tab["element_token"]})
        else:
            x, y = (31, 55)
            click({"pid": pid, "window_id": hwnd, "x": x, "y": y})
        time.sleep(0.25)
        els, _ = elements(pid, hwnd)
        org = origin(els, rect)
        search = find(els, role="Edit", labels=LABELS["search"])
    if search and (search.get("value") or "").strip():
        set_text(pid, hwnd, search, "")
        time.sleep(0.25)
        els, _ = elements(pid, hwnd)
        org = origin(els, rect)

    want = _room_name(pid, target, chat_name)
    if not want:
        log(f"[*] no display name known for {target}; cannot search")
        return False

    def entry(els_, org_):
        def in_list(e):
            f = e.get("frame")
            return (isinstance(f, dict) and 50 <= f["x"] - org_[0] < 350
                    and f["y"] - org_[1] > 60)
        return (find(els_, role="Button", labels=(want,), where=in_list)
                or find(els_, role="Button", prefix=f"{want}, ", where=in_list))

    hit = entry(els, org)
    if not hit and search:
        set_text(pid, hwnd, search, want)
        time.sleep(0.6)
        els, _ = elements(pid, hwnd)
        org = origin(els, rect)
        hit = entry(els, org)
    if not hit:
        log(f"[*] {want!r} not found in the friends list")
        return False
    log(f"[*] clicking list entry {hit.get('label')!r}")
    click({"element_token": hit["element_token"]})
    time.sleep(0.35)
    if wait_open(pid, target, timeout=0.8):
        return True
    # A profile card opened instead; its 聊天 button opens the room.
    els, _ = elements(pid, hwnd)
    org = origin(els, rect)
    chat_btn = find(els, role="Button", labels=LABELS["open_chat"],
                    where=lambda e: (rel_x(e, org) or 0) > 350)
    if chat_btn:
        click({"element_token": chat_btn["element_token"]})
        if wait_open(pid, target, timeout=2.5):
            return True
    return lc.current_open_chat_mid(pid) == target


# ---------------------------------------------------------------------------
# Sending text
# ---------------------------------------------------------------------------

def send_text(args, target, log=print):
    """Windows equivalent of send_message._run_text. Returns an exit code.

    0 sent (or dry-run), 3 no window/composer, 4 route check failed (not
    sent), 6 Enter routes exhausted with the text still in the composer.
    The caller holds the UI lock.
    """
    pid = lc.find_line_pid()
    if not pid:
        log("ERROR: LINE.exe not running; aborting without sending.", file=sys.stderr)
        return 4
    state = {"enter": False}
    try:
        return _send_text(args, target, pid, state, log)
    except UiError as exc:
        if state["enter"]:
            log(f"ERROR: UI failed at/after Enter; delivery uncertain: {exc}", file=sys.stderr)
            return 6
        log(f"ERROR: UI failed before Enter; nothing sent: {exc}", file=sys.stderr)
        return 3


def _send_text(args, target, pid, state, log):
    with session():
        found = main_window(pid)
        if not found:
            log("ERROR: no LINE window found.", file=sys.stderr)
            return 3
        hwnd, rect = found
        log(f"[*] LINE window: hwnd={hwnd} rect={rect} pid={pid}")
        name = (getattr(args, "chat", None) or "").strip()
        if not open_chat(pid, hwnd, rect, target, name,
                         no_search=getattr(args, "no_search", False), log=log):
            log(f"ERROR: open chat mid={lc.current_open_chat_mid(pid)} is not target "
                f"{target}. Aborting without sending.", file=sys.stderr)
            return 4
        log(f"[*] open chat verified {target}")
        els, _ = elements(pid, hwnd)
        composer = find(els, role="Edit", labels=LABELS["composer"])
        if not composer:
            log("ERROR: message composer not found.", file=sys.stderr)
            return 3
        if lc.current_open_chat_mid(pid) != target:
            log("ERROR: room changed before paste; aborting.", file=sys.stderr)
            return 4
        set_text(pid, hwnd, composer, args.text)
        time.sleep(0.35)
        if lc.current_open_chat_mid(pid) != target:
            log("ERROR: room changed before Enter; draft left unsent.", file=sys.stderr)
            return 4
        if getattr(args, "dry_run", False):
            log("[*] --dry-run: NOT pressing Enter. Message is in the input box.")
            return 0
        state["enter"] = True
        route = press_enter(pid, hwnd, composer)
        left = composer_text(pid, hwnd)
        if left and left.strip():
            log("ERROR: composer still holds the text after Enter.", file=sys.stderr)
            return 6
        log(f"[*] Sent (enter via {route}).")
        return 0


# ---------------------------------------------------------------------------
# Attaching a file
# ---------------------------------------------------------------------------

def stage_copy(path, max_bytes):
    """Copy `path` (local or UNC) into a private temp dir under its own name.

    LINE's picker reads a plain local path; a staged immutable copy also
    keeps a network share or a file that changes mid-upload out of it.
    Returns (staged_path, size, cleanup_dir).
    """
    root = os.path.join(tempfile.gettempdir(), "line-uploads")
    os.makedirs(root, exist_ok=True)
    cutoff = time.time() - 6 * 3600
    try:
        for entry in os.scandir(root):
            try:
                if entry.is_dir(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass
    stage = tempfile.mkdtemp(prefix="upload-", dir=root)
    dest = os.path.join(stage, os.path.basename(path))
    with open(path, "rb") as src:
        before = os.fstat(src.fileno())
        if before.st_size > max_bytes:
            raise OSError("file exceeds configured size limit")
        copied = 0
        with open(dest, "xb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise OSError("file grew beyond configured size limit")
                out.write(chunk)
        after = os.fstat(src.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError("source file changed while staging")
    return dest, copied, stage


def _dialog_filename_edit(els):
    for el in els:
        label = (el.get("label") or "").lower()
        if el.get("role") == "Edit" and ("檔案名稱" in label or "file name" in label):
            return el
    cands = [el for el in els if el.get("role") == "Edit" and not any(
        s in (el.get("label") or "").lower() for s in ("搜尋", "search", "位址", "address"))]
    cands.sort(key=lambda e: (e.get("frame") or {}).get("y", 0), reverse=True)
    return cands[0] if cands else None


def _dialog_button(els, labels, min_width=40):
    for el in els:
        if el.get("role") not in ("SplitButton", "Button"):
            continue
        label = (el.get("label") or "").strip()
        if (el.get("frame") or {}).get("w", 0) < min_width:
            continue
        if label in labels:
            return el
    return None


def _pid_windows(pid):
    return [w for w in call("list_windows").get("windows") or []
            if w.get("pid") == pid and w.get("window_id") is not None]


# Standard Win32 file dialog: control 1148 (cmb13) is the filename combo box;
# IDOK/IDCANCEL commit or dismiss it. These messages work in the background,
# including on a locked desktop, unlike clipboard paste or synthesized input.
_FILENAME_COMBO_ID = 0x47C
_WM_SETTEXT, _WM_GETTEXT, _WM_GETTEXTLENGTH, _WM_COMMAND = 0x000C, 0x000D, 0x000E, 0x0111
_IDOK, _IDCANCEL = 1, 2


def _win32_children(hwnd):
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    found = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumChildWindows(wt.HWND(hwnd), proto(lambda h, _l: found.append(h) or True), 0)
    return found


def _win32_class(hwnd):
    import ctypes
    buf = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def _win32_filename_edit(dialog):
    """The Edit inside the dialog's filename combo box (id 1148), or None."""
    import ctypes
    user32 = ctypes.windll.user32
    for child in _win32_children(dialog):
        if user32.GetDlgCtrlID(child) == _FILENAME_COMBO_ID:
            for inner in [child] + _win32_children(child):
                if _win32_class(inner) == "Edit":
                    return inner
    return None


def _win32_text(hwnd):
    import ctypes
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    n = user32.SendMessageW(hwnd, _WM_GETTEXTLENGTH, 0, None)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.SendMessageW(hwnd, _WM_GETTEXT, n + 1, ctypes.addressof(buf))
    return buf.value


def win32_set_picker_path(dialog, path):
    """Put `path` in the picker's filename box; True when it reads back."""
    import ctypes
    edit = _win32_filename_edit(dialog)
    if not edit:
        return False
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
    user32.SendMessageW(edit, _WM_SETTEXT, 0, path)
    return _win32_text(edit) == path


def win32_dialog_command(dialog, command):
    import ctypes
    return bool(ctypes.windll.user32.PostMessageW(dialog, _WM_COMMAND, command, 0))


def attach_file(pid, target, chat_name, local_path, commit, on_commit, log=print):
    """Open `target`, click 傳送檔案, choose `local_path` in the picker.

    Returns (accepted, detail). `on_commit` runs right before Open is
    clicked: after that the upload may happen even if a later step fails.
    """
    found = main_window(pid)
    if not found:
        return False, "no LINE window"
    hwnd, rect = found
    if not open_chat(pid, hwnd, rect, target, chat_name, log=log):
        return False, f"cannot open verified room (open={lc.current_open_chat_mid(pid)})"
    els, _ = elements(pid, hwnd)
    org = origin(els, rect)
    attach = (find(els, role="Button", labels=LABELS["attach"],
                   where=lambda e: (rel_x(e, org) or 0) > 350)
              or find(els, role="Button", labels=LABELS["attach"]))
    if not attach or not attach.get("element_token"):
        return False, "attach button not found"
    if lc.current_open_chat_mid(pid) != target:
        return False, "room changed before attach"
    before = {int(w["window_id"]) for w in _pid_windows(pid)}
    try:
        call("click", {"element_token": attach["element_token"]})
    except UiError:
        call("bring_to_front", {"pid": pid, "window_id": hwnd})
        call("click", {"element_token": attach["element_token"],
                       "delivery_mode": "foreground"})
    dialog = None
    deadline = time.monotonic() + 8
    while dialog is None and time.monotonic() < deadline:
        new = [w for w in _pid_windows(pid)
               if int(w["window_id"]) not in before and int(w["window_id"]) != hwnd
               and _area(w) > 150 * 150]
        if new:
            new.sort(key=_area, reverse=True)
            dialog = new[0]
            break
        time.sleep(0.25)
    if dialog is None:
        return False, "file picker did not open"
    dlg = int(dialog["window_id"])
    log(f"[*] file dialog hwnd={dlg} title={dialog.get('title')!r}")
    time.sleep(0.3)
    committed = []

    def dialog_open():
        return any(int(w["window_id"]) == dlg for w in _pid_windows(pid))

    def close_dialog():
        win32_dialog_command(dlg, _IDCANCEL)
        time.sleep(0.4)
        try:
            if dialog_open():
                els_, _ = elements(pid, dlg, max_elements=400)
                cancel = _dialog_button(els_, DIALOG_CANCEL, min_width=0)
                if cancel and cancel.get("element_token"):
                    call("click", {"element_token": cancel["element_token"]})
        except UiError:
            pass

    try:
        if not win32_set_picker_path(dlg, local_path):
            # Fallback for a non-standard picker: UIA value, then clipboard.
            dels, _ = elements(pid, dlg, max_elements=400)
            name_edit = _dialog_filename_edit(dels)
            if not name_edit:
                close_dialog()
                return False, "picker filename field not found"
            set_text(pid, dlg, name_edit, local_path)
        time.sleep(0.3)
        if not commit:
            close_dialog()
            return True, "picker cancelled without sending"
        if lc.current_open_chat_mid(pid) != target:
            close_dialog()
            return False, "room changed before file commit"
        on_commit()
        committed.append(True)
        win32_dialog_command(dlg, _IDOK)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            time.sleep(0.4)
            if not dialog_open():
                return True, "picker closed"
        close_dialog()
        return False, "picker still open after Open (path rejected?)"
    except (UiError, OSError) as exc:
        close_dialog()
        if committed:
            raise
        return False, f"picker step failed before commit: {exc}"


# ---------------------------------------------------------------------------
# Desktop state
# ---------------------------------------------------------------------------

def desktop_locked():
    """True when the interactive desktop is the lock/logon screen.

    Background UIA (invoke/value) and keys still reach LINE then, but Qt
    ignores synthesized mouse input, so hover-only controls are unreachable.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not desk:
            return True
        try:
            return not user32.SwitchDesktop(ctypes.c_void_p(desk))
        finally:
            user32.CloseDesktop(ctypes.c_void_p(desk))
    except (OSError, AttributeError):
        return True


def idle_seconds():
    """Seconds since the last keyboard/mouse input on this session."""
    import ctypes
    import ctypes.wintypes as wt

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


# ---------------------------------------------------------------------------
# Sync nudge (watchdog)
# ---------------------------------------------------------------------------

def nudge(pid):
    """Click the sidebar 聊天 tab: makes an idle client reconnect. No typing."""
    with session():
        found = main_window(pid)
        if not found:
            return False
        hwnd, rect = found
        els, _ = elements(pid, hwnd)
        org = origin(els, rect)
        tabs = [e for e in els if e.get("role") == "TabItem"
                and (e.get("label") or "") in LABELS["chats_tab"]]
        tab = min(tabs, key=lambda e: rel_x(e, org) or 0) if tabs else None
        if not tab or not tab.get("element_token"):
            return False
        click({"element_token": tab["element_token"]})
        return True
