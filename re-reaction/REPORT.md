# LINE desktop reactions: verified UI path and read-only RE

2026-10-21, LINE **26.0.2.3828**, Wine/Bottles, `DISPLAY=:3`.
Image base **0x140000000**. All image addresses below are **RVAs**;
heap addresses are examples from LINE PID 2304644 and change on restart.

## Result

The implementation uses LINE's own reaction picker, with its **message ID
and room MID read from memory immediately before clicking Like**. Reverse
engineering supplies identification, geometry, and delivery verification.
No native function injection, memory writes, custom authenticated traffic,
or test text/sticker/file sends were used. LINE stayed running and was not
updated. The two initial live Like clicks were on existing user messages
with real metadata mentions of this account.

A reliable native entry point was not established in the investigation
time box. `ChatMessageView::reactMessage` is private, takes Qt/C++ objects,
and must run on the protected client's GUI thread. Finding a meta-method
name is insufficient to safely invoke it from another process. The UI
fallback keeps LINE responsible for authentication, transport and state.

## Reproduce without touching the UI

From the repository root:

```bash
python3 re-reaction/scripts/scan_reactions.py
python3 re-reaction/scripts/scan_reactions.py \
  --message-id 100000000000000010 \
  --room C_GROUP_B_PLACEHOLDER
```

The probe opens `/proc/<pid>/mem` read-only. It prints build-checked enum
values, picker fields, loaded message IDs/heights and narrowly selected
reaction evidence. It does not dump arbitrary heap strings, credentials,
message bodies, or write a roster file. Heap SQL/log copies are temporary;
their later absence is not evidence that a server-side reaction vanished.

## Enums, resources and native path

`PredefinedReactionType` has six int32 values at **0x394eb60**, followed
by six name pointers at **0x394eb78**:

| Value | Name | Name-string RVA |
|---:|---|---|
| 2 | NICE | 0x327c8f4 |
| 3 | LOVE | 0x327c8fc |
| 4 | FUN | 0x327c904 |
| 5 | AMAZING | 0x327c908 |
| 6 | SAD | 0x327c910 |
| 7 | OMG | 0x327c914 |

The UI's `ReactionIconType` uses **1** for Like; this is a separate enum
from the thrift **NICE=2**. A live click logged `icon type: 1` and produced
`PredefinedType:2` in the message's reaction status.

Useful anchors recovered from the unpacked in-memory image:

| RVA | Evidence |
|---|---|
| 0x26c2de0 / 0x26c2e10 | `image://chat/reaction/ic-reaction-like-40.png` / `ic-reaction-like.png` |
| 0x2732308 | newer `image://chat/reaction/1003_icon.png`; adjacent paid/basic assets |
| 0x2823178–0x2823340 | ChatMessageView availability, selected icon, missing message, invalid predefined type, paid/sticon click and completion logs |
| 0x30ae80e | Qt meta-method `onReactionDockItemClicked`, then `ReactionIconType`, `reactionIconType`, `cancel`; paid/sticon slots follow |
| 0x3167a40 | `line::ChatServiceImpl::requestReactMessage::<lambda_1>::operator ()` |
| 0x3167a90 | ChatService `response react message: success=%d, msgId=%s, type=%s` log |
| 0x398fef0 | `ReactionDockWidget` RTTI |
| 0x3992b30 | private `ChatMessageView::reactMessage(QString const&, thrift::Talk::model::ReactionType, bool)` lambda RTTI |
| 0x39a3250 | `ChatServiceImpl::requestReactMessage` lambda RTTI, two strings, ReactionType, bool, callback |
| 0x39d8da8 / 0x39d8e38 | `line::MessageReaction` / `MessageReactionStatus` RTTI |
| 0x3abc0b8 / 0x3ac0118 | model/wire `ReactRequest` RTTI |
| 0x3ac1ba0 | `thrift::Talk::ReactCommand` RTTI |
| 0x3acfe08 / 0x3acfe48 | `TalkService_react_pargs` / `_presult` RTTI |

Request/type reflection strings include `reqSeq`, `messageId`,
`reactionType`, `predefinedReactionType`, `paidReactionType` and newer
paid/sticon fields. Both model and wire versions are present. Embedded
resource/meta strings expose the picker and reaction properties
(`myReaction`, `reactionList`, `reactionHistory`, availability flags).
No stable exported IPC/QML endpoint was found. Source-level QML names and
compiled resource strings do not by themselves establish a callable API.

