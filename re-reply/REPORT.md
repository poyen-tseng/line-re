# LINE quote-reply context: read-only evidence

2026-10-21, LINE 26.0.2.3828 under Wine/Bottles, DISPLAY=:3,
LINE PID 2304644. No UI actions, test sends/reactions, process injection,
database writes, LINE restart or update were used in this investigation.
All message bodies and participant identities below are omitted.

## Recovered schema and fields

Four `CREATE TABLE _message` hits were found in readable process memory.
The complete schema has 36 columns. The relevant zero-based positions are:

| Index | SQL column | Type | Observed quote value |
|---|---|---|---|
| 3 | `_id` | TEXT | 18 decimal digits |
| 4 | `_createdTime` | INTEGER | Unix epoch milliseconds |
| 6 | `_text` | TEXT | Reply body, not the original |
| 10 | `_contentMetadata` | TEXT | JSON object |
| 14 | `_relatedMessageId` | TEXT | Original's 18-digit server message ID |
| 15 | `_messageRelationType` | INTEGER | **3**, quote reply |
| 17 | `_relatedMessageServiceCode` | INTEGER | **1**, talk service |

The serializer field names `relatedMessageId`, `messageRelationType`, and
`relatedMessageServiceCode` occur in live heap JSON. At the initial probe,
185 / 199 / 167 literal occurrences respectively were present. Complete
non-reply JSON objects had relation/service 0; the two real quotes were
recovered from SQLite leaf rows, not complete heap JSON or SQL inserts.
INSERT column lists independently contain all three underscore-prefixed
fields, in alphabetical order rather than SQLite schema order.

Both real quote rows have matching `_contentMetadata` keys:

```json
{
  "message_relation_server_message_id": "<18 decimal digits>",
  "message_relation_type_code": "reply",
  "message_relation_service_code": "talk"
}
```

These values are strings. They are not uppercase `RELATED_MESSAGE_ID`
keys (none found in this scan). Other observed metadata keys include
`MENTION`, `NOTIFICATION_DISABLED`, `app_extension_type`, and `e2eeMark`.
No copied original body/sender was present in either quote's metadata.
The value 3 is established by the real quote rows and their `reply` string,
not by guessing a relation enum from another LINE version.

## Real examples, bodies redacted

| Reply ID | Original ID | Reply text | Original text | Age at reply | Later same-room messages |
|---|---|---|---|---|---|
| 100000000000000007 | 100000000000000006 | [redacted; 12 characters] | [redacted; 19 characters] | 35.583 seconds | 2 |
| 100000000000000017 | 100000000000000008 | [redacted; 42 characters] | [redacted; 31 characters] | **18,512.197 seconds (5h 8m 32s)** | **12** |

Both pairs have matching room MIDs and complete original sender/time fields.
The latter is the recent real request visible in the live bot log about
understanding a quoted message. Its original is outside the eight-message
prompt window but available in another decrypted leaf page.

Example leaf addresses during research: recent quote `0x7f8082e9f620`,
its original `0x7ffffa84ee30`; the earlier pair shared page `0x7ffffa8021b0`.
These are transient addresses, not offsets to hard-code.

The probe recovered 71 distinct messages, with 63 parsed `_message` rows
of 36 columns. Overall recovered timestamps spanned approximately 30 hours
(1792469642444 to 1792578039160); this does **not** establish that arbitrary
30-hour-old quoted originals are recoverable. The measured quoted-original
reach is 5h 8m, dependent on what LINE currently keeps in memory.

## Reproduction

```bash
cd /workspace/line-re
automation/.venv/bin/python re-reply/scripts/probe_replies.py
```

The probe only opens `/proc/<pid>/mem` with `rb`; it does not call the
roster-persisting scanner or any UI/network API. It emits field names,
numeric IDs, redacted character counts and reach measurements. It retains
no dump. Process-memory contents and addresses naturally change.

The on-disk `qw*.edb` files are encrypted SQLite, not ESE. Prior copied-file
experiments are documented in `automation/read_messages.py`; this task does
not extract keys or modify live DB files. The implementation uses complete
decrypted records, with explicit unavailability when absent.

## Implemented lookup and validation

`line_common.py` normalizes the three relation fields and `reply_to_id`
from complete SQLite rows, serializer JSON, named SQL inserts and the
observed content metadata. Only relation 3 / `reply` becomes quote context;
conflicting identities or relation values cannot donate a quote. SQL column
ordering is parsed by name. Unknown services produce unavailable context.

`reply_context.py` first uses the complete current scan (before any prompt
limit), then its ephemeral per-PID cache. The cache holds at most 2,048
records and uses an 8 MiB accounting limit (UTF-8 text plus a 512-byte
per-record allowance); records expire after an hour without observation.
No message bodies are written to a new persistent cache. A normal scan
attaches one level of original context for read/watch output.

For an accepted bot command whose original is missing, an extra read-only
lookup scans at most 64 MiB of writable regions in 256 KiB chunks, with
16 KiB overlap and a 0.75-second deadline. Parsing of the last chunk can
overrun that deadline. It uses the same complete-record parsers; it never
calls the full scanner again. A 512-entry negative cache suppresses repeated
misses for 30 seconds. Fallback records do not enter the bot's new-message
queue. The exact original ID, same room, valid sender and non-future
timestamp are checked before context is accepted.

The bot prompt adds `使用者回覆的原訊息: <sender> <time> Asia/Taipei: <text>`.
Unavailable originals produce `原訊息無法取得 (id=...)`; this is also used
for unsupported services. Media without text use a content-type label,
without downloading attachments. Quoted OTP bodies are redacted before
Grok and the section is capped at 8,000 characters. Diagnostics contain
IDs/found/source, not quoted bodies. Read/watch text show `↪ reply to ...`;
JSON adds the normalized relation fields and a `reply` object.

Live no-send validation on the recent real quote checked all of these:

- Its original text is absent from the existing eight-message transcript.
- Its original text is present in the assembled new Grok prompt.
- Read and watch JSON contain the matching original text, sender and time.
- A model-call guard prevented any accidental Grok invocation.

The normal enhanced scan took **5.979s**, recovering 71 messages. Cached
lookup averaged approximately **0.002ms** across 1,000 calls. An absent
synthetic ID hit the 64 MiB byte limit after **0.037s** and returned
`budget-exhausted`. This bounded sweep can miss higher-address heap pages;
the normal complete scan remains the main source, and availability is
reported honestly. These timings describe this run, not performance
guarantees. There is no arbitrary encrypted-history or attachment-content
recovery and no server history request.

Validation: **152 tests passed, 38 subtests passed** using
`DISPLAY=:3 .venv/bin/python -m pytest -q tests` in `automation/`.
Coverage includes the three record sources and metadata fallback, invalid
relations, lookup/cache/PID/expiry/byte/deadline limits, chunk overlap,
wrong room/ID/sender/time, missing/media originals, prompt context beyond
eight messages, OTP redaction, bot integration, and matching read/watch
text/JSON. All sends/model calls in tests are mocked. Compilation and
`git diff --check` also passed. The live bot stayed running throughout
research and validation, before the one required final detached restart.
