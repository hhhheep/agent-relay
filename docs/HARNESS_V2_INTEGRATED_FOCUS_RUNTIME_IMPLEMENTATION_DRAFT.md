# Harness V2 整合式專注執行環境：實作草稿

Date: 2026-09-29  
Status: executable engineering draft  
Authority: `HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_TASKBOOK.md`

## 0. 我實際準備怎麼做

這不是另一份架構方案。主任務書定義「必須做到什麼、怎樣算完整」；本文件定義「實作者現在要改哪些檔、先寫哪些測試、資料如何流動」。兩者衝突時，以主任務書為準。

目前 checkpoint：

```yaml
phase: DRAFT_READY
authoritative_root_candidate: tools/solution-agent-harness
next_patch: I0_source_and_runtime_identity
do_not_do_yet:
  - 不部署 plugin
  - 不覆蓋 ai-tools/solution-agent
  - 不新增模型輪詢
  - 不刪 terminal_read 相容入口
  - 不宣稱 Codex host 自動喚醒已支援
```

實作順序固定為：

```text
I0 來源與 runtime 身份
 -> I1 Task Capsule
 -> I2 Artifact Clipboard 核心
 -> I3 Selective Read
 -> I4 Terminal Command Result
 -> I5 Apply Engine
 -> I6 WAIT / Capsule / Artifact 串接
 -> I7 MCP surface 與 skill 遷移
 -> I8 package / App / remote 驗證
```

不能跳過 I0 直接寫工具，否則很可能再次把功能寫進未被 App 載入的來源樹。

## 1. 現況程式地圖

### 1.1 已存在、應直接復用

| 能力 | 現有檔案 | 實際用途 |
|---|---|---|
| WAIT schema | `solution_agent/harness/wait_contracts.py` | `WaitRunState`、`WaitEvent`、`WaitAcknowledgement`、`WaitDisposition` |
| WAIT state machine | `solution_agent/harness/wait_runtime.py` | 初始化、事件持久化、dedup、ack、collection transition、monitor 啟動 |
| WAIT sidecar | `solution_agent/harness/wait_monitor.py` | 無模型監控 execution state |
| Stage MCP | `solution_agent/harness/mcp_server.py` | `stage_execute/status/collect` 已接 WAIT runtime |
| Terminal identity/lease | `solution_agent/harness/remote_terminal.py` | profile-bound tmux、writer lease、session identity |
| Raw terminal retention | `solution_agent/harness/remote_terminal.py` | segmented transcript、byte cursor、manifest hash |
| Context pack | `solution_agent/context_gateway.py` | bounded task context、selected file metadata、artifact summaries |
| Repo text search | `solution_agent/tools/search_tools.py` | scope-aware rg/git-grep/PowerShell/Python fallback |
| Patch validation | `solution_agent/tools/patch_tools.py` | write scope、stale file hash、unified diff validation |
| Plugin materializer | `scripts/harness/build_profile_plugin.py` | 保留 profile launcher、更新 plugin tool surface |
| Deployment packaging | `scripts/harness/build_deployment_bundle.py` | bundle 與 manifest |
| Deployment dry run | `scripts/harness/deploy_profile_bundle.py` | conflict-aware deployment |

### 1.2 名字相似但不能當成目標能力

- `solution_agent/tools/artifact_tools.py` 目前是 decision/action request/worker notes 的 renderer，不是 content-addressed Artifact Store。
- `solution_agent/small_task_worker/artifact_writer.py` 是 worker 結果檔寫入器，不是 target writer lease 或 compose engine。
- `solution_agent/tools/patch_tools.py` 有 stale hash 與 patch validation，但其建 patch 流程會暫寫 target 再還原；可抽取 validator，不能直接當成原子 apply transaction。
- `context_gateway.py::_safe_read()` 是 bounded preview helper，不是 selector／read receipt protocol。
- `remote_terminal.py::_read_raw()` 是 raw transcript reader，不知道 command/result 邊界。
- WAIT implementation receipt 只證明 workspace 來源；另一份 `C:/Users/example-user/Desktop/ai-tools/solution-agent` 已觀察到沒有相同 WAIT MCP integration。

## 2. 目標檔案佈局

在唯一 authoritative root 中新增：

