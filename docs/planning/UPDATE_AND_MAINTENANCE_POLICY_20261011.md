# 標準化後的更新與維護原則

日期：2026-10-11（Asia/Taipei）。承接[結構與工程估算](STRUCTURE_AND_ENGINEERING_ESTIMATE_20261011.md)。這是推薦的目標政策；尚未部署更新器、CI 或修改現用 AGENTS.md。規則的實作與驗收歸既有 S1–S7 工程範圍，不因另寫本文件增加一套系統。

## 1. 核心約束：一次定結構，後續在邊界內演進

標準化階段把現用來源、模組責任、公開接口、配置、狀態及交付流程定完整。完成後，普通修復與功能擴充沿正式模組和接口進行。「最小修正」指完整解決問題的最小必要影響範圍，包含需要修改的直接消費端；不是最少行數，也不是持續增加 wrapper、特殊分支和補丁副本。

- 修復從根因所在模組開始；新增功能先使用既有擴展點。不得為一般問題新增競爭 scheduler、transport、資料權威或插件啟動入口。
- 普通 bug fix 不附帶改目錄、改 namespace、換框架、全面升依賴或改狀態模型。必要跨模組修復可以一起完成，理由與影響必須明確。
- 架構例外先記 ADR：現有接口何以不足、最小替代方案、受影響模組、相容方式、遷移與回退。依使用者既有授权執行；超出已核准範圍的結構改造另由使用者決定，不藏在修復內，也不要求每個正常修復重新請示。
- 不以檔案數或行數上限取代判斷；評估公開契約、依賴邊、狀態及部署目標是否擴大。

## 2. 修改、版本和相容性分類

| 變更 | 推薦版本分類 | 必須完成的工作 |
| --- | --- | --- |
| 修復不符合既有契約的行為；不破壞使用方式 | PATCH | 受影響模組修正、有效回歸檢查、更新說明 |
| 新增可選能力／參數，既有呼叫及輸出仍相容 | MINOR | 新能力驗證、受影響 consumer 檢查、設定／文件更新 |
| 移除／改名接口、改必填欄位、改既有語義或不相容格式 | MAJOR | ADR、相容矩陣、遷移方案、消費端協調、回退限制 |
| 移動模組責任、改依賴方向或替換框架 | 架構變更；版本按實際相容性判定 | 獨立列出工程範圍；不能僅以 PATCH 名義豁免架構審查 |
| 第三方依賴、工具鏈或 provider 更新 | 依對外行為判定版本 | 獨立列影響，鎖定版本、平台構建與 consumer 驗證；不能推定必然相容 |
| 僅修改未打包的說明文件 | 不必發布 runtime | 文件與已交付行為一致；打包的 skill／規則若影響行為則另評估發布 |

這是本系統採用的版本規則，正式標準化基線建議為 1.0.0，實際編號在基線驗收時決定。既有 0.x 過渡版本也必須明示破壞性變更，不能以未達 1.0 為理由默默改接口。版本號與 source commit、payload hash 同時記錄；不只比版本字串。

**三種版本各司其職：** component version 表示元件行為；schema/protocol version 表示交換格式與語義；system release ID 指向一次測過的精確元件組合。元件升版不必機械地改 schema version；schema breaking change 必須能被讀取端識別。唯一 system release plan 產生 manifest，不另建第二份人工版本清單。

## 3. 唯一來源與小範圍交付

- 所有修正回到獨立工程 repo 的正式來源；installed plugin、cache、release ZIP、研究 experiments 不作平行維護來源。
- 緊急修復也從正式來源產生包與回執，不直接把安裝版改成無法重建的特殊版本。發現安裝版有未收斂差異，先保全差異並審入來源，不能直接覆蓋。
- 只重建／部署有變更的 artifact 和契約要求協調的元件。共用 provider、schema 或 runtime 改動，要檢查所有實際 consumer，不能只測修改檔所在套件。
- Nx graph 與明示跨語言邊決定初步影響範圍；lock、vendor、build input 變化也納入。影響分析不得省略需驗證的 Windows／Linux 平台。
- 研究特有需求放研究設定、rules 或已宣告 adapter；不硬編碼某研究路徑、名稱和特例到共用 core。

## 4. 相容、淘汰與擴展規則

公開 exports、MCP／CLI、schema 和設定欄位是維護契約。consumer 只能使用公開入口，不能引用其他模組內部檔案、node_modules 具體路徑或研究 venv。模型、provider、prompt/rules、重試與去重行為的改動都可能改研究結果或費用，必須列作行為變更，不包在「更新 SDK」裡靜默切換。

新增可選欄位也要驗證舊 reader：嚴格 schema 未必接受未知欄位。不相容接口優先採「先擴展 → 切換 consumer → 再移除」；reader 先更新或 writer 先更新，由相容矩陣及有效測試決定，不設定一個通用順序。

舊入口／相容 adapter 必須列替代入口、棄用版本、移除條件與受影響 bindings。建議同一 major 內在既定相容範圍保留，移除放下一個 major；不能無限增長 legacy adapter。local client、manager、remote runtime／worker 的可混用版本逐組宣告和驗證，**不先承諾所有 N／N−1 組合均相容**。不相容組拒絕啟用，不能靠使用者猜版本。

## 5. 依賴與上游同步

