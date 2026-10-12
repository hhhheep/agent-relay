# Harness V3 持久連線、多路 Terminal 與受監督命令執行任務書

- 版本：v0.1
- 日期：2026-09-29（Asia/Taipei）
- 狀態：IMPLEMENTATION TASKBOOK / 尚未執行
- 適用 profile：`example_worker_a_fan`
- 本地 folder：`C:\Users\example-user\Desktop\example-research`
- 遠端 workplace：`/srv/example-workspaces/fan-workspace`
- Harness run root：`/srv/example-workspaces/harness-runs`
- 主要約束：每個 workplace 維持一條長連線，禁止為每個命令反覆建立 SSH／server 連線
- managed_markdown：未宣告；本文是本地工程契約，不直接同步 ResearchFlow

## 0. 權威範圍

本任務書是 terminal transport、互動 session、ordinary command、WAIT completion 與故障恢復的下一版權威契約。它取代以下舊假設：

- 一個 profile 只能有一個實際 terminal；
- `terminal_exec` 必須先 attach 唯一 tmux terminal；
- ordinary command 必須注入持久 shell 才能共享 workplace；
- terminal 重啟可以等同 command recovery；
- `status.json=running` 可在沒有活 process 的情況下永久保留。

`HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_TASKBOOK.md` 對 Task Capsule、Artifact Clipboard、selector、apply engine、release 與部署仍有效；若 terminal／command／WAIT 條款與本文衝突，以本文為準。

本文存在不代表可直接部署。實作者必須按 G0–G8 順序完成、驗證、conflict-safe dry-run，再取得適用的遠端變更授權。

## 1. 問題定義

### 1.1 為什麼仍需要單一持久 transport

反覆關閉、重開 SSH／server 連線不符合實際使用，且可能觸發遠端安全限制、連線節流或 IP 封禁。故 transport 必須：

- 每個 workplace 只維持一個受管理的長連線；
- MCP client、terminal 與 command 共用該 transport；
- reconnect 有退避、上限與 receipt，不得形成快速重連迴圈；
- 一次工具 timeout 不得關閉 transport 或遠端工作。

### 1.2 現行真正的設計漏洞

現行實作把「一條 transport」錯誤收縮成「一個 terminal」：

```text
一條 SSH/MCP transport
└── 唯一 tmux terminal main
    ├── interactive input
    ├── ordinary terminal_exec
    ├── output collection
    └── command completion
```

因此任一命令污染 shell、pane 退出、writer lease 卡住或 receipt 未 finalize，就會拖死整個 workplace 的工作入口。

### 1.3 已重現的故障鏈

```text
非 Git workplace root
→ 盤點腳本執行 git status
→ request.sh 內 set -e
→ request.sh 被 source 到持久 shell
→ shell option／非零狀態污染父 shell
→ finalize 未執行
→ status.json 永久 running
→ WAIT 沒有 terminal event
→ manual resume 只能再次看到 running
→ Capsule 與正式 stage 永遠無法開始
```

此故障同時暴露：命令隔離不足、終態來源錯誤、單 terminal 耦合、缺少 orphan reconciliation、manual resume 未完整落地，以及測試只模擬 finalize 而未執行真實 shell。

## 2. 最終架構

```text
Local folder
└── Workplace identity marker
    └── Persistent Transport Manager          # 每 workplace 一條連線
        ├── MCP control channel
        ├── Terminal Hub                      # 一個 tmux server/session namespace
        │   ├── interactive/main              # 子 terminal / tmux window
        │   ├── interactive/task-<id>          # 子 terminal / tmux window
        │   └── interactive/debug-<id>         # 子 terminal / tmux window
        └── Job Supervisor                    # 不依附任何 tmux pane
            ├── job/<command-id>               # isolated process / transient unit
            ├── job/<command-id>
            └── job/<command-id>
```

硬性區分：

