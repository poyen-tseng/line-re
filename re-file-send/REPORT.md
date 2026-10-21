# LINE Desktop 檔案／影片發送路徑（非 UI）

對象：本機 Bottles/Wine 上的 LINE 26.0.2.3828，`LINE.exe run -t 728211730`，pid **1373692**，`DISPLAY=:3`。映像基址 **0x140000000**（`WINEPRELOADRESERVE=140000000-1452ff000`）。以下位址都是這次執行中的 VA；RVA = VA − `0x140000000`。

沒有送出檔案。沒有改 `automation/send_message.py`。群組「GroupA」`C_GROUP_A_PLACEHOLDER` 的文字訊息帶 `e2eeMark: 2`，檔案也必須走客戶端自己的 E2EE 媒體流程。行程外呼叫會打到 Themida 保護的 Qt GUI 執行緒，這次沒有做。

重現掃描（唯讀）：

```text
python3 re-file-send/scripts/scan_file_send.py
```

這次輸出：`open_chat C_GROUP_A_PLACEHOLDER`，contentType 表 0..22 與下方一致，pipe 19 條，活物件數量與下方一致。

## Themida

磁碟上的 `LINE.exe`（32,346,464 bytes）有 15 個區段。前 7 個名稱是空的，後面是 `.debug`、`.edata`、`.idata`、`.tls`、`.rsrc`、`.themida`（VA `0x3e96000`）、`.boot`（VA `0x4b7a000`）、`.reloc`。`.themida` 的 raw size 是 0。字串不在檔案裡。

執行中的映像約 83MB，落在 `0x140000000`–`0x1452ff000`。可讀字串在匿名 `r--p` `0x1426ab000`–`0x1438f8000`（對應檔案 VA `0x26ab000`）。`LineAppMgr.exe`、`LineMediaPlayer.exe`、`LineCall.exe` 同樣有 `.themida`。`LineUpdater.exe` / `LineDiag.exe` / `LineConnector.exe` 沒有。

## contentType（`thrift::Talk::ContentType`）

整數表在 `0x14394ea10`（23 個 int32，值就是 0..22）。名稱指標表在 `0x14394ea70`，第 i 個指標對應值 i。RTTI 參數型別寫的是 `W4type@ContentType@Talk@thrift@@`。

| 值 | 名稱 | 名稱字串 VA |
|---:|---|---|
| 0 | NONE | `0x1433a321c` |
| 1 | IMAGE | `0x1433a3960` |
| 2 | VIDEO | `0x142713100` |
| 3 | AUDIO | `0x1433a3968` |
| 4 | HTML | `0x14327c7ec` |
| 5 | PDF | `0x14327c7f4` |
| 6 | CALL | `0x14327c7f8` |
| 7 | STICKER | `0x1426f59d0` |
| 8 | PRESENCE | `0x14327c828` |
| 9 | GIFT | `0x14327c834` |
| 10 | GROUPBOARD | `0x14327c840` |
| 11 | APPLINK | `0x14327c850` |
| 12 | LINK | `0x14327c858` |
| 13 | CONTACT | `0x14327c860` |
| 14 | FILE | `0x143243570` |
| 15 | LOCATION | `0x14327c868` |
| 16 | POSTNOTIFICATION | `0x14327c878` |
| 17 | RICH | `0x14327c88c` |
| 18 | CHATEVENT | `0x14327c898` |
| 19 | MUSIC | `0x14327c8a4` |
| 20 | PAYMENT | `0x14327c8b0` |
| 21 | EXTIMAGE | `0x14327c8b8` |
| 22 | FLEX | `0x14327c8c4` |

檔案是 **14**，影片是 **2**，圖片是 **1**。`automation/line_common.py` 的 `CONTENT_TYPE_NAMES` 把 1 寫成 sticker、2 寫成 image、5 寫成 file；`MEDIA_CONTENT_TYPES = {2, 3, 5}` 對不上這張表。用那組數字做檔案送出後的記憶體確認會漏掉 FILE，也會把 VIDEO 當成圖片。

這次 heap 裡對得上的列：

