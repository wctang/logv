# TODO — logplot MCP server（外部 agent 控制介面）

狀態：**Phase 1 完成並上線**（2026-10-08；omp 已接線，實測通過）。**Phase 2 唯讀工具**（`list_files` / `list_series` / `find_changes` / `get_window`）已上線。**Phase 2.5 計畫修訂**（2026-10-09：suggest.md 兩輪評價 × 五份真實 log 實測）已成文，工項 M1–M4 / W1–W11 待做。

目標：讓外部 agent（omp）透過 MCP 直接驅動**執行中**的 logplot GUI —— 載入 log、查值、繪圖、跳時間點。不在 UI 內養對話框、不管 LLM key。

## 已定案的決策

| 項目 | 決定 | 理由 |
|---|---|---|
| 傳輸 | **MCP Streamable HTTP**，只 bind `127.0.0.1:8790` | 要驅動使用者眼前那個 GUI 實例；stdio 由 client 另啟子行程，會變成第二個無 GUI 的 logplot |
| 依賴 | **零新依賴**（stdlib `http.server` / `json` / `threading`）| PEP 723 inline block 與 PyStand runtime 是兩套世界，加 `mcp` SDK 要同步兩處，且 pydantic-core/httpx/anyio/uvicorn 都得進 `build.py` 的打包分流 |
| 程式碼位置 | **全部留在 `logplot.py` 內** | `update_script()` 只替換 `sys.argv[0]`，另開模組不會被更新機制帶到 |
| 執行緒 | HTTP 跑 daemon thread；工具呼叫 marshal 回 GUI thread | `self.series` / `self.model` / ViewBox 只能在 GUI thread 觸碰 |
| 埠預設 | **啟動即開 8790**，`--mcp-port PORT` 換埠、`--mcp-port 0` 關閉 | 使用者慣用的啟動方式即可用，不必每次記旗標 |
| 認證 | 可選 Bearer token：`--mcp-token` 或 `$LOGPLOT_MCP_TOKEN`；**不寫進任何檔案** | repo 公開；`%TEMP%` 的探索檔是世界可讀的 |
| 破壞性工具 | **永久不開放** | `remove_all_files` / `remove_file` / `save_state` / `load_state` / `update_script` |
| 唯一契約 | **只假設 CSV 與第一欄是 timestamp**；不得內建任何 payload 文法或值語意 | 實務上格式與形態繁多（見 Phase 2.5 五檔實測）；依「看過的 log 內容」寫死的解析，下一批 log 就失效 |
| 語意來源 | 使用者載入的 SignalList（`display_name`）或呼叫端參數；伺服器只回結構事實 | 同上 |
| `kind` 的角色 | **僅描述 viewer 怎麼畫**，不作為輸出策略的依據；不以基數自動重判 enum / code | `AI_W1`=〔0,1,16,17,256,272,273〕、`SVOHR1`=〔0,18615…18623〕與 `mpc_ctrl_state`=〔1,2,7,8,9,10,12〕結構上不可分 |
| headless / stdio 模式 | **不做**（2026-10-08 決定） | 需求是驅動 GUI，不是無頭批次 |
| omp 設定位置 | **用戶層 `~/.omp/agent/mcp.json`** | 任何 cwd 都生效；不動公開 repo 的檔案 |
| 已放棄路線 | UI 內建對話 agent（openai SDK + `QDockWidget` + 本機 llama.cpp） | 需在 UI 養對話框、管 key、加依賴；MCP 路線同樣能達到目的且零依賴 |

## 既有程式碼的硬限制

1. **單檔 + 自動更新**：新功能必須寫進 `logplot.py`。
2. **依賴有兩套世界**：PEP 723 inline block（`uv run` 用）與 `build.py` 打包的 `D:\log_tools\runtime\Lib\site-packages`（PyStand 用）必須同步。目前兩邊都只有 PySide6 / numpy / pandas / zstandard（runtime 無 pyarrow，`load_csv_files` 會自動退回 C engine）。
3. **Qt 物件只能在 GUI thread 觸碰**：`self.series`、`self.model`、`main_vb` 皆為主執行緒狀態。
4. **不要掛進高頻路徑**：`mouse_moved`（:1427）→ `update_crosshair`（:1435）每次滑鼠移動都跑，agent 狀態更新不得插入此路徑。

