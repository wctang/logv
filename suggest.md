# logplot MCP server 使用評價與改進建議

## 背景（評價所依據的實際案例）

任務：在 `log-htb-20260101.log_mpc_ctrl_101.csv.zst`（337 010 列、125 條訊號、涵蓋 2025-12-31 23:00:03 → 2026-01-01 20:29:03 UTC+8）中，判斷「播放狀態何時異常中止」。

判準：`mpc_ctrl_mode` / `mpc_ctrl_state` 的 FSM。正常循環為 `ready(7) → playing(8) → playing_done(9) → parking(10) → ready(7)`，`mpc_ctrl_t_elapsed` 由 0 計到約 346 232 ms（一場全長）。異常中止 = `playing` 的下一個狀態是 `halt(12)`、沒有 `playing_done`，且 `t_elapsed` 在全長前歸零。

結果：78 場播放中有 3 場異常中止。

| # | playing 開始 (UTC+8) | 中止 halt (UTC+8) | 實際播放 | 中止時 `t_elapsed` | 佔全長 |
|---|---|---|---|---|---|
| A | 2026-01-01 07:36:52.919 | 07:37:20.308 | 27.39 s | 22 403 ms | 6.5 % |
| B | 2026-01-01 07:39:53.321 | 07:40:21.972 | 28.65 s | 23 550 ms | 6.8 % |
| C | 2026-01-01 16:23:30.844 | 16:28:18.528 | 287.68 s | 282 737 ms | 81.7 % |

Unix 秒：A `1767224240.308028`、B `1767224421.971618`、C `1767256098.528062`。三者 `alarm_data` 均為 `(i:0 o:1 s:0.1 m:Halt command received)`（A 另於 07:37:21.603 追加 `PLC has alarm` + `PLC alarm - E-STOP triggered event`）。

使用方式：5 個 MCP 工具中共調用 17 次（`list_files` 1、`list_series` 1、`find_changes` 1、`get_window` 15）。

---

## 結論

**核心查詢引擎好用（4/5），但「分析語意層」幾乎全缺，最後約 70% 的分析工作由 agent 手動完成。**

針對「單檔、狀態機、找異常時點」這類任務，`find_changes` + `get_window` 這兩個 primitive 選得準、回得準，15 次 `get_window` 沒有一次是無效輸出。但 server 只提供「讀訊號」，不提供「比較訊號」，因此判斷邏輯全部落在呼叫端。

### 真正好用的部分

- **`find_changes` 是整個流程的關鍵一擊。** 125 條訊號一次篩成 94 條變動 + 變動次數排序，3 秒內鎖定 `mpc_ctrl_mode`(348)、`mpc_ctrl_state`(348)、`servo_alarm_code_1`(503)，也立刻從「缺席」發現 `mpc_ctrl_engineer_mode_playing_result` 全段未變（=不可用）。這個「先篩再讀」的順序設計非常對。
- **segments 表示法天生適合 FSM。** 「值保持到下一個 segment」等價於狀態機壓縮轉儲，348 筆 `mpc_ctrl_mode` 直接原樣吐出，不需任何還原就能枚舉 78 場播放與每場的結束狀態。
- **`kind` metadata（enum / analog / text）省下大量猜測**，是決定要抓哪些訊號的主要依據。
- **透明度好**：`mode: segments|dense`、`segments_omitted`、`changed_samples`、`distinct` + `values` 計數，明確回答「這是否精確完整」；dense 另附值分布（如 `servo_alarm_code_1`: `{0.0:293827, 95.5:42624, 95.3:507, 51.2:47, E1.7:4}`），一眼看懂量級。

### 痛點（依嚴重度排序）