| id | contentType | 對上的名稱 | 證據 |
|---|---:|---|---|
| `100000000000000001` | 1 | IMAGE | `SID=emi`，`FILE_SIZE=11176`，有 `OID`，`text` 空，`to` 是自己（Keep）。`e2eeMark: 2` |
| `100000000000000002`、`100000000000000003` | 22 | FLEX | `contentMetadata` 有 `FLEX_JSON` / `ALT_TEXT` |
| （先前觀察到的系統列） | 18 | CHATEVENT | 這次 22 列快取裡沒有 type 18；名稱表第 18 格是 `CHATEVENT` |

`0x14281a280` 起的字串是 `video` / `audio` / `file` / `image`。`0x14281a32c` 起連續四個 service id：`emi`、`emv`、`ema`、`emf`，緊接在 `fileDownloadReqType` 後面。活訊息只證明 **`emi` = IMAGE**。另外三個是相鄰字面值，字尾 v/a/f 對應 video/audio/file，還沒有活的影片或檔案列可以核對。

媒體 metadata 樣板在 `0x1427f5cc0`：`contentType`、`DURATION`、`FILE_SIZE`、`category`、`animated`、`extension`、`width`、`height`、`fileSize`、`MEDIA_CONTENT_INFO`、`rawImgPath`、`thumbPath`、`fileTransInfo`、`fileName`、`sourceFilePath`、`sourceFileName`、`originalPath`、`originalFileHash`、`totalSize`。另一個 `FILE_NAME` 字面值在 `0x1431611c8`。

## 客戶端送出鏈

QML 嵌在映像裡，不是磁碟上的 `.qml`。qmldir 有 `CommonFileDialog CommonFileDialog.qml`。元件本體在 `0x142cdb4b8` 附近：

```qml
import Qt.labs.platform
FileDialog {
    readonly property var videoFilter: [ "Video files (*.mp4 *.mov)" ]
    readonly property var mediaFilter: [ "Image and video files (*.png *.bmp *.jpg *.jpeg *.gif *.mp4 *.mov)"]
}
```

選完檔之後進 C++。RTTI（`0x14398e9b3` 一帶）：

- `BaseChatPanel::onFileSelected(QList<QString>)` — private thiscall，回傳 void
- `BaseChatPanel::dropEvent(QDropEvent*)` — protected virtual；lambda 產出 `MessageSendRequest`
- `ChatMessageSender::sendMessageInternal(MessageSendRequest)` — private

Qt meta 字串 `0x1430b522e`：`onFileSelected`、`fileList`、`onSendingCapturedImageRequested`（參數 `fromId` / `path` / `sourcePath`）、`onTouchBarFileUploadBtnClicked`。`FileListPanel` 的 `1onFileSelected(FileData*)`（`0x142718db9`）是檔案列表，不是送出。

`FileChatMessageSendItem` 的 log（`0x1428017a0` 起）把後段寫死了：

- `[BaseChatMessageSendItem][E2EE] encryptContent. orgPath: … encryptedPath:`
- `[FileChatMessageSendItem] original file not exists. path:`
- `create content metadata failed. unavailable content type:`
- `[FileChatMessageSendItem][E2EE] media message flow checked, success:`
- `video thumbnail generated` / `generate video thumbnail failed`
- `[Transcode] transcode succeeded. check mp4 box information. (resultPath :`
- `[Transcode] transcode skipped. path:`

mp4 會先轉碼再上傳。E2EE 加密在上傳之前。這次掃到的群組文字（例如 `100000000000000005`，`to=C_GROUP_A_PLACEHOLDER`）metadata 是 `{"e2eeMark":2}`。Keep 那張圖也是 `e2eeMark: 2`。

上傳：

| 符號 | 錨點 |
|---|---|
| `line::FileUploader::uploadAsync` | 字串 `0x143213148`；`LEA` 在 `0x141c34e2a`。同一函式約從 `0x141c34c10` 起。下一個 log「file doesn't exist, oldOid, newOid, localPath」的 `LEA` 在 `0x141c34e7d` |
| `line::FileUploadQueueWorker::startUpload` | log `[UPLOAD] startUpload, oid:%s, reqSeq:%d, startPos:%d: path:%s` 在 `0x1432575b0`；`LEA` 在 `0x141cfa7c1` |
| `line::ObsCommandHelper` | `ObsCommandHelper.cpp` 路徑字串 `0x143214ec0`。URL 格式字串 `%s://%s%s` 在 `0x1432157d0` |
| `line::FileTransManager` | `FileTransManagerImpl.cpp` 在 `0x1431757a0` |
| `UploadContentTask` / `SendMessageTask` | 原始檔路徑字串 `0x14325e100`、`0x143224f90` |