## 架構

```mermaid
flowchart LR
  OMP["omp<br/>mcp__logplot_*"] -->|"POST /mcp (HTTP)"| SRV["McpHttpServer :2605<br/>daemon thread"]
  SRV -->|"bridge.call_requested<br/>Signal(object), queued"| BR["McpBridge :2209<br/>GUI thread slot"]
  BR --> TOOLS["tools dict<br/>(name → fn)"]
  TOOLS --> APP["CSVPlotViewer<br/>series / model / ViewBox / dataframes"]
  BR -.->|"job.event.set()"| SRV
```

- 常數：分類門檻 `logplot.py:47-48`、MCP 預設 `:53-59`。放在 `VERSION` 之後是必要的——`--mcp-port` 的預設參數在 class 定義時求值。
- `SeriesState.kind` :119（快取的分類）、`_json_value` :2182 與 `_segment_starts` :2191（時間視窗表示法）、`McpBridge` :2209 / `McpHttpServer` :2605。
- `CSVPlotViewer.start_mcp_server()` :2103 負責生命週期，`main()` :2754 解析 `--mcp-port` / `--mcp-token`，在 `window.show()` 後啟動。

## 已實作

| 項目 | 實作 |
|---|---|
| 協定 | 手寫 JSON-RPC 2.0 的 MCP Streamable HTTP 子集：`initialize`（echo client 版號，支援 2024-11-05 / 2025-03-26 / 2025-06-18）、`notifications/initialized`（202 空 body）、`ping`、`tools/list`、`tools/call`。`GET /mcp` 回 405（讓 client 靜默停用背景 SSE listener）；不做 SSE 串流 |
| 執行緒 | HTTP daemon thread；`_McpJob(name, args, event, result)` 經 `McpBridge.call_requested`（`Signal(object)`，跨執行緒自動 queued）送進 GUI thread，HTTP thread 以 `Event.wait(60s)` 等結果；逾時回 `isError` |
| 認證 | 有 token 時檢查 `Authorization: Bearer`，不符回 401 |
| 探索 | 啟動時寫 `%TEMP%\logplot-mcp.json`（`url` / `port` / `auth` / `pid`，**不含 token**），`closeEvent` 時刪除 |
| 開放的唯一工具（Phase 1） | `load_files`（idempotent：已載入的檔案不重讀、不長出重複樹節點；非字串路徑列進 `failed` 而非靜默忽略）；Phase 2 之後另有 4 個唯讀工具，見「工具清單」 |
| UI 回饋 | Help 選單顯示 `MCP Server: 127.0.0.1:8790 (token required / no auth)`；狀態列訊息；使用說明（F1）有對應段落 |

omp 端（用戶層 `~/.omp/agent/mcp.json`）：

```json
"logplot": {
  "type": "http",
  "url": "http://127.0.0.1:8790/mcp",
  "timeout": 120000,
  "enabled": true
}
```

`timeout` 用 120 s（預設 30 s 太短：載入大型 `.zst` 走 C engine 會超過 30 s），且必須大於 server 端 60 s 的 GUI 等待上限，才會由我們回報明確錯誤而不是 omp 端先斷線。

### 驗證紀錄