1. **沒有跨訊號／狀態轉移的查詢能力。** 核心問題是「`playing` 之後不是 `playing_done` 的有哪些」，但 server 只能給原始 segment，不能給轉移對、不支援 `prev(x)!=y` 這類謂詞、沒有 derived signal。結果：三個中止點是肉眼比對 348 筆 segment 找出來的，場長 350.3 s / 346 232 ms 也是自行相減。問題本質是轉移，工具只給狀態——這是最大落差。
2. **enum 沒有 label 對照表。** `mpc_ctrl_mode` 回字串（`ready`/`playing`/`halt`，很好），但同一台機器的 `mpc_ctrl_state` 是 `kind: "analog"`，只回 `7/8/9/10/12/1/2`。必須用時間戳把 state 與 mode 手動 join 才能解碼。`servo_alarm_code_1` 標 enum 卻回 `95.5` 這類數值碼，同樣沒有碼表。
3. **`kind` 決定輸出策略，型別標錯就爆量。** `servo_alarm_code_2` 被標成 analog，回了約 450 筆「95.5 ↔ 0」的每秒切換；同一份資料的 `servo_alarm_code_1` 標 enum 則只回一行分布。同樣是無關緊要的抖動，一個 1 行、一個 450 段。
4. **沒有「只給異常值」的過濾。** 真正想看的是「偏離主導值 / 持續時間 > N 秒」的段落，但沒有 `drop_dominant` / `min_duration` 可用。
5. **`alarm_data` 只給整串拼接字串。** 06:46 那筆一次塞進 12 個 `(i:x o:y s:z. m:...)`，只能自行解讀；若結構化拆解，告警因果（誰先誰後）會清楚很多。
6. **GUI viewer 卻沒有「下指令」的工具。** server 說明寫「the human user sees every change immediately」，但 5 個工具中只有 `load_files` 會寫入，`plotted` 只能讀不能設，沒有 plot / zoom / cursor / highlight，無法把「07:37:20 這三個中止點」推給使用者看。〔推論〕調用可能已改變檢視焦點，但沒有任何回饋可供確認。
7. **小摩擦：**
   - `find_changes` 的 `file` 可省略、`get_window` 的 `file` 必填；全檔只載入一個的情況下仍重複貼了 15 次 60 字元絕對路徑。
   - `list_files` 提供的短 `name` 能否當 `file` 傳入，文件未載明。
   - `find_changes` 回報 `unchanged_series: 31` 卻不列出是哪 31 條。
   - 時間只有 Unix 秒，需自行換算時區。
   - 單調遞增的計數器（`mpc_ctrl_t_elapsed`）窄窗仍回數十筆逐點值，沒有「只回折點」的降採樣。

### 改進建議

| 痛點 | 建議 |
|---|---|
| 無法查轉移 | `get_window` 增加 `mode: transitions`，對 enum 訊號直接回 `[t, from, to, duration]` |
| 無跨訊號運算 | 新增 `find_events`，接受多訊號謂詞（如 `state==12 && prev(state)==8`），`find_changes` 既定的篩選方向保留 |
| enum 無碼表 | `list_series` 對每條 enum / 數值 enum 附 `labels`（值→名稱、值→語意），`servo_alarm_code_*` 尤其需要 |
| 型別標錯爆量 | 允許呼叫端覆寫 `kind`；dense 時對數值 enum 自動改走值分布 |
| 抖動雜訊 | `get_window` 增加 `min_duration` / `drop_dominant` |
| alarm 難讀 | `alarm_data` 回結構化陣列而非拼接字串 |
| 無法引導 viewer | 增加 `plot` / `set_window` / `highlight` 工具，讓 agent 的發現直接出現在人眼前 |
| 路徑冗長 | `file` 一律可省略（單檔時自動選定），並接受 `list_files` 的 `name` |

### 一句話總結

讀取層設計紮實，是這類 log 工具中少見的「壓縮且精確」；但缺少分析層，等於把狀態機的推理成本全部外包給 agent——15 次 `get_window` 中約只有 5 次是工具決定性貢獻，其餘 10 次都是「因為不能 join，所以得自己把原料抓回來對」。

---

## 第二輪評價：案例 B（`log-20260101.log_mpc_ctrl_105.csv.zst`，2026-10-08）

### 案例背景

同一型任務換另一台控制器日誌：328 746 列、125 條訊號、2025-12-31 23:00:03 → 2026-01-01 20:29:03（UTC+8）。判準同案例 A（`mpc_ctrl_mode`→`halt` 而非 `playing_done`）。

結果：**76 場播放，73 場正常、3 場異常中止**，三筆中止時間戳與案例 A **完全相同至毫秒**〔推論〕同一現場事件、兩台控制器（101 / 105）各自記錄。

| # | 起播 (ready→playing) | 中止 (playing→halt) | 播放歷時 | 中止時 `t_play` | 佔全長 |
|---|---|---|---|---|---|
| A | 2026-01-01 07:36:52.921 | 07:37:20.308 | 27.388 s | 22 447 ms | 6.5 % |
| B | 2026-01-01 07:39:53.322 | 07:40:21.972 | 28.650 s | 23 565 ms | 6.8 % |
| C | 2026-01-01 16:23:30.844 | 16:28:18.528 | 287.684 s | 282 756 ms | 81.6 % |

全片長度基準：73 場正常收播的 `t_play` 逐場驗證後落在 **345 825–346 832 ms**（此即「全長」）；異常三場只跑到 6.5 / 6.8 / 81.6 %。