OBS 主機表 `0x14320e048` 起：`obs.line-apps.com`，以及 `obs-beta` / `obs-rc` / `obs-kr` / `obs-jp` / `obs-tw` / `obs-th` / `obs-cn` / `obs-us` / `obs-sg` / `obs-de` / `obs-vn`。判斷函式名 `line::isObsHost` 在 `0x14320e228`。上傳標頭在 `0x14281a2c0`：`X-Line-Access`、`x-obs-oid`、`X-Talk-Meta`。旁邊還有 `x-obs-content-type`（`0x1431b87f0`）。本體是 `application/x-thrift`（`0x1426d7f36` 與 `obsContentInfo` 相鄰）。

## 這次活著的物件

主 vtable（RTTI Complete Object Locator 的 `offset == 0`）。Heap 位址只對這次 pid 有效。

| 類別 | RTTI 名稱 | 主 vtable | 實例 |
|---|---|---|---|
| `ChatMessagePanel` | `0x143964a40` | `0x1426f7420` | 1，物件 `0x7ffff6f499e0` |
| `ChatMessageSender` | `0x14398e258` | `0x1427fd4b8` | 1，`0x7ffff713a860` |
| `FileChatMessageSendItem` | `0x143987268` | `0x1428011e8` | 0（沒有正在送的檔） |
| `FileUploader` | `0x143ab2300` | `0x143213048` | 1，`0x7ffff1939eb0` |
| `FileUploadQueueWorker` | `0x143abb358` | `0x1432572b0` | 9 |
| `FileTransManager` | `0x143a80d98` | `0x143175308` | 1，`0x7ffff1719690` |
| `ObsCommandHelper` | `0x143ab23a8` | `0x143214d18` | 1，`0x7ffff1843c40` |

`BaseChatPanel` 主 vtable `0x1428136c0` 的實例數是 0：目前那個面板的動態型別是 `ChatMessagePanel`，第一個 vptr 不是基底類的。

## Named pipes