- 協定層：401（無 token）、initialize / ping / tools/list / tools/call、未知方法 `-32601`、壞 JSON 400 `-32700`、`GET /mcp` 405、`closeEvent` 後埠停止並刪除探索檔 —— 全部對部署環境 `D:\log_tools\runtime\python.exe` (3.13.3) 實測。
- 端到端：`OMP_MCP_REQUIRE_READY=1 omp -p --no-session --mode json "…"` → omp 在 discovery 階段連上，工具以 `mcp__logplot_load_files` 掛載（`details.xdev.inner.serverName == "logplot"`），回傳的是執行中 GUI 的真實狀態。
- 真實資料：`log-20260101.log_mpc_ctrl_105.csv.zst` → 328,746 列 × 125 欄，2025-12-31 23:00:03 ~ 2026-01-01 20:29:03 (UTC+8)。
- 分類（`kind`）：邊界逐一驗證 —— 字串 14 → `enum`、15 → `text`；數值 2 → `enum`、3 → `analog`；全 NaN → `enum`。8 個 fixture 勾選後的實際落點（`second_vb` / `main_vb` / 不畫）與 `kind` 全部相符，字串 `enum` 才建立 `text_mapping`。真實 125 欄檔案 `kinds` 分佈 enum 73 / analog 51 / text 1，與另一支獨立量測的 distinct 計數一致。
- Phase 2 時間視窗：26 列的 fixture 逐項驗證 —— 階梯訊號的變化清單 `[[t0,0],[t0+3,1],[t0+7,2]]`、單樣本/空視窗/不落在樣本上的視窗、全空白欄回 `[[t0, null]]`、物件欄的連續空白收斂成一段、`max_points` 觸發 `dense`、字串 `dense` 的值分佈含空白且正確截斷、`find_changes` 的逐欄次數（含 `limit` 截斷）與四種參數錯誤訊息。所有 payload 都用**拒絕 `NaN`/`Infinity` 的嚴格 JSON parser** 解析過。
- Phase 2 真實檔案交叉比對：`find_changes` 全檔（328,746 樣本 × 125 欄）**729 ms / 10.5 KB**、1 小時 **37 ms**；`unchanged_series = 34` 與獨立量測一致，`alarm_data` 47、`mpc_ctrl_t_play` 280,140、`t_elapsed` 280,140、`pos_1` 257,490、`mpc_ctrl_mode` 333、`st_1` 922 全部吻合。`get_window` 1 小時：`mpc_ctrl_mode` 18 段 / 0.82 KB / 1.9 ms、`mpc_ctrl_cali_1` 1 段、`alarm_data` 1 段（整小時空白）、`t_elapsed` 走 `dense`（15,074 段略去，0.50 KB）。
- 這輪在真實資料上抓到兩個 fixture 沒涵蓋的缺陷：物件欄的 NaN 被當成「每樣本都變」（`alarm_data` 誤報 324,428 次，實際 47 次）、以及 `to_numpy()` 把 4,100 萬個元素裝箱成 object 陣列導致全檔掃描要 2.7 s（改為逐欄 `shift()` 比較後 0.73 s）。
- Phase 2 工具：`list_files` / `list_series` 以四種檔案寫法（完整路徑 / basename / 反斜線 / 全小寫）呼叫皆命中；未知檔回錯誤並列出已載入檔案；400 欄不截斷、500 欄截斷成 400 並標 `truncated`；`plotted` / `labels` / `text_tracked` 以臨時腳本驅動真實 `CSVPlotViewer` 驗證（數值曲線、2 值的分類字串、20 值的字串只追蹤不繪圖、改名後的 label、空欄位省略）。
- 抖動量化（suggest.md 痛點 3）：`servo_alarm_code_2` 整檔的精確變化清單是 **496 段 / 13.19 KB**（報告估「約 450 筆」，實測 496），`min_duration=60` → 160 段 / 4.50 KB、`min_duration=300` → 26 段 / 1.00 KB。
- A 組（suggest.md 回饋）：26 列 fixture 逐項驗過 `file` 省略／完整路徑／basename 三者一致、多檔時要求 `file`、`transitions`（`from=null` 起始、`held` 由下一段推出、最後一段算到 `t2`）、`min_duration`（含 1.0 s 邊界、去抖後同值段合併、`segments_before`）、`drop_dominant`（`dominant` 與偏離區間、搭配 `transitions` 時報錯）、`dense` 數值分布、ISO 輸入（本地／UTC／帶 `Z`）與 `time_format=iso`，以及 6 種參數錯誤訊息。時間欄位一律斷言為 JSON 數字 —— 這抓到 numpy 純量會被 HTTP 層的 `default=str` 轉成字串的 bug。
- 用 suggest.md 的案例端到端複驗：`log-htb-20260101.log_mpc_ctrl_101.csv.zst` 的 07:36–07:41 窗，`get_window(mpc_ctrl_mode, mode=transitions, time_format=iso)` 一次呼叫（1 ms / 0.90 KB）即重現報告中的中止 A 與 B（`playing → halt` @ 07:37:20.308 held 27.388927 s；@ 07:40:21.972 held 28.651019 s）—— 報告是肉眼比對 348 筆 segment 得出的。`drop_dominant` 對 `mpc_ctrl_state` 在同一窗直接給出 6 段偏離區間（正好是兩次 playing→halt→parking）。`find_changes` 整檔 `unchanged_columns` = 31 條，與報告數字一致。