The sampled tagged messages have `e2eeMark:2`. The live client sent the
reaction through its existing `/S4` connection to a `legy-*.line-apps.com`
host. The TLS/LEGY request was not decrypted or replayed; no claim is made
about the reaction payload's independent E2EE treatment.

## Message identity and geometry

Primary vtables, validated against this build before use:

| Class | Vtable RVA | Example object |
|---|---|---|
| ReactionDockWidget | 0x2866360 | 0x7ffffb4b2540 |
| ChatMessageView | 0x281de98 | 0x7ffffb3dd960 |
| ChatMessageModel | 0x26f6d90 | 0x7ffffb310ee0 (populated model) |
| ChatMessageData | 0x26f5698 | varies by message |

The picker contains Qt 6 QString triples `(header, UTF16 pointer, length)`:

| Offset from picker | Field |
|---|---|
| +0x458 | selected message ID; pointer +0x460, length +0x468 |
| +0x470 | room MID; pointer +0x478, length +0x480 |

The observed strings use `header == pointer - 16`. Empty/unreadable fields,
ambiguous objects or changed vtables abort. A populated ChatMessageModel
stores an array pointer at **+0x60**, count at **+0x68**, stride **16**
(shared data pointer/control block). Each ChatMessageData has its message
ID QString at **+0x1e0** and current row height int32 at **+0x3c**. Synthetic
date/separator rows are included when summing heights.

QWidget's data pointer is at **+0x20**; its inclusive QRect is at
`data+0x14`. QObject's private pointer is at **+8**, its parent at
`private+0x10`. Walking the validated parent chain gives the picker's
screen rectangle. In this layout it is **272×62**; Like is **(26,28)**
inside it. The view origin is window-relative **(365,108)** and its height
is 576 px. These coordinates are checked, not assumed for another build.

The fallback opens only a verified recent room, scrolls to the bottom,
uses the model's row heights to select the requested visible row, and
detects its light-theme grey bubble. Hover exposes the smile button. After
opening the picker it checks **both IDs**, moves to Like, and checks room,
picker identity and geometry again immediately before the click. An
incoming message or layout change may make selection fail; it cannot
authorize clicking a picker for a different message.

## Delivery evidence

The decrypted `_message` schema has `_reactions` at column **19** and
`_reactionStatus` at **35**. The useful new format is `_reactionStatus`:

```json
{"reactionList":[{"reactor":"U_BOT_PLACEHOLDER","type":1,"PredefinedType":2,"cancel":false,"updatedAt":1792561173946}]}
```

Live updates come in **two** forms: the early update can include
`WHERE _id='…' AND _chatId='…'`, while the canonical later update uses
`WHERE _id='…'` alone. Requiring the former caused the first implemented
live check to report uncertainty even though the reaction landed. The
corrected verifier accepts the latter only when a **complete parsed
message JSON/INSERT/SQLite row** independently binds that ID to the room.
It never combines arbitrary neighboring heap fragments.

It additionally requires either ChatService's exact-ID `success=1` log,
or a timestamped `requestReactMessage done: SUCCESS` following the same
message's `icon type:1, cancel:false` click within ten seconds. The latter
must match the reaction's `updatedAt` within two seconds, on the same day,
with no intervening reaction click. A generic SUCCESS string alone is not
proof for a message.

Live evidence captured in **GroupB**,
`C_GROUP_B_PLACEHOLDER`:

| Message ID | Observation |
|---|---|
| 100000000000000009 | Manual RE check at 13:25:16.794; correct picker ID/room, icon type 1, `cancel:false`; ChatService `success=1` at 13:25:17.286886; memory update carried self reactor and NICE=2, updatedAt 1792560317126. |
| 100000000000000010 | Implemented worker at 13:39:21; one click, no retry after uncertainty. LINE logged exact-ID `success=1` and `requestReactMessage done: SUCCESS`. Memory SQL at 0x7ffff6df6490 / 0x7ffff6e9d190 held self reactor, NICE=2, `cancel:false`, updatedAt 1792561173946; a complete message record bound the ID to the room. The reaction was also visible under that message. The corrected read-only verifier confirmed it at approximately 13:41. |

Relevant application log lines include:

```text
[react] start id=100000000000000010 method=ui-picker attempt=1
[react] fail id=100000000000000010 method=ui-picker attempt=1 status=uncertain dur=19.17s click sent; memory/server confirmation unavailable
```

