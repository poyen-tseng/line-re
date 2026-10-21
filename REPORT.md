# LINE Desktop 逆向分析報告（使用者本人帳號自動化控制）

- 分析日期：2026-10-20
- 安裝位置：`~/.var/app/com.usebottles.bottles/data/bottles/bottles/LINE/`（Flatpak Bottles，Wine runner `kron4ek-wine-11.19-amd64`）
- 主程式目錄：`.../drive_c/users/$USER/AppData/Local/LINE/bin/current/`
- LINE 版本：**26.0.2.3828**（官方 LY Corp 簽章版，使用者從 26.5.0.3975 降級，因 26.5 在 Wine 下 `CRYPT32.dll NO_SIGNATURE` 驗證失敗）
- Qt 版本：**6.6.3**（x86_64-little_endian-llp64 shared dynamic release build; by MSVC 2022）
- OpenSSL 版本：**1.1.1n** (15 Mar 2022)
- 發行者：LY Corporation（Tokyo, JP），Appx `Identity Name="LINE"` `Version="26.0.2.0"`
- 分析時 LINE 正在執行中（PID 1373692，DISPLAY=:3）

> 範圍宣告：本報告僅針對使用者本人帳號做自動化 GUI／應用程式控制（發訊、讀訊、開聊天室等）。不涉及破解加密、攔截他人流量、惡意程式或未經授權存取。

---

## 1. 處理序／元件映射（Process / Component Map）

### 1.1 主要執行檔（`bin/current/`）

| 檔案 | 大小 | PE 區段特徵 | 角色（推斷） |
|---|---|---|---|
| `LINE.exe` | 31 MB | `.themida` + `.boot`（7.8 MB）→ **Themida 保護** | 主程式。Qt6 Quick/QML UI、聊天邏輯、訊息資料庫存取、與插件做 IPC。執行時命令列為 `LINE.exe run -t <session_id>`。AppxManifest 中 `Executable=LineConnector.exe`，但實際 UI 進程是 LINE.exe。 |
| `LineConnector.exe` | 591 KB | 無 Themida | MSIX 套件註冊／部署 bootstrap。PDB 路徑 `D:\jenkins_agent\workspace\build_windows_LINE\line-updater\LineConnector\x64\Release\LineConnector.pdb`。匯入 `Windows.Management.Deployment`（WinRT）做 `registerLinePackage`。匯入 `WS2_32.dll`、`bcrypt.dll`、`USERENV.dll`。內含 build tag `LINEDTISBESTTEAM`。 |
| `LineLauncher.exe` | 1.8 MB | 無 Themida | 「BridgeApp」：line-updater 與主程式之間的橋接器，負責啟動 LINE.exe 與各插件，並把 `connectionId`、`encryptionKey`、`groupId` 透過命令列傳給插件。PDB `.../line-updater/BridgeApp/Release/BridgeApp.pdb`。匯入 `ShellExecuteW/ExW`。登錄 `line://` URL protocol handler 指向此 exe（`--booting`）。 |
| `LineAppMgr.exe` | 3.4 MB | `.themida` + `.boot`（3.3 MB）→ **Themida 保護** | 應用程式管理員。匯入 `WS2_32.dll`（WSAEventSelect）、`SHGetFolderPathW`、`PathRemoveFileSpecW`。字串被加密，推測負責安裝／版本管理／插件下載。 |
| `LineUpdater.exe` | 3.4 MB | 無 Themida | 更新器。**關鍵 IPC 字串清楚可讀**（見 §3.3）。PDB `D:\jenkins_agent\workspace\windows_desktop_updater_build\line-biz-common\src\library\CommLib\...`。匯入 `CreateFileW`、`WaitNamedPipeW`、`WriteFile`、`ReadFile`、`PeekNamedPipe`、`WINTRUST.dll`、`CRYPT32.dll`（後者在 Wine 下對 26.5 簽章驗證失敗）。 |
| `LineDiag.exe` | 4.0 MB | 無 Themida | 診斷工具，產生 PDF 報告。字串含 `LineDiagResult`、`LineDiagTaskManager`、`LineDiagTaskWorker`、`LineDiagWindow`、`kLineAppRegistry`、`http://gd2.line.naver.jp:443/204`。 |
| `LineUnInst.exe` | 169 KB | 無 Themida | 解除安裝程式。 |

### 1.2 Qt6 模組（`bin/current/`，部分列舉）

`Qt6Core.dll`、`Qt6Gui.dll`、`Qt6Quick.dll`、`Qt6Qml.dll`、`Qt6QmlModels.dll`、`Qt6Widgets.dll`、`Qt6Network.dll`、`Qt6OpenGL.dll`、`Qt6Multimedia.dll`、`Qt6QuickControls2*.dll`、`Qt6QuickDialogs2*.dll`、`Qt6QuickTemplates2.dll`、`Qt6LabsQmlModels.dll`、`Qt6StateMachine.dll`（在 LineCall 內）、`Qt6TextToSpeech.dll`、`Qt6Svg.dll`、`Qt6ShaderTools.dll`、`Qt6Core5Compat.dll`。

> **沒有** `Qt6WebEngine*` / `Qt6WebView*` / `Qt6WebChannel*`。網頁內容改用 **WebView2**（見 §2）。

### 1.3 Qt 外掛（`bin/current/plugins/`）

