# Harness V2 整合式專注執行環境任務書

Date: 2026-09-29  
Status: implementation authority  
Scope: WAIT、選看／代寫、Artifact Clipboard、終端結果通道與實際 runtime 串接

> Terminal transport、子 terminal、ordinary command 與 WAIT recovery 的條款已由
> `HARNESS_V3_PERSISTENT_TRANSPORT_MULTIPLEXED_EXECUTION_TASKBOOK.md` 取代。
> 本文其餘 Task Capsule、Artifact Clipboard、selector、apply、release 與部署條款
> 繼續有效；衝突時以 V3 terminal taskbook 為準。

Implementation companion:
`HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_IMPLEMENTATION_DRAFT.md`. This taskbook
defines the outcome and completion gates; the implementation draft defines the
current file-level patch sequence, proposed APIs, reusable code, and the first
three vertical slices. The companion is subordinate to this authority and must
not become a competing completion contract.

## 0. 本文件的權威地位

本文件是 Harness V2「端到端是否完整」的唯一驗收依據。下列既有文件仍然有效，但只作為子模組規格或歷史證據：

- `HARNESS_V2_EVENT_DRIVEN_WAIT_TASKBOOK.md`：WAIT 狀態機與事件語義；
- `HARNESS_V2_TOKEN_EFFICIENCY_AND_CORRECTNESS_TASKBOOK.md`：token、context 與使用量治理；
- `HARNESS_V2_REMOTE_TERMINAL_TASKBOOK.md`：profile-bound terminal；
- `CONTEXT_GATEWAY_PLAN.md`：context pack 與 Task Capsule 的早期設計；
- 各種 `*_IMPLEMENTATION_RECEIPT.md`：只證明其明示的來源樹、測試與日期，不自動證明目前 App 或遠端 runtime 已載入。

後續不得因單一 helper、單一測試套件、單一本地來源樹或單一 receipt 通過，就把整體標記為 completed。

完整狀態必須明確區分：

1. `COMPONENT_COMPLETE`：元件本身及單元測試完成；
2. `SOURCE_INTEGRATED`：唯一權威來源樹已接通所有元件；
3. `PACKAGE_INTEGRATED`：插件封裝、manifest 與 schema 對應同一來源；
4. `RUNTIME_LOADED`：目前 Codex App task 實際載入的工具與 hash 已驗證；
5. `REMOTE_DEPLOYED`：指定遠端 workplace 載入同一 release；
6. `HOST_INTEGRATED`：外層 Codex Goal／thread 能被受支援事件暫停及恢復；
7. `OPERATIONALLY_COMPLETE`：端到端驗收全部通過。

任何一層缺證據時，最終狀態只能是 `PARTIAL`、`MANUAL_FALLBACK` 或 `BLOCKED`，不能寫「已完整實裝」。

## 1. 問題與最終成果

目前問題不是缺少個別功能，而是三組功能沒有共用同一個任務狀態、資料身份和 runtime release：

1. WAIT 有本地 sidecar／事件元件，但 active source、installed plugin、remote source 與 Codex host callback 可能不同步；
2. context pack 能預先選資料，但互動式 terminal 仍按 raw byte stream 回傳，命令 echo 會消耗輸出與模型上下文；
3. 缺少一個 task-scoped、content-addressed Artifact Clipboard，導致同一文件在 terminal、prompt、worker 與遠端之間重複查看、重複輸送、重複寫出；
4. 「選看」和「代寫」沒有共用 artifact identity、base hash、read receipt 與 writer lease；
5. receipt 常只證明某個來源樹，沒有證明目前 Codex task 實際載入該版本。

本任務的最終成果是一套單一控制鏈：

```text
User objective
    -> Task Capsule（唯一任務範圍）
    -> Context admission（只納入必要 artifact refs）
    -> Model decision / implementation（只做需要判斷的工作）
    -> Stage or terminal result channel
    -> WAIT sidecar（等待時零模型呼叫）
    -> Durable material event
    -> Collect and verify
    -> Artifact apply（materialize / compose / patch）
    -> End-to-end acceptance receipt
```

搬運、等待、hash、索引、狀態比較、echo 分離、重複判斷與精確組裝全部由 deterministic code 處理；模型只負責研究判斷、設計、程式修改和真正需要生成的內容。

## 2. 不可違反的整合原則

### 2.1 一個任務只能有一個 Task Capsule

Task Capsule 是單次工作的最小權威範圍，至少包含：

