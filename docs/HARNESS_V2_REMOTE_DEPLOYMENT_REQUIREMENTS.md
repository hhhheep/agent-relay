# Solution Agent Harness V2：Example 91 遠端部署需求單

> 歷史需求紀錄，不再作為安裝指令。2026-09-30 起，以 [0.6 現行部署協議](HARNESS_V3_REMOTE_DEPLOY_HANDOFF_20260930.md) 的統一安裝、雙端確認、舊設定退役及完成標準為準；下文的 backup、工具數與舊路徑敘述不得直接用於新部署。

## 目的

下一次更新需提供可稽核、可恢復、無需臨時拼接長 SSH 指令的正式遠端部署流程，讓同一個已驗證 bundle 能安全部署到 Example 91，並在真正執行遠端 Codex 的主機上完成 plugin 註冊、三工具 smoke 與小型 report-writer 驗收。遠端同時必須安裝與本地共用契約的薄執行層；詳見 `HARNESS_V2_TWO_ENDED_TOKEN_CONTRACT.md`。

本文件記錄 2026-09-26 實際部署時確認的缺口。它不是遠端 target-root 的猜測，也不授權覆寫任何現有工作區。

## 已確認的遠端事實

- SSH alias：`EXAMPLE_WORKER_A`，可連線。
- 遠端主機／使用者：`acvlab`／`agent-user`。
- 遠端 Codex：`/home/agent-user/.local/bin/codex`，版本 `0.155.1`。
- 受限 provider env 檔：`/home/agent-user/.remote_materializer/.env` 已存在；不得讀出、複製到 bundle、寫進命令列或 logs。
- `/srv/example-workspaces/fan-workspace` 是研究工作區，不是 solution-agent checkout：
  - 沒有 `cli.py`、`solution_agent/cli.py`、`requirements.txt` 或 `config/remote_execution_profiles.yaml`。
  - 目前已有 `tools/research-token-guard` 與 `tools/research-report-writer`。
  - `.git` 目錄存在，但 `git -C ... rev-parse` 回報不是有效 Git worktree；不得以 Git checkout 身分操作。
- 現有 `EXAMPLE_WORKER_A` profile 指向 `/srv/example-workspaces-b/loupe_workspace/Loupe_agent` 與其 runs root；這是 Loupe 專案工作區，不是 fan-workspace，也不是 Harness 的持久 solution-agent control checkout。
- 有界搜尋只找到 `/ssd6/.../runs/<old-run>/solution_agent/cli.py` 的歷史 run snapshots；這些不可作為部署 target-root。
- 因此，目前沒有已確認的持久遠端 solution-agent checkout。部署器必須由設定明確提供 target-root，不能從 project workdir 推導。

## 必須新增的設定契約

建議新增獨立 profile（名稱可由維護者決定，例如 `EXAMPLE_WORKER_A_FAN`），至少包含：

```yaml
host: EXAMPLE_WORKER_A
solution_agent_root: <明確、持久、可寫且可備份的遠端 checkout 路徑>
project_workdir: /srv/example-workspaces/fan-workspace
remote_run_root: <明確的 lifecycle run/receipt 根目錄>
codex_path: /home/agent-user/.local/bin/codex
python_bootstrap: /usr/bin/python3
plugin_home: /home/agent-user/plugins
personal_marketplace: /home/agent-user/.agents/plugins/marketplace.json
provider_env_file: /home/agent-user/.remote_materializer/.env
```

約束：

- `solution_agent_root`、`project_workdir`、`remote_run_root` 必須是三個語意不同的欄位。
- preflight 必須拒絕 placeholder、空值、同一路徑誤綁，以及 historical run snapshot。
- `solution_agent_root` 若尚不存在，必須由明確的 `initialize_checkout: true` 或等價授權建立；不得自行選擇 `/ssd2/.../tools/solution-agent`。
- `initialize_checkout: true` 還必須指定 `bootstrap.mode` 與受信任來源。overlay 不是完整 checkout，不能單獨 bootstrap。允許模式為既有 checkout、帶 SHA-256 的 release archive，或明確 Git URL/ref。
- `project_workdir` 不得被 overlay installer 當成 target-root。