## 工具清單（每項對應既有 method）

| 工具 | 接到哪裡 | 狀態 |
|---|---|---|
| `load_files(paths)` | `load_csv_files`（:927） | ✅ Phase 1 |
| `list_files()` | `dataframes` → 逐檔 rows / columns / start / end | ✅ Phase 2 |
| `list_series(file?)` | `dataframes` + `series` + `active_text_series`：名稱分 numeric / string，附 `plotted` / `text_tracked` / `labels` / `kinds`（不回數值） | ✅ Phase 2 |
| `find_changes(t1, t2, file?, limit?, time_format?)` | 逐欄 `shift()` 比較，回有變化的訊號與次數（忙的在前）+ 未變動的欄名；篩選層 | ✅ Phase 2 |
| `get_window(file?, column, t1, t2, max_points?, mode?, min_duration?, drop_dominant?, time_format?)` | 變化清單（精確 RLE）／`transitions`／`deviations`，超出預算轉 `dense` | ✅ Phase 2 |
| `search_text(q)` | `search_input.setText` + `search_global_text()`（:1923） | Phase 2 |
| `get_view_range` | `main_vb.viewRange()` | Phase 2 |
| `plot_series` / `unplot_series` | `column_item.setCheckState()` → `on_item_changed`（:1056） | Phase 3 |
| `zoom_to(t1, t2)` | `main_vb.setXRange` + 自動算 y 範圍 | Phase 3 |
| `compute_series(file, col, mode)` | `create_computed_series`（:1321，`1` / `2` / `"abs"`） | Phase 3 |
| `set_timezone(off)` | `set_timezone_offset`（:821） | Phase 3 |
| `measure_delta` | `measure_line{1,2}.value()`（`update_measure_label` :1754） | Phase 3 |
| `screenshot_plot` | `plot_widget.grab()` → MCP image content（僅 vision 模型有用） | Phase 3 |

### 工具參數慣例

（本節與上表是 Phase 2 前的規劃；Phase 2.5 的 W1–W11 是其超集，衝突時以 Phase 2.5 為準。）

