"""LINE sync-stall watchdog for the group-bot loop.

LINE desktop under Wine sometimes loses its long-poll/sync connection to
LINE's servers: the peer closes the socket, LINE leaves it in
CLOSE-WAIT for tens of seconds and does not reconnect during observation.
Restarting LINE fixes it (then the group-bot catch-up answers late
messages).

`SyncWatchdog.tick(pid)` is cheap to call every loop iteration. Every
`check_every` seconds it runs `ss -Htanpe` and checks PID/inode ownership.
It is stalled when LINE.exe has no
ESTABLISHED socket to LINE core servers (147.92/16, 43.223/16; legy /
*.line-apps.com), whatever else is open. Sockets to other LINE hosts
or the CDN (*.line-scdn.net) do not count: a session that kept only a
non-core ESTABLISHED socket still missed mentions until a restart
brought back 147.92.x ESTAB and the late messages. Core CLOSE-WAIT sockets (server hung up, LINE never
closed yet) are logged as extra evidence. The
stall must hold continuously for `stall_secs` before a restart, and restarts
are limited to one per `cooldown_secs`. A healthy check resets the timer,
so a normal idle reconnect does not trigger it.

Inspection failure and PID changes reset continuity. Socket destruction is
opt-in only: the parent's live trial removed CLOSE-WAIT but did not reconnect.
When enabled it runs once per stall, allows a grace period, then retains the
restart fallback. No global TCP settings or live process memory are changed.
"""
import ipaddress
import math
import os
import re
import signal
import subprocess
import sys
import time

DEFAULT_NETS = ("147.92.0.0/16", "43.223.0.0/16")
OPEN_LINE = "/workspace/line-bottles/open-line.sh"
# Last restart time survives bot restarts so the cooldown is not reset.
STAMP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     ".watchdog_last_restart")


def _read_stamp():
    try:
        with open(STAMP) as f:
            stamp = float(f.read().strip() or 0)
            return stamp if math.isfinite(stamp) and stamp >= 0 else 0.0
    except (OSError, ValueError):
        return 0.0


def _write_stamp(t):
    try:
        import line_common as lc
        lc.atomic_write_text(STAMP, f"{t:.0f}\n")
    except OSError:
        pass
_PID_RE = re.compile(r'"LINE\.exe",pid=(\d+)')


def _socket_inodes(pid):
    """Socket ownership fallback when ss omits process names. None = unknown."""
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    inodes = set()
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except FileNotFoundError:
            continue  # fd closed during enumeration
        except OSError:
            return None
        match = re.fullmatch(r"socket:\[(\d+)\]", target)
        if match:
            inodes.add(match[1])
    return inodes


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[watchdog {ts}] {msg}", file=sys.stderr, flush=True)