```text
solution_agent/harness/
  release_identity.py  # source/package/runtime identity and parity comparison
  task_capsule.py      # capsule schemas plus one atomic versioned store
  artifact_runtime.py  # CAS, schemas, selectors, receipts, aliases and quota
  artifact_apply.py    # materialize/compose/patch transaction
  terminal_commands.py # command request/result protocol

scripts/harness/
  verify_integrated_runtime.py
  build_release_manifest.py

tests/
  test_harness_release_identity.py
  test_harness_task_capsule.py
  test_harness_artifacts.py
  test_harness_artifact_apply.py
  test_harness_terminal_exec.py
  test_harness_integrated_focus_e2e.py
  fixtures/integrated_focus/
```

修改：

```text
solution_agent/harness/mcp_server.py
solution_agent/harness/remote_terminal.py
solution_agent/harness/wait_contracts.py
solution_agent/harness/wait_runtime.py
solution_agent/context_gateway.py
scripts/harness/build_profile_plugin.py
scripts/harness/build_deployment_bundle.py
deploy/overlay-files.txt
deploy/plugin-overlay-files.txt
plugins/solution-agent-harness/.mcp.json
plugins/solution-agent-harness/skills/solution-agent-harness/SKILL.md
tests/test_build_profile_plugin.py
tests/test_remote_terminal.py
tests/test_context_gateway.py
tests/test_harness_event_wait.py
```

## 3. I0：來源與 runtime identity

### 3.1 先寫失敗測試

`tests/test_harness_release_identity.py`：

1. 相同 declared files、不同建立時間，tree hash 相同；
2. 改一個 Python byte，tree hash 不同；
3. `state/`、`executions/`、`__pycache__/`、`.venv/` 不進 source hash；
4. manifest 宣稱的 module hash 與實際不一致時驗證失敗；
5. plugin version 相同但 schema hash 不同時驗證失敗；
6. `host_resume_mode=manual` 不得被渲染成 supported。

### 3.2 `release_identity.py`

預計 API：

```python
@dataclass(frozen=True)
class RuntimeIdentity:
    release_id: str
    source_root_sha256: str
    plugin_version: str
    plugin_manifest_sha256: str
    mcp_schema_sha256: str
    module_hashes: dict[str, str]
    profile_id: str | None
    capabilities: tuple[str, ...]
    host_resume_mode: Literal["supported", "manual", "unavailable"]

def build_source_inventory(candidate_roots: list[Path]) -> dict: ...
def build_release_manifest(root: Path, plugin_root: Path) -> dict: ...
def load_runtime_identity(root: Path) -> RuntimeIdentity: ...
def compare_identities(expected: RuntimeIdentity, observed: RuntimeIdentity) -> list[str]: ...
```

Hash 規則：

- 只 hash 明示 release allowlist；
- path 使用 POSIX relative path；
- 每筆 hash material 為 `path + NUL + file_sha256 + LF`；
- 排序後再 hash；
- symlink 必須記 link target，不能跟隨到 scope 外；
- 不把 mtime、absolute path、build timestamp 放進 source hash。

### 3.3 MCP 暴露方式

先不新增 `runtime_identity` top-level tool，以免先擴 surface。新增：

```python
def _server_identity() -> dict[str, Any]: ...
```

並把 compact identity 放進每個 stage/terminal/artifact response 的 `server` 欄位：

```json
{
  "server": {
    "release_id": "...",
    "mcp_schema_sha256": "...",
    "profile_id": "...",
    "host_resume_mode": "manual"
  }
}
```

驗收腳本直接啟動 MCP client、list tools、做一個只讀 probe，不能從文件或 package filename 猜版本。

### 3.4 I0 出口

先產生：

```text
state/integrated_focus_runtime/source_inventory.json
state/integrated_focus_runtime/release_manifest.json
state/integrated_focus_runtime/progress.json
```

`progress.json` 是避免下一個 Codex 忘記的機器 checkpoint：

```json
{
  "schema_version": 1,
  "authority": "docs/HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_TASKBOOK.md",
  "implementation_draft": "docs/HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_IMPLEMENTATION_DRAFT.md",
  "current_phase": "I0",
  "completed_phases": [],
  "next_test": "tests/test_harness_release_identity.py",
  "release_id": null,
  "blockers": [],
  "updated_at": "..."
}
```

它只記 phase、證據 path 與 hash，不存 prompt 或大段文字。

## 4. I1：Task Capsule

### 4.1 Schema

`task_capsule.py` 內集中 schema 與 store；不再另造 contracts/store 兩個只有彼此互用的模組：