50 個 QML 樣式／控制項外掛 dll：`qtquickcontrols2*styleplugin.dll`（basic/fusion/material/universal/imagine/native/windows）、`qtgraphicaleffectsplugin.dll`、`qtvkb*plugin.dll`（虛擬鍵盤，含日/韓/中/泰/拼音/注音）、`qtquickcontrols2nativestyleplugin.dll`（682 KB，原生樣式）、`qtlabsplatformplugin.dll`、`qmllocalstorageplugin.dll`、`qmlfolderlistmodelplugin.dll`、`qmlxmllistmodelplugin.dll`、`qtquicktimelineplugin.dll`、`workerscriptplugin.dll`、`quickmultimediaplugin.dll`、`quicktoolingplugin.dll`、`quickwindowplugin.dll`。

### 1.4 其他 dll

- `WebView2Loader.dll`（166 KB）— Microsoft WebView2 載入器，僅匯入 `KERNEL32.dll`。
- `skottie.dll`（5.2 MB）— Qt Lottie 動畫模組（匯入 `Qt6Gui.dll`、`Qt6Core.dll`），用於播放 Lottie/BodyMovin 動畫（貼圖動畫、特效）。
- `dbghelp.dll`（1.0 MB）— Windows 除錯協助程式庫（崩潰 dump）。
- `libcrypto-1_1-x64.dll`、`libssl-1_1-x64.dll` — OpenSSL 1.1.1n，用於 HTTPS 連線到 LINE 伺服器。
- `msvcp140*.dll`、`vcruntime140*.dll`、`ucrtbase.dll`、`api-ms-win-*` — VC++ 執行階段。

### 1.5 使用者資料外掛（`Data/plugin/`）

| 插件 | 版本 | 內容 |
|---|---|---|
| `LineMediaPlayer` | 1.2.0.635 | Qt6 + ffmpeg（`avformat-57.dll`、`avutil-55.dll`、`swresample-2.dll`）+ `libEGL.dll`、`libGLESv2.dll`（OpenGL ES）。獨立 `linemediaplayer.exe`。 |
| `LineCall` | 1.0.0.897 | Qt6 + `NeuralLine.dll`、`MNN.dll`（阿里 MNN 推理引擎，AI 降噪）、`ElsaKit.dll`、`YukiDesktop.dll`（Yuki 美顏濾鏡）、`ampkit_windows.dll`、`bundle.epk`、`beauty.epk`、`filters.epk`、`extension.epk`。獨立 `linecall.exe`。 |
| `ffmpeg` | 1.0.0.5 | 獨立 `ffmpeg.exe`（轉碼用）。 |

### 1.6 使用者資料目錄（`Data/`）

- `LINE.ini`（8.1 KB）— 主設定檔。含 `luuid`（本機 UUID）、`language`、加密的 `lrst`/`acf`（登入憑證）、多組帳號 `[<mid>]` 區段（`mid` 是 LINE 使用者 ID，base64-like），各種視窗位置 `window_rect_*.qml=@Rect(...)`。
- `setting.ini`、`installLang.ini` — 通用設定。
- `db/` — **ESE（Extensible Storage Engine）資料庫 `.edb`**（不是 SQLite），每個帳號一組：
  - `qw<mid>.edb`（+`-wal`、`-shm`）— 主訊息資料庫（WAL 模式）。
  - `album_<mid>.edb`、`keep_<mid>.edb`、`chatStats_<mid>.edb` — 相簿、Keep、聊天統計。
  - `AutoSuggest/qw<hash>.edb` — 自動完成。
- `Sticker/<id>/`、`Sticon/<id>/` — 已下載的貼圖／動態貼圖 PNG。
- `bgChat/<hash>/` — 聊天室背景圖。
- `ampkit/.jup_storage.db` — LineCall 的 AI 模型儲存。
- `utsinfo/common.dat`、`resource/alarm1.wav` — 通知音效。
- `advertisement/`、`plugin/plugin.bin` — 廣告快取、插件索引。

### 1.7 執行時觀察到的處理序（`ps aux`）

```
LINE.exe run -t 728211730
linemediaplayer.exe mode=0 connectionId=015c1a72-...  encryptionKey=3246538db4dc13e55514ddb111216bdb  groupId=C:\...\LineLauncher.exe  ... mse
linemediaplayer.exe mode=2 connectionId=a93508dc-...  encryptionKey=3246538db4dc13e55514ddb111216bdb  groupId=C:\...\LineLauncher.exe  ... mse
linecall.exe 04d3439f-15c5-4409-b6b0-9d7d2a6867bc  C:\...\LineLauncher.exe  26.0.2.3828  non_appstore
wineserver, winedevice.exe (×2)
```

關鍵觀察：

- 插件由 `LineLauncher.exe` 啟動，命令列帶 `connectionId=<UUID>`、`encryptionKey=<32 hex>`、`groupId=<launcher path>`、`lineVer`、`imageCachePath`、`logSampleRate`、`crashSampleRate`，結尾 `mse`（推測為 MediaSourceElement 標記）。
- `linecall.exe` 命令列為 `<call-UUID> <launcher-path> <version> non_appstore`。
- LINE.exe 進程有 **191 個 fd**，其中包含大量 `pipe:[...]` 與 3 個 `socket:[...]`。
- LINE.exe 在 `Temp/` 持有 ~20 個 `Ticket_<UUID>.lock` 檔，每個 63 bytes，內容為：

  ```
  328                          # Windows 側 PID
  LINE                         # 進程名
  GROK-BOT-VM-371              # 主機名
  9f273b2c-0301-4b63-aba7-290eb35205e0   # session UUID
  ```

  其中 `Ticket_015c1a72-...lock` 的 UUID 與 `linemediaplayer.exe` 的 `connectionId=015c1a72-...` **完全吻合**；`Ticket_04d3439f-...lock` 與 `linecall.exe` 的第一個參數 `04d3439f-...` 吻合。→ **Ticket = IPC 連線的鎖定／身分中繼檔**。

