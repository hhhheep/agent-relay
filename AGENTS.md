# Agent Relay 維護入口

目前為P1 engineering candidate，已有實際Provider分拆、安裝/打包及取消修復；**尚未P1 Verified或Engineering Verified**。最新實作與驗證見[工程狀態](docs/engineering/phase-one/STATUS.md)與`project.json`；舊extraction與完整apps/packages布局是歷史基線/候選。兩端乾淨安裝與當前版本M1–M4隔離Harness跨端檢查已通過；Native App原生身份/讀取與Writer遠端驗證亦通過；跨chat發送、付費模型及現用啟用不屬此次隔離驗收，MUST以實際Git/回執核對，不把本機合成驗證當跨端完成。

最新使用者已授權第一期首次框架整理，見[FIRST_RESTRUCTURE_EXECUTION](docs/planning/normalization-20261012/FIRST_RESTRUCTURE_EXECUTION.md)。MAY在本獨立repo集中調整結構、模組、程式與打包接線；首次不受小步diff限制，不能只停在方案。工程自治逐批驗證，不逐批重新批准；仍不授權現用切換、破壞性state遷移或新的Worker研究政策。

當前第一期範圍 MUST 以[分期交付](docs/planning/normalization-20261012/PHASED_DELIVERY.md)與[ADR-002](docs/planning/normalization-20261012/ADR-002-PHASED-DELIVERY.md)為準：整理來源、Windows×Linux的M1新/舊、M2新/新、M3舊/新、M4舊/舊更新與真實擴展基礎。首次整理可選compact布局並集中重整，不限制保留舊物理目錄，也不照舊941 map機械搬遷；完整updater/schema框架/研究UI按使用觸發後期。目前工程/四列尚未驗收，已授權新chat開始S1–S5；不增加現用部署授權。

修改前 MUST 先讀分期交付，再讀[最新施工任務書](docs/planning/normalization-20261012/TASKBOOK.md)和[維護準則](docs/planning/normalization-20261012/MAINTENANCE_RULES.md)。歷史計劃僅追溯，不覆蓋最新版。此檔列必遵守規則，細節由維護準則展開。

## 適用範圍：僅此專案

本檔及維護準則只約束 Agent Relay repository，MUST NOT 當作全域修改方式。專案經驗只在本專案既有記錄保存、去重、標明適用條件；MUST NOT 自動上升為全域 Codex/使用者 AGENTS、個人 Skill、memory/settings 或其他 repo 規則，也不自動變成強制政策。全域推廣需使用者另有明確、獨立任務；普通維護、寫 ADR 或其他 chat 贊同都不授權推廣。

即使被其他專案引用，此規則也不自動取得跨專案效力。專案文件不能替自己授予全域配置/安裝插件/其他 repo 的權限；讀取 bindings 不等於修改、覆寫、同步或推廣的授權。

工程維護經驗留在 Agent Relay；研究/Worker 經驗留在產生它的研究 project，不匯入共用工具 repo。共用軟體發布只同步工程來源/產物，不夾帶專案經驗、研究流程或把它們變成其他 project 的預設規則。

## 八條強制規則