```yaml
schema_version: 1
task_id: stable-id
objective: one auditable outcome
active_work_unit: one-id-or-null
lifecycle_state: PREFLIGHT
source_release_id: sha256-bound-release
authoritative_artifacts: []
working_artifacts: []
read_receipts: []
writer_lease: null
wait_handle: null
next_material_event: null
approval_owner: app-or-user-or-controller
model_budget:
  max_requests: 1
  max_context_tokens_estimate: 24000
scope:
  readable_roots: []
  writable_roots: []
  remote_profile_id: null
```

預設同一個 Task Capsule 只允許一個 `active_work_unit` 和一個 writer lease。背景進度不得自行生成新的模型工作單元；不相干工作只能進入 queue，等待目前工作結束或明確切換。

### 2.2 一個 run state 只能有一個權威寫入者

- stage lifecycle 由 Solution Agent registry／Harness controller 擁有；
- WAIT sidecar只能提交事件，不能同時擁有另一份 task lifecycle；
- Artifact Store 只管理 immutable content 與 alias，不管理實驗狀態；
- usage ledger 只記量測，不得變成第二個 scheduler；
- Goal 是 Codex thread-scoped completion contract，不得被 Harness 假裝成遠端程序監控器。

### 2.3 ID 與 hash 代替全文搬運

跨工具、跨 worker、跨本機／遠端的正常交接只允許傳遞：

- `task_id`；
- `run_id`／`attempt_id`；
- `artifact_id`；
- `sha256`；
- selector；
- compact structured result；
- evidence path。

不得把完整文件、raw stdout 或大表格當作一般交接 payload。

### 2.4 讀與寫必須分開

讀取工具不得改變外部文件；寫入工具必須宣告 target、base hash、作用域、writer lease 與 rollback／conflict 行為。工具 annotation 必須反映真實 side effect，不能靠 skill 文字代替 server-side 驗證。

### 2.5 Compaction 不等於選看

Codex／Responses 的 compaction 是不透明的上下文延續機制，不是 artifact 檢索、read receipt 或精確引用的替代品。選看必須由 Harness 以 deterministic selector 和 hash 提供。

### 2.6 本地 folder 與 remote workplace 一對一

同一個本地 folder 預設永久綁定同一個 remote workplace。電腦或 MCP client 重新啟動不得產生新 binding ID，也不得因新的 process／session／task UUID 改綁；binding ID 必須來自持久 profile。只有本地 folder 確實搬動、profile 正式改名或舊命名遷移時，才可走明示 maintenance rebind。

Maintenance rebind 必須提供 expected old binding ID 與 exact active release ID，並以 CAS 語義核對同一 terminal、tmux session 和 pane，留下 receipt。它只能換綁 folder/workplace identity；不得修改或掩蓋 `release_id`、schema hash、host、workdir 或 source hash 不一致。

## 3. 唯一權威來源與 release identity

### 3.1 先解決目前已觀察到的來源分裂

目前至少存在兩個不同的 Solution Agent 來源樹：

- 本任務 workspace：`tools/solution-agent-harness/`；
- 另一份 active development checkout：`C:/Users/example-user/Desktop/ai-tools/solution-agent/`。

workspace 來源含 `wait_contracts.py`、`wait_runtime.py`、`wait_monitor.py` 與相關測試；另一份已檢查的 MCP source 沒有同等 WAIT surface。實作者第一步不得直接 copy files，而要先產生 `source_inventory.json`：

```json
{
  "schema_version": 1,
  "candidate_roots": [
    {
      "path": "absolute path",
      "commit": "commit-or-null",
      "tree_sha256": "sha256",
      "dirty": true,
      "capabilities": ["wait_sidecar", "context_gateway"]
    }
  ],
  "selected_authoritative_root": "absolute path",
  "selection_reason": "reviewed reason"
}
```

若兩邊有意義變更，必須做三方合併或列出保留／捨棄決策；不得用整目錄覆蓋。

### 3.2 每次 release 必須有同一份 manifest

新增 `release_manifest.json`，至少綁定：

```json
{
  "release_id": "harness-v2-focus-<sha256-prefix>",
  "source_root_sha256": "...",
  "source_commit": "...",
  "plugin_version": "...",
  "plugin_manifest_sha256": "...",
  "mcp_schema_sha256": "...",
  "python_module_hashes": {},
  "skill_sha256": "...",
  "created_at": "ISO-8601"
}
```