def line_conn_states(pid, nets):
    """Return dict(core_estab, core_closewait=set of (local,peer),
    core_other=[states], estab_443) for pid, or None if ss failed."""
    try:
        result = subprocess.run(["ss", "-Htanpe"], capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    inodes = _socket_inodes(pid)
    owned = False
    r = dict(core_estab=0, core_closewait=set(), core_other=[], estab_443=0)
    for line in result.stdout.splitlines():
        ino = re.search(r"\bino:(\d+)\b", line)
        if not (any(int(m.group(1)) == pid for m in _PID_RE.finditer(line))
                or (inodes is not None and ino and ino.group(1) in inodes)):
            continue
        owned = True
        parts = line.split()
        if len(parts) < 5:
            continue
        state, local, peer = parts[0], parts[3], parts[4]
        host, _, port = peer.rpartition(":")
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            continue
        if ip.is_loopback:
            continue
        if state == "ESTAB" and port == "443":
            r["estab_443"] += 1
        if port == "443" and any(ip in n for n in nets):
            if state == "ESTAB":
                r["core_estab"] += 1
            elif state == "CLOSE-WAIT":
                r["core_closewait"].add((local, peer))
            else:
                r["core_other"].append(state)
    # Permission/PID-namespace errors are not evidence of an outage.
    return r if owned or inodes is not None else None


def destroy_core_closewait(pid, nets):
    """Experimental nudge; only exact, currently PID-owned core TCP tuples.

    ss -K needs CAP_NET_ADMIN here, hence sudo -n (never prompts). A zero
    exit only means the command succeeded, not that LINE reconnected.
    """
    states = line_conn_states(pid, nets)
    if states is None:
        return False
    attempted = False
    for local, peer in sorted(states["core_closewait"]):
        lh, _, lp = local.rpartition(":")
        ph, _, pp = peer.rpartition(":")
        try:
            src = ipaddress.ip_address(lh.strip("[]"))
            dst = ipaddress.ip_address(ph.strip("[]"))
            if not (lp.isdigit() and 0 < int(lp) <= 65535 and pp == "443"
                    and any(dst in net for net in nets)):
                continue
        except ValueError:
            continue
        cmd = ["sudo", "-n", "ss", "-K", "-t", "state", "close-wait",
               "src", str(src), "sport", "=", lp,
               "dst", str(dst), "dport", "=", pp]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            attempted = True
            _log(f"socket nudge {local} -> {peer}: rc={result.returncode} "
                 f"{result.stderr.strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as exc:
            _log(f"socket nudge failed: {exc}")
    return attempted


def _process_identity(pid):
    """Start time protects the restart's delayed SIGKILL from PID reuse."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _wine_prefix(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for entry in f.read().split(b"\0"):
                if entry.startswith(b"WINEPREFIX="):
                    return entry[len(b"WINEPREFIX="):]
    except OSError:
        pass
    return None


def _bottles_line_pids(line_pid):
    # Never stop unrelated Wine applications or shell commands mentioning LINE.
    prefix = _wine_prefix(line_pid)
    pids = {line_pid}
    if prefix:
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == os.getpid():
                continue
            try:
                with open(f"/proc/{name}/comm") as f:
                    comm = f.read().strip()
            except OSError:
                continue
            # Everything Wine runs in LINE's prefix: plugin helpers
            # (linemediaplayer, linecall) and Wine's own services/explorer/
            # rpcss... Before 2026-10-21 these survived each restart and the
            # leaked trees drove the host load average to ~140. Shells/python that merely
            # inherited WINEPREFIX are spared.
            if (comm.lower().endswith(".exe") or comm in ("wineserver", "linemediaplayer")
                    or comm.lower().startswith(("line", "wine"))) \
                    and _wine_prefix(name) == prefix:
                pids.add(int(name))
    return {pid: identity for pid in pids
            if (identity := _process_identity(pid)) is not None}


def restart_line(display=":3", pid=None):
    import line_common as lc
    if pid is None or lc.find_line_pid(pid) != pid:
        _log("restart: target PID disappeared; waiting for rediscovery")
        return False
    pids = _bottles_line_pids(pid)
    _log(f"restart: SIGTERM bottles/wine LINE tree pids={pids}")
    for p, identity in pids.items():
        if _process_identity(p) != identity:
            continue
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(5)
    left = [p for p, identity in pids.items()
            if _process_identity(p) == identity]
    if left:
        _log(f"restart: SIGKILL remaining pids={left}")
        for p in left:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(2)
    env = dict(os.environ, DISPLAY=display)
    try:
        result = subprocess.run(["bash", OPEN_LINE], env=env, timeout=30,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            _log(f"restart: launcher exited {result.returncode}")
            return False
        _log(f"restart: launched {OPEN_LINE} DISPLAY={display}")
        return True
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"restart: launch failed: {e}")
        return False


# Window-relative center of the left sidebar "Chats" icon (742x806 layout).
CHATS_ICON_REL = (32, 108)


def ui_nudge(display=":3"):
    """Click LINE's sidebar Chats icon; never types or sends anything.

    Verified 2026-10-21 12:41-12:48: with core sockets idle-closed
    (CLOSE-WAIT/none), this click made LINE open a new ESTAB to 147.92/16
    within ~1s in 4/4 trials, while idle controls stayed at 0 (2/2).
    Takes the senders' UI lock so it never interleaves with a send.
    Returns True if the click was issued.
    """
    try:
        import send_message as sm
        picked = sm.pick_main_window(display)
        if not picked:
            _log("ui-nudge: no LINE window")
            return False
        wid = picked[0]
        with sm._ui_lock():
            env = dict(os.environ, DISPLAY=display)
            for cmd in (["xdotool", "windowactivate", wid],
                        ["xdotool", "mousemove", "--window", wid,
                         str(CHATS_ICON_REL[0]), str(CHATS_ICON_REL[1])],
                        ["xdotool", "click", "1"]):
                subprocess.run(cmd, env=env, timeout=5, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        _log(f"ui-nudge failed: {exc}")
        return False


class SyncWatchdog:
    def __init__(self, stall_secs=900, cooldown_secs=1800, check_every=10,
                 status_every=60, nets=DEFAULT_NETS, display=":3",
                 enabled=True, socket_nudge=False, nudge_after=30,
                 nudge_grace=30, restart=False, ui_nudge=True,
                 ui_nudge_after=45, ui_nudge_every=60):
        for name, value in (("stall_secs", stall_secs), ("cooldown_secs", cooldown_secs),
                            ("check_every", check_every), ("status_every", status_every),
                            ("nudge_after", nudge_after), ("nudge_grace", nudge_grace)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        self.stall_secs = stall_secs
        self.cooldown_secs = cooldown_secs
        self.check_every = check_every
        self.status_every = status_every
        self.nets = [ipaddress.ip_network(n) for n in nets]
        self.display = display
        self.enabled = enabled
        # Auto-restart is opt-in. LINE's server TLS-closes idle core sockets
        # after ~30s and LINE reconnects on demand, so "no core ESTAB" alone
        # is not a stall; restarting every few minutes wiped memory state and
        # interrupted sends. By default the watchdog only logs.
        self.restart = restart
        # First recovery step: a harmless UI click that makes LINE reconnect.
        self.ui_nudge = ui_nudge
        self.ui_nudge_after = ui_nudge_after
        self.ui_nudge_every = ui_nudge_every
        self.last_ui_nudge = None
        self.ui_nudges = 0
        self.urgent = False
        self._thread = None
        self._stop_evt = None
        self._wake_evt = None
        self.last_activity = None
        self.last_stall_log = None
        self.last_restart_warn = None
        self.socket_nudge = socket_nudge
        self.nudge_after = nudge_after
        self.nudge_grace = nudge_grace
        self.nudged_at = None
        self.pid = None
        self.last_check = None
        self.last_status = 0.0
        self.stall_since = None
        self.last_restart = _read_stamp()
        if enabled:
            _log(f"ui-nudge={'on' if ui_nudge else 'off'} after "
                 f"{ui_nudge_after}s, every {ui_nudge_every}s")
            _log(f"enabled ({'RESTART' if restart else 'log-only'}): sustained "
                 f"{stall_secs}s with no ESTAB to LINE core {','.join(nets)} "
                 f"AND no new messages -> "
                 f"{'restart LINE' if restart else 'log'}; cooldown "
                 f"{cooldown_secs}s; check every {check_every}s")

    def kick(self):
        """A command is about to be answered: check now and, if LINE has
        no core connection, nudge immediately so it is back before Enter
        (12:56 incident: the reply sat unsent ~90s waiting on reconnect)."""
        self.urgent = True
        self.last_check = None
        if self._wake_evt is not None:
            self._wake_evt.set()

    def start(self, find_pid, poll=1.0):
        """Run tick() in a daemon thread so slow scans/sends never delay
        recovery and recovery never delays the bot loop."""
        import threading
        if not self.enabled or self._thread is not None:
            return
        self._stop_evt = threading.Event()
        self._wake_evt = threading.Event()

        def loop():
            pid = None
            while not self._stop_evt.is_set():
                try:
                    pid = find_pid(pid)
                    self.tick(pid)
                except Exception as exc:  # never kill the thread
                    _log(f"thread error: {exc!r}")
                self._wake_evt.wait(poll)
                self._wake_evt.clear()

        self._thread = threading.Thread(target=loop, name="sync-watchdog", daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is not None:
            self._stop_evt.set()
            self._wake_evt.set()
            self._thread.join(timeout=5)
            self._thread = None

    def activity(self):
        """New messages arrived: sync works, whatever the sockets say."""
        self.last_activity = time.monotonic()
        if self.stall_since is not None:
            self.stall_since = self.last_activity

    def tick(self, pid):
        """Return True if the launcher succeeded. Elapsed timers are monotonic."""
        if pid != self.pid:
            self.pid = pid
            self.stall_since = self.nudged_at = self.last_check = None
        if not self.enabled or pid is None:
            self.stall_since = self.nudged_at = None
            return False
        now = time.monotonic()
        if self.last_check is not None and now - self.last_check < self.check_every:
            return False
        self.last_check = now
        res = line_conn_states(pid, self.nets)
        if res is None:
            _log("check: ss failed; skipping")
            self.stall_since = self.nudged_at = None
            return False
        cw = res["core_closewait"]
        stalled = res["core_estab"] == 0
        desc = (f"pid={pid} core_estab={res['core_estab']} "
                f"core_closewait={len(cw)} core_other={res['core_other']} "
                f"estab_443={res['estab_443']}")
        if not stalled:
            self.urgent = False
            if self.stall_since is not None:
                _log(f"check: recovered {desc}")
            self.stall_since = self.nudged_at = None
            if now - self.last_status >= self.status_every:
                self.last_status = now
                _log(f"check ok {desc}")
            return False
        why = ("no core ESTAB, core CLOSE-WAIT" if cw
               else "no core ESTAB")
        if self.stall_since is None:
            self.stall_since = now
        if self.last_activity is not None and self.last_activity > self.stall_since:
            self.stall_since = self.last_activity
        dur = now - self.stall_since
        if (self.last_stall_log is None or dur < 1
                or now - self.last_stall_log >= self.status_every):
            self.last_stall_log = now
            _log(f"check STALL ({why}) {desc} for {dur:.0f}s "
                 f"({'restart' if self.restart else 'warn'} at {self.stall_secs}s)")
        urgent, self.urgent = self.urgent, False
        if (self.ui_nudge
                and not (self.restart and dur >= self.stall_secs)
                and ((urgent and (self.last_ui_nudge is None
                                  or now - self.last_ui_nudge >= 8))
                     or (dur >= self.ui_nudge_after
                         and (self.last_ui_nudge is None
                              or now - self.last_ui_nudge >= self.ui_nudge_every)))):
            self.last_ui_nudge = now
            ok = ui_nudge(self.display)
            self.ui_nudges += 1
            _log(f"ui-nudge #{self.ui_nudges}{' (pre-reply)' if urgent else ''} after {dur:.0f}s stall: "
                 f"{'clicked Chats icon' if ok else 'failed'}; restart only if "
                 f"still stalled at {self.stall_secs:.0f}s")
            self.last_check = now - self.check_every + min(5, self.check_every)  # re-check soon
            return False
        if self.socket_nudge and cw and self.nudged_at is None and dur >= self.nudge_after:
            self.nudged_at = now  # also bound retries after sudo/ss failures
            destroy_core_closewait(pid, self.nets)
        if self.nudged_at is not None and now - self.nudged_at < self.nudge_grace:
            return False
        if dur < self.stall_secs:
            return False
        if not self.restart:
            if now - (self.last_restart_warn or -1e18) >= self.cooldown_secs:
                self.last_restart_warn = now
                _log(f"stall sustained {dur:.0f}s with no new messages; "
                     "auto-restart disabled (use --watchdog-restart)")
            return False
        wall_now = time.time()
        # Clock rollback must not suppress recovery indefinitely.
        since_restart = max(0, wall_now - self.last_restart)
        if self.last_restart > wall_now:
            self.last_restart = wall_now
        if self.last_restart and since_restart < self.cooldown_secs:
            _log(f"stall sustained but in cooldown "
                 f"({since_restart:.0f}s/{self.cooldown_secs}s); not restarting")
            return False
        _log(f"stall sustained {dur:.0f}s; restarting LINE pid={pid}")
        self.last_restart = wall_now
        _write_stamp(wall_now)  # rate-limit failed launches too
        self.stall_since = self.nudged_at = None
        return restart_line(self.display, pid=pid)
