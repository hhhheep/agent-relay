# Agent Relay 分期交付與第一期任務

最新施工授權：[FIRST_RESTRUCTURE_EXECUTION](FIRST_RESTRUCTURE_EXECUTION.md)。使用者已要求新chat開始P1，首次可集中重整框架，不受小步修改/必保留舊物理目錄限制；定版後才轉為漸進維護。下文只規劃/保留主要目錄的先前限制由此授權取代；第一期四情境、原契約/state、現用保護及後期按需範圍不變。
2026-10-12。最新使用者範圍：先整理、讓新設備可安裝、留實際擴展位置；按 Windows 客戶端 × Linux Worker 的新舊組合思考，也要處理舊／舊更新。本文件是當前施工範圍權威，取代完整設計中「全部 C1–C6 一次做完」的預設。第一期已實作並進行隔離安裝與Harness跨端檢查，尚未完整驗收或現用切換；目前結果見[工程狀態](../../engineering/phase-one/STATUS.md)。

## 1. 第一個工程目標

讓目前這套插件與 MCP 工具，能從獨立 GitHub 來源在 Windows 客戶端及 Linux Worker 重建、配置、安裝、連線、維護；舊端可按明確流程更新，保留各研究 project 的資料與狀態。

使用方式維持插件／MCP；不建立新的 agent 平台。模組按責任分清，不為每個功能拆 distribution。整理以移植問題、來源唯一及直接 consumer 為依據；不是照舊941 map機械搬目錄。

「新／舊」指設備及已有部署環境，不直接表示協議版本。新設備可安裝已驗證的舊release；舊設備可安裝新release。軟體相容另以實際identity/contract/artifact/state核對，不承諾任意舊版互通。

## 2. 四種第一期必驗情境

| ID | Windows 客戶端 | Linux Worker | 第一期間要證明什麼 |
| --- | --- | --- | --- |
| M1 新／舊 | 新電腦/乾淨安裝 | 已有Worker | 不借原電腦依賴或私人路徑；配置新client，對既有Worker身份做預檢；使用實測匹配的release配對 |
| M2 新／新 | 新電腦/乾淨安裝 | 新Linux/乾淨安裝 | 兩端從declared來源和依賴重新安裝；配置各自identity/workplace，核心MCP/任務/結果交付可走通 |
| M3 舊／新 | 已有client環境 | 新Linux Worker | 保留client原配置、歷史與其他project；增設/切換明示worker binding；驗證新worker身份和目標，不誤派給舊worker |
| M4 舊／舊更新 | 已有client環境 | 已有Worker | 從已納入支持的基線更新到P1候選；更新前盤點/保全、逐端啟用核對、配置/state不丟失、原IDs/receipts保留、可退回已驗舊版 |

每一列都記device role、OS/arch/runtime、起始/目標release、實際loaded identity、配置/state歸屬、所跑行為、結果及回復證據。四列是工作情境，不能只測M2後說其他三列自然成立。

舊端用已支持舊包及隔離配置/state副本建測試環境，不直接拿現用研究環境做破壞演練。M2須沒有原開發機的venv/node_modules/cache、私有路徑或借用原研究checkout；能按文檔取得鎖定依賴，而非只複製本機離線環境。

M1/M3不能把「新設備連舊設備」解釋成新舊Harness版本隨意混用。protocol 0.6 verify_pair仍要求protocol_version/release_id/payload_sha256/mcp_schema_sha256一致並核對workplace。若舊端不匹配：安裝已驗證匹配的client/worker包，或在另有更新授權時受控更新舊端；否則停止副作用並明示未通過。M1保持舊Worker硬體不變，不等於永遠不能更新它的軟體。

## 3. 第一期間只做這五組工作