映像裡 ASCII 字面值 `\\.\pipe\` 只有一處，`0x142711f88`，夾在 `album_skip_validation` 和 `commandTarget` 之間，沒有接 uuid。

Heap 裡的名字是 UTF-16，長度 45（`\\.\pipe\` + 36 字元 uuid）。和插件命令列對得上的三條：

| pipe | 行程 |
|---|---|
| `\\.\pipe\015c1a72-444c-426f-8e64-951e1412e655` | `linemediaplayer.exe mode=0` 的 `connectionId` |
| `\\.\pipe\a93508dc-5f4c-47de-8258-71a80d76ec0c` | `linemediaplayer.exe mode=2` |
| `\\.\pipe\04d3439f-15c5-4409-b6b0-9d7d2a6867bc` | `linecall.exe` 的第一個參數 |

另外還有 15 條同形狀的 `\\.\pipe\<uuid>`（掃描腳本會印出完整清單），加上 COM 的 `\\.\pipe\lrpc\...` / `\\.\pipe\OLE_...`。沒有看到以 send/file/obs 命名的 pipe。媒體播放器那條帶著命令列上的 `encryptionKey`；這次沒有解它的訊框。它是播放 IPC，不是聊天室送檔。

`/proc/net/unix` 裡沒有 `pipe` 路徑。Wine 把 named pipe 收在 wineserver，名字只出現在行程的 UTF-16 字串。

## 建議的 PoC（這次沒跑）

呼叫點是已經登入的那個 `ChatMessagePanel`，不是新開一條 thrift，也不是寫 pipe。

1. 用 `scan_file_send.py` 確認主 vtable `0x1426f7420` 仍只有一個 `ChatMessagePanel`。
2. 在 **Qt GUI 執行緒** 呼叫 `BaseChatPanel::onFileSelected(QList<QString>{路徑})`。`dropEvent(QDropEvent*)` 是另一條，一樣會做出 `MessageSendRequest`。兩條都進 `ChatMessageSender::sendMessageInternal`。
3. 讓 `FileChatMessageSendItem` 自己做 metadata、mp4 transcode、E2EE `encryptContent`、`FileUploader::uploadAsync`。不要自己組 OBS PUT：群組訊息是 `e2eeMark: 2`。
4. 事後用現有的 heap 掃描找新列：`from=U_BOT_PLACEHOLDER`，`to=C_GROUP_A_PLACEHOLDER`，contentType **2**（mp4）或 **14**（一般檔）。圖才是 1。metadata 裡預期有 `OID`、`FILE_SIZE`，影片再加 `DURATION`，service id 字面值是 `emv` / `emf`（尚未用活列核對）。

不要做的事：遠端執行緒直接打 `uploadAsync`（`0x141c34c10` 不是 Qt slot，參數是 C++ 物件，打錯會把 pid 1373692 打死）；對 `\\.\pipe\<media connectionId>` 寫檔案路徑；用 `{2,3,5}` 當成功條件。

函式本體沒有匯出符號。`onFileSelected` 只在 RTTI 和 meta 字串裡，這次沒有把它的程式碼位址從 `LEA` 定出來。要接到它，得在行程內走 Qt meta / 自己算 vtable slot，並且排進 GUI 執行緒。Themida 還在 `.boot`（`0x144b7a000` 起、`r-xp`）。這就是這次停在研究、沒有送 `[bot test]` 檔案的原因。

## 2026-10-21 follow-up: verified uploads, Flatpak path failure, clipboard probe

This section supersedes the earlier untested UI/path assumptions. LINE stayed
on **26.0.2.3828**, PID **2304644**, `DISPLAY=:3`; its protected bin/shared
permissions were not changed. Bot PID 2386891 remained running throughout
experiments. Every mutating probe took the existing `/tmp/line-ui-<uid>.lock`.
All attachment tests targeted **GroupB** only:
`C_GROUP_B_PLACEHOLDER`. No test text messages were sent.

### Why the earlier rc=5 happened

The old log records the picker remaining open after Open, not a transport
failure. Two independently reproduced causes explain its two path variants:

1. `Z:\workspace\line-share\...` is syntactically Windows-like, but
   `/proc/2304644/root/workspace` **does not exist**. Bottles runs inside
   Flatpak's mount namespace; Z: maps that namespace's root, not the host root.
   The directory-only probe returned **“Path does not exist.”**
2. `Z:/workspace/line-share/` and `/workspace/line-share/` both produced
   **“Invalid character(s) in path”**, explicitly listing `/` as forbidden.
   The old forward-slash workaround/documentation was incorrect. Pasting
   `C:\` through the existing UTF-8 clipboard correctly opened the C: drive.

`xwininfo -id` also omits the parent field the old code expected. Parent-frame
lookup now uses `-tree`; the measured 670×522 frame and 660×488 client match.
The fallback coordinates happened to match, so this was an ancillary defect.

Uploads now copy a bounded, stable snapshot into
`<WINEPREFIX>/drive_c/line-uploads/upload-<random>/<basename>` and select
`C:\line-uploads\upload-<random>\<basename>`. The directory is ASCII;
Chinese and space-containing basenames survived unchanged. Names Windows
cannot represent, reserved names, and long names use `upload.<extension>`.
The bot confirms the actual uploaded name. No LINE binary directory is touched.
Successful/probe copies are removed. Uncertain copies remain because LINE may
still need them; they must only be removed after checking the pending upload.

### Native path and OBS observations (read-only time box)

Re-reading the in-memory enum confirms IMAGE=1, VIDEO=2, AUDIO=3, PDF=5,
STICKER=7 and FILE=14. `line_common.py` now uses that table. The prior QML,
RTTI and private `onFileSelected(QList<QString>)` / GUI-thread findings still
apply. No safe external native invocation or stable file-send IPC was found;
no injection, memory writes, credential extraction or authenticated replay
was attempted. XDND would still require Wine/Qt MIME negotiation and the
same staged namespace-visible paths; no reliable XDND implementation is claimed.

Additional static anchors (RVAs, image base 0x140000000):

- `0x26ecc97`: `BaseTextArea::pasteFromClipboard` diagnostics, including
  `event->hasText()`, `hasImage`, and `hasUrls`.
- `0x3213250`: `[UPLOADER] notifyResultCallback, statusCode:%d, oldOid:%s, newOid:%s`.
- Existing `FileUploader`, `ObsCommandHelper`, and `FileUploadQueueWorker`
  anchors remain as recorded above. Heap URL structure included
  `https://obs.line-apps.com/r/talk/<object>/<object>`; this observation
  does **not** establish the upload method/body or a reusable API.
  Query strings, object values, headers and credentials are not recorded here.

