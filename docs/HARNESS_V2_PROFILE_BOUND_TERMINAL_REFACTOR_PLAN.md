# Solution Agent Harness：Profile-bound Remote Terminal 架構刪改規劃書

- 版本：v0.2-draft，2026-09-28
- 狀態：規劃完成，尚未實作
- 適用 profile：`EXAMPLE_WORKER_A_FAN`
- 前置文件：`HARNESS_V2_REMOTE_TERMINAL_TASKBOOK.md` v0.1
- 決策原則：先刪除不合理複雜度，再增加最小可用遠端窗口

## 1. 結論

本次不把 Solution Agent Harness 改造成通用遠端終端平台，也不把 DeepSeek
Harness（DSH）整套嵌入。只借用它的分層觀念：MCP tool adapter、terminal
session service、PTY/tmux backend 分離；不引入其全域設定、通用多 session
管理或額外 runtime dependency。

目標是讓本地 Codex 透過既有 `EXAMPLE_WORKER_A_FAN` profile，使用一個或多個由
profile 預先宣告、長期存在的遠端 shell／remote Codex 窗口。Transport 身份與
terminal slot 均固定，terminal 內的命令不再逐條映射成 wrapper subcommand 或
registry route。固定 terminal 應跨 Codex 任務與 MCP client 重啟持續存在，不得把
每個操作實作成建立臨時 SSH shell、執行一次命令、再關閉的循環。

完成後 live MCP surface 精確為七個工具：

```text
stage_execute
stage_status
stage_collect
terminal_open
terminal_send
terminal_read
terminal_close
```

三個 stage 工具繼續負責可重現、可稽核、證據完整的研究執行；四個 terminal
工具只負責互動診斷、部署、開發、遠端 Codex 操作與實驗管理。terminal transcript
不是科學結果的 authority，也不能替代 stage receipt、artifact hash 或 Token Guard。

## 2. 為什麼需要刪改，而不是繼續加功能

目前有三種結構同時存在：

1. Harness V2 以「最多三個工具」為不變量，適合 bounded stage，但無法處理部署、
   診斷及互動式 remote Codex。
2. profile wrapper 為每種操作增加一個 subcommand，長期會把遠端 shell 能力拆成
   大量一次性入口。
3. v0.1 terminal 任務書直接設計五個 terminal 工具、任意動態 session、
   list/resume、resize、events ledger 與逐位元組證據，超過目前「一個 profile、
   一個專案、少數固定持久窗口」的實際需求。

若三者都保留，Codex 需要先判斷 wrapper、stage、terminal、臨時 relay 四條路，
管理成本與 token 反而增加。因此本次不是在舊架構旁邊再疊一層，而是明確淘汰
重複路徑。

## 3. 保留的核心架構

下列能力不刪、不降級：

| 保留項 | 原因 |
| --- | --- |
| `stage_execute/status/collect` | 穩定研究任務、長輸出、artifact 驗證與 collect 重驗仍需要正式 lifecycle |
| execution spec 與 script registry | 保障可重現研究 stage；terminal 不應取代科學執行契約 |
| Token Guard receipt、raw stdout/stderr、artifact hash | terminal transcript 不具同等證據地位 |
| ResearchFlow 與 `managed_markdown` GET/SHA/PUT/409 邊界 | terminal 不取得研究紀錄寫入 authority |
| connection policy、固定 SSH alias、profile wrapper | 防止模型自行選 host/user/key；也是 bootstrap 與人工恢復入口 |
| checkout-local `.venv` | 避免系統 Python／Miniconda 差異 |
| bundle SHA、dry-run、backup、無 force installer | terminal 能執行命令不代表可以繞過部署安全門 |
| plugin-creator 正式註冊與新任務 live-tool 驗收 | 不手改 marketplace/config，避免舊任務工具快取造成假通過 |
| report-writer 與固定 provider env 機制 | 不把 key 放入 terminal schema、命令、log 或 Markdown |

## 4. 必須刪除、退役或改寫的架構

### 4.1 將 v0.1 的五工具收斂為四工具，但不混合讀寫權限

不實作並從有效規格中刪除以下公開工具：

```text
terminal_write
terminal_list
```