```python
class ArtifactRef(StrictModel):
    artifact_id: str
    sha256: str
    role: str
    selector_receipt_ids: list[str] = []

class WriterLeaseRef(StrictModel):
    lease_id: str
    target: str
    base_sha256: str | None
    owner_work_unit: str
    expires_at: datetime

class WaitRef(StrictModel):
    wait_handle: str
    run_id: str
    attempt_id: int
    state_version: int
    resume_trigger: list[str]
    manual_resume_required: bool

class TaskCapsule(StrictModel):
    schema_version: Literal[1]
    task_id: str
    objective: str
    lifecycle_state: str
    version: int
    source_release_id: str
    active_work_unit: str | None
    authoritative_artifacts: list[ArtifactRef]
    working_artifacts: list[ArtifactRef]
    read_receipt_ids: list[str]
    writer_leases: list[WriterLeaseRef]
    wait: WaitRef | None
    queued_events: list[str]
    approval_owner: str
    scope: ScopeContract
    model_budget: ModelBudget
```

禁止 mutable list default；實作使用 `Field(default_factory=list)`。

### 4.2 Store

同一個 `task_capsule.py`：

```python
class TaskCapsuleStore:
    def create(self, capsule: TaskCapsule) -> TaskCapsule: ...
    def load(self, task_id: str) -> TaskCapsule: ...
    def compare_and_swap(self, task_id: str, expected_version: int, update: Callable) -> TaskCapsule: ...
    def start_work_unit(self, task_id: str, work_unit: str) -> TaskCapsule: ...
    def finish_work_unit(self, task_id: str, work_unit: str) -> TaskCapsule: ...
    def attach_read_receipt(self, task_id: str, receipt_id: str) -> TaskCapsule: ...
    def set_wait(self, task_id: str, wait: WaitRef) -> TaskCapsule: ...
    def queue_event(self, task_id: str, event_id: str) -> TaskCapsule: ...
```

持久化目錄：

```text
state/task_capsules/<task_id>/capsule.json
state/task_capsules/<task_id>/events/*.json
state/task_capsules/<task_id>/capsule.lock
```

寫入使用同目錄 temporary file、flush/fsync、`os.replace()`。所有 mutation 需要 expected version；版本不符回 `CAPSULE_CONFLICT`，不得 last-write-wins。

### 4.3 核心測試

- 同時啟動兩個 active work units，第二個失敗；
- 兩個 writer leases 指向同一 target，第二個失敗；
- version conflict 不覆蓋；
- background progress event 進 queue，不改 active work unit；
- restart 後完整恢復 wait ref 和 writer lease。

## 5. I2：Artifact Clipboard 核心

### 5.1 Contracts

`artifact_runtime.py` 內集中 immutable artifact contracts、CAS store 與 selectors，避免再拆成三個只有單一 caller 的檔案：

```python
class ArtifactManifest(StrictModel):
    schema_version: Literal[1]
    artifact_id: str                 # sha256:<digest>
    sha256: str
    byte_size: int
    media_type: str
    encoding: str | None
    source_kind: Literal["path", "stage", "terminal", "generated", "compose", "patch"]
    source_ref: str
    task_scope: str
    sensitivity: Literal["normal", "restricted", "secret_rejected"]
    created_at: datetime
    provenance: dict[str, Any]
    outline_ref: str | None

class ArtifactAlias(StrictModel):
    task_id: str
    alias: str
    artifact_id: str
    previous_artifact_id: str | None
    version: int
```

### 5.2 Store 演算法

`artifact_runtime.py::ArtifactStore.put_path()` 實際步驟：

1. resolve path，檢查 task capsule readable scope；
2. `lstat()`，拒絕 directory、device、socket 和 scope 外 symlink；
3. 以 1 MiB chunks 串流計算 SHA-256，不讀全文進 RAM；
4. secret policy 檢查；明確 credential path 或高信度 secret pattern fail closed；
5. object 已存在則驗證 size/hash 後復用；
6. 不存在則 copy 到 object directory 內的 temp file；
7. fsync、重新 hash、`os.replace()`；
8. 寫 immutable manifest；
9. 更新 task alias（若要求）；
10. 回傳 compact ref，不回 content。

預計 API：

