#!/usr/bin/env python3
"""Read-only scan of a running LINE 26.0.2 process for the file/video send path.

Prints the thrift ContentType name table, UTF-16 \\\\.\\pipe\\ names, and
live vtables for the upload objects. Does not write process memory and
does not send a message.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "automation"))
import line_common as lc  # noqa: E402

IMAGE_BASE = 0x140000000
# int32 values 0..22, then pointer[23] to the names. RVA from this build.
VALUES_RVA = 0x394EA10
NAMES_RVA = 0x394EA70
N_TYPES = 23

# Primary vtables (COL.offset == 0), VA at IMAGE_BASE.
PRIMARY_VTABLES = {
    "ChatMessagePanel": 0x1426F7420,
    "ChatMessageSender": 0x1427FD4B8,
    "FileChatMessageSendItem": 0x1428011E8,
    "FileUploader": 0x143213048,
    "FileUploadQueueWorker": 0x1432572B0,
    "FileTransManager": 0x143175308,
    "ObsCommandHelper": 0x143214D18,
}

PIPE_PREFIX = "\\\\.\\pipe\\".encode("utf-16le")


def _regions(pid, writable):
    out = []
    with open(f"/proc/{pid}/maps") as fh:
        for line in fh:
            parts = line.split()
            start_s, end_s = parts[0].split("-")
            start, end = int(start_s, 16), int(end_s, 16)
            perm = parts[1]
            if "r" not in perm:
                continue
            if writable and "w" not in perm:
                continue
            if end - start > 200 * 1024 * 1024:
                continue
            out.append((start, end))
    return out


def _read(mem, addr, n):
    mem.seek(addr)
    return mem.read(n)


def content_types(mem):
    raw = _read(mem, IMAGE_BASE + VALUES_RVA, N_TYPES * 4 + 8)
    # The last value is a single int32 plus padding before the pointer table.
    values = list(struct.unpack_from("<" + "i" * N_TYPES, raw))
    pointers = struct.unpack_from("<" + "Q" * N_TYPES, _read(mem, IMAGE_BASE + NAMES_RVA, N_TYPES * 8))
    rows = []
    for i, (value, ptr) in enumerate(zip(values, pointers)):
        blob = _read(mem, ptr, 32)
        name = blob.split(b"\x00", 1)[0].decode("ascii", "replace")
        rows.append((i, value, ptr, name))
    return rows


def pipe_names(pid, mem):
    found = set()
    for start, end in _regions(pid, writable=True):
        off = start
        chunk = 4 * 1024 * 1024
        while off < end:
            try:
                data = _read(mem, off, min(chunk + 160, end - off))
            except OSError:
                break
            if not data:
                break
            i = 0
            while True:
                j = data.find(PIPE_PREFIX, i)
                if j < 0:
                    break
                chars = []
                raw = data[j:j + 160]
                for k in range(0, len(raw) - 1, 2):
                    cu = raw[k] | (raw[k + 1] << 8)
                    if cu == 0 or cu > 126:
                        break
                    chars.append(chr(cu))
                found.add("".join(chars))
                i = j + 2
            off += chunk
    return sorted(found)


def live_objects(pid, mem):
    needles = {name: struct.pack("<Q", vt) for name, vt in PRIMARY_VTABLES.items()}
    hits = {name: [] for name in needles}
    for start, end in _regions(pid, writable=True):
        off = start
        chunk = 4 * 1024 * 1024
        while off < end:
            try:
                data = _read(mem, off, min(chunk + 16, end - off))
            except OSError:
                break
            if not data:
                break
            for name, needle in needles.items():
                i = 0
                while True:
                    j = data.find(needle, i)
                    if j < 0:
                        break
                    hits[name].append(off + j)
                    i = j + 8
            off += chunk
    return hits


def main():
    pid = lc.find_line_pid()
    if not pid:
        print("LINE.exe not running")
        return 1
    print(f"pid {pid}")
    print(f"open_chat {lc.current_open_chat_mid(pid)}")
    mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
    try:
        print("contentType index value name")
        for i, value, ptr, name in content_types(mem):
            mark = ""
            if name in ("IMAGE", "VIDEO", "AUDIO", "FILE", "CHATEVENT", "FLEX"):
                mark = "  <"
            print(f"  {i:2d}  {value:2d}  {ptr:#x}  {name}{mark}")
        print("pipes")
        for name in pipe_names(pid, mem):
            print(f"  {name}")
        print("live primary vtables")
        for name, addrs in live_objects(pid, mem).items():
            sample = ", ".join(hex(a) for a in addrs[:4])
            print(f"  {name} n={len(addrs)} vt={PRIMARY_VTABLES[name]:#x} {sample}")
    finally:
        mem.close()
    messages, _contacts, _sc, _ma = lc.scan_memory(pid)
    print(f"messages {len(messages)}")
    for mid, row in sorted(messages.items(), key=lambda kv: kv[1].get("createdTime") or 0):
        ct = row.get("contentType") or 0
        if ct == 0:
            continue
        meta = (row.get("contentMeta") or "")[:220]
        print(f"  id={mid} ct={ct} to={row.get('to')} meta={meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