本機封裝、App installed plugin 與遠端部署都必須回報同一 `release_id`。只比較版本字串不夠；版本相同但 bytes 不同必須 fail closed。

### 3.3 新增只讀 runtime identity

MCP server 啟動資訊或現有只讀 status surface 必須可回傳：

- `release_id`；
- `source_root_sha256`；
- `mcp_schema_sha256`；
- `profile_id`；
- `capabilities`；
- `host_resume_mode: supported | manual | unavailable`。

不一定要新增一個 top-level tool；可以併入 `stage_status` 的 server metadata。但驗收腳本必須能在不呼叫模型、不啟動遠端作業的情況下讀到。

## 4. 統一架構與模組責任

```text
┌──────────────── Codex / Controller ────────────────┐
│ Task Capsule                                       │
│  ├─ objective / active_work_unit                   │
│  ├─ artifact refs / read receipts / writer lease   │
│  └─ wait handle / approval owner / budgets         │
└───────────────┬────────────────────────────────────┘
                │ stable IDs + structured contracts
┌───────────────▼────────────────────────────────────┐
│ Harness MCP                                         │
│  ├─ Stage lifecycle + WAIT disposition              │
│  ├─ Terminal command result adapter                  │
│  └─ Artifact read/write facade                       │
└───────┬────────────────┬──────────────────┬─────────┘
        │                │                  │
┌───────▼──────┐ ┌───────▼────────┐ ┌──────▼─────────┐
│ WAIT runtime │ │ Artifact Store │ │ Terminal Store │
│ event/ack    │ │ CAS/index/ref  │ │ raw/result     │
│ sidecar      │ │ receipt/lease  │ │ stdout/stderr  │
└───────┬──────┘ └───────┬────────┘ └──────┬─────────┘
        └────────────────┴──────────────────┘
                         │
               verified material event
```

### 4.1 WAIT runtime

重用並整合：

- `solution_agent/harness/wait_contracts.py`；
- `solution_agent/harness/wait_runtime.py`；
- `solution_agent/harness/wait_monitor.py`。

它負責 lifecycle transition、event、ack、dedup、sidecar heartbeat 和 compact handoff，不負責模型推理。

### 4.2 Artifact Store

新增 task-scoped content-addressed store。建議實體佈局：

```text
state/artifacts/
  objects/sha256/ab/<full-sha256>/content
  objects/sha256/ab/<full-sha256>/manifest.json
  tasks/<task_id>/aliases.json
  tasks/<task_id>/read_receipts.jsonl
  tasks/<task_id>/writer_lease.json
```

artifact object 永遠 immutable；alias 可變，但 alias 更新必須留下舊 target 與事件。遠端缺少該 hash 時才傳輸一次，禁止模型代傳內容。

### 4.3 Selector／Indexer

只索引允許的 artifact，不自動掃描整個磁碟。支援：

- text line range；
- Markdown heading；
- JSON Pointer；
- YAML path；
- CSV／TSV columns、row range 與確定性 filter；
- log command/result segment；
- exact text／regex search；
- old/new hash diff。

初版不得要求 embedding service；SQLite FTS 或確定性索引足夠。embedding 只能是可選 enrichment，不得成為啟動依賴或新的隱性 token 消耗。

### 4.4 Terminal Result Adapter

保留 raw transcript，但正常命令使用 command result channel。每次命令產生：

```text
terminal/<terminal_id>/commands/<command_id>/
  request.json
  stdout.bin
  stderr.bin
  status.json
  result_manifest.json
```

命令 echo 只存在 raw transcript，不計入正常 result preview。

### 4.5 Apply Engine

所有代寫和搬運共用一個受保護 apply engine：

- `materialize`：原樣把 artifact 寫到 target；
- `compose`：依 recipe 組裝多個 refs／selectors；
- `patch`：以 `base_sha256` 為前置套用小型 patch。

三種模式都必須先產生 plan／diff，再做 scope、writer lease、base hash 與權限檢查，最後原子寫入。

## 5. Model-facing 工具表面

工具表面按使用者結果設計，不直接鏡射內部 class。讀和寫明確分離。

### 5.1 Stage lifecycle：保留並改接 Task Capsule

1. `stage_execute`
   - 輸入：reviewed spec path、task capsule ID、release ID；
   - 輸出：job/run/attempt IDs、compact state、WAIT disposition；
   - 若進入等待，必須回傳 `wait_handle`、`state_version`、`resume_trigger`、`manual_resume_required`。

