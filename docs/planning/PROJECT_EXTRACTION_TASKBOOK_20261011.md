# 任務書：獨立組裝、私人 GitHub 基線與後續正規化

日期：2026-10-11（Asia/Taipei）。任務順序依使用者最新指示：**先獨立組裝成工程 project → 上傳私人 GitHub 保存來源基線 → 再正式正規化結構 → 驗收後切換運行系統。**

本文件是可交給 Codex 執行的任務規格；本輪只寫任務書，沒有開始搬移、Git 操作、建立遠端或部署。它調整先前「全部正規化後才首次 push」的建議。既有架構研究作為後續目標保留；首次上傳前仍必須完成來源收斂、獨立構建和提交邊界核對，不能把混雜研究目錄整包上傳。

## 一、目標與交付順序

1. **A：獨立 project 基線。** 把已在用的共用系統程式、必要依賴與構建入口收斂到一份正式來源；離開研究 checkout 仍可構建／檢查。保持現有功能及公開 import／CLI／MCP 語義，先不全面導入新目錄與 Nx。
2. **B：私人 GitHub 來源基線。** 建立可核對 commit／tag，從 clone 驗證基線，再上傳私人 repo。此版本標記為 extraction baseline，不能稱為標準化完成或正式部署成功。
3. **C：正式結構與更新系統。** 從 B 的基線分階段落實已提出的 apps/packages/plugins、Nx、uv、pnpm、契約與更新政策；每批可驗收、可比較與可回退。
4. **D：現用切換與完整驗收。** 通過隔離演練後，按更新 plan 切換已授權的本機／遠端入口，確認實際載入與原狀態接回，才交付標準化版本。

**A 的「獨立」＝來源、依賴及構建獨立；D 才是現用運行入口完成遷移。** 現用服務可以暫時沿原路徑運行，不以維持它們的舊入口為理由把研究 checkout 留作新 project 的構建依賴。

## 二、範圍與來源權威

共用工程包含現用 core/Harness、local lifecycle/router、remote runtime/worker、writer/provider、Token Guard、背景服務、必要交付 adapter、插件描述／skills、Windows launcher、部署腳本、必要 vendor 與有效測試。研究專用程式、資料、vault、狀態與 credentials 不納入。

目前候選工程根是 `C:/Users/example-user/Desktop/ai-tools/solution-agent`，已位於研究目錄之外。使用既有 repo 時保留其 Git 歷史與全部本地修改，不能用其他副本覆蓋整庫。如果需要乾淨的新 checkout／發布來源庫，在實作開始時明示絕對路徑、來源與歷史保留方式；本任務書不指示刪改原 repo。

| 元件 | 已知來源事實 | A 階段處理 |
| --- | --- | --- |
| Python Harness／core | 研究 `experiments/harness-routing-sandbox` 的 122 個 manifest Python 檔已與當時 installed release 匹配；現有工程有缺檔及不同版本 | 開工重新核對，逐檔對照並審入現用基線，保留工程已有其他修改；不能全庫盲拷 |
| router／manager | manifest 輸入跨 routing-sandbox、goal-routing-integration，並依賴 XATS/vendor | 解析真正構建輸入及 transitive 依賴，收齊所需 source/vendor／licenses；不要搬全部實驗目錄 |
| writer／provider | installed/cache 與研究來源不同，現用多 `structuredRequestBody` helper，固定模型亦不同 | 收斂現用差異，驗證 writer 和 background consumer；保留現用 profile／模型，不順便更換 |
| Token Guard | 程式曾確認同 hash，skill 文字不同 | 重驗與選定唯一來源，保留 CLI／receipt／ledger 語義 |
| background／launcher | 研究 tools 下有現用服務、Windows GUI launcher 與驗收資料 | 收齊共用程式及去識別 fixture；DB／attempts／研究 rules 依內容分類，不能當可刪 cache |

參照盤點及精確證據 (local provenance omitted)與其 JSON。歷史 receipt 只能證明當時狀態；開工重新核對與並行修改衝突，不宣稱遠端今天已被重新驗收。

