# 擴展需求台帳

2026-10-12。從與此共用系統直接相關的聊天與已保存討論文件收集，沒有遍讀無關個人聊天。最新使用者澄清優先於較早的助理提案。這份台帳保留具體位置與責任；不把規劃需求當實作授權或已驗證能力。

## 1. 來源與證據層級

| Alias | 原聊天/文件 | 本次讀取範圍 |
| --- | --- | --- |
| E01 | 規劃 Harness 下一步功能 | 讀取UI、cross-check、folder隔離、DSH、context compression等相關歷史頁及最新研究判讀修正；未重新計算研究數字 |
| E02 | 科研證據推理工作台：UI 與人機協作設計 | 完整現有討論；核對Codex主討論、DS輔助、來源/候選/確認分工 |
| E03 | 現在codex遇到問題還是不夠主動的解決，解決不了給我一個回復說 他遇到問題了沒有 你有什麼好的點子活著github skill 去促使他給我一點消息 | 最新治理邊界與討論稿：普通修復、共同流程審查、經驗去重 |
| E04 | 遠端 Worker 改善評估 | 讀取三輪交接/追問及本地已保存回讀紀錄；本次不重跑模型試驗 |
| E05 | 研究文件背景整理：持久狀態與 DS 自動處理 | 完整現有turns，特別採用較晚「只做本地整理」和無閃窗澄清 |
| E06 | FSFM 原任務監督與結果回收 | 所屬執行場景作目前系統背景；本次以E03/E04已核對案例記錄為根據，未重連遠端 |
| E07 | 本次正規化使用者要求與指定討論聊天 | 固定結構、小修維護、預留擴展、四輪架構/維護討論；最新澄清專案經驗不上升全域 |

原thread/turn/message IDs及定位資訊保存於排除發布的本地provenance回執。發布台帳只保留alias和泛化需求，不複製聊天全文、研究內容、host、私人路徑或credentials。聊天中的「已測」是歷史報告；本次未驗證者明示。

最新分期見[PHASED_DELIVERY](PHASED_DELIVERY.md)：目前P1整理/四情境安裝更新；下表位置為候選接點，非P1建立所有app/package的指令。P2真實能力、P3更新自動化、P4研究工作台按使用問題觸發；經驗留原project。

## 2. 具體擴展與位置

