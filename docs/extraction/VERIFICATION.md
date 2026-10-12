# Agent Relay 交付與驗證

## 目前發布範圍（2026-10-11 去隱私後）

工程已改名為 `agent-relay`。原 944 個映射來源中，40 個歷史 benchmark／原始回執檔移至來源樹外，發布來源保留 904 個映射檔。來源與文件中的個人帳號、實際主機/IP、研究本機路徑改成通用範例；歷史 field feedback 改為純技術維護要點。實際憑證、路徑覆寫、原始本機回執及運行狀態不進 Git／來源 ZIP。

改名時 Windows Move-Item 對 junction 的搬移造成中斷；以不遍歷連結的方式恢復搬移、保留實體副本，按原 hash 補回依賴。全部 10,726 個 Node dependency 檔 hash 符合；343 個 junction 均只指向本 project。此處是恢復紀錄，不代表依賴需要納入 Git。

最新檢查：904 個映射來源 hash 符合；Writer 9、Background 12、Token Guard 37 項測試通過，router 候選構建與 XATS／memory SQLite 檢查通過。另選六個 Python 測試檔複查得到 63 通過、9 失敗：三項舊 remote-status 測試涉及未配置的 legacy workspace/bootstrap/conda，六項 continuation staging 測試缺少 workspace planner_policy.yaml。未帶入現用配置來補過測試，也未把這些額外結果算成通過。下方原始核心的取消失敗與平台／線上重裝限制仍保留。

來源打包使用 `scripts/extraction/package-source.py`，按 Git 可提交檔清單及隱私檢查產生 ZIP，逐檔核對 archive bytes/hash。禁止把整個開發目錄壓縮上傳；此次未 stage／commit／push 或切換現用入口。

已建立與現用工程並列的 `agent-relay`，只完成來源獨立、必要接線及本機構建。現用來源、插件、hook、排程和研究資料保持原樣。已初始化新的空白本機 Git 邊界，未 stage／commit，未設定 remote，未上傳 GitHub。

## 來源與依賴

- 944 個複製來源檔有 source→target→hash 對照；原 core 與選定現用來源的差異有清單，原檔未覆蓋。
- 42 個 Python distributions 離線重建到自己的 `.venv`，`include-system-site-packages=false`；仍需已安裝的 Python 3.13.12 基礎 interpreter，沒有借研究 venv。
- 226 個 Node packages 重建到自己的 `.deps`；343 個 junction 全部指向本 project，無外部連結。依賴環境及本機回執不納入未來 Git。
- 原始 App 聊天回執未複製；相關測試使用明示 synthetic fixture。
- 不複製 live workplace profile、研究執行 spec 或 runtime state；背景配置只指向新 project 的空白 fixture vault、獨立 state 與不存在的 credential file。

## 原始獨立驗證結果（去隱私前）

| 檢查 | 結果 |
| --- | --- |
| Harness 隔離自檢／11 個 MCP tools | 通過；未配置現用遠端 profile |
| Python 核心 | 67 通過、2 跳過、1 失敗 |
| Router／goal integration | 77 通過 |
| Writer | 9 通過 |
| Background | 12 通過，mock provider、未呼叫付費 API |
| Token Guard | 37 通過 |
| Harness ZIP／router／XATS | 可在新 project 構建，未安裝 |
| 原生 SQLite | 記憶體 smoke 通過 |
| Windows 無視窗 launcher | WinExe 編譯通過，未啟動／註冊 |
| Source hashes／外部依賴連結 | 944 檔 hash 符合，無外部連結 |
| 原來源／現用入口 | 最終 hash 比對結果見完整 JSON receipts |

**保留問題：** `test_explicit_cancel_stops_only_the_bound_child_job` 在取消剛發起、`wait_ms=0` 的命令時得到 running；單項複查仍失敗。對应 terminal 程式和測試與原來源逐位元相同。本輪未修此邏輯，也未在原運行環境重跑；不能把它描述成全部回歸通過或已驗收遠端取消能力。

未驗證 Linux-native 重建、從網路重新安裝全部依賴或 live 切換。現有環境是離線獨立重建，不是最終跨平台 lock。來源已獨立，完整正規化與標準化驗收仍屬後續工程。

## 本機重建與檢查

從 project 根執行：

```powershell
.\.venv\Scripts\python.exe scripts\extraction\verify-source.py
node components/lifecycle/build-router-package.mjs
node scripts/extraction/build-local.mjs
node --test plugins/research-report-writer/tests/core.test.mjs
node --test tools/research-background/tests.mjs
```

Router tests 的執行位置為 `components/lifecycle`，loader 使用該元件自己的 `node_modules/tsx/dist/loader.mjs`；Python 模組路徑指向 project 根。原始測試日志與構建產物放 ignored `.verification`，公開摘要在本目錄。`rebuild.ps1 -InstallDependencies` 是後續重新安裝入口，未在本輪進行 online install；原生 SQLite 需符合 Node ABI。

## 後續邊界

本地 Git 目前是空白 repo，不能因初始化就視為來源基線 commit 已建立。首次 GitHub 提交前仍需發布內容／歷史檢查；保留文件與 legacy source 中的研究專用示例，須在發布邊界檢查中分類。不要直接安裝這份 extraction candidate，或清理原研究 tools/experiments。先依任務書建立來源基線，再做正式結構與更新流程。

## 功能範圍補充（2026-10-11）

已按 MCP／路由／worker／交付／插件／背景工具逐項補充 [功能與驗證界線](ENTRYPOINTS.md)。Writer 原有 MCP 宣告因個人 live 綁定而未複製，本次補上通用 `.mcp.json`，使 plugin manifest 不再指向缺失檔案；只宣告環境變數名稱，無秘密或現用路徑，未註冊或啟動插件。此新增宣告為 generated extraction 接線，不計入原 944 個複製來源檔。

本次補齊後核對：兩個具 MCP 宣告的第一方插件，其 manifest 引用及入口檔均存在；Writer 宣告只含環境變數名稱，沒有 literal env 值。944 個複製來源 hash 仍符合，343 個 dependency junction 均在 project 內。Writer 9 項既有測試在正常檔案權限下複查全通過；sandbox 初次複查的 atomic rename 遭 EPERM，因此該次 8 通過／1 權限失敗不視為產品邏輯回歸。未執行 API、SSH 或 live 服務。



## 私人 GitHub 基線發布（2026-10-12）

本輪使用者已授權改名與上傳 GitHub；正式名稱為 Agent Relay。來源發布按可提交檔白名單及隱私檢查建立全新 Git 基線，保留上方已記錄測試／平台限制。現用入口、研究配置、憑證和狀態保持原位。先前「尚未提交／上傳」的描述是原始獨立階段的歷史狀態；實際 remote／commit／clone 核對回執另存 ignored `.verification`，不把本機路徑帶入來源。