- 檔案識別一律用 `file`（值為 `list_files` 回報的完整路徑），輸出也用 `file` 當鍵。
- `McpBridge._resolve_file()` 依序嘗試：完全相符 → `normcase(abspath())` 正規化比對（大小寫 / 斜線差異可通過）→ 唯一 basename；都不中就報錯並列出已載入的檔案。
- 訊號識別一律用 `column`，值取自 `list_series` 的 `numeric_series` / `string_series`。
- `list_series` 每檔上限 `MCP_MAX_SERIES_PER_FILE`（400）個名稱，超過時 `truncated: true` 並附 `series_count`；空欄位（`text_tracked` / `labels`）直接省略。
- 時間一律 Unix 秒（UTC, float）。
- `kinds` 是 viewer 自己的繪圖分類，由 `SeriesState.kind` 單一來源計算並記憶：`enum`（右軸車道，distinct 少）／`analog`（主軸）／`text`（自由文字，只追蹤不畫）。門檻為 `STRING_TEXT_THRESHOLD`(15) 與 `NUMERIC_ENUM_THRESHOLD`(3)，兩個數值不再內嵌於勾選路徑。
- 注意：`list_series` 第一次讀一檔會替每欄做一次全欄 distinct 計數（125 欄實測約 0.5 s，在 GUI thread），之後由 `SeriesState` 快取（1.4 ms）。勾選路徑因此不再重算。
- **時間視窗的表示法（`find_changes` / `get_window` 共用）**：變化清單 `segments: [[t, value], …]`，值的有效範圍**到下一段為止**（最後一段到 `t2`；Phase 2.5 W7 將在 `t2` 超過資料末筆時夾限到資料末筆並標 `held_truncated`），頭尾相連故不重複 `t_end`。區間是雙端閉區間；`t ≥ 1e11` 沿用 `search_by_timestamp` 的規則視為 ns。
- **`get_value` 已決議不做**：單點查值對診斷沒有意義（人看的是變化），改由 `find_changes`（篩選）+ `get_window`（變化清單）覆蓋。→ Phase 2.5 覆議點 1：W3 的多欄快照（`t1 == t2`）建議覆蓋此決議，因為實務需求是「某一瞬間 N 條訊號」。
- **空白（NaN）對空白算「沒變」**：物件欄與數值欄都一樣。`_segment_starts()` 用 `pd.isna` 做這個遮罩；輸出時 NaN 一律轉 `null`（`_json_value`），確保 payload 是合法 JSON（不含 `NaN` 字面值），數值為 0 或空陣列時也不會誤判。
- **`file` 可省略**：只載入一個檔案時 `get_window` 不必帶 `file`；`file` 也接受唯一 basename（`_resolve_file(..., allow_default=True)`）。多檔時才回錯並列出已載入檔案。
- **四種 `mode`**：`segments`（連續變化清單，預設）／`transitions`（`[t, from, to, held_seconds]`，起始狀態的 `from` 為 `null`）／`deviations`（`drop_dominant` 觸發，`[t_start, t_end, value]`，因為已不連續所以端點必須明寫）／`dense`（超預算摘要）。四者共用同一個預算規則：**任何形式裝不下 `max_points` 就轉 `dense`**（`max_points` 預設 `MCP_MAX_WINDOW_POINTS`(500)、上限 `MCP_WINDOW_POINT_CAP`(5000)）。`dense` 的數值欄在 distinct ≤ `MCP_MAX_DISTINCT_VALUES`(20) 時也會附 `values` 分布（= 逐值出現次數；是否為代碼由呼叫端判讀，見 Phase 2.5）。
- **`min_duration`（去抖動）**：短於 N 秒的區段直接丟棄、由前一段延伸覆蓋，去抖後相鄰同值再合併（`_merge_equal_runs`），並回報 `segments_before` 以示透明。`drop_dominant` 只保留「佔用時間最長的值」以外的區間。
- **時間格式**：`t1` / `t2` 接受 Unix 秒或 ISO 8601（naive 字串用 viewer 時區解讀）；`time_format: "iso"` 讓回應的時間全部變成帶偏移的 ISO 字串（毫秒先四捨五入再格式化，避免看起來差 1 ms）。回應一律附 `tz`（如 `+08:00`）。
- **`find_changes` 的 `unchanged_columns`** 只在單一檔案時提供（多檔時裸欄名有歧義），並受 `limit` 上限。
- **不採納「呼叫端覆寫 `kind`」**（suggest.md 建議）：實測後確認 `kind` 在 `get_window` 內只影響 metadata，dense 分支是依 `dtype` 判斷，而低基數數值的分布已由 `dense` 的 `values` 提供，所以覆寫參數會是個沒有實際作用的多餘欄位。（Phase 2.5 W2 進一步把 `kind` 排除於輸出政策之外；兩個門檻值不變。）

## Phase 2.5 計畫修訂（2026-10-09）

依據：suggest.md 兩輪評價 × 五份真實 log 實測。行號為 2026-10-08 版 `logplot.py`，以方法名為準。

### 設計原則