`terminal_write` 改為 `terminal_send`，一次呼叫可送出字元／控制鍵、等待，並回傳
該次操作的有界增量輸出；無新輸出或需要稍後追蹤時，再使用唯讀的
`terminal_read`。保留 send/read 分離，是因為 MCP annotation 與 plugin approval
是工具級：若合併成 `terminal_io`，便無法同時讓純讀取自動批准、寫入維持明確
副作用提示。這只比三工具方案多一個小 schema，但避免安全與使用體驗互相衝突。

刪除 `terminal_list` 的原因是第一版只允許 profile 預先宣告的固定 terminal slots，
不允許模型建立或發現任意 session。`terminal_open(terminal_id)` 只能選擇固定 ID，
對既有 session 採冪等 attach；可用 ID 來自 profile，而不是運行期 session 掃描。

### 4.2 刪除動態 session 架構，保留少數固定持久 terminal

以下內容從 MVP 規格移除，而不是延後到實作時偷偷保留：

- 任意 session name 與 session graph。
- 運行期無上限建立 terminal。
- `resume_session_id`、跨 session 搜尋與臨時 session discovery。
- `terminal_list`、attachment list 與接管其他 tmux session。
- 每 session 可選 `shell`、`cwd`、`cols`、`rows`。
- resize、方向鍵與通用 TUI 相容性作為首版驗收條件。
- 把 rotation、segment discovery 暴露成模型需要管理的工具操作。
- `events.jsonl`、多檔事件 ledger 及其 hash 鏈。

profile 可宣告一個或少數具名 terminal slots，例如 `main` 與 `worker`；每個 slot 的
初始 cwd、shell、tmux name 與 run root 均固定。使用者進入 shell 後仍可自行 `cd`
或啟動 `codex`，但模型不能透過 tool schema 改 host、發明新 ID 或建立臨時 session。

固定 terminal 的預設生命週期是長期存活：日常工作只 attach 與 I/O，不為每條命令
呼叫 close，也不因一個 Codex 任務結束而 terminate。只有 profile 退役、session
故障、使用者明確要求清理，或已批准的維護操作，才可真正終止 fixed terminal。

### 4.3 改寫「完整逐位元組 transcript」的不實際承諾

v0.1 要求 terminal 保存「完整、逐位元組 transcript」，但 tmux `pipe-pane` 能可靠
保存的是 PTY 輸出，不等同於可證明每個輸入 byte、終端控制狀態與遠端副作用。

新契約只承諾：

- 以大小上限分段保存 managed PTY 捕獲的 raw output bytes，每個封閉 segment 有
  byte count 與 SHA-256；不承諾無限期單檔增長；
- 回傳 segment-aware cursor、byte count、SHA-256 與有界文字預覽；
- 在 `session.json` 保存 profile identity、時間、狀態與 backend identity；
- terminate 時產生 `final_receipt.json`；
- 不把 terminal receipt 宣稱為科學 stage receipt。

需要科學可重現性、輸入指紋或 artifact 完整性時，必須回到 stage lane。

### 4.4 退役「MCP 最多三工具」的現行不變量

下列 active 文件與測試中的三工具限制，於七工具通過驗收後改為七工具契約：

- `docs/HARNESS_V2_ARCHITECTURE.md`
- `docs/HARNESS_V2_MANAGEMENT_COMPATIBILITY.md`
- `docs/HARNESS_V2_DEPLOYMENT.md`
- `docs/HARNESS_V2_BOOTSTRAP_INSTALL.md`
- `docs/HARNESS_V2_REMOTE_DEPLOYMENT_REQUIREMENTS.md`
- `tests/test_harness_mcp.py`
- `plugins/solution-agent-harness/.mcp.json`

`HARNESS_V2_ACCEPTANCE.md`、`HARNESS_V2_COMPLETION_AUDIT.md` 與 field feedback 中
「當時恰為三工具」是歷史驗收證據，不改寫成七工具。只在文件頂端或後續 superseding
record 說明該工具面已被新版本取代，避免竄改歷史。

### 4.5 退役臨時 JSON-RPC-over-SSH relay

候選檔案：

```text
C:\Users\example-user\Desktop\example-research\tools\static-paired-layer-change\fan_remote_harness_mcp.py
```

它把固定 spec 名稱、工具表及 SSH 參數寫死，是故障期的臨時 transport，不應成為
第二套 MCP client。正式 `example-worker-a_mcp.py` 與七工具 live smoke 通過後：

