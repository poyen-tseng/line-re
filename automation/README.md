# LINE Desktop send + read automation

Personal research and automation of the author's own LINE account.
Not affiliated with LINE. Use at your own risk. Account ids and the
bot mention name are configured outside git; see the repository
[README](../README.md) and `bot_config.example.json`.

Small, conservative tooling for the user's **own** LINE Desktop (Wine/Bottles)
running on this machine. Three tools, plus a unified CLI:

- `line_cli.py` — single CLI with `send` / `send-file` / `schedule-file` / `read` / `watch` / `ai` subcommands
  wrapping the three scripts below (recommended entry point). `ai` calls
  Cursor CLI model `grok-4.7-low-fast` (`ai_backend.py`). Summarize / ask / draft /
  watch-reply only print. `ai --auto-reply` sends a reply unless `--dry-run`.
  `ai --group-bot` answers when anyone @-mentions the
  bot in a group (an optional `--commander` restricts the sender).
  VM actions and model controls additionally require the owner's sender MID — see [Owner VM commands](#owner-vm-commands).
- `send_message.py` — send a text message to a named chat via X11 UI automation
  (`xdotool` + `xclip`).
- `read_messages.py` — print recent messages from the user's LINE history.
- `watch_messages.py` — real-time new-message watcher (polls memory).

None of these cracks encryption, intercepts traffic, or touches anyone
else's account. `read_messages.py` / `watch_messages.py` only read the user's
own already-decrypted SQLite page cache from the running LINE process.

## Requirements

- Python 3.8+
- X11 tools: `xdotool`, `xwininfo`, `xclip` (all already installed)
- For `read_messages.py` strategy 1 only: `dissect.esedb` and `libesedb-python`
  (installed in `.venv`). Strategy 2 (the one that works) needs only the stdlib.

Create the venv:

```bash
python3 -m venv .venv
.venv/bin/pip install dissect.esedb libesedb-python
```

## LINE must be running

All tools need LINE Desktop running. Start it (if not already):

```bash
bash /workspace/line-bottles/run-line.sh
```

LINE runs under Bottles/Flatpak Wine on **DISPLAY=:3** (confirmed via
`/proc/<pid>/environ`). `line_cli.py` and `send_message.py` auto-detect the
display; `line_cli.py`, `read_messages.py`, and `watch_messages.py`
auto-detect the LINE PID. Shared autodetect + memory-scan code lives in
`line_common.py`.

## line_cli.py (unified CLI)