1. **Transport**：遠端連線；單一、長駐、可重連。
2. **Interactive terminal**：需要持續 shell 狀態或人工輸入的子 terminal；可有多個。
3. **Ordinary job**：有明確開始／結束的命令；由 OS process supervisor 執行，不使用 tmux。
4. **Task Capsule**：研究工作與證據的邏輯所有者；不等於 terminal 或 process。

## 3. 不可違反的 invariants

### 3.1 一條連線不等於一個 terminal

- 同一 workplace transport 下允許多個 Harness-owned logical terminals。
- 新增子 terminal 不得新增 SSH 連線或 MCP server process。
- 所有子 terminal 預設從同一個 declared workplace cwd 出發。
- 每個子 terminal 有獨立 terminal ID、tmux window/pane、generation、transcript、lease 與 owner task。
- 一個子 terminal 退出不得關閉 transport、其他 terminal 或 ordinary jobs。
- 不掃描、不接管使用者自行建立的 tmux sessions/windows。

### 3.2 Ordinary command 不依附 terminal

- `terminal_exec` 不得要求先呼叫 `terminal_open`。
- `terminal_exec` 不接受或依賴 interactive terminal 的 shell option、cwd history、aliases 或 prompt。
- 命令需要的 cwd、environment profile、Task Capsule 與 output budget必須在 request 中明示或由 workplace profile 決定。
- 命令腳本以獨立 process 執行，禁止 `.`／`source request.sh`。
- `set -e`、`cd`、`trap`、`exit`、shell function 或環境修改只能影響該 job。

### 3.3 OS process status 是唯一命令終態來源

- 使用 POSIX child-process／`waitpid` 語義取得 exit status。
- Python backend 只能用 `subprocess.Popen.poll()`、`wait()` 或等價 async subprocess 取得終態。
- 若 EXAMPLE_WORKER_A user systemd 可用，長命令優先使用 profile 宣告的 transient service backend，保留 unit、MainPID、ExecMainStatus 與 Result。
- 若 user systemd 不可用，必須部署單一常駐 Job Supervisor daemon；不得把 tmux 當 fallback job supervisor。
- Profile 在部署時固定一個 job backend；不得逐命令隨機切換。

### 3.4 `running` 必須有活 owner

每次查詢都要 reconciliation：

- `RUNNING` 且 process/unit 存活：保持 `RUNNING`。
- process/unit 已退出且有 exit status：寫入 `SUCCEEDED` 或 `FAILED`。
- process/unit 不存在且無可驗證 exit status：寫入 `LOST`，不得繼續回 `RUNNING`。
- terminal generation 消失不得改寫 job 為成功；job 與 terminal 分開處理。
- MCP／App／transport 重啟後，reconcile 必須可重放且 idempotent。

### 3.5 WAIT 不擁有命令狀態

- Job Supervisor 先原子寫入 terminal event，再由 WAIT runtime 消費。
- `WAITING_EXTERNAL` 只表示模型本輪應停止，不表示命令停止更新。
- `host_resume_mode=manual` 時，job 仍必須在背景形成最終 receipt；manual resume 只負責把已存在的事件送回聊天。
- 一次 manual resume 只 reconcile／collect 一次；仍未完成就再次停止，不形成 polling loop。

## 4. Backend 決策

### 4.1 Persistent Transport Manager

每個 `(workplace_identity, profile_id)` 只能有一個 transport owner：

- launcher 啟動時先取得 transport lock；
- client refcount 歸零只 detach，不立即關閉遠端 transport；
- transport health 與 terminal/job health 分開；
- reconnect 採 exponential backoff、jitter、最大嘗試次數與 cooldown；
- cooldown 期間 fail closed，不以平行 SSH 繞過；
- receipt 記錄 connect count、disconnect reason、backoff 與 remote identity，不記 credential。

### 4.2 Terminal Hub

Terminal Hub 使用一個 Harness-owned tmux namespace，子 terminal 映射為受控 windows/panes：

```text
tmux session: sa-<workplace-hash>
windows:
  interactive-main
  task-<short-task-id>
  debug-<short-command-id>
```