## 三、A 階段：先獨立組裝，不全面重構

### A1 保全與清單

- 記錄原 HEAD、tracked diff、待保留 untracked 白名單及 source hashes。使用明示檔案清單保存來源，不執行 git clean、整庫覆蓋或清空研究目錄。
- 確認沒有另一個正在修改同檔案的工作；遇到差異先保存、核對，避免依舊快照覆蓋。
- 產出 `docs/extraction/source-map`：每個檔案的來源路徑、原 hash、目標相對路徑、歸屬元件、選用理由、衝突處理及維護／legacy 分類。
- 產出 `docs/extraction/entrypoints`：全部現用入口、依賴來源、生成 artifact、外部配置／狀態要求。未確認是否仍用的 legacy 記入清單，不默默刪除或全部算入重寫。

### A2 組裝與最小接線調整

可先保留既有 `solution_agent/`、`scripts/`、`tools/`、`plugins/` 等工作布局；具體由來源映射決定。**不要在此階段強制移到最終 apps/packages 目錄、導入 Nx 或重写業務邏輯。** 保留 `solution_agent` namespace 與公開入口。

必需調整包括：移除構建對研究 experiments／tools 的絕對引用、補必要依賴宣告、以 project 相對路徑或明示配置解析入口、加入非秘密設定範例、收齊 vendor build input。每項接線改動記錄原／新行為及對應驗證；不是藉機重新設計調度、傳輸或 DB。

原 venv 有 `include-system-site-packages = true`，不可照搬當作環境獨立。A 必須建立可重建的隔離環境與依賴清單／版本記錄，記 Python／Node／native 平台前置條件，不借 live venv 或 sibling node_modules；C 再統一正式 uv／pnpm workspace 與 locks。未解決的現用必需依賴會阻止 A 驗收，不能只留 TODO 就標獨立成功。

### A3 提交邊界與驗證

- 程式、必要 metadata／構建腳本、licenses、合成 fixtures、設定模板與文件可納入；研究正文、大資料、credentials／個人 override、DB／WAL／queue、API attempts、node_modules／venv／cache、未去識別 receipts 不納入發布樹。
- ignore 用精確範圍，不全面排除 JSON／YAML／Markdown 或 tools；核對正例與反例。ignore 不會取消既有 tracked 檔案，也不會清除 Git 歷史。
- 在另一路徑的隔離 source export／checkout、空白測試配置與環境中驗證構建，確認不依賴原研究 checkout／live env。驗證範圍包含必要 Windows 元件與 Linux runtime artifact；尚未驗證的平台明列，不宣稱完成全平台。
- 跑有效 import／入口／provider consumer／artifact manifest 檢查及受搬移影響的原有測試。使用合成資料／mock provider，不啟動真實研究任務、不重跑付費 DS、不切 live 服務。

**A 驗收必備：** source-map、entrypoints、依賴／平台記錄、可重建入口、非秘密模板、保全紀錄、隔離構建與檢查結果、未解決事項清單。共用系統必需來源／依賴缺失為 blocker；無關 legacy 可記明維護狀態。

## 四、B 階段：先上傳私人 GitHub 基線

Repo 名稱、owner 與 remote URL 在實際上傳時確定，不能自行猜帳號、公開可見性或改既有 remote。此處定私人 repo；發布原始碼不等於更新 installed plugin 或 runtime。

1. A 通過後，建立 extraction baseline commit／tag，採實際驗收日期；工作分支用 `codex/` 前綴。分批 stage 明示白名單，檢視 staged diff，不 git add 全研究目錄。
2. 檢查**將推送的全部可達 Git 歷史**與發布樹，不只最新 staged diff：秘密、研究正文／個人資訊、大型檔案、絕對本機配置及生成環境。private repo 也不能當 credentials／live DB 備份。
3. 若原歷史未通過發布邊界，保留原 repo 與歷史，改用乾淨發布來源庫，記錄來源 commit/hash/provenance；不在本任務中默默 rewrite 原歷史。已確認 secret 外洩的處理另按实际範圍安排，不以加 ignore 視為解決。
4. push 後核對 remote commit/tag，從乾淨 clone 執行基線構建／檢查並保存結果。README 清楚標記「獨立來源基線，結構正規化待完成」。