```python
class ArtifactStore:
    def put_path(self, task: TaskCapsule, path: Path, *, alias: str | None, source_kind: str) -> ArtifactManifest: ...
    def put_bytes(self, task: TaskCapsule, data: BinaryIO, *, media_type: str, provenance: dict) -> ArtifactManifest: ...
    def describe(self, task: TaskCapsule, artifact_id: str) -> dict: ...
    def resolve(self, task: TaskCapsule, artifact_id_or_alias: str) -> ArtifactManifest: ...
    def open_content(self, task: TaskCapsule, artifact_id: str) -> BinaryIO: ...
    def update_alias(self, task: TaskCapsule, alias: str, artifact_id: str, expected_version: int) -> ArtifactAlias: ...
```

`put_bytes()` 只供 stage/terminal/apply 內部使用，不作為讓模型傳 50 KB 字串的 MCP 入口。

### 5.3 Quota/GC

- task quota、object max size、global soft quota 來自 profile；
- immutable object 只有 alias/ref count 為零且超過 retention 才能列入 GC；
- GC 初版只產生 proposal，不自動刪；
- secret-rejected object 不得先寫入 CAS 再標記拒絕。

### 5.4 核心測試

- 同 bytes 不同 path 只產生一份 object；
- CJK、binary、空檔案；
- symlink escape、path traversal、credential fixture；
- copy 中斷不留下可解析 manifest；
- alias CAS conflict；
- task A 的 private artifact，task B 不能 describe/read。

## 6. I3：Selective Read

### 6.1 Selector interface

`artifact_runtime.py` 內的 selector registry：

```python
class Selector(StrictModel):
    type: Literal[
        "whole", "line_range", "markdown_heading", "json_pointer",
        "yaml_path", "table_slice", "search", "terminal_stream"
    ]
    value: Any

class ReadReceipt(StrictModel):
    read_receipt_id: str
    task_id: str
    artifact_id: str
    artifact_sha256: str
    selector: Selector
    selector_sha256: str
    returned_sha256: str
    returned_bytes: int
    truncated: bool
    created_at: datetime
```

### 6.2 實作次序

第一輪只做 deterministic selectors：

1. `line_range`；
2. `markdown_heading`；
3. `json_pointer`；
4. `search`，復用 `safe_search()` 的 policy，但 search root 改為單一 artifact temp view 或 stream scanner；
5. `terminal_stream`；
6. `whole`，只允許小檔或明示 override。

第二輪再做 YAML 和 CSV table slice。不要一開始加入 embedding。

### 6.3 Receipt replay

key：

```text
sha256(task_id + artifact_sha256 + canonical_selector_json + reader_version)
```

若 key 已存在：

```json
{
  "unchanged": true,
  "read_receipt_id": "...",
  "content_replayed": false,
  "returned_bytes": 0
}
```

只有 `replay_content=true` 才重新送內容；這個選項仍受 output budget。

### 6.4 Context gateway 修改

修改 `_selected_file_entry()`：新增 `artifact_id`、`artifact_sha256`、`selector`、`read_receipt_id`。逐步停止直接 `_safe_read()`：

```python
selected = artifact_reader.read(
    task_capsule,
    artifact_id,
    selector,
    max_output_bytes=context_budget.remaining_bytes,
)
```

context pack manifest 要區分：

- `required_and_present`；
- `required_but_not_admitted`；
- `optional_omitted`。

required evidence 超 budget 回 `CONTEXT_REVIEW_REQUIRED`，不能截掉後繼續。

## 7. I4：Terminal Command Result

### 7.1 為什麼不從 transcript 猜

目前 `send()` 以 output quiet 作等待結束，然後 `_read_raw()` 按 byte cursor 取資料。這無法可靠分出：

- shell prompt／echo；
- command stdout；
- stderr；
- command 是否真正完成；
- heredoc 邊界；
- background process。

所以新增 side channel，不修改 raw logger 的證據角色。

### 7.2 Command store

每個 terminal slot：

```text
<slot_root>/commands/<command_id>/
  request.json
  request.sh             # mode 0600
  stdout.bin
  stderr.bin
  status.json
  result_manifest.json
```

`terminal_commands.py`：

```python
class TerminalCommandRunner:
    def prepare(self, slot: TerminalSlot, command: str) -> CommandRequest: ...
    def launch_in_attached_shell(self, manager: ProfileTerminalManager, request: CommandRequest) -> None: ...
    def wait_result(self, request: CommandRequest, wait_ms: int) -> CommandResult: ...
    def read_result(self, request: CommandRequest, max_preview_bytes: int) -> CommandResult: ...
```