2. `stage_status`
   - 只讀；只回傳 material state change、compact usage、runtime identity；
   - 相同 state version 預設不重送全文。

3. `stage_collect`
   - 驗證 terminal state、receipt、artifact hashes；
   - 回傳 artifact refs，不回傳完整 raw logs。

### 5.2 Terminal：把互動與命令結果分開

4. `terminal_open`：保留。

5. `terminal_exec`：新增。用於有開始／結束邊界的一般命令。
   - 輸入：`command`、`terminal_id`、`wait_ms`、result budget；
   - 輸出：`command_id`、`status`、`exit_code`、stdout/stderr artifact refs、bounded previews、hashes；
   - timeout 只表示尚未完成，不得假裝 exit status。

6. `terminal_send`：保留，但只用於 REPL、互動程式與 control keys。
   - 文件必須明示：一般命令不得優先使用它；
   - output 是 raw interactive preview，不是可靠命令結果。

7. `terminal_read`：保留名稱，但明確定義為 raw diagnostic surface。
   - 明確標為診斷 raw transcript；
   - 保留 byte cursor、retention 與 hash；
   - UTF-8 必須在 code-point boundary 截斷；非 UTF-8 以 raw artifact ref 保存。

8. `terminal_close`：保留。

保留既有 `terminal_read` 名稱並把語義收窄為 raw diagnostic surface；不另外新增 `terminal_read_raw` alias。正常命令結果一律走 `terminal_exec`，避免兩套 reader 長期並存。

### 5.3 Artifact read：選看

9. `artifact_inspect`
   - 單一只讀入口，`mode: describe | read | diff`；
   - `describe` 只回 metadata、size、hash、outline、schema、可用 selectors，不回完整內容；
   - `read` 必須帶 artifact ID／hash 和 selector，可選 heading、range、pointer、table slice、search 或 terminal result segment；
   - `diff` 比較兩個 immutable artifacts，或 artifact 與 target current hash；
   - 每次有 hard byte/token estimate limit並回 read receipt；大型 diff 只回 compact summary 加 diff artifact ref。

### 5.4 Artifact write：剪貼板與代寫

10. `artifact_store`
    - 從已允許 path、stage artifact 或 terminal result 捕獲 immutable object；
    - 不接受把大段模型文字當作正常搬運方法；
    - 去重後回傳既有 artifact ID。

11. `artifact_apply`
    - `mode: materialize | compose | patch`；
    - 寫入前要求 writer lease；
    - patch 必須帶 `base_sha256`；
    - compose recipe 只能引用 artifact refs 與 selectors；
    - 回傳 before/after hash、diff ref、write receipt 與 rollback path（若政策允許）。

不要為 clipboard 增加 OS clipboard 讀寫。系統剪貼板缺少 immutable identity、scope、provenance、版本和遠端去重，不能承擔研究／程式工作交接。

## 6. WAIT 的完整串接

### 6.1 狀態流程

```text
CREATED
  -> PREFLIGHT
  -> RUNNING
  -> WAITING_EXTERNAL
  -> MATERIAL_EVENT_RECEIVED
  -> COLLECTING
  -> REVIEW_REQUIRED | CLOSED | FAILED
```

必須另存正交狀態：

- `execution_status`；
- `observation_status`；
- `artifact_validation_status`；
- `scientific_status`；
- `host_resume_status`。

SSH／monitor 失聯只改 `observation_status`，不能直接把 execution 判定失敗或重新提交。

### 6.2 WAIT disposition

進入 WAIT 時的最小回傳：

```json
{
  "lifecycle_state": "WAITING_EXTERNAL",
  "suppress_model_continuation": true,
  "next_model_action": "END_TURN",
  "wait_handle": "stable-handle",
  "run_id": "...",
  "attempt_id": "...",
  "state_version": 7,
  "resume_trigger": ["completed", "failed", "budget", "review_required"],
  "manual_resume_required": true,
  "host_resume_mode": "manual"
}
```

`manual_resume_required` 只能在已驗證 host callback 後改為 false。

### 6.3 外層 host 邊界

Harness sidecar 完成不等於 Codex App Goal 已被暫停／喚醒。若目前沒有受支援 callback：

- 正常行為是結束模型 turn；
- sidecar 在背景只寫狀態與事件；
- 使用者明確檢查或受支援的外部 adapter 才恢復；
- 禁止用 periodic Goal turn、模型 heartbeat 或 model-driven `stage_status` polling 補洞。