## 必須新增的 lifecycle 操作

需要正式 CLI/API，而不是在任務中組合長 SSH／SCP：

1. `remote-deploy-preflight`
   - 回傳 host/user、三種 root 的角色、存在性／權限、Git identity、Python/Codex 路徑、env 檔是否存在（只回傳布林值）。
   - 不讀出任何 credential 值。

2. `remote-deploy-stage-artifact`
   - 接收本地 bundle 路徑、預期 SHA-256、profile。
   - 以 solution-agent lifecycle 傳送「declared artifact」。
   - registry 記錄本地 hash、遠端暫存路徑、遠端 hash、大小、傳送時間與狀態。
   - 不把 bundle 混入 project workdir 或任意歷史 run snapshot。

3. `remote-deploy-dry-run`
   - 在遠端再次驗證 SHA-256。
   - 對明確 `solution_agent_root` 執行 `install_overlay.py`，回傳結構化 `conflicts`、`unsafe`、plan count。
   - 任一 conflict／unsafe 必須 fail closed；沒有 force 選項。

4. `remote-deploy-apply`
   - 只接受已驗證且 hash 未變的 dry-run receipt。
   - 套用前建立 installer backup；回傳 backup root、ledger、applied count。
   - 禁止 `git reset --hard`、force overwrite、刪除或清理 dirty worktree。

5. `remote-runtime-setup`
   - 在 `solution_agent_root/.venv` 建立或重用 checkout-local venv。
   - 安裝 `requirements.txt` 與 `requirements-harness.txt`。
   - 執行 `plugins/solution-agent-harness/scripts/server.py --check`。
   - 不得退回缺少 `mcp` 的系統／Miniconda Python 作為 runtime。

6. `remote-plugin-register`
   - 必須在真正執行遠端 Codex 的同一 user/home 上呼叫 plugin-creator helper 與 `codex plugin add`。
   - 禁止手改 marketplace JSON 或 Codex config。
   - 應提供 per-host MCP config 產生器：固定 checkout-local Python、`SOLUTION_AGENT_ROOT`、`PYTHONPATH` 與受限 `REPORT_WRITER_ENV_FILE` 路徑，但不寫入 key。
   - 更新時使用 plugin-creator cachebuster helper。

7. `remote-codex-smoke`
   - lifecycle 建立新的遠端 Codex 任務，因為既有任務不會刷新 MCP tool inventory。
   - 先驗證 Harness tool surface 精確為三個 stage 工具
     `stage_execute`、`stage_status`、`stage_collect`，加四個 profile-bound
     terminal 工具 `terminal_open`、`terminal_send`、`terminal_read`、
     `terminal_close`。
   - 再執行 allowlisted no-API smoke；回傳 subprocess exit、metrics、兩個必要 artifacts 與 hashes。

8. `remote-report-writer-smoke`
   - 只接受去識別化的小型 evidence packet。
   - key 僅從既有受限 env 檔載入到程序環境。
   - 驗證 canonical report、claims sidecar、provider usage、Token Guard receipt 與 artifact hashes。
   - 不重跑既有昂貴三案例 A/B；除非新部署結果與既有驗收不一致。

9. `remote-deploy-status`／`remote-deploy-collect`
   - 可恢復查詢，不依賴同一個長 SSH session。
   - 最終收集結構化摘要、receipt、backup path、hash 與未解決衝突。

## Plugin runtime 必修正項目

本機實際部署暴露了兩個 portability 缺口，下一版 bundle 應直接修正，不應再靠 personal plugin 手工客製：