中止瞬間的共同指紋（同一 sample，±0.4 s 內全部發生）：`mode` playing→halt、`state` 8→12、`t_play`/`t_elapsed`/`t_replay_left` 歸 0、`run_1`/`run_2` 1→0、`st_1`/`st_2` actuated→deactuating→idle、`en_1`/`en_2` 1→0（分別在 halt 後 +88 ms / +354 ms）、`alarm_data` 出現 `(i:0 o:1 s:0.1 m:Halt command received)`。次序顯示 halt 命令先到、伺服才被 disable、作動器告警才隨後（C 場晚 52–59 s），故成因是命令式中止而非軸先故障。

調用統計：手動 29 次（`get_window` 24、`find_changes` 3、`list_files` 1、`list_series` 1），另於 `eval` 內以程式驅動 74 次（1 次重取 + 73 次單點探測驗證正常收播落點）。

### 結論

**探索階段 8/10；聚合／求證階段 5/10。** 選對 primitive 時（`find_changes` 窄窗 + `mode=transitions`）原本要寫 pandas 的 log 鑑識變成 3–4 次呼叫就有輪廓；但 server 本質是 viewer 而非 query engine——「看某訊號在某時段」很順，「跨區段統計／彙總」完全沒有，最後一哩仍得在呼叫端自己兜。相較案例 A 的「70% 分析工作手動完成」，本輪因 `transitions`、`min_duration` 已到位而明顯改善。

### 真正好用的部分

- **`find_changes` 窄窗是決定性一擊。** 2 秒窗（16:28:17.5–19.5）直接列出中止瞬間 32 個變動訊號（另 93 條未變），等於自動圈出因果鄰域，省掉逐一翻 125 欄。全檔掃描則一次分出「91 有戲 / 34 沒戲」。
- **`mode=transitions` 一次抓完狀態機。** 76 場播放的 from/to/duration 全在一份回覆內，且欄數少（6.5 k token／21.5 小時），是「壓縮且精確」的典範。
- **`min_duration` 過濾抖動。** `servo_alarm_code_1` 用 `min_duration=5` 立刻看出 95.5 是每個播放週期約 12.5 s 的背景告警（16:30–17:55 全程反覆），不是肇因——這一條直接排除了最像兇手的訊號。
- **dense fallback 不退件。** 5 分鐘 `t_play` 窗超額時改回 first/last/min/max + 時間戳，「歸零」正是從 `extremes.min` 讀到的。
- **`time_format: iso` + 回傳 `tz`** 解決案例 A 抱怨的「只有 Unix 秒」；`file` 可省略也讓 29 次呼叫不必重貼長路徑。
- **透明度**：`segments_omitted`、`changed_samples`、`truncated`、`unchanged_truncated` 都明示精確性。

### 痛點（依嚴重度排序）

1. **沒有聚合／分組，最關鍵的答案要自己算。** 「76 場、3 場異常、73 場正常、正常時長 349.852–351.847 s」這個三行答案，是把 4–5 k token 的 transitions 全 dump 回來後在 `eval` 內統計才得到。server 缺 `group_by("to")` 這類摘要模式：問題本質是「依轉移結局分組」，工具只給原始轉移列。
2. **沒有單點取值。** 要問「16:28:18.4 的 `t_play` 是多少」只能開 0.4 s 窗；為驗證 73 場正常收播得跑 **73 次窗查詢**——若不是 `eval` 能直接驅動 tool，實務上不可能做。缺 `get_at(columns, t)` 或 `t1 == t2` 語意。
3. **沒有多訊號同讀。** 中止瞬間要逐一抓 mode / state / t_play / run / st / en / alarm 共 7–8 次呼叫；一趟 `get_window(columns=[...])` 就能取代。
4. **重複／別名訊號未標示，造成冗餘呼叫。** `mpc_ctrl_state` 與 `mpc_ctrl_mode` 同構（同 333 次變更）；`t_play` / `t_elapsed` / `t_replay_left` 三者變更數完全相同（280 140）；`over_travel_1` ≡ `over_travel_2`。我為此 dump 了整份 `mpc_ctrl_state` transitions（約 1.5 k token、零新資訊）才確認對映，`over_travel_2` 那次純屬重複。→ 建議 `list_series` 增加 `duplicate_of`。
5. **`kind` 誤標會誤導策略。** `mpc_ctrl_state` 只有 7 個相異值卻標 `analog`（其實是狀態碼，害第一輪沒直接用 transitions 看清，只能靠時間戳與 mode 手動 join）；`servo_regenerative_1/2` 標 analog 而 `_3` 標 enum，同家族不同標。
6. **enum 仍無碼表。** `servo_alarm_code` 回 `95.5` 這種數值碼，無值→語意對照；`mpc_ctrl_state` 的 7↔`ready` 亦然。
7. **`alarm_data` 仍是拼接字串。** 06:46 那筆一次塞 12 個 `(i:x o:y s:z m:...)`，只能自行解讀先後。
8. **GUI viewer 仍無「下指令」工具。** `plotted` 唯讀且永遠空陣列；我算出 3 個異常點卻無法框給人看——這是「agent 驅動 GUI viewer」最大的功能缺口。
9. **小摩擦：**
   - 文件未載明 `[t, from, to, held_seconds]` 的 held 屬於 from 還是 to（本次以首列 `null→halt, 481.28` 反推為「新值的持續時間」）；也未註明 `min_duration` 是否適用 `transitions`（實測可用，但 schema 只寫 `drop_dominant` 是 segments-only）。
   - 窄窗仍回整份 `unchanged_columns`（2 秒窗也回 93 條 + `unchanged_truncated: true`），資訊佔比高、價值近零。
   - 單調計數器（`t_play`）小窗仍逐點回，無「只回折點」的降採樣。
   - 併發不友善：一次丟 73 個呼叫得 `RuntimeError: Operation aborted`，降到 12 一批才過；〔推論〕為 harness/bridge 層限制，但 server 端若給明確 backpressure 訊息，agent 才知道要節流。