**B 驗收必備：** 私人 repo URL、remote commit/tag、發布樹及可達歷史核對結果、乾淨 clone 構建結果。若尚未取得實際上傳授权／repo 資訊，先完成可審查本地基線，明示 B 未完成，不假稱已推送。

## 五、C 階段：在 GitHub 基線上正規化

按照[具體架構](STRUCTURE_AND_ENGINEERING_ESTIMATE_20261011.md)及[更新政策](UPDATE_AND_MAINTENANCE_POLICY_20261011.md)實作，先核定 A 所揭露的差異再凍結目標責任及接口。

1. 定模組與公開 API/schema、相容規則和必要 ADR；來源映射更新為 source→module→artifact。
2. 分批落實 apps/packages/plugins，保留公開 namespace／相容入口；建立 Nx 明示 task graph、uv／pnpm locks、Hatchling／esbuild／native build 與 vendor provenance。
3. 收斂唯一 provider、配置 loader／override 規則、契約 authority／生成 binding；writer/background consumer 一起驗證。
4. 落實單一 system release plan、manifest、一個維護入口及原 deployment IDs／receipts 的更新／回退／中斷接續。
5. 做受影響回歸、第二個合成 workspace 隔離、失敗及恢復演練；把規則落入 AGENTS／CI／維護文件。

每批提交說明 before/after、相容分類、檢查證據；目錄搬移與實質行為修改盡量可分辨，必要原子修改一起交付。不得用多個尚不一致的包覆蓋 extraction baseline 的可重建性。

**C 驗收必備：** 正式模組及邊界檢查、獨立構建 locks、consumer／contract 結果、平台包、更新／回退隔離演練、AGENTS／CI 與正式維護文件。完成 C 仍不等於現用 D 已切換。

## 六、D 階段：最後才切換現用系統

更新 plan 列所有所選本機／遠端元件、全域 plugin 受影響 bindings、當前／目標 hashes、活動命令、session reload、相容與資料 migration／回退條件。先 staging 再按受驗證相容順序啟用；每端保存原回執，部分完成不可標整體成功。

身份、狀態及活動 execution release 保留；不重派不確定 command、不整庫還原活躍 SQLite。查實際載入版本、health、原身份接回與所有 selected targets；現用切換需要部署範圍的实际授权。舊入口／舊包保留到驗收及活動引用解除，不能提前清理研究 tools／experiments。

**D 通過才建立 standardized baseline 並宣告完整標準化。** 之後 Codex 在既定模組內小幅演進，普通修復不得附帶大改結構。

## 七、工程量與依賴關係

順序為 A→B→C→D；重新安排既有 S1–S7，不是先做一遍獨立，再重複做一遍來源收斂。A 來源映射、依賴驗證、接線與構建證據直接供 C 使用。

- A 初步抓 **4–7 工程人日**，屬先前總預算內的第一批工作；由依賴 closure、vendor 與安裝分叉收斂程度校正。
- B 的 Git 清單／歷史與 clone 驗證納入原 S7；不能因先 push 就省略後續 CI。
- 全部工程仍以 **17–28 基礎人日、含餘量約 21–35 人日**為暫定估算。A 完成後重估剩餘 C／D；若必須新增大量歷史清理或發現額外現用元件，列增量及原因，不默默擴範圍。

## 八、執行與回報規則

執行者每階段回報：改動來源／檔案、artifact／commit/hash、有效檢查結果、未解決問題、下一階段前置條件；階段未通過不得宣告完成。持續處理授權範圍內的可逆工作，缺少非必需偏好不反覆請示；阻塞只針對真實缺少資訊或授權的動作。

本文件的建立並未啟動 A–D 執行或新 chat。後續可直接在此 project 依任務書執行；若使用者另要求新 chat，再把此文件作為該 chat 的任務規格。