- uv／pnpm locks、Python／Node runtime、native ABI、vendor revision／patch 都是發布输入；正式構建使用已鎖定依賴，不在啟動時追 latest。
- 框架／依賴維護與研究功能修復分開列變更範圍；若修復確需升級依賴，寫明因果並一起驗證。避免一次無理由升級所有套件。
- vendor 同步先比對上游、檢查本地 patch 仍需保留與是否已被上游吸收，再重建／驗證。記錄來源 revision、授權與 patch；不手改安裝中的第三方副本。
- 普通依賴更新走同一發布流程；高優先修復可縮短等待，但不省掉相關契約、構建與啟用檢查。這裡沒有建立定期 scheduler 或自動升級授權。

## 6. 配置、資料和身份的更新邊界

升級程式不得覆蓋研究原文、人工筆記、個人 override、credentials、DB／WAL、queue、attempts、lease 或執行回執。模板只提供推薦預設值；已存在的 project 設定採明示 merge／migration plan，列新增／變更鍵、有效優先序和衝突，保留使用者值，不能整檔替換。

workplace／project／task／command 身份與原始 provenance 保持，release ID 不取代它們。活躍任務保留原 execution release 與 command ID；升級後查原回執接續，不重派不確定是否完成的工作。

資料 migration 必須有版本前置條件、可核對結果、失敗中止點與可恢復方案；先用合成或隔離副本演練。SQLite 備份採一致性方式並考慮 WAL；不能複製一個活躍主 DB 檔便宣稱可恢復。可續跑 migration 必須設計重入規則；不推定所有腳本天然冪等。

**程式回退與資料回退分開。** 舊程式可讀新資料才可直接回退；若不可讀，需先安排一致性恢復／停寫，列出升級後新資料的保留與可能損失，再決定操作。不能回退二進位後讓它誤讀新 schema，也不能用舊備份覆蓋正在產生的新資料。

## 7. 單次更新的執行規則

1. **status／plan：** 列目標 host／project bindings、實際 installed 與 desired manifest、預期舊 hash、新 hash、活動工作、相容要求、migration、啟用方式與回退限制。全域 personal plugin 的範圍包含该主機所有受影響 project；不能暗改它們的 locks。
2. **build／verify：** 從唯一來源與 locks 構建一次，保留 payload／manifest／source provenance；consumer、平台及有效回歸通過。各端安裝同一個已驗證的對應平台 artifact，不各自拉最新來源重建。
3. **stage／apply：** 先放候選 release，核對 plan 前置 hash；如果現況變了，重新 plan，不能強制蓋過。依相容矩陣啟用；需要停啟的服務使用既有 supervisor，活躍工作按已驗證的保留／排空策略處理。
4. **activate／check：** 實際進程／MCP session 載入目標版本，檢查 schema、health、身份与原狀態接回。磁碟檔案換了、ZIP 上傳成功或 source pull 成功都不算啟用完成。
5. **receipt／resume：** 沿原 deployment IDs／receipts 記每端 installed hash、activation 和結果。中斷後先核對實際狀態與原回執再續，不重新派研究 command；部分失敗顯示部分完成。
6. **rollback／close：** 按當前狀態產生反向 plan，使用保留的已驗證包，先檢查資料相容；不能重用過期 plan。全部所選 targets 驗收後才標完整更新。

自動化預設範圍是檢查、構建與產生更新計畫；是否部署依該任務／專案的既有授權。可另設受授權的穩定版自動更新，但需相同 gates，不能從本文件推導無人值守部署授權。各研究 project 鎖定驗證過的 release 組合；候選版本先在隔離 workspace 驗證，正式進程不背景 git pull。

## 8. CI、AGENTS 與完成證據

| Gate | 必須阻止的情況 |
| --- | --- |
| 模組邊界與依賴 | 非法跨層 import、內部 API 直連、未宣告研究路徑／環境依賴 |
| 契約與版本 | schema binding 漂移、consumer 失敗、破壞性變更卻未明示／無 migration |
| 構建與發布 | lock 不一致、payload／manifest／metadata 不一致、來源或平台不可重建 |
| 狀態與恢復 | 受改動影響的身份、lease/CAS、command recovery、去重／重啟檢查失敗 |
| 部署驗收 | 實際載入版本未確認、全域 client binding 衝突、部分 target 未完成卻標成功 |

測試依改動風險選擇：修復以能重現問題的回歸檢查為主，共用契約跑其 consumer，狀態／恢復改動做隔離演練。純文件等低影響改動不造無意義測試；發布必要 gates 完成即可，不為展示而重跑無关全庫測試。未實作的 gate 不可記作已通過。

未來根 AGENTS.md 記全局邊界與更新規則，模組 AGENTS.md 記公開接口、維護來源及有效檢查入口；CI 執行可機械驗證的規則。文件、AGENTS 和 CI 都引用這份政策與正式契約，不維護三套不同版本語義。

每次可交付變更至少記錄：**問題／新行為、受影響模組、版本與契約分類、檢查結果、source commit／release manifest、部署範圍與啟用結果、migration／回退限制**。僅修復來源尚未發布時明示狀態；文件不代替實際回執。

**舊版保留與清理：** 在新版本完成驗收、回退演練且無活動命令／session 引用前，不刪其 runtime 或回執。之後按明示保留政策清理，研究狀態與原始資料不隨軟體版本清理。保留數量／時間於 S4 依容量與長任務需求定版。

## 9. 納入現有工程估算

S1 定公開接口／相容與來源權威；S2 做邊界檢查；S3 定配置／schema 演進；S4 實作更新、回退及回執；S5 演練失敗／接續；S6 實際啟用驗收；S7 落實 AGENTS、CI 與變更文件。這些已包含在 17–28 基礎人日、21–35 預算人日中。若新增無人值守部署監控、多版本長期支持或其他 OS，另外估算。