1. MCP server 從 Codex plugin cache 啟動時，child wrapper 的 cwd 不是 solution-agent checkout。`safe_runner` 雖使用 checkout-local Python，但子程序無法匯入 `solution_agent`。
   - 正式修正可選：啟動器設定 `PYTHONPATH=<solution_agent_root>`、server 在啟動前 `chdir(root)`，或 `safe_runner` 明確傳入 `cwd=root` 與受控 env。
   - 必須新增「從安裝後 cache 路徑啟動」的真實測試；現有測試曾以 monkeypatch 注入 `PYTHONPATH`，沒有覆蓋部署情境。

2. report-writer stage 只讀 `DASHSCOPE_API_KEY`，但 harness 沒有正式的 allowlisted env-file 載入契約。
   - 新增 `provider_env_file` 設定，只允許 profile 中明確列出的受限檔案。
   - 僅把允許的 key 載入 child environment；不得保存值到 spec、registry、receipt、stdout/stderr、handoff 或 bundle。

## Report-writer 相容性必修正項目

本機 provider smoke 的兩次有界嘗試均失敗，且已停止重試：

- 800 output-token 嘗試：provider 回應存在，但 `content` 為空。
- 2048 output-token 單次修正：回傳內容仍無法解析為 JSON 物件。

下一版需要：

- 記錄不含 prompt／credential 的 response metadata：response id、executed model、finish reason、usage、`content_present`、`reasoning_content_present`、HTTP status。
- 相容 provider 可能把結果放在 `reasoning_content`、tool call 或其他結構化欄位的情況；不可只假設 `message.content`。
- 保留嚴格 schema 與未知 claim-id 拒絕規則。
- corrective retry 最多一次，且只在 fingerprint 或參數有實質改變時允許。
- failure artifact 必須足以診斷 empty content／non-JSON，而不保存 prompt body 或原始敏感回應。

## 遠端 bundle 與 plugin 驗收閘門

遠端部署完成必須同時滿足：

- bundle 遠端 SHA-256 等於本次 stage-artifact receipt 記錄的預期 SHA-256。不得把舊版 bundle hash 寫成跨版本常數。
- dirty worktree 已檢查且未被清理／覆寫。
- dry-run：`0 conflicts`、`0 unsafe`。
- apply count 與 bundle manifest 一致，installer backup／ledger 可讀。
- checkout-local `.venv` 的 `server.py --check` 成功，`mcp_version` 可回報。
- 新遠端 Codex 任務工具表精確七項（三個 stage 加四個 terminal）。
- no-API smoke `succeeded`、metrics 有效、必要 artifacts 與 hashes 齊全。
- report-writer 產生 report、claims sidecar、provider usage、verified Token Guard receipt 與 artifact hashes。
- API key 從未出現在命令列、Markdown、bundle、logs、registry、receipt 或遠端新增檔案。
- 最終報告分列本地部署、插件載入、遠端部署、遠端插件載入、smoke、備份、bundle hash、未解決衝突。

## 已確認的 control-plane 路徑決策

2026-09-26 已確認採用使用者層級 core：

```yaml
solution_agent_root: /home/agent-user/.local/opt/solution-agent
project_workdir: /srv/example-workspaces/fan-workspace
remote_run_root: /srv/example-workspaces/harness-runs
initialize_checkout: true
bootstrap:
  mode: release_archive
```

此路徑屬於 `agent-user` 使用者的執行 core，不是 ResearchFlow／文件管理目錄。研究資料與大型執行輸出繼續放在 `/ssd2`。完整 release archive 建成並稽核後，才把其實際來源與 SHA-256 寫入可執行 profile；不得預先填入 placeholder。

仍待產生的是完整、去除秘密的 release archive 及其 SHA-256。完成前，遠端 apply 與 plugin 註冊仍保持停止。

在 release archive 與 SHA-256 明確之前，遠端 apply 與 plugin 註冊都必須保持停止。