要求：

- `terminal_open(terminal_id)` 建立或 attach 指定子 terminal；
- 每個 terminal 最多一個輸入 writer，讀 transcript 可多 consumer；
- `terminal_close(action=detach)` 只 detach 該 owner；
- `terminal_close(action=terminate)` 只終止指定子 terminal；
- terminal hub 本身不得因一個 pane dead 而整體 restart；
- 整個 hub restart 屬於高階 maintenance，必須先列出受影響的 Harness-owned panes 和活 job（活 job正常應為零依賴）。

### 4.3 Job Supervisor

Ordinary job 的 canonical request：

```json
{
  "command_id": "...",
  "task_id": "...",
  "work_unit_id": "...",
  "cwd": "/srv/example-workspaces/fan-workspace",
  "shell": "/bin/bash",
  "script_sha256": "...",
  "environment_profile": "workplace-default",
  "timeout_seconds": null,
  "created_at": "..."
}
```

執行方式：

```text
/bin/bash --noprofile --norc <request.sh>
```

stdout/stderr 直接寫檔，不能使用容易因 pipe buffer 塞滿而 deadlock 的未消費 PIPE。外層 supervisor 從 child/unit 取得 exit status，原子寫入 receipt；finalize 不能放在 request script 後方依賴 shell 繼續執行。

### 4.4 Profile capability probe

G0 必須只讀確認：

- user systemd 是否可用；
- tmux 版本與多 window 能力；
- process group／signal／filesystem semantics；
- transport 的實際長連線機制與 server 安全限制；
- 最大並行 terminal／job 配額。

結果寫進 profile capability receipt。EXAMPLE_WORKER_A 的 production backend 必須明確固定，不能只寫「systemd 或 Popen 皆可」。

## 5. 狀態模型

### 5.1 Transport

```text
DISCONNECTED → CONNECTING → READY
                     ↓        ↓
                  BLOCKED ← BACKOFF
```

### 5.2 Interactive terminal

```text
ABSENT → STARTING → RUNNING → DETACHED
                    ↓   ↑
                  EXITED → RESTARTING
```

### 5.3 Ordinary job

```text
PREPARED → RUNNING → SUCCEEDED
                   → FAILED
                   → CANCELLED
                   → LOST
```

終態不可逆；相同 `command_id` 的重新查詢只回相同 receipt，不得重跑命令。

### 5.4 Task Capsule

Task Capsule 的 active work unit、writer lease 與 artifact refs 繼續以 CAS 管理。Job concurrency 不得繞過 task writer lease：可平行執行多個 read-only job，但會寫同一 authority target 的 job 仍只能有一個 writer。

## 6. Model-facing 工具契約

### 6.1 保留 11 個工具，不新增散裝工具

維持現有 11-tool surface。只修正 terminal 語義與 schema，不新增 `terminal_list_all`、`job_status`、`terminal_recover` 等平行工具。

### 6.2 `terminal_open`

- 僅管理 interactive child terminal。
- `terminal_id` 可為 profile default 或 Task Capsule 派生的 Harness-owned ID。
- `restart=true` 只重啟該子 terminal，不重啟 transport 或 hub。
- 回傳 hub ID、window ID、generation、cwd、owner、lease 與 transcript ref。

### 6.3 `terminal_exec`

- 預設使用 Job Supervisor，不再使用 tmux。
- 新命令輸入：`command`、`task_id`、`work_unit_id`、`cwd`、`wait_ms`、preview budget。
- 查詢輸入：`command_id`、`task_id`、`wait_ms`、preview budget。
- 移除 ordinary job 的 `terminal_id` requirement 與 `terminal_not_attached` 錯誤。
- 回傳 process/unit identity、state、exit status/signal、stdout/stderr refs、hash、bytes、bounded previews 與 event ID。

### 6.4 `terminal_send`／`terminal_read`／`terminal_close`