只有兩件事可以假設：**CSV 格式、第一欄是 timestamp**。其餘一律當未知——欄數可為 1、欄名任意、值是數值／字串／`[]`／hex／JSON-ish／空白、單位與取樣率未知、任何 payload 文法未知。

伺服器只回答**結構事實**：dtype 家族、相異值數、變更位置與次數、取樣間隔、時間範圍。**語意**（`12` 是什麼、`AI_W1` 是 bitfield、`LOG` 那串字的意義）只能來自呼叫端，或使用者自己載入的 SignalList。

### 撤回的前一版構想

| 構想 | 處置 | 實測理由 |
|---|---|---|
| `alarm_data` 內建 `(i:.. o:.. s:.. m:..)` 解析 | **撤回** | 那是對特定 log 內容的猜測；改由呼叫端傳 pattern 的 `find_text`（W6'）取代 |
| 依基數把低基數整數自動改判 enum / code | **撤回** | 反例見「已定案的決策」的 `kind` 列 |
| 把「同變化位置」的配對叫 `codes`（碼表） | **降級為中性事實** | `mpc_ctrl_orig_1/2`、`GT1/GT3_2`、`SVOST1*`（49 欄同時變）同時間但值不同；只有值序列也相同才是同一訊號 |

保留的推論只有「變化位置完全相同 ⇒ 時序同步」，這是結構事實。

### 五份 log 的結構實測（驗收基準）

| 檔案 | 資料欄 | 列數 | timestamp 分支 | 中位取樣 | 最大間隙 | 相異值 ≤2 的欄 | 同變化位置群組 | 值也相同的可去重名 | 從不變的欄 |
|---|---|---|---|---|---|---|---|---|---|
| `log-20260814_mpc_104.csv.zst` | 205 | 138 470 | `%Y-%m-%d %H:%M:%S.%f` | 1.000 s | 1.0 s | 139 | 20 群 / 127 欄 | 41 | 60 |
| `osman_cakir_log-20260830_mpc_104.csv.zst` | 178 | 256 906 | 同上 | 0.200 s | 2 025 s | 130 | 7 群 / 87 欄 | 21 | 49 |
| `log-fujiq-20260706_mpc_101.csv.zst` | 210 | 146 760 | 同上 | 0.200 s | 1.0 s | 161 | 10 群 / 92 欄 | 36 | 44 |
| `log-20260101.log_msg_105.csv.zst` | 1（純文字，45 273 相異值） | 287 960 | 同上 | 0.100 s | 22 462 s | 0 | — | — | 0 |
| `log-htb-20260101.log_mpc_ctrl_101.csv.zst` | 124 | 337 010 | 同上 | 0.100 s | 22 481 s | 67 | 15 群 / 61 欄 | 46 | 2 |

- 五檔皆 monotonic、**重複時間戳 = 0**；全數命中同一 format 分支（是清單巧合，同時也是風險，見 W10）。
- 結構掃描成本：相異值 0.07–0.51 s／檔、變化位置簽章 0.12–0.24 s／檔。
- 文字欄「窗內值先去重再比對」：45 273 個相異值 / 0.027 s。
- FSM 判準複驗（`mpc_ctrl_mode`）：101 檔 349 段 / 14 種 `(from,to)` / playing 78 場、3 場 `→halt`、正常 350.260–351.457 s；105 檔 334 段 / 76 場、3 場、349.852–351.847 s；三筆中止時間與 suggest.md 一致至毫秒。

### 工項