直到真實 App task 觀察到「等待期間零 continuation，完成事件恰好恢復一次」，`HOST_INTEGRATED` 必須保持 false。

## 7. 選看協定

### 7.1 Metadata first

任何未知 artifact 的正常流程：

```text
artifact_inspect mode=describe
  -> choose selector
  -> artifact_inspect mode=read
  -> record read receipt
  -> only request another selector if the decision still lacks evidence
```

禁止預設全文讀取。只有在檔案小於明示閾值且工作確實要求全文時，才可用 `selector: whole`；receipt 必須記錄原因。

### 7.2 Read receipt

```json
{
  "read_receipt_id": "...",
  "task_id": "...",
  "artifact_id": "sha256:...",
  "artifact_sha256": "...",
  "selector": {"type": "heading", "value": "Results"},
  "returned_sha256": "...",
  "returned_bytes": 2048,
  "truncated": false,
  "created_at": "..."
}
```

相同 task、artifact hash 和 selector 再次請求時，工具先回傳既有 receipt 與 `unchanged: true`；除非 caller 明示 `replay_content: true`，不得再次把內容灌進模型。

### 7.3 Context admission

Task Capsule 只放 artifact refs、selector receipts 和必要 compact facts。context pack 必須由這些 refs 生成，不再由 worker 自由掃 repo、outputs、remote roots 或舊 logs。

required evidence 不能為了預算被靜默丟掉。超過 budget 時應回 `CONTEXT_REVIEW_REQUIRED`，列出必要但尚未納入的 evidence。

## 8. 代寫與精確搬運協定

### 8.1 Materialize

用於「把同一份內容放到另一處」。內容不經模型重述。驗證：來源 hash 等於 target hash。

### 8.2 Compose

recipe 範例：

```yaml
mode: compose
target: docs/report.md
base_sha256: null
parts:
  - artifact_id: sha256:aaa
    selector: {type: heading, value: Summary}
  - literal_template_id: report-divider-v1
  - artifact_id: sha256:bbb
    selector: {type: json_pointer, value: /metrics}
format: markdown
```

recipe 是小型結構，不是讓模型重新輸出整篇文件。

### 8.3 Patch

- 必須指定 target current `base_sha256`；
- base 不一致回 `WRITE_CONFLICT`，不得 fuzzy apply 或覆蓋；
- patch 前後都要有 hash；
- 套用後執行相應 parser／lint／test；
- 驗證失敗要保留失敗 artifact，不得把半成品標記為完成。

### 8.4 Writer lease

同一 target 同時只能有一個 lease：

```json
{
  "lease_id": "...",
  "task_id": "...",
  "target": "absolute-or-profile-relative-path",
  "base_sha256": "...",
  "owner_work_unit": "...",
  "expires_at": "..."
}
```

lease timeout 不能直接授權覆蓋；必須先重新讀 target hash 並確認沒有別的有效 writer。

## 9. Terminal 結果協定

`terminal_exec` 必須透過 wrapper 或 side channel 建立可靠邊界，不能從 shell 畫面猜 echo 長度。

正常輸出：

```json
{
  "command_id": "...",
  "terminal_id": "main",
  "status": "completed",
  "exit_code": 0,
  "stdout": {
    "artifact_id": "sha256:...",
    "bytes": 203,
    "preview": "bounded result",
    "truncated": false
  },
  "stderr": {
    "artifact_id": "sha256:...",
    "bytes": 0,
    "preview": "",
    "truncated": false
  },
  "raw_transcript_ref": "sha256:...",
  "started_at": "...",
  "finished_at": "..."
}
```

要求：

- command echo 不進 stdout preview；
- stderr 即使很長也不能被靜默隱藏；
- 長輸出回傳 bytes、hash、artifact ID、首尾小摘錄；
- CJK 在 UTF-8 boundary 截斷；
- `wait_ms` 到期但 command 未完成時回 `running`，不回假 exit code；
- 完成事件可被 WAIT/event runtime 消費；
- raw transcript 繼續保留，供互動式重現與稽核。

## 10. 具體刪改與遷移清單

### 10.1 保留並接入主鏈

- WAIT contracts、runtime、monitor 及其 acceptance tests；
- execution port 的 `check_once()`，但它只負責程序觀察；
- context gateway／context pack builder；
- profile-bound terminal identity、lease 和 raw transcript retention；
- usage ledger 與 Research Token Guard raw evidence；
- stage execute/status/collect 三工具生命週期。

