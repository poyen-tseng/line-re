"""Small OS shims so the Linux (Wine/X11) and native Windows paths share code.

Only process, lock and launcher plumbing lives here. LINE-specific UI work
for Windows is in `win_line.py`; memory access is in `line_common.py`.
Nothing here talks to LINE servers or changes another process's memory.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

# The real OS. Primitives below (locks, process control, launchers) use it.
_NT = os.name == "nt"
# Which LINE client path the bot drives: native Windows UIA or Linux/Wine X11.
# Equal to the OS; tests patch this flag to exercise the Linux path anywhere.
IS_WINDOWS = _NT

if _NT:
    import msvcrt
else:
    import fcntl


# ---------------------------------------------------------------------------
# File locks (fcntl.flock on POSIX, msvcrt byte-range lock on Windows)
# ---------------------------------------------------------------------------

def _try_lock(fh, exclusive=True):
    """One non-blocking attempt. True when the lock is held."""
    if _NT:
        # msvcrt locks a byte range starting at the current position. Lock
        # byte 0 of the (possibly empty) lock file; the range may lie past EOF.
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(fh, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(fh):
    if _NT:
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(fh, fcntl.LOCK_UN)


def lock_file(fh, timeout=None, poll=0.05):
    """Take an exclusive lock on an open file. timeout None waits forever.

    Raises TimeoutError when `timeout` elapses first.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while not _try_lock(fh):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("lock is held by another process")
        time.sleep(poll)


@contextmanager
def locked_path(path, timeout=None):
    """Exclusive advisory lock on `path` (created if missing)."""
    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "a+") as fh:
        lock_file(fh, timeout=timeout)
        try:
            yield fh
        finally:
            _unlock(fh)


def user_tag():
    """Per-user suffix for shared temp lock names (uid on POSIX, name on NT)."""
    if hasattr(os, "getuid"):
        return str(os.getuid())
    name = os.environ.get("USERNAME") or "user"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:40] or "user"


def temp_lock_path(name):
    return os.path.join(tempfile.gettempdir(), f"line-{name}-{user_tag()}.lock")


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def no_window_flags():
    """creationflags that keep console helpers from flashing a window."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if _NT else 0


def detached_popen_kwargs():
    """Popen kwargs for a child that must outlive its parent and its group."""
    if _NT:
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
        return dict(creationflags=flags, close_fds=True)
    return dict(start_new_session=True, close_fds=True)


def _win_process_times(pid):
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = wt.DWORD()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
            return None  # exited (259 = STILL_ACTIVE)
        times = [wt.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        created = times[0]
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        k32.CloseHandle(handle)


def process_token(pid):
    """Stable identity for a live PID (start time), or None if gone/zombie.

    Guards delayed kills against PID reuse on both platforms.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if _NT:
        try:
            return _win_process_times(pid)
        except OSError:
            return None
    try:
        # comm may contain spaces or parentheses; fields after its final ')'.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def kill_tree(pid, hard=True):
    """Terminate `pid` and its descendants. POSIX: its process group."""
    if _NT:
        argv = ["taskkill", "/PID", str(int(pid)), "/T"] + (["/F"] if hard else [])
        try:
            subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, creationflags=no_window_flags())
        except (OSError, subprocess.SubprocessError):
            pass
        return
    import signal
    try:
        os.killpg(int(pid), signal.SIGKILL if hard else signal.SIGTERM)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# CLI launchers
# ---------------------------------------------------------------------------

_CMD_META = re.compile(r'[&|<>^%!"\r\n()]')


def _newest_version_dir(root):
    pat = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}(?:-\d{2}-\d{2}-\d{2})?-[a-f0-9]+$")
    try:
        dirs = [p for p in Path(root).iterdir() if p.is_dir() and pat.match(p.name)]
    except OSError:
        return None

    def key(p):
        return [int(x) if x.isdigit() else x for x in re.split(r"[.-]", p.name)]
    dirs.sort(key=key, reverse=True)
    return dirs[0] if dirs else None


def _windows_node_entry(name, shim):
    """`node.exe script.js` behind a known npm/cursor shim, or None."""
    base = Path(shim).parent
    if name == "cursor-agent":
        ver = _newest_version_dir(base / "versions")
        if ver and (ver / "node.exe").is_file() and (ver / "index.js").is_file():
            return [str(ver / "node.exe"), str(ver / "index.js")]
    if name == "codex":
        script = base / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = base / "node.exe"
        node = str(node) if node.is_file() else shutil.which("node")
        if script.is_file() and node:
            return [node, str(script)]
    return None


def resolve_cli(name):
    """argv prefix that runs CLI `name` without a shell, or None if missing.

    On Windows, npm/cursor `.cmd` shims re-parse every argument through
    cmd.exe; prompts that come from chat could then inject commands. Known
    shims are resolved to their node entry point. Unknown `.cmd`/`.bat`
    launchers are returned flagged so `safe_argv` can refuse risky args.
    """
    found = shutil.which(name)
    if not found:
        return None
    if not _NT:
        return [found]
    low = found.lower()
    if low.endswith((".cmd", ".bat")):
        entry = _windows_node_entry(name, found)
        if entry:
            return entry
        return [found]
    if low.endswith(".ps1"):
        return None
    return [found]


def safe_argv(argv):
    """Refuse argv that cmd.exe would re-parse unsafely (Windows .cmd/.bat)."""
    if _NT and argv and str(argv[0]).lower().endswith((".cmd", ".bat")):
        if any(_CMD_META.search(str(a)) for a in argv[1:]):
            raise OSError(f"refusing to pass shell metacharacters to {argv[0]}")
    return argv