### 7.3 Shell wrapper 草稿

`prepare()` 把使用者命令寫入 0600 `request.sh`。送進 tmux 的只有固定模板和經驗證的 command ID/path：

```sh
__sa_dir='<validated command dir>'
__sa_started='<ISO timestamp>'
{
  . "$__sa_dir/request.sh"
} >"$__sa_dir/stdout.bin" 2>"$__sa_dir/stderr.bin"
__sa_rc=$?
python '<runner module>' finalize \
  --command-dir "$__sa_dir" \
  --exit-code "$__sa_rc" \
  --started-at "$__sa_started"
unset __sa_dir __sa_started __sa_rc
```

使用 `.` 是為了讓 `cd`、`export` 等 shell state 在持久 terminal 中生效。必須另外測試：

- request 內 `exit`／`exec`／未閉合 heredoc；
- command 被 interrupt；
- MCP transport 中斷；
- finalize 沒被執行；
- shell top-level 死亡。

若無法安全支援會終止 shell 的命令，`terminal_exec` 要回明確 limitation；這類操作只能經 `terminal_send` 互動執行，不能偽造 result status。

### 7.4 UTF-8 preview

不要使用 `decode(errors="replace")` 後把 replacement 當正常結果。實作：

```python
def bounded_utf8_preview(path: Path, limit: int) -> Preview:
    raw = read_at_most(limit + 4)
    for cut in range(min(len(raw), limit), max(-1, limit - 4), -1):
        try:
            text = raw[:cut].decode("utf-8", errors="strict")
            return Preview(text=text, raw_bytes=cut, truncated=path_size > cut)
        except UnicodeDecodeError:
            continue
    return Preview(text=None, binary_artifact_required=True, ...)
```

### 7.5 MCP signature

```python
def terminal_exec(
    command: str | None = None,
    command_id: str | None = None,
    terminal_id: str | None = None,
    wait_ms: int = 30000,
    max_preview_bytes: int = 4096,
) -> dict[str, Any]:
```

規則：

- 第一次必須給 `command`，建立 command ID；
- 後續只給 `command_id` 可查同一 deterministic result；
- 兩者同時給或都不給，拒絕；
- `running` 不代表成功；
- 預期長時作業應改用 `stage_execute`；
- 不允許模型循環查 `command_id`，只能 material event 或明確人工檢查。

### 7.6 現有 `remote_terminal.py` 修改

- `ProfileTerminalManager.exec()` 委派 `TerminalCommandRunner`；
- `send()` docstring 改為 interactive input only；
- `read()` 內部改名 `read_raw()`；
- MCP 保留 `terminal_read` alias，一個 release 內加 `deprecated: true` metadata；
- `_read_raw()` 使用 UTF-8 boundary helper；
- `open().next_action` 改為普通命令用 `terminal_exec`，互動才用 `terminal_send`。
- folder/workplace binding 使用 profile 的持久 `binding_id`，不使用 boot 或 client UUID；
- `terminal_open(rebind_from_profile_id, expected_release_id)` 只作搬移／舊命名維修口，CAS 核對同一 tmux session/pane 並寫 receipt；release/schema 不一致直接拒絕。

## 8. I5：Apply Engine

### 8.1 不重新發明 patch parser

從 `solution_agent/tools/patch_tools.py` 復用／抽取：

- `inspect_patch_files()`；
- `check_changed_files_allowed()`；
- `check_patch_applies()`；
- stale hash 語義。

不復用其「暫寫真 target 再還原」作為正式 transaction。

### 8.2 Two-phase plan/commit

`artifact_apply.py`：

```python
class ArtifactApplyEngine:
    def plan_materialize(self, task, artifact_id, target, expected_target_sha256) -> ApplyPlan: ...
    def plan_compose(self, task, recipe, target, expected_target_sha256) -> ApplyPlan: ...
    def plan_patch(self, task, patch_artifact_id, target, base_sha256) -> ApplyPlan: ...
    def commit(self, task, plan_id, writer_lease_id) -> ApplyReceipt: ...
```

`ApplyPlan` 綁定：

- task/capsule version；
- target resolved path；
- target before hash；
- input artifact IDs/hashes；
- output candidate hash；
- diff artifact ID；
- validation commands；
- plan expiry。

commit 步驟：