### 與第一輪建議的對照

| 第一輪建議 | 現況 |
|---|---|
| `get_window` 增加 `mode: transitions` | ✅ 已實作，本輪主力工具 |
| 增加 `min_duration` / `drop_dominant` | ✅ 已實作，直接排除背景告警 |
| dense 時給值分布 | ✅ 已實作（另附 `values` 計數） |
| 時間別只有 Unix 秒 | ✅ 已有 `time_format: iso` + 回傳 `tz` |
| `file` 一律可省略 | ✅ `get_window` 已載明單檔可省略 |
| enum 附 `labels` 碼表 | ❌ 仍未見 |
| `alarm_data` 結構化 | ❌ 仍為拼接字串 |
| `plot` / `highlight` 引導 viewer | ❌ 仍無 |
| 型別標錯（`kind`） | ❌ 仍在 |

### 改進建議（本輪新增）

| 痛點 | 建議 |
|---|---|
| 無法按結局分組 | `mode="summary"` 或 `transitions` 加 `group_by`：回各 (from,to) 次數 + 時長 min/median/max |
| 無單點取值 | `get_at(columns=[...], t)`，或允許 `t1 == t2` 回最近樣本 |
| 無區間彙總 | `mode="aggregate", every=N, agg=[min,max,last]`，可取代逐場 73 次探測 |
| 無多欄同讀 | `get_window(columns=[...])` |
| 重複訊號 | `list_series` 附 `duplicate_of` / 等價群組 |
| 低基數誤標 analog | 整數且值域小時改判 enum，並附 `values` 清單（如 `mpc_ctrl_state` 的 7 個狀態碼） |
| 文件歧義 | 補述 `held_seconds` 語意、`min_duration` 是否套用 transitions |
| 窄窗贅述 | `unchanged_columns` 依窗長自動縮減或改 opt-in |

### 一句話總結

本輪的 `transitions` + `min_duration` + `find_changes` 窄窗三件組，已把「狀態機找異常時點」推到近乎一鍵；剩下的落差集中在「**把 76 個區段依結局分組統計**」與「**某瞬間 N 個訊號各是什麼值**」這兩類查詢——補上 `group_by/summary`、`get_at`、`aggregate`，這題可從 29 次手動呼叫（外加 74 次程式化探測）壓到 5 次以內。

---

## 傳輸層討論：MCP vs CLI（2026-10-10）

### 問題

TODO.md 第 201 行已明文決議 **`不做（維持決議）：… headless / stdio`**。本節評估是否應將 MCP 功能改以 CLI 實作。

兩個前提先講清楚：

- 前述兩輪評價的痛點，**只有一項屬於傳輸層**（併發 73 個呼叫失敗），其餘（無聚合、無單點取值、無多欄同讀、enum 無碼表、`kind` 誤標、`alarm_data` 未結構化）都是**查詢引擎層**，換傳輸方式不會改善。
- 因此問題不是「MCP 換成 CLI」，而是「除了 MCP 之外，是否還需要一個無 GUI 的介面」。

### 量測（本機實跑，`uv run --no-project` 隔離環境）

| 項目 | 實測 |
|---|---|
| `import pandas, pyarrow, zstandard` | **0.445 s**（warm）／3.20 s（cold，首次讀 DLL） |
| `import PySide6.QtWidgets, pyqtgraph` | **1.933 s** |
| `pd.read_csv` `log-htb-…_101.csv.zst`（337 010 × 126，zstd） | **0.408 s** |
| （既有）`find_changes` 全檔 337k × 125 | 729 ms |
| （既有）`get_window` 1 小時窗 | 1.9 ms |