`line_cli.py` is the recommended entry point — one CLI with six
subcommands that wrap the existing working logic from the three
standalone scripts. `ai` reads messages through the same memory scan
and asks `grok-4.7-low-fast`. Summarize / ask / draft / watch-reply never send.
`ai --auto-reply` does — see [Auto-reply](#auto-reply). The shared LINE-pid autodetect, X-display
autodetect, SQLite leaf-page parser, memory scanner, and formatting
helpers were refactored into `line_common.py`, which all four files
import. The standalone scripts continue to work and expose a few extra
advanced options.

```bash
# Top-level help (lists subcommands):
.venv/bin/python line_cli.py --help

# Each subcommand has its own --help:
.venv/bin/python line_cli.py send --help
.venv/bin/python line_cli.py read --help
.venv/bin/python line_cli.py watch --help
.venv/bin/python line_cli.py ai --help

# Send (dry-run does everything except the final Enter — no send):
.venv/bin/python line_cli.py send --chat "Keep Memo" --text "hi" --dry-run
.venv/bin/python line_cli.py send --chat "Alice" --text "hello" --display :3

# Read recent messages:
.venv/bin/python line_cli.py read --limit 20
.venv/bin/python line_cli.py read --limit 3 --json

# Watch for new messages in real time:
.venv/bin/python line_cli.py watch --interval 2 --backfill 3
.venv/bin/python line_cli.py watch --json
.venv/bin/python line_cli.py watch --state-file ~/.line-seen
.venv/bin/python line_cli.py watch --exec 'notify-send "$LINE_MSG_FROM: $LINE_MSG_TEXT"'

# AI (prints only — never sends). OTP / verification-code text is redacted
# and those messages never get a suggested reply. To send a draft, pass
# it yourself to `send`:
.venv/bin/python line_cli.py ai --summarize --limit 10
.venv/bin/python line_cli.py ai --ask "最後一則訊息在講什麼？" --limit 10
.venv/bin/python line_cli.py ai --draft-reply --chat "Keep Memo" --limit 10
.venv/bin/python line_cli.py ai --watch-reply --interval 3

# Auto-reply (sends unless --dry-run — see the Auto-reply section):
.venv/bin/python line_cli.py ai --auto-reply --chat "Keep Memo" --dry-run --once

# Group command bot (sends unless --dry-run — see Group command bot):
.venv/bin/python line_cli.py ai --group-bot --dry-run --once --backfill-scan
```

Subcommand signatures:
- `send --chat NAME --text TEXT [--display :N] [--dry-run]`
- `send-file --mid MID PATH [--chat NAME] [--max-file-mb N] [--file-timeout SEC] [--dry-run]`
- `schedule-file --mid MID PATH --at ISO_TIME [--schedules-file FILE] [--dry-run]`
- `read [--limit N] [--json]`
- `watch [--interval N] [--backfill N] [--json] [--exec CMD] [--state-file F]`
- `ai (--summarize | --ask QUESTION | --draft-reply | --watch-reply | --auto-reply | --group-bot) [--limit N] [--chat NAME] [--all-chats] [--interval N] [--min-interval N] [--max-replies N] [--once] [--dry-run] [--system TEXT] [--state-file F] [--commander MID] [--self-mid MID] [--mention TOKEN] [--include-dm] [--cooldown SEC] [--group-map FILE] [--backfill-scan]`

Status/progress messages go to **stderr**; message output goes to
**stdout**, so `| jq` and piping to webhooks work cleanly with `--json`.
For the extra advanced options (`--no-search`, `--search-bar-rel`,
`--msg-input-rel` on send; `--chat`, `--mid`, `--pid`, `--try-file` on
read; `--exec-timeout`, `--pid`, `--quiet`, `--once` on watch), use the
standalone scripts below.

## Auto-reply

`line_cli.py ai --auto-reply` closes the loop: a new **incoming** message
shows up in the memory scan → `ai_backend.ask_grok` (`grok-4.7-low-fast`) drafts
a short reply → `send_message.py` sends it into that chat. It reuses
`watch_messages.establish_baseline` / `do_scan` and `send_message.run`. It
does not reimplement the memory scan or the X11 send steps.

**Removing `--dry-run` will actually send.** `--dry-run` still reads
messages and calls grok, then prints the draft and the exact `send` command
it would run. It does not call `send`. Use it until you have watched the
drafts. Start with a chat you own (for example your own "Keep Memo").

```bash
# Safe test: baseline, one poll, print a draft if something new arrived.
# Does not send.
.venv/bin/python line_cli.py ai --auto-reply --chat "Keep Memo" \
  --dry-run --once --interval 2

# Live, this chat only, at most one reply, persist handled ids:
.venv/bin/python line_cli.py ai --auto-reply --chat "Keep Memo" \
  --max-replies 1 --state-file ~/.line-auto-reply \
  --system "你是我的 LINE 自動助理，用繁體中文、口語、簡短回覆"

# Every chat (will not run unless you pass this flag):
.venv/bin/python line_cli.py ai --auto-reply --all-chats --dry-run
```

Safety flags and rules:

| Flag | Default | Behavior |
|---|---|---|
| `--chat NAME` | required | Only this chat (display-name substring). The command **refuses to start** if `--chat` is omitted. |
| `--all-chats` | off | Explicit opt-in to reply in every chat. This is the only way to run without `--chat`. If both are set, `--chat` wins. |
| `--dry-run` | off | Draft + print the `send` command. Do not send. |
| `--interval SEC` | 3 | Poll interval. |
| `--min-interval SEC` | 8 | Minimum seconds between sends (anti-spam). |
| `--max-replies N` | 0 | Stop after N replies. `0` means unlimited. |
| `--once` | off | Baseline, wait one poll interval, scan once, exit. For testing. |
| `--system TEXT` | none | Persona prepended to the grok prompt. |
| `--state-file F` | none | Persist handled message ids (one id per line) across restarts. |
| `--limit N` | 20 | Recent messages used as context, capped at 8 for auto-reply. |

Other rules (always on):

- Only **incoming** messages are answered (`_from` is not our mid). Outgoing messages, including ones this loop just sent, are ignored so it cannot reply to itself.
- `--self-mid` identifies this account (default is the configured account mid). Without it, only an unambiguous 1:1 participant inference is used; group frequency cannot identify the account.
- On startup every message id already in memory is marked handled (**baseline**). Old history is not answered. `--state-file` ids are handled too.
- OTP / verification-code / 2FA messages (the same detector as the other `ai` modes) are never sent to grok and never answered.
- LINE official / system senders (`LINE`, `LINE Official`, `LINE Official Account`, `LINE Notify`, `LINE Pay`, `LINE TODAY`, `LINE GAME`, `LINE Points`) are skipped.
- Grok is asked for a short reply only. The output is trimmed to a single message (quotes and code fences stripped, newlines collapsed, length capped at 300 characters). Output that looks like a verification code is dropped.
- Ctrl+C / SIGTERM stops the loop and flushes `--state-file`. If the LINE pid changes, the loop re-detects the window and re-baselines instead of treating history as new.

## Group command bot

`line_cli.py ai --group-bot` watches **every** chat via the same memory scan
as `watch` (`watch_messages.do_scan` / `establish_baseline`). It drafts a
short reply with `ai_backend.ask_grok` (`grok-4.7-low-fast`) and sends it through
`send_message.run` into the group the command came from. It does not
reimplement the scan or the X11 send steps.

It acts only when all of these are true:

1. Anyone may command the bot by @-mentioning it. If `--commander MID` is set, only that sender is accepted. The default is empty (`any`).
2. The message is in a group (`_to` starts with `c`). 1:1 chats are ignored unless `--include-dm`.
3. The bot account (`--self-mid`, from config, `LINE_BOT_MID`, or a placeholder) is @-mentioned.

**Removing `--dry-run` will actually send.** `--dry-run` still reads messages
and calls grok, then prints the matched command, the resolved group name,
the draft, and the exact `send` command it would run. It does not call
`send`.

```bash
# Safe test against messages already in memory. Does not send.
.venv/bin/python line_cli.py ai --group-bot --dry-run --once --interval 2 \
  --backfill-scan

# Live: baseline history, then answer new @-mentions only.
.venv/bin/python line_cli.py ai --group-bot --max-replies 1 \
  --state-file ~/.line-group-bot
```

### @-mention

The mention list is not a separate table. It is a column on `_message`:
`_contentMetadata` (the scan stores it as `contentMeta`). The value is JSON.
The key `MENTION` is itself a JSON string:

```json
{"MENTIONEES": [{"M": "<mentioned mid>", "S": "<start>", "E": "<end>"}]}
```

`M` is the mentioned account mid. `S` and `E` are offsets into `_text`
covering the `@顯示名稱` token. If that object parses, it is authoritative:
the bot runs only when `M` is `--self-mid`. If the column has no usable
`MENTION` object, the bot falls back to a display-name token, `--mention`
(default `@mybot`). The token is stripped from the text; what remains
is the command, plus the last few messages of that group, sent to grok.

`contentType` 18 rows near a mention are system events (`LOC_KEY` such as
`A_MC`, `A_MR`, `C_PN` in `_contentMetadata`). They are not the mention
list and are never answered. The `_chunks` column is opaque payload, not
the mention list.

### Group name

Routing uses the room mid. Every scan merges `_groupChat`, group `_chat`
rows, and plaintext inserts into ignored `groups.json`. A known display
name helps search the UI; `--group-map FILE` may override that name.
When no name is known, the sender opens the chat list by its in-memory
order and verifies the selected mid. A missing or mismatched mid aborts.
Text sends check the mid before paste and again before Enter; attachments
check before selecting a file. The clipboard and UI are locked across
our sender processes, but a person can still change the UI during an action.

### Quoted replies (LINE reply feature)

When a mentioned command quotes an older message, the bot adds a separate
section to the Grok prompt, independent of the eight recent messages:

```text
使用者回覆的原訊息: <sender> <YYYY-MM-DD HH:MM:SS> Asia/Taipei: <original text>
```

The recovered fields are `_relatedMessageId`, `_messageRelationType=3`,
and `_relatedMessageServiceCode=1`. Heap JSON uses their camelCase names.
The observed metadata fallback uses `message_relation_server_message_id`,
`message_relation_type_code="reply"`, and
`message_relation_service_code="talk"`. Other relations are not quotes.
Conflicting column/metadata values do not supply quote context.

Lookup uses the complete current scan, then a process-local cache populated
by normal scans. That cache holds at most 2,048 records, has an 8 MiB
accounting budget (UTF-8 text plus per-record allowance), and expires records
after one hour without observing them; changing LINE PID clears it.
A missing original on an accepted bot command gets one best-effort read-only
fallback: up to 0.75 seconds / 64 MiB, in 256 KiB chunks (the last chunk's
parsing can overrun the deadline). Misses are cached for 30 seconds.
The fallback does not run another full scan or enqueue recovered history
as new commands. Room, exact ID, sender and timestamp must match.

If unavailable, the prompt says `原訊息無法取得 (id=...)`. Unsupported
services also use this fallback text. Media without text has a type label;
the bot does not download or interpret quoted attachment contents. Quoted
OTP text is redacted before Grok, and original text is capped at 8,000
characters in this section. Original messages are context, not new commands.

`read` and `watch` text output show `↪ reply to <id> <sender> <time>: <text>`
or the unavailable line. Their JSON adds `reply_to_id`,
`related_message_id`, `message_relation_type`,
`related_message_service_code`, and a `reply` object with `found`, `source`,
original sender/name/time/text, or an `unavailable` explanation. Non-replies
have `reply: null`. Readers use their existing scan/cache without extra
per-message memory sweeps. Bot diagnostics include
`[reply] msg=... reply_to=... found=yes/no source=...`.

Read-only validation recovered a real quoted original **5h 8m 32s older**
than its reply, beyond 12 later same-room messages. Availability depends on
LINE's decrypted cache; arbitrary old history in encrypted `.edb` files
cannot be read by this implementation. Nothing fetches history from LINE's
servers or opens/scrolls chats for quote lookup. Evidence and a body-redacting
probe are in [re-reply/REPORT.md](../re-reply/REPORT.md).

### Flags

| Flag | Default | Behavior |
|---|---|---|
| `--commander MID` | empty (`any`) | Anyone who @-mentions is obeyed; set a mid to restrict the sender. |
| `--self-mid MID` | config, `LINE_BOT_MID`, or a placeholder | This account. Used for mention metadata and to ignore our own messages. |
| `--mention TOKEN` | config, `LINE_BOT_MENTION`, or `@mybot` | Text fallback when mention metadata is absent. |
| `--include-dm` | off | Also accept an @-mention in a 1:1 chat. |
| `--cooldown SEC` | 10 | Per-group gap. A command inside the window is skipped. |
| `--min-interval SEC` | 8 | Minimum seconds between sends. |
| `--max-replies N` | 0 | Stop after N replies. `0` means unlimited. |
| `--once` | off | One scan window, then exit. |
| `--backfill-scan` | off | Consider cached messages. Without it, baseline history, with recent unhandled messages eligible for catch-up. |
| `--catchup-minutes N` | 15 | At startup/LINE restart, evaluate recent ids absent from the handled state. `0` disables catch-up. |
| `--dry-run` | off | Print the draft and the `send` command. Do not send. |
| `--group-map FILE` | none | `{room_id: display name}` for rooms the scan cannot name. |
| `--state-file F` | `groupbot_state.json` when reactions are enabled | Persist handled ids and reaction claims across restarts; legacy files migrate to version-2 JSON. |
| `--interval SEC` | 3 | Poll interval after the baseline (or after a backfill scan, if not `--once`). |
| `--limit N` | 20 | Group context given to grok, capped at 8. |
| `--system TEXT` | none | Persona prepended to the grok prompt. |

Other rules (always on):

- Messages from `--self-mid` are ignored, so a reply cannot loop.
- `--commander` equal to `--self-mid` is refused at startup.
- OTP / verification-code messages are never given to grok and never answered.
- LINE official / system senders are skipped. `contentType` 18 system events are skipped.
- On startup, cached history is marked handled, except recent unhandled ids within `--catchup-minutes` (default 15). `--backfill-scan` explicitly considers all cached messages. Persisted ids are not reprocessed.
- Explicit VM commands are parsed before Grok. Other requests use a fixed JSON action list (at most `--max-actions`, default 5): chat/file/schedule actions and the owner actions below. Text is limited to 300 characters; malformed action JSON and verification-code text are dropped. Plain prose falls back to one cleaned text action.
- Ctrl+C / SIGTERM stops the loop and flushes `--state-file`. If the LINE pid changes, the loop re-baselines instead of treating history as new.

## Owner VM commands

Mention the bot in a group, followed by one of these commands. Explicit forms
work without a Grok request; other natural wording uses the action layer.

| Command after `@mybot` | Result |
|---|---|
| `請截圖桌面` | Capture DISPLAY=:3 as PNG and upload to this group, followed by confirmation/error. |
| `截圖 :1` | Capture another live local X display; unavailable displays return a bounded discovery list. |
| `建立一份 Python 程式類的工作：<描述>` | Start a detached Codex job and immediately acknowledge its ID. |
| `建立工作：[cursor grok-4.6-high] <描述>` | Use Cursor; resolves the model to `cursor-grok-4.6-high-fast`. |
| `建立工作：[codex MODEL] <描述>` | Override the Codex model for one job. |
| `工作狀態` / `工作狀態 ID` | Recent/running jobs in this group, or one job's status/model/duration. |
| `取消工作 ID` | Cancel this group's job and terminate the agent's process group. |
| `切換模型 grok-4.6-high` | Validate against the real Cursor catalog, select its fast alias, and persist it. |
| `目前模型` | Report the persisted reply model (default `grok-4.7-low-fast`). |

Every operator action, including status and current-model queries, checks the
actual memory record's sender MID in the executor. The owner mid is not stored
in this repository. Put it in ignored `bot_config.json` (`admins`) or in
`LINE_OWNER_MID`. The bot's own mid is `self_mid` or `LINE_BOT_MID`. The
mention token is `mention` or `LINE_BOT_MENTION` (example: `@mybot`). When
the file and those variables are absent, the process uses non-routable
placeholder mids. Display names, text claims, quoted messages, action JSON
and Grok cannot supply the identity. Other senders receive
`抱歉，這個指令只有管理員可以使用。` Normal chat, likes, file uploads and
schedules retain their existing mention rules.

Repeat `ai --admin MID` to replace the admin list. `--bot-config PATH`
selects another local config. Copy `bot_config.example.json` and replace
the placeholders with real user mids before relying on the file; a copied
example is rejected until those placeholders are real mids. Example:

```json
{
  "admins": ["U_OWNER_PLACEHOLDER"],
  "self_mid": "U_BOT_PLACEHOLDER",
  "mention": "@mybot",
  "model": "grok-4.7-low-fast"
}
```

Model switches preserve other config keys and use atomic writes with a file
lock. `ai_backend` reads the selected model for every reply. Catalog discovery
uses `cursor-agent --list-models` with a 12-second timeout and a five-minute
in-process TTL. Only real `-fast` IDs are selected; unknown names receive
close suggestions. The observed Grok catalog on 2026-10-21 contains
`grok-4.7-{low,medium,high,xhigh}[-fast]`,
`cursor-grok-4.6-{low,medium,high,xhigh}[-fast]`, and
`cursor-grok-4.5-{low,medium,high}[-fast]`.
`grok-4.6-high` is an alias, not a literal catalog entry.

Jobs default to `/workspace/line-bot-jobs/<ID>/`, initialized with Git and
an isolation instruction in `AGENTS.md`. The root must be outside this repo.
`--jobs-root DIR`, `--job-timeout SEC` (default 1800, maximum 86400), and
`--max-jobs N` (default 2) configure local limits. The detached supervisor and
agent have separate process groups, closed stdin and job-local `agent.log` /
`worker.log`. A supervisor enforces timeout even while the bot is stopped;
the bot also checks supervisor liveness and cleans up an orphan agent.
Cancellation/timeout terminates the agent group, including ordinary child
processes. A child that deliberately creates another session can escape the
group; this is not an OS sandbox.

Metadata is stored in `<root>/<ID>.json` (room, requester, backend/model,
prompt, PID/PGID, status/times/exit code/notification). `<root>/.control/<ID>/`
holds supervisor input, process identity, cancellation and exit records, and
Codex's final message. These persist across bot restarts. Once complete, the
scan loop posts status/duration/exit code, safe agent tail/final text, and a
bounded list of created files to the original room. It attaches up to three
small source/text outputs (each at most 1 MiB) through the verified uploader;
logs, hidden/secret paths and symlinks are excluded. Scanning continues while
uploads await verification.

The completion notification is durably claimed before sending and normally
sent once. An interrupted claim or unconfirmed delivery stays `unconfirmed`
and is never automatically replayed: LINE has no transactional/idempotent
send API, so a crash between claim and delivery can lose a notification.
Use `工作狀態 ID` and inspect the job directory in that case. Runtime state,
logs and screenshots are retained locally; prune them manually when no longer
needed. Do not delete records of running jobs.

Codex uses `codex exec --dangerously-bypass-approvals-and-sandbox -m MODEL`.
The requested default remains **`gpt-6-luna-max`**, configurable with
`--codex-model` or the per-job `[codex MODEL]` form. A tiny live probe on
2026-10-21 was rejected: this model is not supported with the connected
ChatGPT account. Default Codex jobs will therefore report failure until an
account-supported model is selected. Cursor jobs always use `-fast` models.

Agents run with the owner's VM privileges. Working-directory isolation and
instructions are not an access-control sandbox. They are told to keep project
changes in the job directory and never read/modify `/workspace/line-re` unless
the original owner command explicitly permits it, for example
`允許修改 /workspace/line-re` or `edit /workspace/line-re`. Grok cannot grant
that exception. Prompts containing recognizable inline credentials/OTP are
rejected; common credential patterns are filtered from completion text and
text attachments. This is a heuristic, not general secret detection. Do not
put secrets in job requests. Agent environment inheritance is restricted to
basic user/locale/PATH variables; CLI authentication still uses home files.

Screenshot capture uses installed ImageMagick `import` and `xdpyinfo`, with
bounded display probes, a five-second UI-lock wait and an eight-second
capture timeout. PNGs live in `<jobs-root>/screenshots/`. Capture releases the
shared UI lock before the existing Wine-staged uploader acquires it for file
selection. Upload confirmation has its existing bounded timeout and never
automatically resends an uncertain selection. Desktop images include whatever
is visible on that display.

Offline validation (UI/network actions mocked; fake jobs are real detached
local Python processes):

```bash
cd /workspace/line-re/automation
DISPLAY=:3 .venv/bin/python -m pytest -q tests
```

## Upload files from the VM

The same verified uploader serves CLI, mentioned group requests and scheduled
files. It uses LINE's own media/E2EE pipeline and checks the resulting self
message's room, new numeric message ID, content type, object/service metadata
and file size. FILE rows also require the selected filename. Dialog closure
alone does not mean delivery succeeded.

From `automation/`:

```bash
# Preview routing and validation without opening the picker or sending:
DISPLAY=:3 .venv/bin/python line_cli.py send-file \
  --mid C_GROUP_B_PLACEHOLDER \
  /workspace/line-share/test_clip.mp4 --dry-run

# Upload; --chat is optional and only helps locate the verified MID:
DISPLAY=:3 .venv/bin/python line_cli.py send-file \
  --mid C_GROUP_B_PLACEHOLDER --chat GroupB \
  '/workspace/line-share/影片 2026.mp4' --max-file-mb 200 --file-timeout 30

# Persist a future file send (choose a future time within 30 days):
.venv/bin/python line_cli.py schedule-file \
  --mid C_GROUP_B_PLACEHOLDER --chat GroupB \
  /workspace/line-share/report.pdf --at '2026-10-22T12:00:00+08:00' \
  --schedules-file schedules.json
```

The running `ai --group-bot` dispatches due rows from `schedules.json`.
Legacy text rows remain valid. File rows use `"type":"send_file"`, `"path"`,
`"room"`, `"group"`, `"at"`, `"id"` and `"status":"pending"`. They are
claimed as `sending` before any UI effect, then marked `sent` with a
`message_id`, `failed`, or `unconfirmed`. An interrupted `sending` or
`unconfirmed` row is never automatically replayed. File existence, allowed
roots and size are checked again when it fires; schedule the final path and
keep that file available.

Mention examples: `@mybot 傳 /workspace/line-share/test_clip.mp4 給我`,
`@mybot 把 report.pdf 傳上來`, or a request to send that file at a future
time. Grok selects `send_file` or `schedule_file`; actions always use the
requesting room. Successful immediate sends receive `已傳送 filename (size)`;
missing, forbidden or oversized files receive an error. Possible uploads
without confirmation receive an uncertainty reply and are not retried.

Both CLI and bot default to roots `/workspace` and `~`, with a
**200 MiB** limit (`--max-file-mb`). Repeat `--file-root DIR` to override roots;
the scheduling bot must allow the same root. Hidden/secret paths and symlink
files remain denied. Bare filenames require one unique match; searches stop
after five seconds and ask for an absolute path if incomplete. Spaces and
Chinese filenames work. Windows-invalid/reserved/long names retain their
extension under an `upload` basename, reported in the confirmation.

Bottles/Flatpak cannot see the host `/workspace` through Z:. The uploader
copies a stable snapshot into `<WINEPREFIX>/drive_c/line-uploads/` and selects
its C: path with backslashes. It does not change Flatpak permissions or LINE
binaries. Confirmed/probe staging copies are deleted; uncertain copies remain
for pending client reads. Remove retained copies only after checking that
LINE no longer needs the pending upload.

Selection and cleanup share the existing text/reaction/watchdog UI lock.
Verification releases that lock. The bot continues memory scans during an
upload and buffers new messages for subsequent serial handling. Routing has
a 15-second budget; picker appearance/closure have five/six-second waits;
verification defaults to 30 seconds (`--file-timeout`, maximum 60), plus an
in-progress scan. CLI emits a JSON result and `[file]` logs: exit **0** is
confirmed/probe, **4** is a definite pre-selection failure, **6** is uncertain
and must not trigger an automatic resend.

Verified on LINE 26.0.2.3828 / DISPLAY=:3 / 742×806 layout: Chinese TXT,
original MP4 as FILE=14, PNG as IMAGE=1, and scheduled ZIP as FILE=14.
Large uploads and client/server size limits were not live-tested. Unknown
send/original/limit modals are cancelled and reported as uncertain. A media
conversion changing the size, unavailable heap evidence, or an unsupported
layout can also leave delivery uncertain. Files are not independently
downloaded and hash-checked. Clipboard `text/uri-list` was investigated but
has no verified delivery and is not offered as a supported method.
See [the upload RE report](../re-file-send/REPORT.md#2026-10-21-follow-up-verified-uploads-flatpak-path-failure-clipboard-probe).

## send_message.py

```bash
# Dry-run (does everything except the final Enter — no message is sent):
./send_message.py --chat "Keep Memo" --text "automation test" --dry-run

# Actually send (use a chat you own, e.g. your own "Keep Memo" memo chat):
./send_message.py --chat "Keep Memo" --text "hello from automation"

# Explicit display:
./send_message.py --chat "Alice" --text "hello" --display :3

# If the target chat is already open, skip search:
./send_message.py --chat "Keep Memo" --text "x" --no-search
```

How it works:

1. `xdotool search --class line.exe` on DISPLAY=:3, pick the 742×806 window
   named "LINE" (the AllInOneWindow).
2. `xdotool windowactivate --sync` to focus it.
3. Click the chat-search box at window-relative `(180, 80)`, clear it, paste
   the chat name via `xclip` + Ctrl+V, and click a result row. Check the open
   mid in memory; try the recent-chat list if needed. Search never presses Enter.
4. Click the message input at window-relative `(480, 765)`, clear it, paste
   the message via xclip + Ctrl+V.
5. Press Enter to send (skipped with `--dry-run`).

UTF-8 (Traditional Chinese) goes through the X CLIPBOARD selection, not
`xdotool type` (XIM is unreliable under Wine). Coordinates are
window-relative, so they survive window movement. Override them with
`--search-bar-rel x,y` and `--msg-input-rel x,y` if LINE's layout changes.

Limitations:

- LINE's UI is Qt6 Quick/QML rendered via DXVK; X11 only sees the top-level
  Qt window, so we drive it by clicking window-relative coordinates derived
  from the standard 742×806 layout. A different window size or theme will
  break the coordinates — use the override flags.
- The LINE window must be visible (not minimized).
- UI automation takes over the mouse/keyboard briefly.
- `xclip` keeps a child process serving the selection. The sender reaps its
  short-lived parent with a timeout and aborts if clipboard setup fails.

## read_messages.py

```bash
# Print 20 most recent messages:
./read_messages.py

# Print 50 most recent:
./read_messages.py --limit 50

# Filter by chat display name (substring):
./read_messages.py --chat "LINE"
./read_messages.py --chat "Alice"

# Filter by chat mid:
./read_messages.py --mid U_BOT_PLACEHOLDER

# Also show that the on-disk .edb parse fails (strategy 1):
./read_messages.py --try-file

# Explicit PID:
./read_messages.py --pid 1373692
```

Two strategies are attempted:

**Strategy 1 (instructed, FAILS — documented blocker).** Copies the live
`qw<mid>.edb` (+ `-wal` + `-shm`) to a temp file and tries
`dissect.esedb`, `libesedb` (pyesedb), `sqlite3` raw, and `sqlite3` with the
SQLite magic restored. All fail. The file is actually a **SQLite** database
(not ESE/JET Blue as the earlier REPORT.md guessed) with **full-content
encryption** applied by LINE: the `SQLite format 3\0` magic is replaced with
a custom 8-byte marker (`1a c6 04 c9 14 d5 2c 3a` + 8 bytes that vary), and
every page including the header page is encrypted. The encryption key lives
only inside the Themida-protected `LINE.exe` process. Run with `--try-file`
to see the exact error strings.

**Strategy 2 (WORKS).** While LINE is running, SQLite holds the **decrypted**
page cache in its process heap. We scan `/proc/<pid>/mem` for 4096-byte
SQLite leaf-table b-tree pages (page type `0x0d`) whose cells parse as
`_message` records (both `_from` and `_to` are LINE mids, `_createdTime` is
a sane millisecond epoch), extract the rows with a minimal SQLite record
parser, dedupe by message `_id`, sort by `_createdTime` descending, and
print the N most recent. We also pick up `_contact` pages to map mids to
display names. Read-only; never writes to or signals LINE.

Schema (recovered from LINE.exe process memory):

```sql
CREATE TABLE _message(_from TEXT, _to TEXT, _toType INTEGER, _id TEXT,
  _createdTime INTEGER, _deliveredTime INTEGER, _text TEXT, _location TEXT,
  _hasContent INTEGER, _contentType INTEGER, _contentMeta TEXT, ...);
CREATE TABLE _contact(_mid TEXT, _createdTime INTEGER, _type INTEGER,
  _status INTEGER, _displayName TEXT, _displayNameOverridden TEXT, ...);
```

Limitations:

- Only works while LINE is running. If LINE just started, few pages will be
  cached — open a chat or two first to warm the cache.
- Only pages currently in SQLite's page cache are visible; older history may
  not be present. To read more, scroll the chat in LINE to load more pages.
- Incomplete and overflow SQLite records are rejected (overflow pages are
  not followed). Complete heap JSON/SQL may still recover the message.
- Must run as the same user that owns the LINE process (to read
  `/proc/<pid>/mem`).
- A few false-positive "contact" rows may appear (e.g. a `_contentMeta` JSON
  blob misparsed as a contact name). The message rows themselves are
  reliably filtered by the mid + timestamp heuristic.

## Files

- `line_cli.py` — unified CLI with `send` / `read` / `watch` / `ai` subcommands
  (recommended entry point; wraps the three scripts below)
- `ai_backend.py` — `ask_grok()` wrapper around `cursor-agent` model
  `grok-4.7-low-fast`
- `line_common.py` — shared module: LINE-pid autodetect, X-display
  autodetect, SQLite leaf-page parser, memory scanner, formatting helpers
- `file_upload.py` — Wine-visible staging, attachment verification and bounded uploads
- `send_message.py` — X11 UI automation sender (standalone; extra options)
- `read_messages.py` — on-disk parse (fails) + in-memory page-cache
  reader (works); standalone; extra options including `--chat`/`--mid`/`--try-file`
- `watch_messages.py` — real-time new-message watcher (polls memory,
  emits only new); standalone; extra options including `--once`/`--quiet`
- `../ARCHITECTURE.md` — components, data flow, and current limitations
- `.venv/` — Python venv with `dissect.esedb` + `libesedb-python`

## watch_messages.py

Real-time new-message watcher. Reuses the working memory-scan approach from
`read_messages.py` (scans `/proc/<LINE_pid>/mem` for decrypted SQLite
`_message` leaf pages; mids resolved via `_contact`). Polls on an interval,
dedupes by message `_id`, and emits only genuinely new messages. The on-disk
`.edb` is never touched (it's encrypted; key is inside Themida LINE.exe).

```bash
# Baseline silently, then watch (default interval 3s):
./watch_messages.py

# Show 3 most-recent baseline messages first, then watch every 2s:
./watch_messages.py --interval 2 --backfill 3

# One JSON object per new message per line (for piping to jq / webhooks):
./watch_messages.py --json

# Persist seen ids across restarts so old messages are never re-emitted:
./watch_messages.py --state-file ~/.line-seen

# Run a shell command per new message (alerting / automation):
./watch_messages.py --exec 'notify-send "$LINE_MSG_FROM: $LINE_MSG_TEXT"'
./watch_messages.py --exec 'curl -s -X POST -d "$LINE_MSG_TEXT" http://localhost:9000/hook'

# Single scan and exit (debugging):
./watch_messages.py --once

# Explicit LINE PID (auto-detected if omitted):
./watch_messages.py --pid 1373692
```

For each new message the default (text) output is:

```
[2026-10-20 12:56:28] 0 → 100000000000000004 (text)
    hello
```

`--json` output is one object per line:

```
{"id": "...", "time": "2026-10-20 12:56:28", "createdTime": 1792472188644,
 "from": "u...", "from_name": "0", "to": "u...", "to_name": "...",
 "type": "text", "contentType": 0, "text": "hello"}
```

`--exec` env vars (one invocation per new message):

| Env var | Value |
|---|---|
| `LINE_MSG_ID` | message `_id` |
| `LINE_MSG_TEXT` | message text (raw, may be empty for non-text types) |
| `LINE_MSG_FROM` | sender display-name if resolvable, else mid |
| `LINE_MSG_TO` | target display-name if resolvable, else mid |
| `LINE_MSG_TIME` | UTC+8 timestamp string `YYYY-MM-DD HH:MM:SS` |
| `LINE_MSG_TYPE` | content type name (`text`, `sticker`, `image`, ...) |

The `--exec` command runs via `/bin/sh -c` as the same user, with a
`--exec-timeout` (default 30s) kill timeout. Failed/timed-out execs are
logged to stderr but do not stop the watcher.

Behavior notes:

- On startup, all currently-cached message ids are added to the seen set
  **without** being printed (silent baseline), so only genuinely new
  messages are shown. Use `--backfill N` to also print the N most-recent
  baseline messages first (oldest first, so the stream reads chronologically).
- If LINE restarts (pid changes), the watcher re-detects and re-baselines
  silently — it does **not** dump the entire existing history as "new".
- If LINE is gone, the watcher waits and retries until LINE comes back.
- Status/progress messages go to **stderr**; only new-message output goes
  to **stdout**, so `| jq` and piping to webhooks work cleanly with `--json`.
- Graceful Ctrl+C / SIGTERM — flushes `--state-file` on exit.

Limitations (same as `read_messages.py`):

- Only pages currently in SQLite's page cache are visible. If LINE just
  started, few pages will be cached — open a chat or two first to warm
  the cache.
- Incomplete and overflow SQLite records are rejected; complete heap
  JSON/SQL can still recover the message. Overflow pages are not followed.
- Must run as the same user that owns the LINE process (to read
  `/proc/<pid>/mem`).
- `--state-file` is plain text, one id per line; it can grow unbounded
  over time — prune it manually if needed.

## Safety

These scripts automate the user's **own** LINE Desktop. They do not crack
encryption, intercept others' traffic, or send on anyone's behalf without the
user's command. `send_message.py` sends unless `--dry-run` is set. `ai --auto-reply` will send unless
you pass `--dry-run`, and it refuses to run without `--chat` unless
`--all-chats` is explicit. `ai --group-bot` will send unless you pass
`--dry-run`, and accepts anyone who @-mentions the bot unless `--commander` restricts the sender. `read_messages.py` is read-only.


## Sync watchdog and offline verification

The live group bot checks PID-owned TCP :443 sockets to `147.92.0.0/16`
and `43.223.0.0/16`. After 90 seconds without a core ESTABLISHED socket,
it restarts LINE, at most once per 300 seconds. Other CDN/AWS connections
are not proof of sync health. Failed inspection or a PID change resets
the stall timer. `--no-sync-watchdog` disables it; dry-run and once modes
also disable it.

`--watchdog-socket-nudge` is **experimental and off by default**. It tries
`sudo -n ss -K` on exact PID-owned core CLOSE-WAIT tuples once after 30
seconds, waits at least 30 seconds, then retains the restart fallback.
The parent's earlier live socket kill did not reconnect; no lightweight
repair has been validated. See `../ARCHITECTURE.md` for the current limitations.

Outside the Codex PID sandbox, observe without sending messages:

```bash
cd /workspace/line-re/automation
./tools/socket_experiment.py --duration 900 --interval 2 > socket-observe.log
# Optional socket-destruction trial (no process restarts by this tool):
./tools/socket_experiment.py --nudge-closewait --nudge-after 0 --duration 360 > socket-nudge.log
# Optional keepalive launch trial: restarts LINE, keeps the bot running.
./tools/keepalive_experiment.py --restart-line-with-shim
./tools/socket_experiment.py --duration 900 --interval 2 > socket-keepalive.log
```

The observer records `ss -tnoie`, filtered `/proc/net/tcp`, and memory counts
and timestamps, without message bodies. The keepalive trial sets core sockets
to 10/5/3 seconds through an LD_PRELOAD shim injected explicitly into Flatpak.
Verify the shim log, process maps and a core socket keepalive timer before
calling it an injected trial. A normal LINE restart removes the experiment.
A recovered TCP socket alone does not prove messages are syncing; observe
new incoming memory timestamps without sending a test message.

Offline checks (sending, X11 and watchdog mutations are mocked; the C harness
stubs connect and never connects to a remote address):

```bash
cd /workspace/line-re
./automation/.venv/bin/python -m unittest discover -s automation/tests -v
```

Handled ids are flushed before live command actions. Schedules atomically
claim `pending` → `sending` before using the UI. A crash in `sending` or an
unconfirmed send requires manual inspection; automatic replay could duplicate
a delivery. Only return codes 2/3/4 prove a pre-send failure and may retry.
Unreadable handled state stops startup; corrupt schedules are preserved and
logged. Schedule transactions use a file lock; run one bot instance per
handled-state file. `schedules.json`, lock files, compiled shims and logs
are runtime data and must not be committed.

## Automatic Like reactions

`ai --group-bot` acknowledges accepted **tagged group commands** with
LINE's Like/NICE reaction. Replies and reactions share the same mention
filter: metadata mentioning `--self-mid`, or an explicit `--mention` token
when usable mention metadata is absent. Other mentions, rejected commands,
DMs, system events and the bot's own messages never trigger reactions.
Reply cooldowns do not suppress acknowledgement of a real metadata mention.

```bash
# Normal live command: automatic likes are enabled.
DISPLAY=:3 .venv/bin/python -u line_cli.py ai --group-bot --interval 2 \
  --state-file groupbot_state.json

# Disable likes while retaining existing reply behavior:
DISPLAY=:3 .venv/bin/python line_cli.py ai --group-bot --no-react
# Equivalent environment setting: LINE_BOT_NO_REACT=1
```

Grok starts before reaction work is queued. The worker shares the sender's
UI lock, checks the picker message ID and room MID immediately before Like,
and releases the lock before verifying memory and LINE's success logs. A
ready reply preempts pre-click work; a safe retry waits until reply actions
finish. `--dry-run` creates no reaction worker and makes no reaction clicks.

Reaction claims and handled IDs share one atomic version-2 JSON state file.
There are at most three attempts before a click, with bounded backoff.
Fresh mentions run before deferred retries; retries wait until the entire
command/send batch is idle and recheck memory for an existing self Like.
Successful, exhausted,
uncertain or interrupted claims cannot replay after restart. An uncertain
click is never retried because another click could remove the landed Like.
Use one bot per state file and do not delete claims to force a retry.

The fallback supports LINE 26.0.2.3828's 742×806 light-theme text layout.
Short chats use their actual content height. Known room names use the same
verified search path as sending; nameless rooms use the recent-chat list.
Offscreen messages, unavailable rooms, unsupported layouts or mismatched picker IDs produce bounded
`[react] start/ok/fail id=… method=… dur=…` logs and no unverified click.
See [the RE report](../re-reaction/REPORT.md) and its read-only scan script
for enum tables, object offsets, live evidence and remaining limits.

Production restarts must be detached. First wait for an active command's
`actions done` line, stop the single old bot, then run:

```bash
cd /workspace/line-re/automation
DISPLAY=:3 setsid nohup .venv/bin/python -u line_cli.py ai --group-bot \
  --interval 2 --state-file groupbot_state.json >> groupbot_live.log 2>&1 < /dev/null &
```

Verify exactly one bot and fresh scan lines at least 30 seconds later.