| 批次 | 工作 | 交付/通過條件 |
| --- | --- | --- |
| S1 來源與支持清單 | 整理source owner、build產物、vendor與歷史測試；列現行插件/MCP/CLI入口、支持Windows/Linux target及舊release起點 | 唯一編輯來源、每個入口owner、支持/不支持項、10個既有fail歸屬；現有904來源保全可追溯 |
| S2 移植與安裝輸入 | 只修硬編碼路徑、資源查找、cache/研究目錄依賴及真共享Provider耦合；鎖定必要依賴，沿既有builders產出兩端包 | 任意工作目錄可啟動；設定/credentials/state在來源外；Python/Node/native按目標重建；每包source/version/hash/入口可查 |
| S3 新端重建與配置 | 實際驗證Windows client與Linux worker乾淨重裝、模板/本地override、最小preflight和獨立project配置 | 清楚的兩端安裝步驟、環境要求、配置位置及檢查命令；不需要原電腦檔案；不自動註冊現用排程或部署研究任務 |
| S4 四情境與舊端更新 | M1–M4逐列验证；保留原協議、MCP工具面、命令與receipt/CAS/lease；沿既有更新腳本或明確操作步驟 | 四份結果；原配置及合成state升級/回退前後核對；loaded身份正確；uncertain查原ID；不重派原command |
| S5 第一版交付 | 文檔、最少必要CI/檢查、來源與artifact隱私、發布版本、擴展入口示例、安裝/更新/交接清單 | 另一人/agent只依repo與自己的配置能重做；版本可定位、舊包保留；小型擴展在原owner落地且不搬核心 |

第一個實作動作是S1：確認現用入口和兩端支持/舊版起點，不能先全面移動941檔。此段保留第一步施工順序；S1–S5已推進至工程候選，是否完成以工程狀態與逐列回執為準。

## 4. 整理到什麼程度

保留現有主要目錄，固定其責任和公開入口。第一期通過後以此來源/入口為維護基礎；後期增量加實際模組，不把apps/packages全面搬遷當必做。

| 現有來源區 | 第一期間的責任/處理 |
| --- | --- |
| solution_agent/ | Harness核心、MCP、CLI、worker及原契約；保持原namespace和一個核心單元 |
| plugins/solution-agent-harness/ | 插件描述、skill與client入口；其現有transport權威清楚記錄，不另手抄核心 |
| plugins/research-report-writer/ | Writer MCP/業務schema/remote adapter；目前留原位置，不另建apps/report-writer副本 |
| plugins/research-token-guard/ | 五個既有CLI及skill、同來源local/remote交付；不先為它新增MCP或強制獨立wheel |
| components/lifecycle/ + components/goal-routing-integration/ | 一個Lifecycle owner；沿既有router/goal入口整合，不為整理立即強搬資料夾 |
| components/xats-routing-lab/upstream/ | 固定第三方來源/授權；只隔離必要我方入口，不先搬整個vendor或升版本 |
| tools/research-background/ | 本地整理/queue/安靜啟動；保持原驗收範圍，正式Provider入口不讀installed Writer cache |
| packages/report-provider/（S2需要時建立） | 唯一真正共享transport/config/safeusage；Writer與Background兩consumer實測；業務schema/remote adapter仍歸Writer |
| scripts/harness/、既有component builders | 沿用打包/部署來源；必要修移植問題，不先建立通用maintain平台 |
| config/、tests/、docs/、scripts/extraction/ | 通用模板、按owner驗證、維護入口及不可改寫的歷史provenance；不搬研究資料 |

原[migration map](migration-map.json)及[symbol split](split-decisions.json)保留為完整設計候選與來源分析，**不是第一期搬檔指令**；其owner分析可引用，apps/packages目的地不構成後期必做承諾。[ADR-002](ADR-002-PHASED-DELIVERY.md)取代ADR-001的全面布局施工預設。必要的個別移動須記實際原因、來源/consumer與驗證，不能再搬一次只為目錄整齊。

依賴可以先保留各component現有工具與鎖定方式；在實際目標驗證後固定版本。不要求先完成root uv/pnpm workspace、通用schema-bindings或Nx，才准裝插件。構建仍只有一個來源authority，不在安裝副本永久修補。

## 5. 第一期間必守與可以後延

| 第一期間必守 | 可以後延 |
| --- | --- |
| MCP/CLI名稱、identity/command/receipt/CAS/lease不變；受影響契約及失敗回歸 | 通用schema生成/跨語言bindings框架；不因此放鬆現有契約 |
| 來源/包版本/hash、支持平台依賴/native閉包與loaded身份核對 | 全套release compatibility資料平台、廣泛OS/版本支持 |
| M4受控更新、配置/state保全、已支持起點回退和停止條件 | 自動status/plan/apply/resume/rollback updater與全端部署編排 |
| 按改動及四情境選有效測試，privacy/source/安裝文檔檢查 | 全repo每次重測、完整failure-injection總平台、Nx task graph |
| owner/固定公開入口、一個小型實際擴展驗證 | 新UI/DSH/context/backend/Worker研究策略，空package或pluginbus |