---

## 2. UI 技術棧與對自動化的意涵

### 2.1 UI 技術棧確認

| 證據 | 結論 |
|---|---|
| 50+ 個 `Qt6*.dll`、`qtquickcontrols2*plugin.dll`、`qtgraphicaleffectsplugin.dll`、`qtvkb*plugin.dll` | **Qt 6.6.3 Quick / QML** 為主 UI 框架 |
| `Cache/qmlcache/*.qmlc`（29 個檔，header `qv4cdata=`） | QML 被 Qt V4 引擎編譯為位元組碼並快取 |
| `LINE.ini` 內 `window_rect_profile\ProfileWindow.qml=@Rect(...)`、`window_rect_profile\GroupProfileWindow.qml=@Rect(...)`、`window_rect_AllInOneWindow=@Rect(...)` | 視窗以 QML 檔名命名，確認主視窗 `AllInOneWindow`、子視窗 `ProfileWindow.qml`、`GroupProfileWindow.qml` 等 |
| `Qt6OpenGL.dll`、`Qt6ShaderTools.dll`、`libEGL.dll`、`libGLESv2.dll`、`D3DCompiler_47.dll`（在 LineCall） | QML 透過 OpenGL ES / ANGLE 渲染 |
| `line-run.log` 內 `DXVK`、`VK_FORMAT_B8G8R8A8_UNORM`、`VK_PRESENT_MODE_FIFO_KHR`、`Buffer size: 288x512` | **Wine 下透過 DXVK（Vulkan）渲染 Qt Quick**；288×512 為 splash/登入視窗尺寸 |
| `WebView2Loader.dll` 存在；LINE.exe 匯入 `GetAvailableCoreWebView2BrowserVersionString` | **WebView2** 用於嵌入式網頁內容（推測為 LINE Shopping / Travel / Manga / 服務分頁、廣告、活動頁） |
| `skottie.dll` | Lottie 動畫（貼圖特效、入場動畫） |
| `Windows.UI.Notifications.ToastNotificationManager` 錯誤訊息（Wine 不支援） | 原生會用 Windows Toast 通知，Wine 下失敗 |

### 2.2 對自動化的意涵

1. **Qt Quick/QML 不是原生 Win32 控制項**，傳統 UI Automation（UIA / MSAA）在 Wine 下幾乎拿不到有意義的 accessibility 樹。Qt 的 `accessible` 附加屬性需要 QML 端明確標注，LINE 未保證有標注。
2. **QML 內容被編譯為 V4 位元組碼**（`.qmlc`），且 LINE.exe 本體被 **Themida** 加密，無法直接從磁碟讀到 QML 原始碼／物件名稱。QML 物件樹要在執行時透過 Qt 內部 API 才能列舉。
3. **渲染走 DXVK/Vulkan**，不是 GDI。代表畫面是 GPU 合成，傳統 `BitBlt`/`PrintWindow` 截圖可能失效；但 X11 層級的截圖（`import`、`xwd`、`ffmpeg -f x11grab`）仍可用。
4. **X11 視窗可被外部工具識別**（已驗證，DISPLAY=:3）：
   - 主視窗 `0x3000001`，1434×868，`WM_CLASS=("line.exe","line.exe")`，無標題。
   - `0x3000004 "QTrayIconMessageWindow"` — 系統匣訊息視窗。
   - `0x30000dd "LINE"`、`0x3000040 "LINE"` — 兩個有「LINE」標題的小視窗（91×30、80×30），推測是 tooltip／按鈕。
   - `0x1401660`（742×806，16 個子視窗）— 主客戶區，含聊天室內容。
   - `linemediaplayer.exe`、`linecall.exe` 各有 1434×868 的獨立視窗（與主視窗同尺寸，推測為嵌入或全螢幕覆蓋）。
   - `xdotool`、`xwininfo`、`xprop` 在本機都可用。
5. **WebView2 內容是獨立的 Edge 子進程**，其 DOM 可透過 WebView2 的 DevTools（CDP）除錯協定存取，但需要 LINE 端有開啟遠端除錯旗標（預設未開）。這條路徑對「聊天」本身無用，只對「服務分頁」有用。

---

## 3. 可行控制路徑排序（含成本與風險）

### 3.1 路徑 A：X11 UI 自動化（xdotool / xwininfo / 圖像比對）

**成本：低　風險：低　推薦：第一階段先做**

- 工具皆已安裝：`xdotool`、`xwininfo`、`xprop`、`ffmpeg`（x11grab）、`python3` + Pillow + OpenCV。
- 做法：
  1. `xdotool search --class line.exe` 取得 LINE 主視窗 ID。
  2. `xdotool windowactivate <wid>` / `windowfocus` 聚焦。
  3. `xdotool key`／`type`／`mousemove --window` 操控鍵盤滑鼠。
  4. `xdotool getwindowname`、`xwininfo -id <wid> -tree` 列舉子視窗結構（但 QML 子項不會出現在 X11 樹，只有頂層 Qt 視窗會）。
  5. 用 `ffmpeg -f x11grab -video_size 1434x868 -i :3+3,29` 或 `import -window <wid>` 截圖，配合 OpenCV 模板比對定位按鈕／輸入框。
- 限制：
  - QML 內部沒有穩定的座標／控制項 ID，只能靠**視覺**（圖像比對、OCR）。
  - 解析度／DPI／主題變動會破壞座標。
  - 中文輸入需透過 XIM 或剪貼簿（`xclip`/`xsel`）貼上。
  - LINE 視窗必須可見（不能最小化；可用 `xdotool windowminimize` 復原）。