That initial failure is retained in the log as an audit record; it did not
cause a second click. The native exact-ID success and typed memory record
establish delivery. Later heap/log churn can make a fresh historical scan
unable to reconfirm it; persisted successful/uncertain claims still block
replay.

## Runtime behavior and remaining limits

Grok's subprocess starts before the callback enqueues a reaction. One
background worker performs at most **three pre-click attempts**, durably
claiming each in the same version-2 JSON state as handled command IDs.
Legacy newline state migrates automatically. Successful, exhausted,
interrupted and uncertain claims cannot requeue after restart. A failed
state write disables reactions before a click. Reaction persistence never
overwrites handled IDs from the main loop.

The worker reuses the sender/watchdog UI lock. A ready reply preempts
pre-click work; a safe retry waits for the reply's action sequence to
finish. Heap discovery and post-click verification run **outside** the UI
lock. Dry-run and `--no-react` start no worker.

Supported fallback: this exact build, 742×806 window, light theme, visible
incoming grey text bubbles and a room resolvable by verified search/recent list.
Older/offscreen messages, different layouts/themes or unavailable memory
fail closed with bounded logs. Menus are dismissed without sending or
clearing composer text. A person's concurrent X11 input remains an
unavoidable race between the final memory check and the mouse click.

The group-name regression was a preexisting partial-cache behavior exposed
by restart: discovery and contacts used raw scan names while groups.json
already retained known names. The scanner now enriches only currently
observed rooms from the persistent roster, whose path is anchored to
automation/, before publishing contacts and discovery data.

## Update 3 — real simultaneous tagged messages (14:20)

Two real commands exposed issues not covered by the first live checks:
one new group had short contents and an absent cached `_chat` row, while
another message used the reply path's explicit own-name text fallback.
The previous reaction filter incorrectly excluded that fallback.

Read-only profiling measured a 12.08s verifier, with 10.87s inside
`completion_logs`. The timestamp regex was scanning arbitrary binary
bytes. A literal `[ChatMessageView][Reaction]` search now selects bounded
complete log lines first; evidence scanning measured **0.61s** afterward.
Two-anchor open-chat discovery now uses one pass and is warmed during
startup. Production timing logs expose preparation, UI and verification.

For a bottom-scrolled model, the content end is now
`viewTop + min(viewHeight, sum(rowHeights))`. This preserves top alignment
for short rooms and bottom alignment for scrollable ones. New groups absent
from the cached chat order use `_open_search_result(..., max_rows=1)` with
the known name and an observed room-MID check. All previous picker checks
still run immediately before clicking.

Reaction and reply eligibility share one `mention_path` implementation:
authoritative metadata first, otherwise the configured explicit bot-name
token. Rejected commands never enqueue reactions. The worker schedules
fresh mentions before deferred retries. The whole command/send batch holds
retries, including gaps between two commands; ready replies preempt UI work.
There are at most three persisted attempts with backoff. Every retry checks
for an existing self NICE before any click; uncertainty never authorizes a
second click.

The running bot was not killed for the experiments. One-off checks used
its UI lock, sent no text/stickers/files, and liked only the two exact
messages authorized by Update 3, after checking memory first:

```text
14:15:18.476 [react] ok id=100000000000000012 method=ui-picker attempt=1 status=ok dur=3.92s NICE=2 self=U_BOT_PLACEHOLDER room=C_GROUP_B_PLACEHOLDER evidence=0x7f80822c44a0 server_ack=1 client_success=1
14:16:57.373 [react] ok id=100000000000000011 method=ui-picker dur=7.91s NICE=2 self=U_BOT_PLACEHOLDER room=C_GROUP_C_PLACEHOLDER evidence=0x7f80a1000200 server_ack=1 client_success=1
```

The UI lock was held for **1.290s** for the already open text-mention room
and **4.697s** for the metadata-mention room including verified search.
Both produced self-reactor, NICE=2 memory records and exact-ID server ACKs
plus correlated `requestReactMessage done: SUCCESS`. The metadata case's
earlier missing-room preflight failed before any reaction click.

Validation: **106 tests pass** on DISPLAY=:3, and with DISPLAY unset the
read-only capture test skips cleanly (105 executed, 106 discovered).
All mutating UI tests use mocks; the optional live test captures pixels only.
Remaining limitations are unsupported layouts/themes, offscreen messages,
unresolvable rooms and concurrent human input between final check and click.