1. 先確認任何 taskbook、skill、wrapper、測試與日常命令都不再引用它；
2. 保存檔案 SHA 與歷史位置到 migration receipt；
3. 再從 active tools 刪除；field feedback/archive 內的歷史提及保留。

在替代路徑完成前不得先刪，以免失去診斷依據。

### 4.6 停止增加一次性 wrapper／registry route

新 terminal 穩定後，部署、唯讀檢查、測試、Git 操作與 remote Codex 控制，不再為
每個命令新增 `example-worker-a.ps1` subcommand 或 script-registry route。

第一版保留 wrapper 的 `info`、`identity`、`gpu`、`preflight`、`workplace-init`、
`status` 與 MCP launcher，作為 bootstrap、人工恢復及 terminal 故障時的 bounded
診斷面；`tmux-probe` 在正式 terminal smoke 通過後刪除，因為它只驗證臨時 probe。

這裡不刪既有研究 routes。只有未被研究 spec 使用、純粹為繞過 terminal 缺口而新增
的一次性 route，才可在引用掃描與 regression test 後逐一退役。

### 4.7 改寫 Skill 中對 SSH shell 的錯誤禁止方式

必須改成四條可執行規則：

- SSH shell 本身不是被禁止的能力；正式 terminal 的底層 transport 可以是 SSH。
- 一般工作必須重用 profile 已宣告的一個或多個 fixed terminal，不得反覆建立／關閉
  臨時 SSH shell，也不得由模型自選 alias、host、user、key 或 SSH options。
- 明確的 bootstrap／人工恢復可以走既有 profile wrapper，但不能因此變成日常的
  ad-hoc SSH 路徑。
- 穩定、可重現研究仍優先走 stage。

涉及：

- `plugins/solution-agent-harness/skills/solution-agent-harness/SKILL.md`
- `example-remote-codex` 的可部署 source（不可只改安裝 cache）

## 5. 新增的最小架構

```text
Codex App
  -> personal plugin / MCP launcher
  -> C:\...\ai-tools\tools\example-worker-a_mcp.py
  -> profile-bound SSH stdio（既有 transport）
  -> remote solution_agent.harness.mcp_server
       |-- stage_*        -> 既有 bounded lifecycle
       `-- terminal_*     -> ProfileTerminalManager
                              -> 一個或少數 profile-declared tmux sessions
                              -> bash / remote codex / experiment control
```

關鍵限制：

- terminal backend 在遠端 MCP server 內運作，不再為每次操作開第二層 SSH。
- tool schema 不接受 host、port、user、key、SSH options 或任意 session name；只接受
  profile 宣告的 `terminal_id`。
- launcher 只傳公開 profile identity、project workdir、run root 與 solution-agent root。
- 每個 profile 只有一個或少數 deterministic fixed tmux sessions；不掃描或接管使用者
  其他 session。
- 每個 fixed terminal 同一時間只允許一個 writer；只有 profile 驗證為不同 worktree
  的 fixed terminals 才可同時寫入。共用同一 cwd 時整個 profile 只能有一個 writer，
  競爭時回傳 `busy`，不默默交錯輸入。
- MCP tool timeout 只結束本次等待，不殺遠端 shell 或 foreground process。
- Codex task 或 MCP client 結束不自動 close；下一個 client 繼續 attach 同一 terminal。

profile 中的非秘密宣告建議採固定 allowlist，例如：

```yaml
remote_terminals:
  default: main
  slots:
    main:
      tmux_name: solution-agent-main
      cwd: /srv/example-workspaces/fan-workspace
      shell: /bin/bash