1. 重新載入 capsule 與 writer lease；
2. 驗證 target 仍在 write scope；
3. 驗證 target current hash 等於 plan before hash；
4. 把 candidate 寫到 target 同目錄 temp file；
5. fsync；
6. 在 temp/candidate 上跑 parser/lint；
7. `os.replace()`；
8. hash target；
9. store 新 target 為 artifact；
10. 寫 receipt，更新 alias，釋放 lease。

Windows 上 replace 已存在 target 的行為要有專門測試；不能假設 POSIX 語義完全相同。

### 8.3 Compose 不是自由模板引擎

初版 recipe 只接受：

- artifact selector part；
-已註冊 literal template ID；
- newline／separator enum；
- JSON/YAML canonical serialization；
- Markdown section wrapper。

不允許 recipe 執行 Jinja expression、Python 或 shell。

### 8.4 MCP API

單一 write tool，但維持 plan/commit：

```python
def artifact_apply(
    task_id: str,
    mode: Literal["materialize", "compose", "patch"],
    request: dict[str, Any] | None = None,
    plan_id: str | None = None,
    writer_lease_id: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
```

- `commit=false` 只建立 plan，仍屬 state-changing（會寫 plan artifact），annotation 不標 read-only；
- `commit=true` 必須給 plan ID 和 lease；
- consequential target 仍由 App／既有 approval policy 決定，不在 Harness 自動 approve。

## 9. I6：WAIT、Capsule、Artifact 串接

### 9.1 WaitDisposition 修改

目前 `WaitDisposition` 擴充：

```python
class WaitDisposition(StrictModel):
    lifecycle_state: WaitLifecycle
    suppress_model_continuation: bool
    next_model_action: str
    wait_handle: str
    run_id: str
    attempt_id: int
    state_version: int
    resume_trigger: list[str]
    manual_resume_required: bool
    host_resume_mode: Literal["supported", "manual", "unavailable"]
```

`wait_handle` 由穩定欄位生成，不放絕對路徑：

```text
wait:<sha256(run_id + NUL + attempt_id + NUL + release_id)>
```

### 9.2 `stage_execute`

修改後流程：

```python
capsule = capsule_store.load(task_id)
assert capsule.active_work_unit == work_unit_id
assert capsule.source_release_id == server_identity.release_id
job_id = register_execution(...)
state = run_execution(...)
wait_state = synchronize_execution_state(...)
disposition = wait_disposition(...)
capsule_store.set_wait(task_id, WaitRef.from_disposition(disposition))
start_wait_monitor(...)
return compact_response(server, capsule_version, disposition)
```

為兼容舊 spec，`task_id` 可先從 spec 讀；新 schema 要求明示 capsule ID 和 work unit ID。若沒有 capsule，只允許 compatibility mode 並回 `focus_runtime_integrated: false`，不能計入新驗收。

### 9.3 Material event

WAIT runtime apply event 後：

1. 將 event ID 寫 capsule queue；
2. 不開模型 turn；
3. completion/failed/budget/review event 才將 `next_material_event` 標記 ready；
4. host adapter supported 時發一次 durable notification；
5. manual mode 只等待人工 check；
6. `stage_collect` 驗證後，把 outputs store 成 artifact refs；
7. capsule lifecycle 進 `REVIEW_REQUIRED` 或 `CLOSED`。

### 9.4 exactly-once key

```text
consumer_id + event_id + task_id + attempt_id
```

ack 只能在 capsule state commit 後寫。若 crash 發生在 commit 後、ack 前，重送 event 時由 applied event ID 回 duplicate ack，不重做 collect/apply。

## 10. I7：MCP surface 與 skill 遷移

### 10.1 目標 tools

```text
stage_execute
stage_status
stage_collect
terminal_open
terminal_exec
terminal_send
terminal_read          # raw diagnostic surface; one implementation, no alias
terminal_close
artifact_inspect       # mode: describe | read | diff
artifact_store
artifact_apply
```

目標共 11 個 tools。保留既有 `terminal_read` 名稱，但把用途明確收窄為 raw diagnostic；不額外保留 `terminal_read_raw`/`terminal_read` 兩套入口。`tests/test_build_profile_plugin.py` 目前硬編碼 7 tools，必須改為：

- canonical tool set constant；
- expected schema hash；
- profile launcher preservation。

不能只把數字 7 改成 11。

### 10.2 Annotation 草稿