- 適用：批次發訊、定時打卡、讀取可見訊息截圖 OCR。

### 3.2 路徑 B：模擬鍵盤快速鍵 + 剪貼簿貼上

**成本：低　風險：低**

- LINE Desktop 支援的快速鍵（從 `LINE.ini` 的 `window_rect_*` 與 Qt 慣例推測，需執行時驗證）：
  - `Ctrl+1..9` 切換主分頁（聊天/好友/動態消息/錢包/服務...）。
  - `Ctrl+F` 搜尋。
  - `Ctrl+Tab`／`Ctrl+Shift+Tab` 切換聊天室。
  - `Enter` 送出、`Shift+Enter` 換行、`Ctrl+V` 貼上。
- 流程：聚焦聊天室清單 → 用搜尋框打對方名稱 → `Enter` 進入聊天室 → 在輸入框貼上文字 → `Enter` 送出。
- 限制：仍是「盲打」，需先截圖確認游標位置。

### 3.3 路徑 C：注入 Qt / QML（DLL injection + Qt internal API）

**成本：高　風險：中-高　推薦：第二階段**

- 關鍵發現：**LINE.exe 被 Themida 加密**（`.themida` + `.boot` 區段）。直接 DLL injection 在 Wine 下可行（`CreateRemoteThread` / `SetWindowsHookEx`），但要呼叫 Qt API 需：
  1. 載入 `Qt6Qml.dll`、`Qt6Quick.dll` 的匯出表（這些 dll **沒有** Themida，可正常解析符號）。
  2. 用 `QmlEngine::rootObjects()`、`QObject::findChildren<QObject*>()` 列舉 QML 物件樹。
  3. 對目標 `TextField` 呼 `setText()`、對 `Button` 呼 `click()`。
- 難點：
  - Themida 會偵測除錯器／注入器（anti-debug），可能崩潰或行為異常。Wine 下 Themida 的 anti-debug 通常較弱（許多檢查依賴 Windows 核心 API，Wine 回傳假值）。
  - Qt 的 C++ RTTI 與 MOC meta-object 資料需要符號或比對；Qt6 dll 是公開版本，可從 Qt 官方下載相同版本（6.6.3）的 pdb 來輔助。
  - 注入器需用 64-bit PE，在 Wine 下可用自己寫的小 dll + `rundll32` 或 `CreateRemoteThread`。
- 適用：穩定讀取聊天室清單、訊息內容、聯絡人清單；觸發「送出」按鈕。

### 3.4 路徑 D：本地 IPC / 具名管道（仿插件協定）

**成本：很高　風險：中　推薦：第三階段（研究用）**

- 從 `LineUpdater.exe` 字串確認 IPC 架構：

  ```
  \\.\pipe\
  [App] IPC pipe: {}
  [Pipe] connected / disconnected / start reading / read finished / read pending / read error
  [Pipe] data received, number of bytes : {}
  [Pipe] send failed, invalid handle / error : {}
  PipeCommunicator
  CommLib::connect_to_pipe
  [IPC] connecting, pipe : {}
  ticket_appdata
  construct_stateful_ticket / construct_stateless_ticket
  [PROXY] proxy test started : ticket=%s
  ```

- 並從執行中插件命令列確認每條連線帶 `connectionId=<UUID>` 與 `encryptionKey=<32 hex>`，加上 `Temp/Ticket_<UUID>.lock` 中繼檔。
- 推測協定：`\\.\pipe\<某前綴>_<connectionId>`，訊框用 `encryptionKey` 做對稱加密（可能 AES-128-CBC，因 32 hex = 16 bytes key；或當作 HMAC key）。
- 風險：
  - 沒有文件，需逆向 `PipeCommunicator` 序列化格式（在 `LineUpdater.exe` 與被 Themida 保護的 LINE.exe 內）。
  - 加密金鑰由 LINE.exe 產生，外部程式無法取得（除非掛鉤產生點）。
  - LINE 伺服器端可能對異常訊框做檢查。
- **不建議**為了「發訊」走這條路：成本遠高於 UI 自動化，且違反 LINE ToS 風險較高。
- 適用：學術研究、寫自己的 LINE 用戶端（但這等同重做 LINE 協定，已超出「自動化本人帳號」範圍）。

### 3.5 路徑 E：非官方 API / LINE Protocol

**成本：很高　風險：高　不推薦**

- LINE 的長連線協定（talk.longpolling、FRODO、TALKM）已有社群逆向（如 `line-bot-sdk`、`line-thrift`、`ocean` 專案），但：
  - 需要從手機 LINE 抓 `authKey` / `certificate`，PC 版的 `lrst`/`acf`（在 `LINE.ini`）格式不同，無法直接用。
  - 連線會被 LINE 伺服器當成「另一台 PC」，可能踢掉現有 session。
  - 明顯違反 LINE Tos，帳號可能被停權。
- **本報告不深入此路徑**（超出「本人帳號 GUI 自動化」範圍）。

### 3.6 路徑 F：直接讀 ESE 資料庫（`.edb`）

**成本：中　風險：低（唯讀）　推薦：輔助**

- LINE 用 ESE（Extensible Storage Engine，即 Windows 內部用的 JET Blue）資料庫，不是 SQLite。
- 工具：
  - `esentutl.exe`（Windows 內建，Wine 有帶）可 dump。
  - 開源工具：`libesedb`（Joachim Metz，python binding `libesedb-python`）可解析。
  - **必須唯讀**且最好先複製一份（ESE 對並發開啟敏感；LINE 正在寫時外部開啟可能失敗或拿到不一致快照）。