| ID | 需求／目前狀態 | 固定接入位置與最少契約 | 本次正規化要做／後延 |
| --- | --- | --- | --- |
| R01 | 科研證據工作台：問題/現象、調查畫布、條件比較、原文側欄、局部graph；尚未實作此UI | 未來apps/research-workbench；讀原vault/typed relations/Harness references，不建另一主DB | 記讀取、版本引用、狹窄修訂/衝突契約；UI和framework後延，不建空package |
| R02 | 跨文件核查：來源/數值/條件/主張支持/同源重複/過度推論；方案而非已訓練模型 | 未來packages/evidence-review，有真正consumer再建立；候選狀態、理由、method/version/source hashes | 固定研究權威與輸出需求；案例集、模型接入/訓練另估 |
| R03 | 從證據狀態→解釋缺口→探索分支，保留已做分析/反證/效應/區間/收束理由 | 原研究內容經workbench投影；reference+conditions+reason+decision revision | 不發明總可信度分數，不將每句不足變成新任務；不強制每問題快速重啟 |
| R04 | 按研究問題取有限context、保留查證能力；已有context_gateway/index/context_pack來源 | harness-runtime內原context API/policy；inputs/source versions/budget/artifact refs/selection reasons | 保持現有namespace及公開入口；新selector策略在原module加，不建通用context框架 |
| R05 | 管理端長聊天壓縮/找回歷史；billion-context桌面接入未驗證 | 可選外部client adapter；能力探測、session binding、保留引用；不改Harness任務權威 | 只留需求/評估；不自動代理桌面模型，不以worker壓縮冒充桌面改善 |
| R06 | model/backend/route策略可替換，按實際能力與成本驗證 | 原runtime worker/provider selector；明示profile/capabilities/budget/receipt | 先保留原注入點，模型選擇由caller；不新增scheduler或自動升級策略 |
| R07 | Worker自行查證/修復、求助/異議、管理後續、workunit繼承與重啟；改善草案 | 原task/review/help/events/manager，task/command refs、證據、owner、下一步、未解狀態 | 回歸原有可靠性；新增賦權/停滯trigger/方法Skill/能力試驗後延，不新增仲裁Agent |
| R08 | 專案內條件式經驗保存與去重；不得自動變成共同流程或上升全域 | 產生該經驗的研究project既有案例/任務紀錄；來源、條件、反例、驗證/採納狀態；共用repo僅保留工程機制，經驗內容不匯入 | 文件整合保留規則：既有足夠不重寫、忘記有代價才存、同類補原紀錄；治理變更正式採用需使用者審查 |
| R09 | 低速本地自動整理、落盤進度、停啟續跑、安靜/無終端閃窗；已有來源與歷史驗收 | research-background + 共用report-provider；來源hash/rules/provider/attempt/output/usage | 必須保留現行能力；不要求Codex/Obsidian常開；不借新遠端傳輸擴大原驗收 |
| R10 | 一folder一專案，多folder共享程式，各自配置/state/workplace，方便同步升軟體版本 | project descriptor/init/check、release lock、已註冊bindings及deployment | 本輪合成雙project隔離/版本更新驗收；實際新研究folder/remote啟用需相應操作範圍 |
| R11 | DS工具操作與持久session，可接DSH；E01報告CLI讀檔/session隔離已測，本次未重測且不在904來源 | 可選backend adapter，future app/module按真正consumer成立；endpoint/model/credential reference、capabilities、session/project/version | 先source/dependency/license inventory；不整包納入runtime，不以DSH決定UI、不宣稱省token |
| R12 | 本地/遠端成果交付、selected Markdown同步與衝突；現有artifact/hash/Phase0plan | 原artifact store/apply/task_md_sync/md_snapshot/md_sync；command ID、provenance、版本/CAS | 正規化保留現有語義；無人值守遠端收件/全vault雙向同步僅未來候選，不是此次必要功能 |

## 3. 不可被舊討論覆蓋的澄清

- Codex與使用者是研究討論主力，DS背景輔助；網頁不另建兩套聊天入口。
- UI把已有研究Markdown、節點、關係呈現和修訂，不宣稱原系統完全沒有競爭假設/證據鏈。
- 收束未知應說明對決策的影響；已有判斷與機制尚未識別可以並存，不無限增加實驗。
- Background原完成範圍是本地文件整理，遠端收發件不算未完成項。較早助理把它列缺口的說法已被使用者更正。
- 專案經驗不自動上升全域 Codex/個人 Skill/其他 repo，也不代表批准流程。本專案流程正式採用/修改前需使用者審查；全域推廣需另有明確獨立任務。普通任務內修復沿既有授權自主完成。
- 建議/過往smoke/來源存在/生產驗收是不同證據層級；本輪不把它們混用。

## 4. 擴展進入工程的門檻

每項新功能先指定現有owner或在預留位置成立實際模組，寫實際consumer、輸入/輸出/權威、成本/授權、相容fixture與退出/回退條件。普通可相容新增不搬核心目錄。若需要改原責任/資料權威/依賴方向，先提ADR例外和具體代價；不以補件不斷堆疊。

未來UI和source/backend adapters可以有新的接口版本與module，不預先承諾任意插件動態載入、跨host零停機或所有版本互通。先用成熟部件時仍檢查license、dependency closure及如何接既有資料；喜歡UI不代表替換其整套agent backend。

最新作用域：工程維護經驗留在 Agent Relay；研究/Worker 經驗留在產生它的研究 project，不匯入共用工具 repo。共用軟體發布只同步工程來源/產物，不夾帶專案經驗、研究流程或把它們變成其他 project 的預設規則。
