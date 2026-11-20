# Native Windows LINE Desktop

The same tools run against the native Windows client (verified with LINE
26.6.0.4004 on Windows 11). The Linux/Wine path is unchanged; every module
branches on `platform_compat.IS_WINDOWS`.

## How it differs

| Piece | Linux / Wine | Windows |
|---|---|---|
| Find LINE | `/proc` cmdline | Toolhelp snapshot of `LINE.exe` |
| Read memory | `/proc/<pid>/mem` | `ReadProcessMemory` (read-only handle) |
| Open-chat check | 26.0.2 RTTI anchors | 26.6 anchors; ASLR base from the module list |
| Send text | X11 clicks + xclip + Return | UI Automation via [cua-driver](https://github.com/trycua/cua): value pattern, background Enter |
| Upload | Wine picker by coordinates | `傳送檔案` button, then Win32 `WM_SETTEXT` / `IDOK` on the picker |
| Sync watchdog | `ss`, restart Bottles tree | `netstat`; "restart" only re-shows LINE (never kills it) |
| Screenshot | ImageMagick `import` | Pillow `ImageGrab` |
| Coding jobs | process groups, signals | detached process group, `taskkill /T`, creation-time identity |
| Likes | X11 hover + pixel check | real-cursor hover + capture diff on an unlocked, idle desktop |

Text sends, uploads, quote lookups and operator commands also work while the
Windows session is **locked**: they use UI Automation patterns, key
messages and Win32 dialog messages, which reach a locked desktop.

## Requirements

- Windows LINE Desktop, signed in (enable Auto-Login so a relaunch does
  not stop at the login screen), zh-TW or English UI (`win_line.LABELS`).
- Python 3.11+ (`python.exe`, or `pythonw.exe` for a console-less daemon).
- `cua-driver` on `PATH`. Pillow for the screenshot command.
- A reply backend: `cursor-agent`, the Antigravity CLI (`agy`), or an
  OpenAI-compatible endpoint (see `bot_config.example.json`).

## Run

```powershell
cd automation
python line_cli.py send --room <room-mid> --text "hello" --dry-run
python line_cli.py send-file --room <room-mid> C:\path\report.pdf --file-root C:\path
python line_cli.py ai --group-bot --include-dm --dm-without-mention `
    --commander <owner-mid> --state-file groupbot_state.json `
    --log-file groupbot_daemon.log --pid-file groupbot_daemon.pid
```

`--log-file` / `--pid-file` let `pythonw.exe` run it as a detached daemon.

## Configuration

`automation/bot_config.json` (gitignored) adds these optional keys:

- `backend`: `cursor` (default), `antigravity` or `openai`;
  `fallback_backends`: tried in order when the primary fails.
- `agy_bin`, `openai_url`: override CLI path / endpoint.
- `file_roots`: directories `find_files` / `send_file` may use.
- `room_plugins`: Python files that may claim a room. A plugin defines
  `handles_room(room_mid)`, `is_allowed_sender(user_mid)` and
  `handle_group_command(**kw)` returning `(actions, source, truncated)`;
  its actions still run through the verified executor.

## Likes on Windows

LINE draws its reaction button only while the real mouse cursor hovers a
message; Qt ignores synthesized background mouse input and UIA exposes no
reaction control. The Windows liker therefore runs only on an unlocked
desktop whose user has been idle for `LINE_LIKE_MIN_IDLE` seconds (default
30): it hovers the message row found in the UIA list, finds the smiley and
then the 👍 icon by diffing window captures, clicks them with the real
cursor (restored afterwards) and confirms the reaction in memory. While the
desktop is locked or in use, likes are skipped and logged, never clicked.