```

這是可用 terminal 的完整集合，不是建立範本。沒有列在 `slots` 的 ID 一律拒絕；
若日後增加第二個可寫 slot，必須給它已確認存在的獨立 worktree/cwd。

### 5.1 `terminal_open`

輸入可包含 profile allowlist 中的 `terminal_id`；省略時使用 profile 的 default
terminal。此操作必須冪等：fixed session 不存在時建立，存在時只 attach，不重置
cwd、shell state 或 foreground process。回傳：

- profile ID、hostname、remote user；
- session/backend identity；
- current cwd、started/updated time；
- running/attached 狀態；
- output cursor、transcript path/hash。

若 metadata 與 tmux 狀態不一致，fail closed 並回傳結構化錯誤，不自動接管未知
session。正常工作流程在每個 Codex 任務至多 open/attach 一次，之後持續使用
`terminal_send`／`terminal_read`，不得對每條命令重複 open。

### 5.2 `terminal_send`

最小輸入：

```json
{
  "terminal_id": "main",
  "chars": "pwd\n",
  "control": null,
  "after_cursor": 0,
  "wait_ms": 1000,
  "max_output_bytes": 8192
}
```

`terminal_id` 必須是 profile 宣告值。`chars` 與 `control` 至少提供一項；
`control` 首版只支援 `enter`、`interrupt`、`eof`。回傳本次 send 之後的 bounded
output、next cursor、truncated、wait reason、session status、transcript bytes/hash
與結構化錯誤；不重新 quote `chars`，也不把內容映射成 execution spec。

同一 fixed terminal 同時只能有一個 active send。`timeout` 或輸出暫停只表示本次
等待結束，不能宣稱 foreground command 已退出。

### 5.3 `terminal_read`

純讀取指定 `terminal_id` 與 `after_cursor` 後的 bounded retained output，不送出
stdin、不改 terminal 狀態，應標為 read-only 並在 plugin 中允許無副作用批准。
回傳 `next_cursor`、`truncated`、`wait_reason`、top-level session status 與目前
transcript hash。輸出靜默不等於 command success，只有 top-level session exit 或
明確的 command marker 才能聲稱退出狀態。

### 5.4 `terminal_close`

接受 profile allowlist 中的 `terminal_id` 及 `action: detach|terminate`。`detach`
只釋放當前 client，不停止 fixed terminal；Codex task 自然結束時甚至不必主動呼叫。
`terminate` 對指定 managed session 結束程序並寫 final receipt，但不是日常收尾動作，
必須有使用者明確要求、profile 退役或批准的故障維護理由。不得影響其他 fixed
terminal 或非 Harness tmux session。

## 6. 檔案級變更計畫

| 類型 | 路徑 | 預定變更 |
| --- | --- | --- |
| 新增 | `solution_agent/harness/remote_terminal.py` | fixed-slot manager、tmux identity、cursor、bounded output、receipt |
| 修改 | `solution_agent/harness/mcp_server.py` | 註冊四個 terminal tools；保留三個 stage tools schema |
| 修改 | `plugins/solution-agent-harness/.mcp.json` | 宣告七工具；`terminal_read` 唯讀，send/close 維持明確副作用批准 |
| 修改 | `plugins/solution-agent-harness/scripts/server.py` | `--check` 增加公開 profile/runtime readiness，不輸出秘密 |
| 修改（control checkout） | `C:\Users\example-user\Desktop\ai-tools\tools\example-worker-a_mcp.py` | 從已驗證 profile 注入四個公開 identity env；不加入 provider env |
| 修改（control checkout） | `C:\Users\example-user\Desktop\ai-tools\tools\example-worker-a.ps1` | 正式 smoke 後移除 `tmux-probe`；保留 bootstrap/recovery commands |
| 修改 | `tests/test_harness_mcp.py` | 三工具 assertion 改為精確七工具及 annotations |
| 新增 | `tests/test_remote_terminal.py` | fixed identity、exclusive lease、send/read、wait reason、cursor segments、encoding、quota、restart 與 nesting tests |
| 修改 | plugin 與 Example skills | stage/terminal 分工、禁止 raw SSH、允許正式 terminal |
| 改寫 | `docs/HARNESS_V2_REMOTE_TERMINAL_TASKBOOK.md` | 標為 superseded，指向本規劃與後續 implementation taskbook |
| 條件刪除 | `tools/static-paired-layer-change/fan_remote_harness_mcp.py` | 七工具替代路徑驗收後刪除 active copy |

不修改 ResearchFlow、report-writer 科學內容、既有研究結果或歷史 acceptance 數字。

## 7. 實作順序與刪除門檻

### Phase 0：凍結與基線

1. 記錄 integration checkout、control checkout 與遠端 checkout 的 dirty state。
2. 對候選修改／刪除檔案計算 SHA-256。
3. 保存現有三工具 no-API smoke、collect revalidation 與 plugin inventory。
4. 不處理任何不相關的使用者修改。

門檻：基線不可重現時停止，不進入刪除。

### Phase 1：先建立替代品，不刪舊路徑

1. 實作 profile-declared fixed-terminal manager 與四工具。
2. 用 fake tmux/process adapter 做本地 deterministic tests；Windows 不強求本機安裝 tmux。
   測試同一 fixed ID 重複 open 只 attach、不重啟，未宣告 ID 必須拒絕。
3. 驗證既有 stage tests、Token Guard、report-writer 與 Markdown sync tests 不退化。
4. 產生 bundle，執行本地 target dry-run；conflict/unsafe 非零立即停止。

門檻：七工具精確 inventory、所有新增測試與既有關鍵回歸通過。

### Phase 2：本地 plugin 驗收

1. 依 plugin-creator cachebuster/reinstall 流程更新 personal plugin。
2. 新開 Codex 任務，確認 live surface 精確七工具。
3. 對 terminal backend 做無 API smoke；不得用舊任務的 cached inventory 代替。

門檻：open/attach → 多次 send/read → interrupt → 跨 client open/attach 完成，期間同一
terminal 未被重建或關閉；另外以專用維護測試驗證 detach/terminate，輸出有界且
transcript hash 可重算。

### Phase 3：遠端安全部署

1. 透過正式 lifecycle 傳 bundle，驗證本地／遠端 SHA。
2. installer dry-run 必須 `safe: true`、`conflicts: []`；保留 backup 與 ledger。
3. 使用遠端 checkout-local `.venv` 安裝 requirements 並執行 server `--check`。
4. 在實際執行 Codex 的遠端 host 正式註冊 plugin，新開任務驗證七工具。
5. 執行無 API terminal smoke，再做既有三工具 no-API regression smoke。
6. 只在兩者一致後做一個小型 report-writer stage；不重跑昂貴三案例 A/B。

門檻：profile/hostname/user/cwd 正確；沒有 raw-SSH fallback；沒有 key 進入命令、
transcript、bundle、log、receipt 或 Markdown。

### Phase 4：最後才刪除舊架構

只有 Phase 1–3 全部通過後才可：

1. 從 active 規格刪除五工具／任意動態 session 設計。
2. 更新 active docs/tests 的三工具硬限制。
3. 刪除 wrapper `tmux-probe`。
4. 在零引用確認後刪除臨時 JSON-RPC relay。
5. 保留歷史 feedback、audit、hash 與 migration receipt。

任何 regression 都先回退 plugin/bundle 到前一已驗證版本，不使用 `git reset --hard`、
force overwrite 或刪除使用者修改。

## 8. 驗收標準

全部成立才算可部署：

1. live MCP surface 精確為三個 stage 加四個 terminal 工具。
2. 工具 schema 無 host/user/key/SSH option/session-name 選擇。
3. profile identity 缺失或不符時 server fail closed。
4. profile 宣告的一個或多個 fixed terminal 可跨 Codex 任務與 MCP client 持續
   attach；相同 ID 不被重建，不同 ID 的輸入與 transcript 不串線。
5. `terminal_send` 支援 command、Ctrl-C、EOF、wait reason 與有界增量輸出；
   `terminal_read` 提供唯讀 cursor/poll，兩者 annotation 與 approval 不混淆。
6. tool timeout 或 client 結束不終止遠端 session；每 terminal 的 single-writer lock
   可阻止輸入交錯。
7. transcript 路徑位於 harness run root，不進入 ResearchFlow authority path。
8. terminal receipt 不被當成 stage receipt；科學 artifact 仍由 collect 重驗 hash。
9. 既有三工具 no-API smoke、report-writer 小型 smoke、provider usage、claims
   sidecar、Token Guard receipt 與 artifact hash 全部通過。
10. local/remote dirty worktree 均未被覆蓋；bundle、remote bytes、backup 與 ledger
    均有可追溯 hash。
11. 新流程正常時，Codex 重用 fixed terminal，不再為每條操作建立／關閉 SSH、臨時
    relay 或一次性 command route。
12. `terminal_send` 對一般短命令直接回傳 bounded output，不強迫每次再 read；只有
    等待中或長輸出才使用 `terminal_read`，且 stage 結果品質與證據門不下降。
13. dead shell 不會被普通 attach 靜默重建；tmux name 相同但 owner/profile marker
    不符時拒絕接管。
14. 多個 fixed terminals 不能在未隔離的同一 worktree 同時取得 writer lease。
15. raw transcript segment 有大小／總量上限、`0600` 權限與 hash manifest；模型只
    收到有界 sanitized preview 及其與 raw bytes 的差異標記。
16. remote Codex nesting guard 能阻止內層 Codex 再控制同一 fixed terminal。

## 9. 停止條件

遇到以下任一情況立即停止，不以臨時路徑繞過：

- target dirty file 與 overlay baseline 衝突；
- bundle／remote hash 不符，或 installer 回報 unsafe；
- profile identity、hostname、user、project root 任一不符；
- server `--check`、七工具 inventory 或 checkout-local Python 失敗；
- terminal ID 不在 profile allowlist、同一 ID 被重建，或可能接管非 Harness tmux session；
- writer lease 無法可靠釋放、第二 writer 能 force-steal，或多個 terminals 會並行寫入同一 worktree；
- dead shell 會被普通 attach 自動重建，或 nesting guard 無法阻止同 profile 遞迴；
- cursor/hash 無法重算、輸出遺失或 tool timeout 會殺 session；
- transcript 無大小／權限邊界，或 sanitized preview 未標記其與 raw bytes 的差異；
- stage smoke、collect hash、ResearchFlow 邊界或 report-writer 證據退化；
- provider key 出現在任何命令列、Markdown、bundle、log、receipt 或遠端新檔案；
- 必須 force、reset、clean 或覆蓋使用者修改才能繼續。

## 10. 回退策略

每次部署保留 installer backup、manifest、bundle SHA 與 plugin 版本。若 terminal
驗收失敗：

1. 停止建立新 terminal session；
2. 使用 installer ledger 恢復上一版檔案，不碰 ledger 外的 dirty files；
3. 依 plugin-creator 正式流程重裝上一個已驗證 plugin；
4. 新開任務確認三個 stage 工具及 no-API smoke；
5. 臨時 relay 若尚未到 Phase 4，不刪除；若已刪除，只能從有 hash 的 migration
   backup 恢復，不能憑記憶重寫。

## 11. 明確不做

- 不做通用 SSH client 或任意 host terminal。
- 不建立 Web terminal UI。
- 不做 DSH 全域安裝或把 DSH 當 runtime dependency。
- 不做多 profile 自動選擇；只使用 connection policy 明確指定的 profile。
- 不做任意動態 terminal；只使用 profile 宣告的一個或少數 fixed terminal slots。
- 不以 terminal 取代 ResearchFlow、Token Guard、execution spec、registry 或 collector。
- 不在本任務中執行 GPU 實驗、FSFM S0/D1、昂貴 A/B 或未授權研究修改。
- 不在尚未驗收替代路徑前刪除可恢復／診斷能力。

## 12. 成熟設計差距檢查與最小補強

本節只加入會改變可靠性或授權語義的補強，不把 Harness 擴成通用 terminal
framework。查核基準包括 DeepSeek Harness 的 persistent PTY service、tmux control
mode／attach 行為、OpenSSH connection multiplexing，以及 MCP tool annotations。

| 潛在問題 | 嚴重度 | 最小補強，不做大改 |
| --- | --- | --- |
| 合併 send/read 後無法正確標示 read-only 與 approval | 高 | 已改為 `terminal_send` 與 `terminal_read`；前者可變更狀態，後者唯讀 |
| 兩個 Codex 任務同時向同一 terminal 輸入，或第二個 client 強制接管 | 高 | 每個 fixed terminal 使用 OS-backed exclusive writer lease；第二個 writer 回傳 `busy`，沒有 force-steal；client 消失後由 OS 釋放 lease |
| 多個 fixed terminals 指向同一 dirty worktree 並行修改 | 高 | profile validation 預設拒絕多個 write-capable slots 共用同一 cwd；需要平行寫入時必須使用不同 worktree／cwd 或由使用者明確接受衝突風險 |
| 輸出暫停、timeout 或 inferred idle 被誤認為命令完成 | 高 | `wait_reason` 與 `session_status` 分開；silence/timeout 不能產生成功或 exit-code 判定 |
| top-level shell 已死，`open` 卻悄悄建立新 shell，導致 cwd/env/foreground state 遺失 | 高 | 回傳 `exited`／`needs_restart`；只有明確 maintenance restart 才重建，普通 attach 不自動復活 |
| 固定 terminal 長期存在，cwd、venv、Git branch、dirty state 與最初假設不同 | 中 | 每次 attach 回傳 shell PID、current command、cwd、started time；工作前做短 identity/state probe，但不自動 `cd`、checkout、reset 或清理 |
| tmux output 可能包含 invalid UTF-8、ANSI/OSC control sequences，直接送模型或 UI 會混淆 | 中 | raw bytes 只寫受限檔案並 hash；tool result 回傳明確標記的 sanitized UTF-8 preview、replacement count 與 truncation，不把 preview 假裝成 raw transcript |
| 永久 terminal 造成 transcript 無限增長 | 中 | backend 內部採固定大小 segment、總量配額與 retention；封閉 segment 寫 hash manifest，rotation 不暴露給模型管理 |
| transcript／shell history 記下 provider key 或敏感命令 | 高 | transcript、metadata、lock 皆 `0600`；provider env 不得在 terminal source；paths 排除 bundle/ResearchFlow；preview 不做秘密回顯。若秘密已進 raw log，停止並按 credential incident 處理，不靠靜默 redaction 假裝不存在 |
| remote Codex 在 fixed terminal 內再次呼叫同一 terminal controller，形成遞迴或自鎖 | 中 | 啟動 remote Codex 時注入非秘密 nesting marker；內層 Codex 不得 attach/send 同一 profile terminal，但仍可使用被允許的 stage lane |
| tmux name 被其他程序碰巧建立，或 metadata 指向重啟前的舊 session | 高 | 除 tmux name 外，同時核對 remote UID、tmux socket、session/pane ID、profile marker、metadata version；任一不符即 fail closed，不採用名稱相同的未知 session |
| MCP／SSH transport 重連被誤認為 terminal 重啟 | 中 | terminal durability 由遠端 tmux 保證；SSH 可以重新連線。可使用既有 profile 的安全 connection sharing，但 correctness 不依賴永久 SSH master |

DeepSeek Harness 的正式 terminal service 把 send 與 read 分開、每個 PTY 只允許一個
active send，並把 `timeout`／`inferred_idle` 與 session exit 分開；它也使用 exact-owner
fencing。此處因需求是跨 Codex 任務共用 fixed terminal，不能直接照搬「agent 結束即
cleanup」，因此以 profile allowlist、OS writer lease 和不自動 terminate 取代；這是
針對本系統的推論，不是 DSH 原契約。

tmux 官方文件說明 detach 後 session 會繼續存在，`new-session -A` 可 attach-or-create；
control mode 建議使用不歧義的 session/window/pane IDs，且明確警告 pane output 可能
不是合法 UTF-8並含 escape sequences。因此固定名字只能作入口，不能作唯一身份，
也不能把 raw PTY bytes 直接當普通 Markdown 文字。

OpenSSH 的 `ControlMaster`／`ControlPersist` 可重用底層 transport，但它解決的是連線
成本，不是 terminal state。故本規劃不要求 SSH master 永久不斷：MCP/SSH 可以重連，
固定 terminal 本身不得因 transport 斷線被關閉或重建。

參考來源：

- [DeepSeek Harness terminal tool contract](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/terminal/tool-terminal/README.md)
- [DeepSeek Harness persistent PTY subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/terminal.md)
- [tmux control mode](https://github.com/tmux/tmux/wiki/Control-Mode)
- [tmux attach/detach semantics](https://github.com/tmux/tmux/wiki/Getting-Started)
- [OpenSSH `ssh_config(5)` multiplexing](https://man.openbsd.com/ssh_config.5)
- [MCP tool annotations and result contract](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-06-18/schema.mdx)

## 13. 本規劃的批准點

批准本規劃代表同意以下架構決策，而不是立即授權遠端修改：

1. 保留 stage lane，新增 terminal lane。
2. terminal 由 v0.1 的五工具收斂為四工具；send/read 因權限語義不同而保留分離。
3. 首版固定一個 profile、一個或少數預先宣告且持久的 managed terminals；不做
   任意建立、session list 或每次操作開關 terminal。
4. 取消「terminal transcript 等同科學證據」的假設。
5. 正式替代驗收後，刪除臨時 relay、`tmux-probe` 與 active 三工具硬限制。
6. 所有刪除均在替代品通過本地及 EXAMPLE_WORKER_A/FAN 驗收後進行。