- 僅處理 interactive child terminal。
- 不得用 `terminal_send` 模擬 ordinary job completion。
- raw transcript 不是 ordinary job receipt。
- close 子 terminal 不影響 Job Supervisor 內的命令。

## 7. 錯誤與恢復契約

至少新增或修正：

- `transport_backoff`
- `transport_blocked`
- `terminal_not_found`
- `terminal_exited`
- `terminal_writer_busy`
- `job_not_found`
- `job_backend_unavailable`
- `job_failed`
- `job_lost`
- `job_receipt_incomplete`
- `job_scope_conflict`

`job_lost` 必須包含最後已知 PID/unit、process existence probe、terminal independence、stdout/stderr refs 與安全下一步。不得建議用 maintenance restart 假裝恢復命令。

## 8. 具體修改名單

### 8.1 Production source

1. `solution_agent/harness/remote_terminal.py`
   - 從單一 fixed tmux session 改為一個 hub、多個 Harness-owned child terminals；
   - lease、generation、metadata、transcript 全部改為 per-terminal；
   - restart scope 限縮到指定 child terminal；
   - 不再承擔 ordinary command lifecycle。

2. `solution_agent/harness/terminal_commands.py`
   - 移除 `. request.sh`／`source request.sh`；
   - 改成 Job Supervisor client 與 receipt reader；
   - 以 OS process/unit status finalize；
   - 新增 orphan reconciliation 與 `LOST`；
   - stdout/stderr 直接寫檔並原子產生 manifest。

3. 新增單一 `solution_agent/harness/job_runtime.py`（或等價一個 owner 模組）
   - profile-fixed backend；
   - process/unit launch、poll、wait、cancel、reconcile；
   - atomic receipt 與 exactly-once terminal event；
   - 不複製到 MCP wrapper。

4. `solution_agent/harness/mcp_server.py`
   - `terminal_exec` 改接 Job Supervisor；
   - 查詢 command 不要求 terminal attach；
   - interactive terminal tools 改接 child terminal；
   - WAIT 只消費 job events；
   - schema 和 annotations 如實更新。

5. `solution_agent/harness/wait_runtime.py`／`wait_monitor.py`
   - command terminal event 與 manual-resume receipt；
   - server/client restart 後重放未消費事件；
   - 不因模型 turn 結束停止 job finalization。

6. Profile／launcher
   - 宣告 transport persistence、terminal quota、job quota、job backend；
   - 加入退避／cooldown，不增加 model-selected SSH 參數；
   - 保留 `.solution-agent/workplace.id` exact-byte handshake。

7. Plugin skill
   - 明示一條 transport 可有多個 child terminals；
   - ordinary command 不需 `terminal_open`；
   - WAIT manual fallback 是一次恢復，不是 polling；
   - terminal restart 不能修復 job receipt。

### 8.2 命令與盤點腳本

- 所有 optional Git 探測先用 `git -C <path> rev-parse --is-inside-work-tree`；
- 非 Git workplace 記錄 `git_repository=false` 後繼續；
- optional probe 不得因 `set -e` 中止整份盤點；
- required probe 失敗則形成明確 `FAILED` receipt。

### 8.3 必須刪除，不保留 backup code

- `source request.sh` wrapper；
- ordinary job 對唯一 tmux terminal 的依賴；
- `terminal_exec` 的 attach prerequisite；
- 全 profile 單一 terminal writer lease；
- 只有 `status.json`、沒有 process reconciliation 的永久 `running`；
- 把 terminal restart 當作 command recovery 的文件與 next_action；
- 以 FakeManager 後手動呼叫 `finalize()` 冒充 end-to-end shell 驗證的唯一測試；
- 任務書與 skill 中「一個 fixed slot 就是一個實際 terminal」的舊說法；
- `.bak`、`_old`、`legacy`、註解掉的舊 wrapper或第二套 job backend。

## 9. 實作階段

### G0：能力與來源盤點