| Tool | readOnly | destructive | idempotent | openWorld |
|---|---:|---:|---:|---:|
| stage_execute | false | false | false | false |
| stage_status | true | false | true | false |
| stage_collect | false | false | true | false |
| terminal_open | false | false | true | false |
| terminal_exec | false | true | false | true |
| terminal_send | false | true | false | true |
| terminal_read | true | false | true | true |
| terminal_close | false | true | true | false |
| artifact_inspect | true | false | true | false |
| artifact_store | false | false | true | false |
| artifact_apply | false | true | plan idempotent、commit by plan ID | false |

`artifact_apply` 若 framework 只能接受單一 boolean，標 `idempotentHint=False`；server 內仍以 plan ID 保證 replay safety。

### 10.3 Skill 路由文字

Skill 只保留選擇規則：

- 穩定可審核 operation：stage；
- 一般短命令：terminal exec；
- REPL/control key：terminal send；
- 看 metadata、精讀 selector、比較 diff：artifact inspect 的對應 mode；
- 精確搬運／組裝／patch：artifact apply；
- WAIT disposition 要求 END_TURN；
- host manual fallback 明示。

不要在 skill 重複 800 行任務書，也不要讓模型負責 hash、receipt 或 event dedup。

## 11. I8：Build、App、remote

### 11.1 Build

修改 `scripts/harness/build_profile_plugin.py`：

1. 從 MCP server 或 canonical schema fixture取得 tool set；
2. 產生 `release_manifest.json`；
3. 保留 installed profile launcher；
4. 對 plugin payload 建 manifest hash；
5. build output 回傳 release ID、tool count、schema hash、deprecated aliases。

修改 `build_deployment_bundle.py`：

- 把新增 modules/tests/scripts 納入 allowlist；
- bundle manifest 內綁 release manifest hash；
- 缺一個 declared module 時 build fail。

### 11.2 App 驗收

必須開新 Codex task，因舊 task 的 MCP schema 可能已快取。驗收順序：

1. list tools；
2. 比對 canonical schema hash；
3. 只讀 identity probe；
4. artifact store/describe/read local smoke；
5. terminal exec `printf` smoke；
6. stage no-API fixture；
7. WAIT 回應確認 manual/supported truth；
8. 把結果寫 canonical acceptance report。

### 11.3 Remote

1. build bundle；
2. remote dry-run；
3. 若有 conflict，保留 snapshot 並停止，不 force；
4. deploy；
5. remote runtime identity；
6. no-API smoke；
7. bounded shadow run；
8. local/remote release parity。

## 12. 第一個實際 vertical slice

不要先一次寫完 11 個 tools。第一個可運行 slice 是：

```text
release identity
 + Task Capsule create/load/CAS
 + ArtifactStore.put_path/describe
 + line_range selector/read receipt
 + one MCP artifact_inspect describe/read path
 + context_gateway consumes one artifact ref
```

測試情境：

1. 建立 task capsule；
2. store 一個 10,000 行 fixture；
3. describe 只回 metadata 和 outline；
4. read lines 500-520；
5. context pack 只包含這 21 行與 receipt；
6. 相同 read 第二次不 replay content；
7. 修改 fixture 後產生新 artifact ID，舊 receipt 不誤命中；
8. runtime response 包含 release identity。

這個 slice 通過後，才能做 terminal result 和 apply。原因是 terminal result 最終也要存入同一 Artifact Store；若先做 terminal，會再造一套 output storage。

## 13. 第二個 vertical slice

```text
terminal_exec
 -> stdout/stderr command store
 -> artifact_store internal ingestion
 -> artifact_inspect terminal_stream selector
 -> compact MCP result
```

fixture：20 KB CJK heredoc command、200-byte stdout、20-byte stderr、exit 7。驗收一次 call 得到：

- command ID；
- exit 7；
- stdout 200 bytes、無 echo、無亂碼；
- stderr 不隱藏；
- raw transcript artifact ref；
- stdout/stderr artifact IDs。

## 14. 第三個 vertical slice

```text
stage_execute
 -> WAIT disposition
 -> capsule wait ref
 -> sidecar material event
 -> stage_collect
 -> collected artifacts stored by hash
 -> artifact_apply materialize/patch
```

fixture 使用本地 sleep/write-metrics，不呼叫 provider。完成 event 重送十次，只 collect/apply 一次。

## 15. 實作期間如何避免再次散裝