| # | 工項 | 目標 | 驗收 |
|---|---|---|---|
| W1 | `list_series detail=true` | 事實層：`distinct` / `changes` / `constant` / `duplicate_of`（值序列也相同）/ `same_timing_as`（僅時間同步）/ `value_pairs`（時間完全同步且映射為函數，中性命名、≤32 筆） | 五檔皆 <0.3 s；200+ 欄檔要標出 44–60 欄「從不變」群組 |
| W2 | `kind` 解耦 | `segments` / `transitions` / `dense` 只依窗內段數與 `max_points` 決定，不依 `kind`；schema 明寫 `transitions` 對任何欄可用；`list_series` 附 `kind_reason`（distinct=N / 門檻） | `servo_alarm_code_1/2`（值與變化位置相同）走同一路徑；門檻值不動 |
| W3 | `get_window` 多欄與快照 | `columns=[...]`；`t1 == t2` → `mode="snapshot"`（每欄最後一個 ≤t 的樣本） | 中止 A（07:37:20.308）一次呼叫列出 mode / state / t_play / run / st / en / alarm_data |
| W4 | `mode="summary"` | `group_by="transition"`（from→to 次數、held min/median/max）與 `group_by="value"`（各值佔用時間與次數） | `mpc_ctrl_mode` 全檔 14 組、`playing→halt` = 3、正常場長與三筆中止時間同 suggest.md |
| W5 | `mode="aggregate"` | `every=N` + `agg=[min,max,last,first]`，dense 時附 `buckets` | `t_play` 5 分鐘窗一眼看出爬到全長後歸零 |
| W6' | `find_text` | 呼叫端傳 `pattern`（預設 literal，可切 regex）/ `ignore_case` / `columns?` / `t1,t2` / `limit`；回每欄命中數、前 N 筆 `[t, column, value]`、去重命中清單 | `find_text("Halt command received")` 在 htb 101 的 `alarm_data` 與 `log_msg_105` 的 `msg` 都命中；**伺服器內不含任何 `(i:o:s:m:)` 或 `I:stop:` 知識** |
| W7 | 家務與語意修正 | `unchanged_columns` 改 opt-in（永遠回計數）；`held_seconds` 末端夾限到 `min(t2, 資料末筆)` 並標 `held_truncated`；`min_duration` 描述改為「segments 與 transitions 皆適用」；標 `window_too_small` | `t2` 超過資料末筆時 `held_seconds` 不再隨之膨脹 |
| W8 | 背壓 | 待處理 > 8 筆回明確 `server busy`（工具本來就在 GUI thread 序列化） | 併發 73 個呼叫得到可讀錯誤，而非無聲中止 |
| W9 | viewer 控制（Phase 3） | `plot_series` / `unplot_series` / `zoom_to` / `highlight_range` / `search_text` / `get_view_range` / `compute_series` / `set_timezone` / `measure_delta` / `screenshot_plot` | 算出的異常點能在使用者眼前框出來 |
| W10 | 載入透明度 | `list_files` / `load_files` 每檔附 `timestamp_column` / `timestamp_parse`（命中哪個分支）/ `monotonic` / `duplicate_timestamps` / `sampling_median_s` / `largest_gap_s` / `constant_columns`；`failed.reason` 帶真正例外；解析後範圍檢查（1990–2100），歧義時標 `timestamp_ambiguous` | 兩份新 log 的 `sampling_median_s` 回 1.0 / 0.2 s、`largest_gap_s` 回 1.0 / 2 025 s |
| W11 | 契約測試 | 五檔 smoke（含 2 欄純文字檔與 206 欄檔）＋「只有 timestamp 一欄」的合成 CSV | 單欄檔得到清楚錯誤而非靜默丟棄（現行 `load_csv_files` `:978` 的 `df.shape[1] < 2` 直接 `continue`） |

實作要點：

- 接觸點：`SeriesState._classify`（:133）、`SeriesState.kind`（:119）、門檻（:47-48）、`_tool_list_series`（≈:2483）、`_tool_get_window`（≈:2600）、`_dense_window`（≈:2702）、`_drop_short_runs`（:2221）、`_merge_equal_runs`（:2235）、`_describe_file`（≈:2418）。
- 事實層快取失效點：`load_csv_files`（:969）、`remove_file`（:1287）、`remove_all_files`（:1279）、`create_computed_series`（:1356，會新增欄）。
- `held_seconds` 的夾限必須同時用在 `_drop_short_runs(..., end=...)`，否則去抖與 `transitions` 的時長會不一致。
- 現行 `load_csv_files`（:1027）把例外只 `print` 到 stdout，MCP 端永遠只看到 "missing, empty, or unreadable"——W10 要把它接進 `failed.reason`。
- `pattern` 的 ReDoS 邊界（覆議點 3）：pattern 長度上限、只對窗內去重後的字串比對、逐值 try/except、總時限。