- 驗證 authoritative source、installed plugin、remote runtime；
- 只讀探測 user systemd、tmux、transport 與連線限制；
- 固定 EXAMPLE_WORKER_A production job backend；
- 建立 baseline failure fixture。

出口：有 capability receipt 與唯一 backend 決策。

### G1：真實 shell failure fixture

- 真 bash 執行 `set -e; false`；
- 驗證 parent／transport 不退出；
- 驗證 exit code、stderr 與 receipt；
- 重現舊版永久 `running`，測試先紅。

出口：測試不是 FakeManager 手動 finalize。

### G2：Job Supervisor

- isolated process launch；
- stdout/stderr files；
- process/unit status、signals、timeout、cancel；
- atomic terminal receipt；
- orphan reconciliation。

出口：成功、非零、signal、server restart、process missing 五類都進入不可逆終態。

### G3：Terminal Hub multiplexing

- 一個 hub、多 child terminals；
- per-terminal lease/generation/transcript；
- attach/detach/restart 單一 child；
- child failure isolation。

出口：三個 child terminal 共用同一 transport，殺一個不影響另外兩個與 job。

### G4：MCP schema 與 Task Capsule

- `terminal_exec` 脫離 terminal attach；
- task/work-unit binding；
- read-only job concurrency 與 writer lease；
- 11-tool inventory 不增加。

出口：新聊天可直接執行 ordinary job，無需先 open terminal。

### G5：WAIT 與 recovery

- job completion event；
- exactly-once consumption；
- manual resume 一次 reconcile；
- App/MCP/transport restart recovery。

出口：模型停止後 job 自行 finalizes；下次 resume 直接 collect terminal receipt。

### G6：盤點與非 Git 修復

- 修正 FSFM 前置盤點；
- 非 Git root 不失敗；
- required/optional probe 分離。

出口：原 Command 情境可完成並形成明確 receipt。

### G7：刪除舊路徑與 package

- 同一 vertical slice 移除舊 source-wrapper；
- 更新 plugin、skill、manifest、schema、tests；
- backup-code audit 為零。

出口：只有一套 production ordinary-job backend。

### G8：零衝突部署與真實任務驗收

- conflict-safe dry-run；
- apply、remote server check、新 App chat；
- 先 no-API smoke，再恢復原 FSFM 任務；
- 不以測試數量替代真實執行。

出口：FSFM 任務可通過盤點、Capsule CAS、smoke stage、event-driven wait，再進入方法 A。

## 10. 必須通過的驗收

### A1. 單一 transport

建立三個 child terminals 與三個 ordinary jobs；remote transport/server 連線數不得隨之增加。

### A2. Multiplex isolation

終止一個 child terminal；其他 child terminals、jobs、MCP 與 transport 持續正常。

### A3. `set -e` isolation

執行 `set -e; false`，取得 `FAILED` 與非零 exit code；parent shell、transport 與下一個 job 不受影響。

### A4. No attach prerequisite

全新 MCP client 不呼叫 `terminal_open`，直接用 `terminal_exec` 完成 `pwd` 與 Unicode smoke。

### A5. No permanent running

模擬 supervisor crash、PID 消失、unit collect、receipt 半寫；reconcile 後只能為 `FAILED` 或 `LOST`，不得永久 `RUNNING`。

### A6. Exact output

stdout/stderr、exit code、signal、bytes、SHA-256、head/tail preview 可核對；長輸出不塞住 child process。

### A7. Transport backoff

模擬連線拒絕；只允許受控退避與 cooldown，不產生快速重連或平行 SSH。

### A8. Manual WAIT

模型在等待時零輪詢；job 背景完成並寫 receipt；一次 manual resume 收到完成事件並繼續。

### A9. Automatic callback honesty

沒有 Codex host callback 時仍標 `MANUAL_FALLBACK`，但這不影響 command terminalization。不得把 manual fallback 說成 job 仍在執行。

### A10. Non-Git workplace