### 10.2 修改

1. `solution_agent/harness/mcp_server.py`
   - 接入 Task Capsule ID 和 release identity；
   - 補 `terminal_exec`、artifact read/write facade；
   - `stage_status` 回傳 material state version；
   - tool annotations 按真實 side effect 重做。

2. `solution_agent/harness/remote_terminal.py`
   - 保留 `_read_raw()` 作診斷；
   - 新增 command result side channel；
   - 修正 UTF-8 邊界；
   - 將一般命令與 interactive send 分開。

3. `solution_agent/context_gateway.py`
   - selected files 改用 artifact IDs／hash／selectors；
   - 引用 read receipts；
   - 禁止 materializer 再次 broad scan。

4. worker／writer path
   - 正常回傳 artifact refs 和 compact result；
   - 全文報告只寫入 canonical target；
   - controller 只審 diff／claim sidecar／必要 selectors。

5. plugin skill
   - 描述何時用 stage、terminal exec、interactive send、artifact read/apply；
   - 不把缺少的 host callback 描述成已支援；
   - 不要求模型手動執行 deterministic housekeeping。

### 10.3 Deprecated，驗收後刪除

- 以模型週期性呼叫 `stage_status` 的 polling 流程；
- normal command 經 `terminal_send` 後再猜 cursor／echo 邊界的流程；
- worker 自由掃 repo／outputs／remote roots／full logs；
- 把 raw stdout/stderr body 直接放進 model-facing result；
- 同一文件以 prompt、heredoc、terminal echo 反覆搬運；
- 沒有 base hash 的整份覆寫；
- 只靠日期或新 task directory 判定前置 artifact 失效；
- receipt 只寫「tests passed」而沒有 source、package、runtime identity。

刪除前必須先有替代路徑、遷移測試和至少一個已驗證 release。不得一邊移除舊路徑、一邊讓部署中的 plugin 仍依賴它。

### 10.4 刪減紀律

- 不在 source tree 建立 `.bak`、`_old`、`_copy`、`legacy_v2` 或註解掉的大段舊實作；歷史交給版本、artifact hash 與 receipt。
- 同一 capability 只能有一個 production implementation；compatibility 只允許薄 adapter，且必須有 removal phase。
- replacement 與舊 caller 的刪除應在同一個 vertical slice 完成，不能把「之後整理」當作完成條件。
- 每個 phase 回報新增、修改、刪除檔案與 source LOC delta；新增多於刪除時必須解釋新增責任，不能以備份或未接線 helper 充數。
- `mcp_server.py` 只保留 tool registration、輸入驗證與 orchestration；演算法不能再複製一份進 tool wrapper。
- 生成快取、build output 和歷史 deployment evidence 不參與一般 source search。可再生快取直接刪除；歷史 evidence 保留在專用目錄並由 ignore 規則隔離。
- acceptance 必須檢查沒有未被 production caller 使用的新模組、沒有兩套 artifact identity、沒有兩套 terminal reader、沒有兩個 lifecycle writer。

## 11. 實作階段與每階段出口

### G0：Authority inventory

工作：

- 盤點所有來源樹、installed plugin、remote checkout；
- 產出 `source_inventory.json`；
- 選定唯一 authoritative root；
- 建立 baseline 測試與既知失敗清單。

出口：所有後續 patch 有唯一 target root；沒有未解釋的同名 divergent file。

### G1：Release identity gate

工作：

- 產生 `release_manifest.json`；
- 在 server metadata／status 暴露 identity；
- 寫 `verify_integrated_runtime.py` 比對 source、package、installed App、remote。

出口：故意替換一個 module byte 時 verifier 必須失敗。

### G2：Artifact Clipboard MVP

工作：

- immutable CAS、task aliases、manifest、scope、quota、GC；
- `artifact_store`、`artifact_inspect` 的 describe 模式；
- 本地 path／stage output／terminal result ingestion；
- remote hash negotiation。

出口：同一內容重複 store 只建立一個 object；跨 task 預設不可見。

### G3：Selective read

工作：

- selector engine、outline/schema、read receipts；
- `artifact_inspect` 的 read/diff 模式；
- context gateway 改用 refs 和 receipts。

出口：10 MB log 的指定 20 行只向模型回傳目標內容和少量 metadata；再次讀取只回 unchanged receipt。