`logplot.py` 的 `PySide6` / `pyqtgraph` 是**模組層 import**（第 25–34 行），故同檔 CLI 每次啟動都付滿約 2.38 s；若改為 lazy import，每次固定成本降至約 0.45 s。

### 核心結論：成敗取決於「一 process 一查詢」還是「一 process 多查詢」

- **naive CLI**（每次呼叫起一個 process）：73 次查詢 = 73 × 0.45–2.38 s ≈ **33–174 s**。
- **batch CLI**（stdin 讀 JSONL，一個 process 跑完 73 個查詢）：0.45 s（import）+ 0.41 s（解析）+ 73 × ~2 ms ≈ **1 s**。
- **MCP**：每次呼叫的固定成本只有 HTTP + 執行緒跳轉（量級為毫秒），計算本身 1.9 ms 級。

**「改成 CLI」若實作成逐呼叫起 process，會比 MCP 慢 40 倍以上。** 這一點比抽象比較重要，也是這個決議能否翻盤的門檻：CLI 必須支援單 process 多查詢，否則不應做。

### 場景對照

| 場景 | 勝方 | 理由 |
|---|---|---|
| 互動式單次查詢、人在看畫面 | **MCP** | 零固定成本、狀態已在記憶體、schema 由 client 自動發現 |
| 「把這 3 個異常點框給我看」 | **MCP 獨佔** | CLI 做不到 |
| 無人值守 / 排程 | **CLI** | MCP 需要在場開著 GUI |
| 跨 20 份 `.zst` 掃同一套判準 | **CLI** | MCP 綁單一 viewer，`load_files` 還會改動使用者畫面 |
| 73 次同型查詢（案例 B） | **batch CLI** | 一 process 解決，無 IPC、無逾時、無併發上限 |
| 中斷長查詢 | **CLI** | MCP 只能等 `MCP_CALL_TIMEOUT`(60 s) 逾時 |

MCP 現行的結構性瓶頸（行號為 2026-10-08 版）：呼叫一律排進 GUI thread 序列化（`McpBridge.call` 2277 → `_run_job` 2290），並行度上限為 1（即案例 B「73 個並行失敗、降到 12 一批」的根因，對應 TODO.md W8；2026-10-12 第三輪實測 9 並行 × 2 批全數成功，安全批次 ≥9）；開啟 modal dialog 時整個 server 停滯（逾時訊息自身即寫 `is a modal dialog open?`）；綁死單一實例（`dataframes` 2485 / 2498 / 2603 / 2661），換一份 log 就得 `load_files` 並改動使用者畫面。這三點在 CLI 全部消失。

### 「不做 headless / stdio」決議的評價

**該決議對它原本的 scope 是正確的。** 其目標是「讓 agent 遙控一個正在給人看的 viewer」，headless 對此零貢獻，而它換來零依賴、零 key、零對話框。這個取捨成立。

是否需要翻盤，取決於是否承認**第二個目標**：無人在場、跨檔批次的分析。支持其存在的證據：本 repo 工具鏈本質即批次形態（`db2zst.bat`、`logcsv.bat`、`analysis_R0082_power.py` + `.bat`），五份實測 log 也是為了掃描而非逐份瀏覽。以 729 ms/檔 推估，20 檔全掃約 15 s，是可排程的異常偵測器——而這是 MCP 結構上做不到的。

若無此需求，新增 CLI 只是多一個需與 MCP 保持同步的介面。

### 無論是否做 CLI 都值得先做的事：抽出 Qt-free 引擎

把五個 `_tool_*` 抽成不碰 Qt 的 `LogQueryEngine`。理由不是為了 CLI，而是**契約測試**：Phase 2 的驗收是以「臨時腳本驅動真實 `CSVPlotViewer`」完成的，需要 Qt、需要視窗、需要事件迴圈。抽離後 W1–W7 的驗收可全數變成純 pandas 測試，速度與穩定性提升一個量級；CLI 只是順手得到的第二個介面。

抽離面已實測，比預期窄：

| 依賴 | 位置（2026-10-08 版） | 性質 |
|---|---|---|
| `viewer.dataframes` | 2485 / 2498 / 2603 / 2661 | 純 pandas dict ✅ |
| `viewer.series`（僅讀資料欄位） | 2507 / 2627 / 2668 | 純資料容器，內含 Qt 物件但不觸碰 ⚠️ |
| `viewer.active_text_series` | 2517 | 純 set ✅ |
| `viewer.load_csv_files()` | 2470 | **GUI** ⚠️ |
| `viewer.x_axis.utcOffset` | 2557 / 2633 / 2672 | **GUI** ⚠️，但 `_viewer_tz(utc_offset)` 已是純函式 |