- 可讀到：訊息歷史、聊天室清單、聯絡人、貼圖中繼資料 → 用於「讀訊」離線分析。
- 不能：發訊、改狀態（這些需走 IPC／伺服器）。
- 適用：備份、搜尋歷史訊息、訊息統計。

### 3.7 路徑 G：WebView2 DevTools Protocol（僅對服務分頁）

**成本：中　風險：低　推薦：若需自動化 Shopping/Travel 分頁才做**

- LINE 用 WebView2 嵌入服務分頁。若啟動 LINE 時設定環境變數 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222`，可讓 WebView2 開 DevTools 連接埠。
- 之後用 Chrome DevTools Protocol（CDP）可操控該分頁的 DOM。
- 限制：只對 WebView2 內容有效，**對原生 QML 聊天室無效**。
- 適用：自動點服務分頁的按鈕、抓取活動頁資料。

### 3.8 推薦組合

| 目標 | 推薦路徑 |
|---|---|
| 發訊息給指定聊天室 | A（X11 + xdotool）或 C（Qt injection） |
| 讀取歷史訊息 | F（ESE 唯讀） |
| 讀取目前可見訊息 | A + OCR |
| 開啟指定聊天室 | A（搜尋框打名稱） |
| 接收新訊息事件 | A（監看閃爍/數字 badge）或 C（hook QML signal） |
| 服務分頁自動化 | G（CDP） |

---

## 4. 具體執行過的調查步驟與發現

以下為實際執行的指令與觀察（節錄重要片段；完整輸出已留在終端機歷史）。

### 4.1 檔案系統盤點

```bash
BIN=~/.var/app/com.usebottles.bottles/data/bottles/bottles/LINE/drive_c/users/$USER/AppData/Local/LINE/bin/current
ls -la "$BIN/"                      # 50+ Qt6 dll, 7 個 exe, WebView2Loader.dll, skottie.dll
ls -la "$BIN/plugins/"              # 50 個 QML 樣式外掛
ls -la "$BIN/platforms/"            # 只有 qwindows.dll（無 xcb/wayland）
ls -la "$BIN/resource/"             # default.dat（22 MB Qt 資源封存）
ls -la "$BIN/assets/"               # MSIX splash/tile logo PNG
```

發現：`platforms/` 只有 `qwindows.dll`（Qt Windows 平台插件），證實 Qt 是用 Windows 原生平台而非 XCB/Wayland。`resource/default.dat` 開頭 `qres\0\0\0\3\1T` 是 Qt 資源封存格式。

### 4.2 PE 標頭與區段

```bash
objdump -f "$BIN/LINE.exe"          # pei-x86-64, start address 0x144b7a058
objdump -h "$BIN/LINE.exe"          # 15 區段，含 .themida（0 size）+ .boot（7.8 MB）
objdump -h "$BIN/LineAppMgr.exe"    # 同樣有 .themida + .boot（3.3 MB）
objdump -h "$BIN/LineConnector.exe" # 無 .themida（未加密）
objdump -h "$BIN/LineLauncher.exe"  # 無 .themida
objdump -h "$BIN/LineUpdater.exe"   # 無 .themida
```

發現：**LINE.exe 與 LineAppMgr.exe 被 Themida 加密**；LineConnector、LineLauncher、LineUpdater、LineDiag、LineUnInst 未加密。

### 4.3 PE 匯入表

```bash
objdump -p "$BIN/LINE.exe" | grep -A200 'Import Tables'
# LINE.exe 匯入：kernel32, WebView2Loader.dll (GetAvailableCoreWebView2BrowserVersionString),
# api-ms-win-* (namedpipe, processthreads, toolhelp, com, winrt, ...), PSAPI, MFReadWrite.dll

objdump -p "$BIN/LineConnector.exe" | grep -A200 'Import Tables'
# 匯入 USERENV (GetUserProfileDirectoryW), bcrypt (BCryptEncrypt/GenerateSymmetricKey),
# WS2_32, kernel32 (CreateProcessW, OpenProcessToken), WINTRUST/CRYPT32 (in LineUpdater)

objdump -p "$BIN/LineUpdater.exe" | grep -E 'DLL Name|CreateNamedPipe|CreateFile|WriteFile|ReadFile|Pipe'
# 匯入 CreateFileW, WaitNamedPipeW, WriteFile, ReadFile, PeekNamedPipe, WINTRUST, CRYPT32, WS2_32, bcrypt, dbghelp
```

發現：
- LINE.exe 透過 `WebView2Loader.dll` 啟動 WebView2。
- LineConnector 用 WinRT `Windows.Management.Deployment` 註冊 MSIX 套件。
- LineUpdater 用 `WaitNamedPipeW` + `PeekNamedPipe` 走具名管道 IPC。

### 4.4 字串掃描

```bash
strings -n 4 "$BIN/LINE.exe" | wc -l            # 371016（多為 Themida 雜訊）
strings -n 4 "$BIN/LineConnector.exe" | wc -l   # 2449
strings -n 4 "$BIN/LineLauncher.exe" | wc -l    # 1899
strings -n 4 "$BIN/LineAppMgr.exe" | wc -l      # 41970（但被 Themida 加密）
strings -n 4 "$BIN/LineUpdater.exe" | wc -l     # ~30000（可讀）

