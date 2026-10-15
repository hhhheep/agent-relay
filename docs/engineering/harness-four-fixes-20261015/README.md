# Harness 四項問題修正與本地驗收

日期：2026-10-15。基線 commit：`4c4438b`。分支：`codex/harness-four-fixes-20261015`。

狀態：正式 Agent Relay 來源上的最小修復；本地驗收完成，未部署、未合併原分支、未推送，未啟動真實 Worker 或研究。P1 原交付紀錄與已驗證舊包保留；本修正不是重新驗收全部 P1 或新發布身份。

## 範圍、owner 與必要直接 consumer

僅處理 wait_ms、派工 phase、partial 呈現、斷線後原提交恢復四項。按本 repo 的 AGENTS、分期交付、施工任務書及維護準則執行；不引入提示篩選、經驗傳遞、模型切換或其他功能。

| 問題 | 正式來源／修正 | 驗收 |
| --- | --- | --- |
| 非法等待參數在啟動後拒絕 | Harness terminal_commands 共用 validate_wait_ms；MCP schema 暴露 0..30000，直接入口也在 manager/packet/capsule/lease 前拒絕。runner 在 prepare/launch/cancel 前同樣驗證 | 越界、非整數及 bool 不進入副作用；0/30000 保持可用 |
| phase 與完整交付錯配 | MCP 工具描述與原 Skill 明確說明三種 phase；full prompt 明確要求在既有授權內完成實作、執行及檢查 | 三種 phase 傳到實際 worker invocation；預設仍 prepare_code，沒有用自然語言猜 phase 或擴大授權 |
| 合法 partial 被當成程序失敗 | codex_worker 從原 receipt 派生 next_action；MCP 收件呈現程序非零、契約無效、合法 partial、任務失敗及待驗收的不同原因 | 合法 partial 保留 terminal failed/非零 wrapper exit、Codex exit0、contract valid、task partial 與原 artifacts；不當作整體成功 |
| 提交後失去觀察／回覆 | runner 在有持久 job ID 後的 launch/wait/read 例外攜回原 command；MCP 保留 task/workunit/command 與 unknown，scope 錯誤仍按原規則拒絕 | launch/wait/read 故障能正向找回原 job；lookup-only 不啟動第二次工作，原同 ID 收集測試保持通過 |
| 上述恢復的直接 consumer | Lifecycle TaskPublisher 將有匹配 task/workunit 的合法 command ID 保存到既有欄位；recoverPending 包含仍 prepared 的已知 command | 重啟後走已知 ID recovery，不重送；外來 task/workunit 或非法 ID 不採納 |

## 變更分類、契約與回退

分類：既有行為修復。涉及 Harness 執行 owner、其既有 worker prompt，以及 Lifecycle 的直接收件 consumer；需要跨這些檔案才能完整保留失去回覆的原命令身份。替代方案是只改 Skill 或只回傳 ID，但前者不能修程式時序，後者仍會被 consumer 丟失。

不新增工具、必填參數、receipt 欄位、DB 表／欄位或 lifecycle 狀態。不改 terminal／wrapper 的非成功退出語義。next_action 為既有欄位的更精確說明；新錯誤 code command_outcome_unknown 仍走原來的非成功／unknown 處理，已核對 HarnessPort 對一般錯誤的 consumer 行為。

wait_ms 現在嚴格接受整數，拒絕 bool 與小數；它們不是合法的整數等待契約。MCP input schema 與描述會改變，因此下一次打包須透過既有 builder 產生新的 schema/source/release hash，Client/Worker 仍需通過原 protocol 0.6 exact-pair 與 workplace gate。不能把這份來源變更直接塞進舊安裝包冒用舊身份。

不需 state migration。程式回退仍保留既有 command、lease、receipt、publication DB，不刪 claim 或重派 uncertain 工作。舊 consumer 對 prepared+known-ID 的恢復有原缺陷，回退後不可聲稱此修復仍生效。正式部署與平台驗收另循原更新流程；本次沒有授權切換。

## 已跑驗證

以下命令從 repo/worktree 執行；Python 使用同一正式 repo 的測試依賴，Node 依 components/lifecycle/package-lock.json 重建獨立 worktree 開發依賴，無鎖檔變更。