亦即五個工具中只有 `load_files` 與時區讀取真的綁 GUI。重構成本低，且行為不變、可逐項對照現行 MCP 回應驗收。

### 若決定要做 CLI，四條硬約束

1. **必須支援單 process 多查詢**（stdin JSONL 或 `--script`），否則比 MCP 慢 40 倍以上。
2. **必須 lazy import Qt**，否則每次多付 1.933 s。
3. **必須與 MCP 共用同一份 `_tool_*` 實作與輸出契約**，CLI 只當第二個 frontend，否則兩份驗證邏輯會漂移。
4. `sys.stdout.reconfigure(encoding="utf-8")`——cmd.exe 預設 cp950，JSON 輸出會直接噴 `UnicodeEncodeError`。

### 不變的部分

W9 只能靠 MCP。`plot_series` / `unplot_series` / `zoom_to` / `highlight_range` / `set_view_range` / `search_text` / `screenshot_plot` 本質是 GUI 操作，CLI 不可能取代，M4 照原計畫執行。

### 一句話總結

MCP 與 CLI 不是替代關係而是分工：MCP 是有狀態的 GUI 遙控器（零固定成本、獨佔畫面操作），CLI 是無狀態的批次資料處理器（跨檔、並行、可排程）；原決議「不做 headless / stdio」在其 scope 內正確，翻盤與否取決於你是否承認「無人在場的批次分析」為第二個目標——而在此之前，先抽出 Qt-free 引擎對兩條路線都是純收益。

### 後續（2026-10-12 落地）：第三條路 —— skill + 直打 HTTP 的 proxy

本節原本假設只能在「MCP（有狀態 GUI 遙控器）」與「batch CLI（無狀態批次處理器）」之間選。實際落地的第三條是：**協定留 MCP，客戶端換成一支 per-call 的 script**。

- 形態：`~/.agents/skills/logplot/SKILL.md` + `logplot_mcp.py`（stdlib only；`status` / `tools` / `call` / `batch` / `start`）。打的是同一個 `POST 127.0.0.1:8790/mcp`（`initialize` / `tools/list` / `tools/call`），**server 端零改動、輸出契約零漂移**，`mcp_test.py` 的 169 項驗證全部繼續有效。
- 成本：proxy 不需要 pandas 也不需要 Qt，實測 `json+urllib` 版直譯器啟動 **143 ms**（對照本節量到的 pandas 445 ms、PySide6 1 933 ms）；`batch` 一個 process 跑完 N 次查詢，所以「逐呼叫起 process 慢 40 倍」的結論不適用於這一支。
- 與本節 CLI 方向的差別：它仍是「遙控眼前那個 GUI」（`load_files` 照樣改動使用者畫面），不是 headless 批次，也不提供跨檔掃描——所以它**不取代**本節評估的 batch CLI 需求。
- 動機：omp 的 MCP 連線發生在 session 啟動、沒有 lazy 模式，而且**從未連上過的 HTTP server 不會被再探測**（只有「曾連上後斷線」的才有 15 s→5 min 的背景探測）→ GUI 沒開時工具根本不存在，且「先開 omp、後開 logplot」在同一 session 內永遠接不上。proxy 把「連線」變成使用者呼叫當下才發生的事。
- 可攜性：`SKILL.md` 是跨 agent 的開放標準（agentskills.io），`~/.agents/skills` 同時被 omp、Codex CLI、Gemini CLI、Claude Code 等讀取；沒有 shell 的宿主則改用同一支管子包成 stdio MCP bridge。

---

## 第三輪評價：案例 C（fleet 場景：9 檔同載，2026-10-12）

### 案例背景

同型任務，從「單檔」升級為「多節點機隊」：9 台控制器 `log-20260928_mpc_ctrl_101..109.csv.zst` 同時載入同一 viewer，各 43 859–51 339 列、206 條訊號、同一時間窗（2026-09-28 19:01:46 → 09-29 01:12:24 UTC+8，約 6.2 h），各為不同節點。此 log 集的 `mpc_ctrl_mode` 直接是字串 enum（`ready/playing/playing_done/parking/halt/maintenance/shutdown/idle/calibrating/initializing`），不需像案例 A/B 解碼數值 state。

判準同前兩輪：`playing → halt`（無 `playing_done`）為異常中止。

結果：每節點 9–10 場播放（5 場正常收播：20:54:59 / 21:07:12 / 21:20:53 / 23:30:30 / 00:48:17），4–5 場異常中止，歸併為 7 組事件：