取消1項若屬支持入口必須在P1定位並修到符合既有語義；legacy配置9項依S1支持入口判定，支持的必修，不支持的保留分類/證據。不能為減少第一期工作量刪測試、放寬身份或把仍支持的失敗後延。

第一期不改state格式，M4只針對S1列出的舊release→P1 release。若發現不可避免的破壞性state migration、未知舊版無法配對、沒有可退舊包，該項停止並另列必要工程，不假稱第一期完成。仍活動的命令保留原execution release與IDs；不強殺或重派。現用部署要依實際授權，不由本規劃自動執行。

## 6. 擴展位置如何留下

第一期留下「owner、公開入口、配置注入、來源/工具面與驗證方法」，不先建所有候選功能。

- 新MCP工具：在既有server/owner新增，明示input/output/error及回歸；不另造框架。
- 新provider/backend：在真共享Provider或現有worker selector接有consumer的adapter；配置及模型選擇仍由caller/project決定。
- 新本地整理規則：在Background現有rules/config/input介面加入；研究經驗留所屬project，不發布成全域規則。
- 未來研究UI：只留需求及讀取原研究來源的owner，待實際查看/修訂需要才建實作。

S5優先用已有consumer的規則/config/Provider override fixture證明擴展入口可用，無付費API，核對還原後核心和研究state不變。若沒有真實consumer，只寫owner/契約及接入示例，不為驗收新增假MCP工具或空adapter；Writer/Background双consumer可提供既有實際案例。

## 7. 後續各期：由使用需求觸發

| 時期/方向 | 何時需要進入 | 到時做什麼 | 結束條件 |
| --- | --- | --- | --- |
| P0 獨立與私人GitHub | 已完成來源獨立/發布 | 保留基線與provenance | 已存在；不等於P1安裝驗收 |
| P1 整理、四情境安裝/更新與擴展基礎 | 現在的目標 | S1–S5；Windows client、Linux Worker、M1–M4 | 來源可維護、两端可重建、四情境驗證，舊端可保全與回退 |
| P2 真實功能擴展 | 出現具體新工具/provider/backend/source需求，已有consumer及驗收案例；日常用法能指出缺口 | 在既定owner加該項功能/adapter，必要時新增真實module | 新能力及舊consumer通過；不改核心布局，不附帶實作其他候選需求 |
| P3 更新自動化與多端管理 | 人工更新已反覆發生版本不同步/漏端/中斷恢復問題，或要同時管理多組client/worker，已有case/成本證據 | 將P1已驗流程機械化：status/plan/apply/resume/verify/rollback、必要compat matrix/部署回執 | 多端/partial故障可定位與恢復；沿原IDs/state，不重派研究任務 |
| P4 研究工作台/協作能力 | 研究使用真正需要跨文件比較、證據可視化/狹窄修訂或多人審閱；有原始資料權威與UI案例 | 按需求台帳建workbench/review，保留原vault及人機判讀責任 | 實際研究操作有幫助、引用可查、修改不越權，不變成第二主DB |

P2/P3/P4是按需方向，不必按號碼全部升級，也不由日期或文件存在自動啟動。普通bugfix仍是P1維護，不必開下一期。新增OS/arch僅在真的有該設備時作target擴展，不先承諾所有平台。

開始下一期前只需明確「實際使用問題/consumer、該期範圍、驗收、成本/授權」；不為每個普通修復加批准流程。仍需另行決策的範圍維持根AGENTS。不同研究project經驗不因軟體更新互相同步，更不自動上升全域。

## 8. 第一期間估算與驗收證據

暫定人日：S1 1–2；S2 3–4；S3 2–3；S4 2–4；S5 1–2。合計9–15，含約25%餘量約12–19人日。這是雙平台與四情境、現有功能不變、沿既有builders的規劃估算，不是Codex日曆承諾。

前提：實際Linux發行版/arch/runtime明確且有隔離測試目標；S1舊包及起點可取得；無破壞性state migration；已知失敗不需整體重寫。S1完成時按真正支持範圍重估；前提不成立列缺口/增量工作，不靠縮小驗收假裝成本變低。先前20–34人日屬完整工程候選，不再是P1必做成本。

P1結果回執要有S1支持表、两端實際依賴/建構、M1–M4逐列證據、原/目標loaded identity、舊端配置/state核對、退回驗證、既有fail處置與NOT RUN/NOT AVAILABLE。缺任何必支持情境只能寫partial，不能以M2乾淨安裝成功宣稱P1完成。