每一個 patch 都必須同時更新：

1. schema；
2. implementation；
3. focused tests；
4. `progress.json`；
5. release manifest inputs；
6. MCP schema fixture（若 tool surface 改變）；
7. 主 acceptance report 的 pending/verified 狀態。

並維護：

```text
state/integrated_focus_runtime/deletion_ledger.json
```

每個 replacement 記錄：

```json
{
  "capability": "terminal command result",
  "new_owner": "solution_agent/harness/terminal_commands.py",
  "removed_paths_or_symbols": ["old symbol"],
  "temporary_adapters": [],
  "removal_test": "tests/test_harness_terminal_exec.py",
  "source_loc_added": 0,
  "source_loc_deleted": 0,
  "reason_net_growth_is_required": ""
}
```

規則：

- 不建立 `.bak`、`_old`、`_copy`、`legacy_v2` 或註解掉的大段 code；
- production source 不保存手工備份，版本與 receipt 才是歷史；
- 同一能力只有一個 owner module；薄 adapter 不含第二份演算法；
- 新 module 必須有 production caller 和 acceptance test，否則同一 phase 刪除；
- replacement 接線後立即刪除舊 branch、舊 helper、舊 tool description 和舊 build allowlist entry；
- 生成的 `__pycache__`、`.pyc`、build output 不進 code review 或一般搜尋；
- `artifacts/`、`dist/`、`replay/` 是歷史證據，不混入 source search，也不因「精簡」而擅自刪除；
- 每個 phase 必須報 source LOC added/deleted。淨增長不是失敗，但必須對應新的唯一責任，不能來自備份或平行實作。

禁止建立沒有入口、沒有 caller 或沒有 acceptance test 的 module。新增 function 前先寫「誰呼叫、輸入 ID、輸出 ID、失敗如何恢復」。

Code review 每次檢查：

```text
[ ] 這段 code 是否屬於 Task Capsule、WAIT、Artifact、Terminal、Apply 其中一個 owner？
[ ] 是否另造了第二份 lifecycle 或第二份 artifact identity？
[ ] 是否把全文送給模型來完成 deterministic 搬運？
[ ] 是否在 active runtime 中可到達，而不只是 test 可 import？
[ ] build/deploy allowlist 是否包含它？
[ ] installed runtime 能否回報它的 hash？
[ ] failure 是否 fail closed 並留下 compact next action？
[ ] replacement 接線後，舊實作和備份 code 是否已在同一 phase 刪除？
[ ] deletion ledger 與 source LOC delta 是否更新？
```

## 16. 真實完成時的單一操作路徑

完成後，一般工作的實際使用應是：

```text
1. Controller 建 Task Capsule，設定 objective、scope、budget。
2. 現有文件 artifact_store 一次，取得 sha256 ID。
3. artifact_inspect describe mode 看結構，不看全文。
4. artifact_inspect read mode 只取需要的 heading/range，receipt 寫回 capsule。
5. Codex 做單一核心工作；若要執行：
   - stable/long -> stage_execute；
   - short command -> terminal_exec；
   - interactive -> terminal_send。
6. 長作業進 WAIT，Codex END_TURN；sidecar 不呼叫模型。
7. material event 後 collect，輸出直接成 artifact refs。
8. 若要寫文件：artifact_apply plan -> writer lease -> commit。
9. 驗證 diff/hash/test，更新 alias 和 capsule。
10. acceptance evidence 足夠才關閉 task。
```

模型不再負責：重複開文件、貼全文、猜 terminal echo、輪詢進度、手動比較 hash、搬運相同內容、在多份文件間同步狀態。

## 17. 下一個 Codex 的第一回合

下一個 Codex 不應一開始寫 Artifact Store。第一回合只做：

```text
1. 讀主任務書與本實作草稿。
2. 檢查 state/integrated_focus_runtime/progress.json；不存在就建立 I0 checkpoint。
3. 盤點 workspace、ai-tools checkout、installed plugin、remote declared checkout。
4. 產生 source_inventory.json，列出 WAIT/context/terminal/build 能力矩陣。
5. 選定 authoritative root；若 divergent user work 無法安全合併，停止並報 blocker。
6. 新增 test_harness_release_identity.py 的 failing tests。
7. 不部署、不遠端覆寫、不改 approval policy。
```

完成這一回合時應交付 inventory、failing tests 和下一個最小 patch，而不是再次宣布整體完成。