### G4：Terminal result channel

工作：

- `terminal_exec` wrapper、command store、status marker；
- stdout/stderr 分離、raw transcript ref；
- UTF-8／binary／timeout／cancel tests；
- `terminal_read` 遷移為 raw diagnostic surface。

出口：20 KB CJK heredoc 命令產生 200-byte 結果時，一次 bounded response 取得完整結果，沒有 echo 污染或亂碼。

### G5：Apply engine

工作：

- materialize／compose／patch；
- writer lease、base hash conflict、atomic write、diff receipt；
- parser／lint／test hook。

出口：精確搬運 hash 相同；衝突拒絕覆寫；三段 compose 不需要模型重新輸出全文。

### G6：Task Capsule + WAIT integration

工作：

- 一個 active work unit；
- WAIT disposition 寫回 capsule；
- material event 更新同一 state version；
- collect 產生 artifact refs；
- duplicated event／controller restart／SSH loss recovery。

出口：模擬兩小時等待零模型呼叫；完成事件重送十次只 collect 一次。

### G7：Package、App 與 remote deployment

工作：

- 使用正式 cachebuster／plugin install 流程；
- 建立全新的 Codex task，不能沿用已載入舊 MCP schema 的 task；
- runtime identity 與 tool schema 驗證；
- remote dry-run conflict detection，禁止 force overwrite；
- remote no-API smoke。

出口：source、package、App runtime、remote 四方 `release_id` 和 schema hash 相同。

### G8：Host resume 與端到端 shadow run

工作：

- 若有受支援 Codex host event adapter，接入並測 exactly-once resume；
- 若沒有，保留 manual resume 並清楚呈現；
- 執行一個 bounded remote shadow run；
- 收集 model calls、context bytes、tool output bytes、duplicate blocks 和 artifact reuse。

出口：只有真實 App task 驗證成功才能設 `HOST_INTEGRATED=true`；否則整體狀態是 `MANUAL_FALLBACK`，不是 completed。

## 12. 必須通過的端到端驗收

### A1. Runtime parity

同一 verifier 證明 authoritative source、built plugin、installed App runtime 和指定 remote runtime 使用相同 release。任何 hash 不一致即失敗。

本機冷啟動後須從同一持久 profile 恢復相同 folder/workplace binding，不能建立新 remote session。若 folder 真正搬動，只能經 maintenance rebind 並保留 CAS receipt。

### A2. Zero-model WAIT

模擬兩小時、120 次 progress update：WAIT 區間 controller 與 remote model calls 都是 0；普通 heartbeat 只寫磁碟。

### A3. Exactly-once event handling

相同 completion event 投遞十次：一次 state transition、一次 collect、九次 duplicate ack，沒有重跑。

### A4. Crash recovery

在 event publish 前、publish 後 ack 前、collect 後 commit 前分別 crash；重啟後不丟事件、不重複 stage、不重複 apply。

### A5. Observation loss

SSH／monitor 暫時失聯不改 last verified execution、不啟動新 attempt；恢復後接回原 run。

### A6. Selective read budget

10 MB log、100k-line source、large JSON、large CSV 各做一次 selector 測試。送進模型的內容不得超過請求 selector 與固定 metadata budget。

### A7. No repeated content injection

同一 hash、同一 selector 的第二次讀取預設只回 receipt；artifact 變更後才回新內容。

### A8. Terminal result integrity

CJK、ANSI、binary stderr、long stdout、timeout、interactive REPL 全部有 fixture。一般命令結果不含 echo，raw transcript 可追溯。

### A9. Exact transfer and compose

20 KB 文件本地至遠端 materialize 後 hash 相同；由三個 selectors compose 的文件符合 golden file，模型 payload 不含三份原文全文。

### A10. Safe patching

正確 base hash 可套用；錯誤 base hash 回 `WRITE_CONFLICT`；writer lease 衝突不覆寫；驗證失敗不發布 canonical alias。

### A11. Task focus

等待期間背景 progress 不建立新 work unit；不相干事件進 queue。一次任務只能有一個 active work unit 和每個 target 一個 writer。

### A12. Security and scope

task-scoped artifact 不可被另一 task 直接讀；secret pattern、credential file、scope 外 path、path traversal 和 alias swap 都有 negative tests。

### A13. Token／context 誠實性

- provider usage 可得時記實測；
- 不可得時記 `unknown`，不得記 0；
- byte／token estimate 明確標示 estimate；
- 沒有 paired baseline 與 answer-equivalence，不得宣稱節省百分比。