1. **R1 階段與範圍**：MUST 確認任務目標、既有授權及 repo 階段。MUST NOT 修改原研究資料、installed plugin/cache、hooks、credentials、live DB/state、排程或服務，除非另有具體授權。使用隔離依賴和合成 fixtures；不借現用配置補測試。
2. **R2 唯一來源**：MUST 找到 source owner，再改 authority。現況見[來源映射](docs/extraction/source-map.json)；第一期來源/owner見分期交付第4節。舊[migration map](docs/planning/normalization-20261012/migration-map.json)及[symbol split](docs/planning/normalization-20261012/split-decisions.json)只保留完整候選分析，不是必做搬遷指令。MUST NOT 手改生成 schema/bindings、dist、安裝副本作永久修復。生成輸入改後依固定工具重生；vendor revision/license/patch 可追溯。
3. **R3 定版後最小完整修復**：首次框架整理以最新施工授權為例外，可集中重整；P1驗收定版後，MUST 完整修復行為，包括必要的直接 consumer 和有效驗證；最小修改按影響判斷，不按行數。MUST NOT 夾帶無必要的改名、搬目錄、換框架、全面升依賴、特殊 wrapper 或重複 runtime。
4. **R4 契約與狀態**：MUST 保留 `solution_agent`、CLI/MCP identities、既有工具面和 command/receipt/artifact/CAS/lease 語義。每個 contract ID 僅一個可編輯 authority。strict schema 的 optional 新欄位也 MUST 按收發方向驗證。MUST 保持 protocol 0.6 `verify_pair` 的 protocol_version/release_id/payload_sha256/mcp_schema_sha256 與 workplace 驗證，不以新 system manifest 放寬它。未知 peer 禁副作用；命令結果不確定先查原 ID，不盲目重送。
5. **R5 變更分類與例外**：MUST 分清 fix、compatible extension、breaking change。跨 owner、依賴方向、公開契約或 state 邊界先寫影響、必要性、替代方案、遷移/回退與驗證；再核對實際授權。ADR 不等於批准。MAY 自主完成已授權的普通修復；更改目標、核心架構、研究方法、資源承諾或正式採用本專案 Worker/Skill 流程須使用者決策。保存專案經驗不等於採用政策，也不得上升全域。
6. **R6 發布可追溯**：MUST 使用唯一 repo authority、既有 builders 及可核對的來源/產物 hash。鎖定後的依賴、平台 ABI 和 vendor 更新獨立說明影響；不使用私人研究狀態構建。MUST NOT 宣稱 planned updater/CI 已可用。正式更新需核對實際載入版本/session/bindings；partial 如實保留；回退不覆寫不相容 state、不重派研究 command。現用切換依實際授權。
7. **R7 驗證與失敗**：MUST 按影響驗證並記錄命令、結果和未跑項目。MUST NOT 刪測試、降斷言、藏 skips 或空測試造綠燈。既有 cancel 1 項及 legacy 9 項失敗不是永久豁免；改動觸及必重新定位。無關 baseline failures 可明示保留。缺工具/平台標 NOT RUN 或 NOT AVAILABLE，限制完成/發布宣稱。
8. **R8 交接與擴展**：MUST 交接目標/授權、repo 階段、source commit、變更分類/owner、已驗證/未驗證、失敗處置、下一步及必要來源。只在已授權且必要時查活動任務識別，不為交接讀取 live state。新模組 MUST 有真實 consumer，對照[擴展台帳](docs/planning/normalization-20261012/EXTENSION_REQUIREMENTS.md)；MUST NOT 為候選 UI/DSH/context 功能建空包或宣稱已接入。

MUST NOT 為了讓當前改動通過，附帶刪除或放寬 AGENTS/維護 gate；改規則本身須屬明確授權的任務，保留理由、影響與驗證。

## 立即可用的檢查與交付限制

MUST 在 staging/打包前跑 `python scripts/extraction/check-publication.py`，只從 Git candidate files 發布；不整包開發目錄。credentials、真實帳號/host/絕對部署路徑、研究內容、live state、raw local receipts、private origins MUST NOT 進 Git 或包。只使用環境變數名稱、相對路徑及合成範例。

本設計階段檢查：`python docs/planning/normalization-20261012/build-plan-inventory.py --check`、`python scripts/extraction/verify-source.py`。它們不能替代未來 runtime/跨平台驗收。源碼修改時，904 原始 hash 的變動必須逐項記錄、由新 release provenance 承接；不可改寫歷史 source-map 假裝來源未變。

未授權範圍、非 authority 修補、受影響測試未處置失敗或未經驗證的相容宣稱，MUST 阻塞「完成」/合併/正式交付。MAY 在隔離分支保留明示 WIP 與失敗證據。P1交付需兩端重建及M1–M4逐列、實際契約、平台與保全/回退驗證；不能用新/新成功代替另外三列。發布依聲明targets驗收，不要求先建後期全套部署平台；未跑不能稱已通過。細節見維護準則。文件提交不要求虛構 runtime 測試。