| # | 起播 (UTC+8) | 中止 (UTC+8) | 已播 | 節點 | 中止時報警 |
|---|---|---|---|---|---|
| A | 09-28 23:01:19 | 23:01:24 | ~5 s | 107–109 | 107：HMI STOP (4.14) + S1–S6 伺服報警 (5.1)；108/109：僅 Halt (1.2) |
| B | 09-28 23:08:49 | 23:09:07 | ~17 s | 104–106 | 104：Halt + HMI STOP + S1–S6；105/106：僅 Halt |
| C | 09-28 23:11:40 | 23:12:02 | ~21.5 s | 101–103 | 三節點均 Halt + S1–S6 (4.26–4.31) |
| D | 09-28 23:17:10 | 23:17:32 | ~22.4 s | 101–103 | 103：Halt + HMI STOP + S1–S6；101/102：僅 Halt |
| E | 09-28 23:22:29 | 23:22:52 | ~23.2 s | **全部 9 節點** | 全部 Halt + S1–S6（106 缺 S5） |
| F | 09-29 00:25:41 | 00:28:06 | ~144 s | 全部 9 節點 | 全部 Halt；107 另 FPGA (5.12) |
| G | 09-29 00:34:56 | 00:37:21 | ~145 s | 全部 9 節點 | 全部 Halt；107 另 FPGA (5.12) |

失敗呈**兩窗聚簇**：23:01–23:22 連續 5 輪播放全數失敗（含 3 輪全機隊同時中止），23:30 恢復正常，00:25 / 00:34 再失敗 2 輪，00:48 恢復——不是隨機單發。多組中止伴隨 S1–S6 六通道**同時**報警，指向共因事件（總線 / 電源 / HMI STOP）而非單台伺服故障。附帶發現：107 於 00:44:21 另有單發 RIO disconnected (4.3) + 全 I/O（SDI/SDO/DI/DO）報警風暴，之後保持 shutdown 至日志結束（00:48 那輪正常播放 107 實際未參與）；103 出現未見模式值 `GWD`（21:27:33–21:46:24，約 15.5 min，同段其他節點為 ready）；108 於日志截斷時（01:12:24）仍在 playing（01:09:19 起），不計為中止。

調用統計：MCP 21 次（`list_files` 1、`list_series` 1、`get_window` 19），其中 18 次為 per-file `get_window`（9 × alarm_data 段 + 9 × mpc_ctrl_mode 段）；另文件讀取 3 次、`eval` 時區換算 1 次。`find_text` 讀了文件但 0 使用（發現步驟失誤，見痛點 3）。

### 結論

**單訊號取證 9/10；機隊關聯 4/10。** 前兩輪要求的查詢層（`summary/group_by`、`columns` 多讀、`drop_dominant`）全部到位，單檔鑑識已近乎無摩擦；但本輪主場景是「同一訊號 × 9 節點」，server 仍無任何跨檔能力，「23:22:52 全 9 節點同時中止」的結論仍是從 9 份結果人工比對出來的。機隊缺口是本輪最大落差，也是前兩輪（單檔場景）未曾觸及的方向。

### 真正好用的部分

- **`summary` + `group_by: value` 一次給出報警分類學。** 單次呼叫讀 `alarm_data` 值分布（12 種非 null 字串 + 各佔據秒數），全部報警代碼（1.1/1.2/1.3/3.x/4.x/5.x/6.1）一覽無遺——第一輪「alarm_data 拼接字串」的痛點，至少值分布讓 *發現* 可用，因果先後仍不可讀。
- **`drop_dominant` 再次是主力（本輪兩段都靠它）。** 每檔 28–31 段報警、約 60 段 mode，全帶起止時間戳，5 萬列的 null/"ready" 被自動濾除。第二輪建議中價值最高的一項，在 fleet 場景再次驗證。
- **`columns=[...]` 多訊號同讀（第二輪建議）落地：** 首次呼叫 6 條訊號（alarm/warn/state/mode/servo_alarm_1/t_last_sync）一趟讀完。
- **9 並行呼叫穩定。** 兩批各 9 個 `get_window` 全數成功，對照案例 B「73 並行 RuntimeError、降到 12 一批才過」→ 併發上限介於 12 與 73 之間，9 並行是安全批次。
- **透明度持續在線：** `held_truncated`（窗尾超出文件末端 0.06 s，回傳明言已 clamp）、`groups_truncated`（`t_last_sync` 704 個相異值，截 100 群並標註），無靜默省略。
- **`list_files` 元資料對 fleet 一致性檢查有用：** monotonic、`sampling_median_s`、`largest_gap_s`、常數欄清單，一眼確認 9 檔同節奏同 schema、可互相比對。