### A14. Upgrade compatibility

舊 `terminal_read` alias、舊 seven-tool client 和新 schema 的遷移行為有測試；不支援的舊 client 收到可理解的錯誤，不得靜默降級成 raw full-output path。

## 13. 完成證據格式

最終只能有一份 canonical acceptance report：

`HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_ACCEPTANCE.md`

它必須包含：

- authoritative source path、commit、dirty state、tree hash；
- release manifest hash；
- plugin package path、version、hash；
- installed App plugin path／identity；
- MCP tool list 和 canonical schema hash；
- local 與 remote runtime identity；
- 每個 G0-G8 的狀態和 evidence paths；
- A1-A14 的實際命令、exit code、receipt/hash；
- baseline 與新增失敗身份；
- host resume 是 supported 還是 manual fallback；
- 未完成限制與 rollback 方法。

禁止只貼測試數量。`140 passed` 可以作為證據之一，但不能替代 runtime parity、deployment 與真實 App 驗收。

## 14. 「完善」的判定

只有同時滿足以下條件才能寫 `OPERATIONALLY_COMPLETE`：

1. A1-A14 全部通過；
2. G0-G8 全部有 evidence，或 G8 明確按產品能力標為不適用且使用者接受 manual fallback；
3. 一個新 Codex task 能用 runtime identity 證明載入正確 release；
4. WAIT 期間零模型呼叫；
5. 普通 terminal 命令不再靠 echo/cursor 猜結果；
6. 同一文件可由 artifact ref 選看、搬運、組裝與 patch，不必反覆讀全文；
7. controller、worker 和 writer 都使用同一 Task Capsule 與 artifact IDs；
8. receipt 可讓另一個 Codex 在不相信敘述的前提下，透過 verifier 和 evidence paths 重驗；
9. 舊散裝路徑已刪除或標為有期限的 deprecated，且不再是正常流程；
10. 沒有把缺少的 Codex host callback 包裝成已完成能力。
11. 最終 source tree 沒有 backup code、未接線 helper 或只為保留舊做法而存在的第二套 implementation。

若只有本地元件測試通過，狀態是 `COMPONENT_COMPLETE`。若 App 還載入舊 source，狀態是 `PACKAGE_MISMATCH`。若 remote 未部署，狀態是 `LOCAL_ONLY`。若沒有自動 Goal callback，狀態是 `MANUAL_FALLBACK`。

## 15. 實作者停止條件

遇到以下情況要停止擴大修改並留下 blocker：

- 權威來源樹無法判定；
- divergent source 含無法安全合併的使用者修改；
- installed plugin path／runtime identity 無法讀取；
- remote dry-run 有 conflict；
- Codex host 沒有受支援 callback；
- artifact scope 或 credential boundary 不明；
- base hash 不一致；
- acceptance 需要用模型輪詢才可能通過；
- 為了縮 context 必須丟掉 required evidence。

停止不等於失敗，可以交付 `BLOCKED` 或 `MANUAL_FALLBACK`；但不得改寫完成標準來製造綠燈。

## 16. 給下一個 Codex 的執行指令

```text
以 docs/HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_TASKBOOK.md 為唯一端到端完成契約。
先執行 G0 source inventory，不要假設目前 checkout、installed plugin 或 remote
runtime 相同。舊 WAIT、token、terminal、context taskbook 只作為子模組規格；receipt
只證明其明示 scope。依 G0-G8 順序實作，每階段先寫 failing acceptance，再做最小
整合修改。不得用模型 polling 實作 WAIT，不得把 raw terminal stream 當一般命令結果，
不得用 prompt 搬運 artifact，不得無 base hash 覆寫文件。最後只更新一份 canonical
acceptance report，並用 verify_integrated_runtime.py 驗證 source/package/App/remote parity。
若 host callback 不存在，誠實交付 MANUAL_FALLBACK，不得宣稱 OPERATIONALLY_COMPLETE。
```

## 17. 官方設計邊界

- Codex Goal 是 thread-scoped、evidence-checked、event-driven continuation，不是任意背景 polling loop：<https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex>
- compaction 是不透明的延續狀態，不是精確選看工具：<https://developers.openai.com/api/docs/guides/compaction>
- MCP 工具應按使用者結果設計、區分 read/write、使用穩定 ID 和 structured output：<https://developers.openai.com/plugins/plan/tools>