# LineUpdater.exe 關鍵字串
strings -n 5 "$BIN/LineUpdater.exe" | grep -iE 'pipe|ticket|connectionid|http'
# → \\.\pipe\  /  PipeCommunicator  /  CommLib::connect_to_pipe
# → ticket_appdata  /  construct_stateful_ticket  /  construct_stateless_ticket
# → https://desktop.line-scdn.net/win/v2/real/update_info.json
# → https://linedt.line-objects-dev.com/win/v2/{rc,beta,debug}/update_info.json

# LineUpdater.exe 命令列旗標
strings -n 5 "$BIN/LineUpdater.exe" | grep -E '^--'
# → --update  --check  --check_nia  --deploy  --restore  --updated  --update_failed

# LineUpdater.exe PDB 路徑
strings -n 8 "$BIN/LineUpdater.exe" | grep -iE '\.pdb|jenkins'
# → D:\jenkins_agent\workspace\windows_desktop_updater_build\line-biz-common\src\library\CommLib\src\...

# LineConnector.exe build tag
strings -n 5 "$BIN/LineConnector.exe" | grep -i LINE
# → LINEDTISBESTTEAM

# Qt 版本
strings -n 10 "$BIN/Qt6Core.dll" | grep -E 'Qt 6\.[0-9]'
# → Qt 6.6.3 (x86_64-little_endian-llp64 shared (dynamic) release build; by MSVC 2022)