在非 Git root 跑盤點，回報 `git_repository=false` 並完成其他 probes。

### A11. Restart scope

重啟一個 child terminal 不改寫 job receipt、不終止 transport、不影響其他 panes。

### A12. 真實 FSFM lifecycle

以新 Task Capsule 完成：release gate → preflight → CAS claim → no-API smoke stage → WAIT → collect → 方法 A。不得重跑已完成的 S0 boundary。

## 11. 完成標準

只有同時滿足以下條件才可標 `OPERATIONALLY_COMPLETE`：

1. A1–A12 全部有真實 receipt；
2. EXAMPLE_WORKER_A 實際只有一條受管理 transport；
3. 至少三個 child terminals 在同一 hub 下通過隔離測試；
4. ordinary job 完全不依附 tmux；
5. 所有命令都有 OS 可核實終態，沒有永久 `running`；
6. WAIT 不輪詢且不阻止背景 finalization；
7. App 新聊天載入正確 release/schema；
8. 原 FSFM 任務實際越過 Capsule/smoke 並開始方法 A；
9. 舊 source-wrapper、全局單 terminal 假設與 backup code 已刪除；
10. acceptance report 清楚區分 transport、terminal、job、Capsule 與 host callback。

## 12. 停止條件

- remote capability 無法固定唯一 job backend；
- transport 連線數無法觀察；
- dry-run conflict／unsafe；
- child terminal 會接管非 Harness tmux window；
- job supervisor 只能靠模型 polling 完成；
- job exit status 無法從 OS 驗證；
- 修復必須保留第二套 production backend；
- 真實 FSFM 驗收會碰觸未授權資料、checkpoint 或昂貴 API。

停止時保留 evidence 並標 `BLOCKED` 或 `MANUAL_FALLBACK`，不得把完成標準改鬆。

## 13. 交付物

- source/test/skill 修改清單與 LOC delta；
- 刪除清單與 backup-code audit；
- capability probe 與 backend 決策 receipt；
- transport connect/reconnect ledger；
- terminal hub／child terminal inventory；
- job receipts 與 failure recovery evidence；
- local、package、installed App、remote runtime identity；
- zero-conflict deployment ledger；
- A1–A12 acceptance report；
- FSFM 真實 lifecycle 結果與仍存在的限制。

## 14. 給實作者的短指令

```text
以 HARNESS_V3_PERSISTENT_TRANSPORT_MULTIPLEXED_EXECUTION_TASKBOOK.md 為 terminal、
ordinary job 與 WAIT recovery 的權威契約。保留每 workplace 一條長 transport，
但把 transport、interactive child terminal、ordinary job、Task Capsule 完全拆開。
先用真 bash 重現 source + set -e 的永久 running，再採 OS child process／profile-fixed
supervisor 實作；tmux 只保留互動用途。每個 child terminal 必須獨立，terminal_exec
不得要求 attach。所有 running 狀態都要 reconcile 活 PID/unit；不存在就 FAILED/LOST。
同一 vertical slice 刪除舊 source-wrapper 與單 terminal 假設，不留 backup code。
完成本地真 shell 測試、EXAMPLE_WORKER_A zero-conflict 部署、新 App chat smoke 後，才恢復原
FSFM Task Capsule 生命週期；不得以單元測試數量代替真實任務。
```

## 15. 採用的成熟基礎

- POSIX `wait/waitpid`：process termination 與 exit status 的權威語意：<https://pubs.opengroup.org/onlinepubs/9699919799/functions/wait.html>
- Python `subprocess.Popen`：child PID、poll、wait、return code 與 signal 語意：<https://docs.python.org/3/library/subprocess.html>
- GNU Bash：`set -e`／errexit 與 shell/subshell environment 行為：<https://www.gnu.org/software/bash/manual/bash.html>
- systemd transient service／service state：使用 MainPID、ExecMainStatus、Result，而不是 terminal 畫面推測終態：<https://www.freedesktop.org/wiki/Software/systemd/dbus/>
