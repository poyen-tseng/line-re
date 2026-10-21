# Architecture

Local automation around the author's own LINE Desktop client (Wine).
It does not ship account ids, chat logs, or infrastructure details.
Real mids and the mention name come from `automation/bot_config.json`
or the environment; see `automation/bot_config.example.json`.

## Components

- `automation/line_common.py` — find the LINE process, scan its decrypted
  in-memory pages, and parse message, contact, and group records.
- `automation/read_messages.py` — print recent messages from that cache.
  The on-disk database is encrypted and is not a working reader.
- `automation/watch_messages.py` — poll the same cache and emit new ids.
- `automation/send_message.py` — open a chat and send text through the
  X11 window. It checks the open chat id before sending.
- `automation/file_upload.py` — stage a file where the Wine client can
  read it, drive the file picker, and look for a matching outgoing row.
- `automation/line_cli.py` — one CLI for send, read, watch, and the
  group bot. The bot answers an @-mention, optionally limited to a
  commander mid.
- `automation/ai_backend.py` — ask the configured fast model for a reply
  or a bounded action list.
- `automation/bot_config.py` — load the owner mid, bot mid, mention token,
  and reply model. Missing file falls back to placeholders.
- `automation/reactions.py` and `reaction_memory.py` — like a message the
  bot already accepted as a mention, then check memory for that like.
- `automation/group_actions.py`, `operator_commands.py`, `vm_control.py`,
  `coding_jobs.py` — schedules, file search, screenshots, and isolated
  coding jobs. Owner-only actions require the configured admin mid.
- `automation/reply_context.py` — attach a quoted original when the
  command is a reply and the original is still in the cache.
- `automation/sync_watchdog.py` — notice when the client has no healthy
  core sync socket and, when enabled, restart the local client.
- `re-file-send/`, `re-reaction/`, `re-reply/` — research notes and
  read-only probes for uploads, reactions, and quote replies.
- `automation/tools/` — optional socket and keepalive experiments.
  They do not send chat messages.

## Data flow

1. LINE Desktop is already running. Scanners read its process memory;
   they do not decrypt the on-disk database and do not call LINE servers.
2. The group bot baselines ids it has already seen, then watches for a
   new group message that @-mentions this account.
3. Explicit owner commands are parsed locally. Other text can be turned
   into a small JSON action list by the model.
4. Sends and uploads go through the desktop UI, under one UI lock, and
   are checked against a new outgoing record in memory.
5. Likes use the same mention rule and the same lock, then look for the
   bot's own reaction on that message id.
6. The watchdog only observes local sockets. A configured restart
   relaunches the desktop client; catch-up then considers recent ids
   that were not handled yet.

## Current status and limitations

- Usable for the author's own desktop session: read the live cache,
  send text, upload some files, like a mention, and run a mention bot.
- Only pages currently in memory are visible. History that the client
  has not decrypted cannot be read here.
- Click positions assume one window layout. A different size or theme
  misses controls.
- An upload that is not confirmed is reported as uncertain and is not
  sent again automatically.
- The model returns suggestions. Owner actions still check the sender
  mid from the message record, not from the model text.
- The watchdog can restart the local client. It does not change host
  network settings.
- Placeholder defaults are not a real account. Set config or environment
  variables before expecting live mentions or owner commands to match.