# OpenSSL 版本
strings -n 8 "$BIN/libcrypto-1_1-x64.dll" | grep -iE 'OpenSSL [0-9]'
# → OpenSSL 1.1.1n  15 Mar 2022
```

發現：LineUpdater 的 IPC 類別名稱 `PipeCommunicator`、函式 `CommLib::connect_to_pipe`、ticket 種類 `stateful`/`stateless`、更新檢查 URL 全部可讀。

### 4.5 MSIX 資訊清單

```bash
mkdir -p /tmp/line-msix && cd /tmp/line-msix
unzip -o "$BIN/LINE.msix"
# → AppxManifest.xml, AppxBlockMap.xml, [Content_Types].xml, AppxSignature.p7x
cat AppxManifest.xml | head -40
```

發現：
- `Identity Version="26.0.2.0" Name="LINE" Publisher="CN=LY Corporation, ..."`
- `Application Id="LINE" Executable="LineConnector.exe" uap10:TrustLevel="mediumIL" uap10:RuntimeBehavior="win32App"`
- `rescap:Capability runFullTrust`、`unvirtualizedResources`
- `windows.shareTarget` 支援一大串副檔名（`.3gp`、`.docx`、`.png`...）→ LINE 可接收系統「分享到 LINE」。
- `AppxBlockMap.xml` 只列 `AppxManifest.xml` 一個檔 → **MSIX 只是 manifest 容器，實際程式碼在 `bin/current/`**。

### 4.6 設定檔

```bash
DATA="$BIN/../Data"
cat "$DATA/LINE.ini" | head -40
# → [global] luuid=... language=en-US last_updated_version=26.0.2.3828
#   lrst="<base64 encrypted blob ~2 KB>"  acf=...
#   window_rect_AllInOneWindow=@Rect(578 268 742 806)
#   window_rect_profile\ProfileWindow.qml=@Rect(...)
#   window_rect_profile\GroupProfileWindow.qml=@Rect(...)
#   executed_plugin_version_LineMediaPlayer=1.2.0.635
#   executed_plugin_version_LineCall=1.0.0.897
#   lma=822B383DBCD0  lmah=fde625882cd290f63c9be6fdbd5aaa33
# 多個 [<mid>] 區段，mid 是 base64-like 使用者 ID
```

發現：`LINE.ini` 是主要設定／狀態檔；`luuid` 是本機識別碼；`lrst`/`acf` 是加密的登入憑證；`window_rect_*` 證實 QML 視窗命名。

### 4.7 資料庫目錄

```bash
find "$DATA/db" -maxdepth 3
# → qw<mid>.edb (+ -wal + -shm)  album_<mid>.edb  keep_<mid>.edb  chatStats_<mid>.edb
#   AutoSuggest/qw<hash>.edb  f59c00309acd23908b1254ee081d8258 (無副檔名，可能是索引)
file "$DATA/db/qwb217673f5ebec75203f608ead75c9b.edb"
# → ESE database（JET Blue format）
```

發現：LINE 用 **ESE（JET Blue）資料庫**，不是 SQLite。`-wal`/`-shm` 表示 WAL 模式。每個帳號一組資料庫。

### 4.8 執行中處理序觀察

```bash
ps aux | grep -iE 'line|wine' | grep -v grep
# → LINE.exe run -t 728211730
# → linemediaplayer.exe mode=0 connectionId=015c1a72-... encryptionKey=3246538db4dc13e55514ddb111216bdb  groupId=C:\...\LineLauncher.exe ... mse
# → linemediaplayer.exe mode=2 connectionId=a93508dc-... encryptionKey=<同上> ...
# → linecall.exe 04d3439f-15c5-4409-b6b0-9d7d2a6867bc  C:\...\LineLauncher.exe  26.0.2.3828  non_appstore
# → wineserver, winedevice.exe (×2)
```

發現：插件以**獨立進程**執行，由 `LineLauncher.exe` 啟動，命令列帶 `connectionId`（UUID）、`encryptionKey`（32 hex）、`groupId`（launcher 路徑）。`encryptionKey` 在同一 session 內所有插件共享同一把。

### 4.9 Ticket 鎖定檔

```bash
ls ~/.var/app/com.usebottles.bottles/data/bottles/bottles/LINE/drive_c/users/$USER/AppData/Local/Temp/Ticket_*.lock
# ~20 個 63-byte 鎖定檔
cat ".../Temp/Ticket_015c1a72-444c-426f-8e64-951e1412e655.lock"
# → 328\nLINE\nGROK-BOT-VM-371\n9f273b2c-0301-4b63-aba7-290eb35205e0\n
```

發現：Ticket 檔 = IPC 連線中繼資料（Windows 側 PID + 進程名 + 主機名 + session UUID）。UUID 與插件的 `connectionId` 吻合 → **Ticket 是 IPC 連線的鎖定／身分檔**。

### 4.10 LINE.exe 開啟的檔案描述子

```bash
ls -la /proc/1373692/fd/ | wc -l    # 191 個 fd
ls -la /proc/1373692/fd/ | grep -E 'socket|pipe' | head -30
# → 多個 socket:[174178526], [174739966], [174186714]
# → 數十個 pipe:[...]
# → 多個 Ticket_*.lock 檔
# → 多個 .edb / .edb-wal / .edb-shm 資料庫檔
# → ~/.var/app/.../Temp/Ticket_<UUID>.lock
```

發現：LINE.exe 同時開啟 3 個 socket（含 1 個 TCP `127.0.0.1:35027 -> 127.0.0.1:33230`，推測為 WebView2 子進程通訊）+ 數十個 pipe（Wine 內部具名管道對應的 Unix pipe）+ 所有 ESE 資料庫檔 + 所有 Ticket 鎖定檔。

### 4.11 X11 視窗結構

```bash
# LINE.exe 的環境顯示為 DISPLAY=:3
cat /proc/1373692/environ | tr '\0' '\n' | grep DISPLAY    # → DISPLAY=:3
DISPLAY=:3 xwininfo -root -tree | head -40
# → 0x3000001 (line.exe) 1434x868+3+29  主視窗
# → 0x3000004 "QTrayIconMessageWindow" (line.exe)  系統匣
# → 0x30000dd / 0x3000040 "LINE" (line.exe)  小視窗
# → 0x1401660 () 742x806+578+268  16 children  主客戶區
# → linemediaplayer.exe / linecall.exe 各有 1434x868 視窗
DISPLAY=:3 xdotool search --class line.exe
# → 多個視窗 ID（可用 xdotool 操控）
```

發現：**X11 UI 自動化可行**。LINE 主視窗可被 `xdotool`/`xwininfo` 找到並操控。QML 子項不會出現在 X11 樹（只有頂層 Qt 視窗）。

### 4.12 Wine 執行日誌

```bash
tail -50 /workspace/line-bottles/line-run.log
# → DXVK Vulkan 渲染管線資訊
# → "RoGetActivationFactory Failed to find library for Windows.UI.Notifications.ToastNotificationManager"（Wine 不支援 Toast）
# → "ALSA lib pulse_connect: Connection refused"（音效無法連 PulseAudio）
# → Buffer size: 288x512（splash 視窗）
```

發現：Wine 下用 DXVK/Vulkan 渲染 Qt Quick；Toast 通知與 PulseAudio 連線失敗（不影響聊天功能）。

### 4.13 使用者的安裝腳本

```bash
cat /workspace/line-bottles/README.md
# → 安裝步驟、版本資訊、降級原因、反自動更新策略
cat /workspace/line-bottles/run-line.sh
# → flatpak run --command=bottles-cli com.usebottles.bottles run -b LINE -p LINE
cat /workspace/line-bottles/kill-line.sh
# → pkill -f -i 'AppData.Local.LINE' + wineserver
cat /workspace/line-bottles/env.sh
# → export DISPLAY=:10（注意：實際 LINE 跑在 :3，env.sh 是預設值）
```

發現：使用者已建立完整 Bottles 啟動／停止腳本；`env.sh` 預設 `DISPLAY=:10` 但實際 LINE 跑在 `:3`（因為 Bottles/flatpak 可能改寫了 DISPLAY）。

---

## 5. 需要使用者接下來提供／確認的事項

### 5.1 立即可做（不需額外授權）

1. **確認目標場景優先順序**：是要 (a) 發訊、(b) 讀訊、(c) 開聊天室、(d) 接收新訊息事件、(e) 服務分頁自動化？這決定先實作哪條路徑。
2. **確認可接受的副作用**：UI 自動化期間 LINE 視窗需可見且會被滑鼠／鍵盤搶走焦點，是否可接受？若不行，需改走注入路徑（成本高很多）。
3. **確認是否可掛鉤／注入**：路徑 C（Qt injection）需在 Wine 內載入自製 dll 到 LINE.exe 進程。使用者是否同意？這不會碰加密或他人流量，但會修改 LINE 進程記憶體。

### 5.2 進一步調查需要

4. **維持 LINE 26.0.2.3828 執行中**：許多動態分析（IPC 監看、QML 物件樹列舉、X11 視窗結構）需要 LINE 在跑。請先 `bash /workspace/line-bottles/run-line.sh`。
5. **提供 IDA Pro / Ghidra 授權（選用）**：若要深入逆向 `PipeCommunicator` 協定或 QML 物件結構，需反組譯器。免費替代品：**Ghidra**（已可在 Linux 跑）足夠。**不需要** IDA Pro 授權即可進行。
6. **同意安裝額外套件**（選用）：
   - `libesedb-python`（解析 ESE 資料庫，唯讀歷史訊息）。
   - `python3-xlib`、`python3-Pillow`、`python3-opencv`（X11 自動化 + 圖像比對）。
   - `python3-pyautogui`（更高階的 X11 自動化）。
   - `wine-staging` 的 `winedbg`（若要做注入／掛鉤除錯）。

### 5.3 風險確認

7. **帳號風險認知**：UI 自動化（路徑 A/B）對 LINE 伺服器來說與真人操作無異，被停權風險極低，但仍應避免短時間大量發訊（可能觸發反垃圾訊息）。請確認可接受的發訊頻率上限。
8. **唯讀資料庫存取認知**：路徑 F（ESE 唯讀）不會影響 LINE 運作，但需先複製 `.edb` 檔再解析（避免 ESE 鎖定衝突）。請確認可接受偶爾複製資料庫檔。

### 5.4 不需要的事項

- **不需要** IDA Pro 授權（Ghidra 即可）。
- **不需要** 手機 LINE 的 authKey/certificate（本報告不碰 LINE 伺服器協定）。
- **不需要** 升級到 26.5（26.5 在 Wine 下簽章驗證失敗，且 26.0.2 已足夠）。
- **不需要** 關閉 Themida（不需要解密 LINE.exe 本體；Qt dll 未加密即可呼叫）。

---

## 6. 結論與建議下一步

### 6.1 已確認的事實

1. LINE Desktop 26.0.2 是 **Qt 6.6.3 Quick/QML** 應用，渲染走 DXVK/Vulkan，網頁內容用 **WebView2**。
2. 主程式 `LINE.exe` 被 **Themida** 加密，但所有 `Qt6*.dll` **未加密**，可正常解析符號。
3. 插件（LineMediaPlayer、LineCall）以**獨立進程**執行，與主程式透過 **Windows 具名管道 `\\.\pipe\`** 做 IPC，每條連線帶 `connectionId`（UUID）+ `encryptionKey`（32 hex）+ `Temp/Ticket_<UUID>.lock` 中繼檔。
4. 訊息資料庫是 **ESE（JET Blue）`.edb`**，不是 SQLite。
5. LINE 正在執行時，X11 視窗可被 `xdotool`/`xwininfo` 找到並操控（DISPLAY=:3）。
6. IPC 類別 `PipeCommunicator`、函式 `CommLib::connect_to_pipe`、ticket 種類 `stateful`/`stateless` 等字串在 `LineUpdater.exe` 中完全可讀。

### 6.2 建議優先順序

1. **第一步（1–2 天）**：實作路徑 A（X11 + xdotool + 圖像比對）的 PoC：
   - 用 `xdotool search --class line.exe` 找主視窗。
   - 用 `xdotool type` + `xclip` 貼上訊息，`xdotool key Return` 送出。
   - 用 `ffmpeg -f x11grab` 截圖 + OpenCV 找按鈕座標。
   - 目標：能對指定聊天室發一則文字訊息。
2. **第二步（3–5 天）**：加路徑 F（ESE 唯讀）讀歷史訊息：
   - 用 `libesedb-python` 解析 `qw<mid>.edb`。
   - 目標：dump 出某聊天室最近 N 則訊息。
3. **第三步（1–2 週，選用）**：路徑 C（Qt injection）做穩定的事件掛鉤：
   - 寫一個小 dll，用 `Qt6Qml.dll` 匯出函式列舉 QML 物件。
   - 在 Wine 下用 `CreateRemoteThread` 注入。
   - 目標：hook「收到新訊息」signal，觸發外部腳本。
4. **不建議**：路徑 D（仿 IPC 協定）與路徑 E（非官方 API），成本/風險比遠高於 UI 自動化。

### 6.3 待釐清的疑點（需動態分析）

- `\\.\pipe\<實際管道名稱>` 的完整格式（需用 Wine 的 `wine server` 內部狀態或 `Process Monitor` 觀察）。
- `encryptionKey` 的加密演算法（AES？HMAC？需逆向 `PipeCommunicator`）。
- QML 物件樹的實際命名（需注入後用 `QObject::findChildren` 列舉）。
- ESE schema（需用 `libesedb` dump 後比對欄位）。

---

## 附錄 A：關鍵檔案路徑速查

```
安裝根                  ~/.var/app/com.usebottles.bottles/data/bottles/bottles/LINE/
主程式                  .../drive_c/users/$USER/AppData/Local/LINE/bin/current/LINE.exe
設定檔                  .../drive_c/users/$USER/AppData/Local/LINE/Data/LINE.ini
訊息資料庫              .../drive_c/users/$USER/AppData/Local/LINE/Data/db/qw<mid>.edb
QML 快取                .../drive_c/users/$USER/AppData/Local/LINE/Cache/qmlcache/*.qmlc
IPC Ticket 鎖定檔        .../drive_c/users/$USER/AppData/Local/Temp/Ticket_<UUID>.lock
Wine 執行日誌           /workspace/line-bottles/line-run.log
啟動腳本                /workspace/line-bottles/run-line.sh
停止腳本                /workspace/line-bottles/kill-line.sh
```

## 附錄 B：執行時關鍵環境變數

```
DISPLAY=:3                              # LINE 實際跑在這個 X 顯示
WINEPREFIX=.../bottles/LINE             # 由 Bottles 管理
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS  # 可用來開 WebView2 DevTools 連接埠（選用）
```

## 附錄 C：本報告未做但可進一步驗證的事項

- 用 `winedbg` 附著到 LINE.exe，列舉模組、找 Qt QML engine 物件位址。
- 用 `strace -f -e trace=network,ipc` 追蹤 LINE.exe 的 IPC／網路系統呼叫。
- 用 `tcpdump -i lo` 抓 WebView2 子進程的 localhost TCP 流量（會看到 CDP 流量若開了除錯）。
- 用 `inotifywait` 監看 `Data/db/` 目錄，看新訊息到達時哪些檔案被改寫。
- 用 `libesedb-python` 實際 dump 一個 `.edb` 看 schema。