### 執行順序

```mermaid
flowchart LR
  M1["M1<br/>W7 + W8 + W10"] --> M2["M2<br/>W4 + W1"]
  M2 --> M3["M3<br/>W6' + W3 + W5"]
  M3 --> M4["M4<br/>W9"]
  M1 -.-> W11["W11 契約測試<br/>五檔 smoke"]
```

M1 低風險先落地；M2 是分析層主力（做完後 suggest.md 案例 B 的三行答案由 1 次呼叫取得）；M3 含兩項待覆議決策。

### 待覆議

1. **W3 多欄快照**與本檔「`get_value` 已決議不做」衝突——建議覆蓋，因為實際需求是「某一瞬間 N 條訊號」，不是單點查值。
2. **W9 是否提前** plot / zoom / highlight（suggest.md 兩輪都點名 `plotted` 唯讀是最大缺口）。
3. **W6' 是否開放具名群組的 regex**（換得結構化輸出，代價是 ReDoS 邊界，需 pattern 長度上限等防護）。

### 不做（維持決議）

內建任何內容文法解析（alarm / LOG / msg / hex）；以基數自動重判 `kind` 或聲稱「碼表」；通用謂詞引擎 `find_events(prev(x)!=y)`（若日後仍不足，再收斂成 `find_events(column, from, to)`）；`get_at`；`kind` 覆寫參數；headless / stdio；破壞性工具。

## 待辦

- [ ] **M1 — 低風險**：W7 家務／語意修正、W8 背壓、W10 載入透明度
- [ ] **M2 — 分析層主力**：W4 `mode="summary"`、W1 `list_series detail`
- [ ] **M3 — 覆議後動工**：W6' `find_text`、W3 多欄與快照、W5 `mode="aggregate"`
- [ ] **M4 — Phase 3 UI 操作**：W9（`plot_series` / `unplot_series` / `zoom_to` / `highlight_range` / `search_text` / `get_view_range` / `compute_series` / `set_timezone` / `measure_delta` / `screenshot_plot`）

每階段可獨立 smoke test，不必等全鏈路完成；W11 契約測試隨任一段一起跑。

## 風險

- [ ] **context 爆掉**：工廠 log 動輒百萬列，工具永遠不回傳原始 df，只回聚合摘要與下採樣點。
- [ ] **tool schema 精簡**：模型對巢狀 object 與 enum 耐受度低，schema 手寫成扁平 `string` / `number` / `boolean`。
- [ ] **GUI 阻塞**：GUI 開著 modal dialog 時工具會卡住，靠 `Event.wait` 逾時回錯（60 s）。HTTP 本身已在 worker thread。
- [ ] **omp 會改寫字串參數**：omp 送出前會把字串值中「像內部 URL」的（含 `local://`、file-backed 內部 URL）換成實際檔案路徑，所以參數驗證要以改寫後的 payload 為準。另 `i` 欄位會被剝除。
- [ ] **埠衝突**：第二個實例 bind 失敗只印 `[mcp] could not listen on 127.0.0.1:8790`，程式照常運作，不會影響正在跑的那個。
- [ ] **PyStand 版落後**：`D:\log_tools\logplot.exe` 與 `D:\log_tools\logplot.py` 是先前部署的副本，沒有 MCP；要走 exe 得重打包（`build.py`），或先用 `logplot.bat`。
- [ ] **格式假設**：任何以「看過的 log 內容」為依據的解析（欄名語意、值文法、單位、碼表）都會在下一批 log 失效；伺服器不得內建語意，見 Phase 2.5「撤回的前一版構想」。時間戳解析鏈亦同——五檔目前都命中同一 format 分支，屬清單巧合（W10 要回報實際命中分支並做範圍檢查）。