```text
python -m pytest tests/test_harness_codex_worker.py tests/test_worker_job_reattach.py tests/test_terminal_commands.py -q
# 修改前基線：52 passed, 2 skipped

python -m pytest tests/test_harness_four_fixes.py tests/test_harness_mcp.py tests/test_terminal_commands.py tests/test_worker_job_reattach.py tests/test_harness_codex_worker.py -q
# 107 passed, 2 skipped

python -m pytest tests/test_harness_release_identity.py tests/test_harness_protocol_06.py tests/test_harness_bootstrap_release.py tests/test_remote_terminal.py tests/test_help_resume.py -q
# 41 passed

# components/lifecycle；PYTHONPATH 指向本 worktree 和正式 repo 測試依賴
node --import tsx --test task-publisher.test.ts publication-service.test.ts harness-port.test.ts failure-isolation.test.ts
# 16 passed, 0 failed
```

兩項 Windows skips 是既有 native POSIX bash supervisor cases，未新增 skip 或降低斷言。其他原命令恢復／收集及 cancellation 的本地測試通過。CI 的既有核心測試命令加入新的四項回歸及原 reattach suite；未觸發遠端 CI。

過程中修正了新測試的 MCP SDK input_schema 欄位使用錯誤。Node 測試初次也暴露 prepared+known-ID 的查詢缺口，已補修。其餘首次 Node 測試失敗來自新 worktree 缺少測試 Python／依賴路徑，補齊本地環境後原斷言通過，沒有為此改產品功能。離線 npm 快取不完整，改以原鎖檔準備依賴，停用安裝腳本。

## 規則與來源檢查

- `python scripts/extraction/check-publication.py`：991 個 Git 候選檔案，零 findings。
- `python scripts/release/provenance.py`：historical_map_unchanged=true；31 個 extraction 檔案差異為全部既有工程累計，並非本次新增 31 個改動。歷史 source-map 的 Git diff 為空。
- `git -c core.whitespace=cr-at-eol diff --check` 通過；provenance 保留基線原有 CRLF 格式，遵守 .gitattributes 的 exact-byte 要求；此參數只用於檢查，不修改 Git 設定。
- 歷史 `build-plan-inventory.py --check` 因 `.gitattributes` 與設計期 baseline hash 不同而失敗；在未改動的原分支 `4c4438b` 同樣重現。這是既有設計期 checker 的基線不符，不是本次新回歸；未修改 gate、baseline 或來源 hash 來掩蓋。當前工程 provenance 檢查通過。

## 尚未驗證／不宣稱

Linux 原生 supervisor、真正網路斷線、實際模型執行及現用 Client/Worker 啟用未跑；本次是本地合成故障與 consumer 持久化驗收。未重跑 P1 的跨端安裝／M1–M4，也沒有新發布可用宣稱。實際部署該平台前仍須相關平台與配對驗收。

完整 Git diff 包含核心、直接 consumer、Skill 參考、測試、CI 測試清單及本說明。歷史 extraction/source-map 不改寫，更新由 scripts/release/provenance.py 產生的 source-changes 作為新變更追溯。

## 相對基線的來源 hash

下表由 Git 基線與當前正式來源生成，不包含安裝副本或私有研究材料。

| 來源 | 基線 SHA256 | 修改後 SHA256 |
| --- | --- | --- |
| `solution_agent/harness/terminal_commands.py` | `7907b344e874a18633ad3910fe19f1dacf8c3a40f248a9647b5c6ebc98eb5d34` | `eea2afc16558b8024256552d6840b9cd436ecbea9d7e19cb7851b8482b8baf05` |
| `solution_agent/harness/mcp_server.py` | `42971c6ebcd71c32bfc8f529619bce3ff33976e5f92c5750d7f0c0d437137d46` | `206946816048f1c07b80712e9d59b05ceb4bdcb862e83c86d2e8ef5ecc0a8f89` |
| `solution_agent/harness/codex_worker.py` | `1742d5c672f64b4f26bd3ab2dc049e2cc6e95299a6d427e4956ac7d974ac5444` | `34424e9a6f61a0d188c928f2f0439ce54581136ddfe26cff327389e98e43d900` |
| `solution_agent/orchestrator/prompt.py` | `2f6b82f90dcb872e6a4e7fde261b91baa66d30f3a4ed35cdd42a61bf680d9ee0` | `1df487f0df7d7d75dbc8c49863ce04f9ddcb470c09654b6ab94066f9fa14060a` |
| `components/lifecycle/task-publisher.ts` | `6486711f8c6ad10479a1928ac6482501a847963793c011e49849841edb29b1b0` | `4cb71c2206dd8945981a0841b924005c3abbd8cad7f621665eb80bb356239c80` |
| `plugins/solution-agent-harness/skills/solution-agent-harness/references/remote-codex.md` | `ff6d4dd8000aaf02a0cbb1686b21df7119b4e525e3070c37f506e59ec88e0ab0` | `1a61d3a31599141dc85ad109dfd345d9bf2190389df9c8328dab7fc78b18b2dd` |