Live MP4 logs showed E2EE encryption of the staged C: path at 17:52:16.999,
then `[UPLOAD] startUpload` at 17:52:17.005, before the final message's
17:52:19.965 creation timestamp. That selection produced FILE=14, `SID=emf`
and exact original size, avoiding the VIDEO transcode path. PNG selection
produced IMAGE=1, `SID=emi`, original-category 16×16 metadata and exact size.
A 17:54:01.360 uploader callback returned **201** for that image's preview
with an object identifier matching the image row. It is supporting evidence
for the preview, not an independently claimed original-upload ACK.

One alternate probe staged a 414-byte PDF, offered a percent-encoded host
`file://` URI as X11 `text/uri-list`, and pasted into the verified composer
without Return. No attachment row appeared during the bounded verification
and the composer stayed empty. This did not establish Wine CF_HDROP translation.
It was **not retried**, and this method is
not exposed as a working option. A directory-only picker probe and final
Cancel probe sent nothing; final picker enumeration was empty.

### Live evidence and verification

All rows below were recovered from complete decrypted message records, from
self `U_BOT_PLACEHOLDER`, to the room above. All carried
`e2eeMark=2`, a nonempty object ID, the matching service and exact FILE_SIZE.
Numeric server message IDs, rather than local `l<uuid>` pending IDs, are required.
The UI showed sent attachment bubbles without upload progress; later captures
also showed recipient read indicators. No cross-room send occurred.

| Filename | Bytes | ContentType / SID | Message ID | Entry point |
|---|---:|---|---|---|
| 測試上傳 2026.txt | 26 | FILE=14 / emf | 100000000000000013 | one-off shared sender |
| test_clip.mp4 | 15,257 | FILE=14 / emf | 100000000000000014 | `send-file` CLI |
| upload-check.png | 79 | IMAGE=1 / emi | 100000000000000015 | `send-file` CLI |
| upload-check.zip | 160 | FILE=14 / emf | 100000000000000016 | durable schedule dispatcher |

The first TXT verifier expected FILE_NAME; LINE instead uses the row's text
for FILE filenames. The first MP4 verifier expected VIDEO; this picker sent
FILE. Both initially reported uncertainty, were corrected and reconfirmed
**read-only**, with no resend. PNG and ZIP were confirmed automatically.
The ZIP's isolated test schedule was durably claimed before selection and
finished `sent`. No live PDF picker send or large-file upload is claimed.

Reproduce safe, selected evidence without exposing object values:

```bash
python3 re-file-send/scripts/inspect_upload.py \
  --message-id 100000000000000013 \
  --message-id 100000000000000014 \
  --message-id 100000000000000015 \
  --message-id 100000000000000016
```

The production verifier excludes baseline IDs, local IDs, other senders,
other rooms, old times, mismatched names/sizes/services, and ambiguous rows.
Original IMAGE/VIDEO must match size even when no filename is present.
A transformed size, expired heap evidence, timeout or unknown confirmation
remains uncertain; no automatic resend follows a possible selection.
This is client-memory delivery evidence, not an external download/hash check.

The shared UI lock covers routing, clipboard, picker selection and cleanup.
Verification releases it so the watchdog can reconnect. A separate upload
lock prevents competing file verifiers. During bot uploads, its main thread
continues read-only scans and buffers incoming rows for the next serial
command. Basename search has a five-second budget; routing uses a 15-second
budget; picker wait/close use five/six seconds; confirmation defaults to 30
seconds (configurable up to 60, plus one in-progress memory scan). Unknown
send/original/size-limit modals are cancelled, never blindly accepted.
Large files, client/server limits and different UI layouts remain unverified.