### 痛點（依嚴重度排序）

1. **無跨檔查詢——本輪核心缺口。** 核心問題是「同一時刻哪些節點出了哪種報警」，但 `get_window` 只接受單檔，只能 9 份 segments 各抓一次再人工合併時間線；沒有「跨檔同值同段聚類」，也沒有「`file` 省略時回 per-file entries」。三輪「全機隊同時中止」全靠肉眼比對，這正是 fleet 任務的高頻查詢。
2. **`list_series` 多檔爆量。** 206 訊號 × 9 檔、schema 完全相同，輸出 50 KB+ 且中段 elide，只能靠第一份清單。無 `file` 參數（不能只列單檔）、無「同 schema 只回一次」的去重。
3. **`find_text` 是發現步驟的對的工具，但回傳只有「前 N 筆命中」。** 省略 `file` 全檔搜尋、回各檔命中數 + 相異值——「哪些節點有 servo alarm」正好一步到位。但對密集變化訊號，命中列表不能取代完整 change list，仍要落回 9 次 per-file `get_window`。一半是工具限制（無 per-file segments 模式），一半是我自己的流程失誤（讀過文件卻直接走 per-file segments，沒先拿它當發現步驟）。
4. **`kind` 跨檔不一致。** `alarm_data` 在 8 檔為 `kind: "enum"`、107 為 `kind: "text"`（疑似相異值數觸發升級），同一訊號名兩種標籤，呼叫端無法按名稱固定策略。
5. **未知 enum 值無碼表可問。** 103 的 `GWD` 模式值直接出現在 segments 裡，無 label 表可查語意，只能人工跟進。
6. **小摩擦（本輪自身責任）：** `time_format: iso` + `tz` 自第二輪已存在，我未用，事後 `eval` 二次換算 Unix→+08:00；文件 on-demand 讀取多 3 次往返（可接受成本）。

### 與第二輪建議的對照

| 第二輪建議 | 現況 |
|---|---|
| `summary` / `group_by` 依結局分組 | ✅ 已實作，本輪發現階段主力 |
| 單點取值 `t1 == t2` / `snapshot` | ✅ 已實作（本輪未用） |
| `aggregate` 區間彙總 | ✅ 已實作（本輪未用） |
| `get_window(columns=[...])` 多欄同讀 | ✅ 已實作，已使用 |
| 重複訊號 `duplicate_of` | ❌ 仍未見 |
| `kind` 誤標 | ❌ 仍在，且本輪新增「同訊號名跨檔 kind 不同」案例 |
| enum 附 `labels` 碼表 | ❌ 仍缺（此 log 集 `mpc_ctrl_mode` 直出字串，症狀性緩解；`mpc_ctrl_state` 仍是數值碼 0/1/4/5/6/7/8/9/10/14，未解碼） |
| `alarm_data` 結構化 | ❌ 仍為拼接字串（值分布發現可用，因果先後仍不可讀） |
| `plot` / `highlight` 引導 viewer | ❌ 仍無（工具仍為 6 個，無 plot/zoom/highlight） |

### 改進建議（本輪新增）

| 痛點 | 建議 |
|---|---|
| 無跨檔查詢 | `get_window` 的 `file` 在多檔載入時可省略 → 回 per-file entries；或增 `fleet` 模式：給訊號 + 偏差條件（如 `drop_dominant`），回「值 × 時段 × 涉及文件清單」聚類行，直接回答「幾個節點同時」 |
| `list_series` 多檔爆量 | `list_series` 加 `file` 參數只列單檔；全部 schema 相同時回首份清單 + `same_as: [...]` |
| `find_text` 對密集訊號不足 | 加可選 `mode: segments`（或 per-file 完整 change list），讓它成為發現→細節的一站式工具 |
| `kind` 跨檔不一致 | 依訊號名跨所有載入檔統一計算 kind，或回 `kind_per_file` 明示 |
| 未知 enum 值 | 第二輪已建議的 enum 附 `values` 相異值清單仍值得優先，讓 `GWD` 型值可對照碼表 |

### 一句話總結

前兩輪「讀取層紮實、分析層缺席」在單檔側已修好一半（`summary`/`columns`/`drop_dominant` 全數落地，單檔鑑識近乎一鍵）；本輪 fleet 場景暴露的是分析層的另一半——**跨節點關聯**：21 次呼叫中 18 次是同一查詢跑 9 檔的機械重覆，若「同訊號 × N 檔」能一次回答，本題可壓到 10 次以內，而「全機隊同時中止」會從人工比對結論變成工具回傳的一行。
