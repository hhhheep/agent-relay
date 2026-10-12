# 四輪討論與決策紀錄

2026-10-12。使用者指定的「討論專案正規化」聊天進行四輪實質回讀與追問。本地Codex查實際來源/imports/entrypoints，再送具體證據討論；另一聊天沒有直接逐檔審閱repo，不把它的意見稱為獨立程式驗收。聊天身份、turn/message IDs及回讀方式存本地排除發布的provenance回執。

## 第一輪：責任邊界與過度設計

送出完整系統背景、維護目標、初始apps/packages/plugins/vendor布局和其他chat擴展需求。回覆指出frontend owner、source→artifact、相容與啟用契約尚不足；建議Nx後延、只為真實行為建立模組。Harness MCP不必再建harness-mcp package；XATS daemon與我方入口分開。

採納：Nx不列本輪必要條件；保留單runtime；每個MCP入口明確owner；新研究UI/backend/source adapter只記需求。保留：Guard/provider是否值得拆需實際consumer證據，而不是只依目錄名稱。

## 第二輪：以source/import證據修正

本地送出Python相互引用、lifecycle對goal store多處引用、Writer provider→Writer core和Background動態provider依賴、Guard五個CLI及local/remote payload需求。

結論：Harness保持一個distribution；Lifecycle吸收goal-routing；provider有真實雙consumer，但只下沉共用transport/config/safeusage，Writer schema/remote adapter留Writer；Guard有真實package內容。schema-bindings是生成consumer，現有模型保持原authority。

新增必要條件：strict extra=forbid下optional字段也可能拒絕；按sender/receiver方向驗證，release只列已測exact組合。原state權威及command lookup/recovery保持，未知peer禁止副作用，但可安全唯讀診斷。工具版本/檔案替換不能代替實際session啟用驗證。

修正背景：較晚使用者已澄清Background只做本地整理，遠端收發件不是其驗收缺口。DSH是其他chat已試接的候選，未在904基線內，不自動納入部署，也不決定UI。工程估算拆可交付構建、隔離發布驗收、可選cutover，避免double count。

## 第三輪：核對vendor、授權與freeze

本地查出Paperclip goal parser有明示本地改編，XATS package只有CLI/channel bin、沒有startServer公開library export。修正為我方Paperclip衍生adapter+vendor provenance，以及單一受控vendor-port深層import固定XATS入口。

共用preset是唯一版本化配置資料，由caller選；provider不route/fallback，也不反向importWriter。exact compatibility tuple包括component构建hash、contract/tool surface/state scope及平台前置條件，不綁真實安裝路徑。

本地反對第二輪「每批驗收後需再批准」：未來若完整隔離工程已獲授權，各批自主依gate推進；只在改架構/目標/共同研究流程/資源承諾/未授權livecutover時交使用者判斷。另一聊天撤回逐批批准要求，認同gate與授權分開。共同Worker流程正式採用仍按使用者先前要求審查。

最終意見：沒有需重新設計模組邊界的重大問題，設計包可交付；需本地完成文件/映射一致性檢查。Design Ready與Engineering Verified分開：本次前者，後者必須施工跑跨平台/package/consumer/失敗恢復gates。任何「設計通過」不當作測試或部署通過。

## 第四輪：下一個 agent 的維護約束

使用者追加「要討論維護準則，避免下一個 agent 亂改」，因此送出八條規範並完整回讀審查。另一聊天指出三個漏洞：最小 diff 不等於完整修復；optional 不等於 strict receiver 相容；寫 ADR 不等於獲得授權。本地採納，直接落到根 AGENTS 和 MAINTENANCE_RULES。

八條：R1階段/授權；R2來源權威；R3最小完整修復；R4契約/state；R5分類/例外；R6供應鏈/發布；R7驗證/失敗；R8交接/擴展。根文件本身包含約束，不只列連結。設計布局、CI、locks、updater 均標 planned，不把文件審查稱為工程通過。

完成/合併/正式交付須 gate 通過；明示隔離 WIP 可保存失敗證據。不相關既有 failure 不阻塞文件修復，但支持 release targets 的失敗不能永久豁免。受影響失敗不得刪測試/降斷言。無工具/platform標 NOT AVAILABLE/NOT RUN，限制宣稱。

維護約束現在由人/agent遵守；boundary/schema/manifest/platform/rollback自動CI留待施工。未來整個隔離工程若授權可逐批自治，不加逐批批准。研究方法/共同Worker政策/livecutover等沿實際使用者授權，不以保存本工程文檔採用新研究政策。

第四輪結論：維護規範可定版，沒有新的架構級阻塞；需本地核對根AGENTS、maintenance、taskbook、mapping與contracts一致後交付。
### 第四輪補充：專案經驗不上升全域

使用者最新澄清維護方法屬專案經驗，不是全局修改方式。已再送指定聊天核對並完整回讀：規則只在Agent Relay生效；被其他repo引用不自動擴大作用域；經驗不等於採用政策；檢查bindings不等於可修改/覆寫/同步。對方同意沒有新的架構阻塞。本地同步根AGENTS、maintenance、taskbook、ADR、contracts及extension台帳，不改全域Codex/個人Skill/其他repo。

專案經驗在本專案既有記錄去重、保留條件/反例，不自動成為強制規則。若日後要全域推廣，必須使用者另有明確獨立任務；專案文件和其他聊天不能替自己授權。此補充是第四輪範圍澄清，不另建治理Agent或研究政策。

## 落實與未驗證

已落實於主任務書、ADR、契約/相容表、擴展台帳及941逐檔map；源頭904檔保全追溯。Framework選型以uv/Hatchling、pnpm/esbuild為核心，Nx以後只增targets。

施工留待驗證：正式locks/tool pins、Python resources/root discovery、runtime支持/legacy closure、10項既有fail、XATS vendor-port/native/shutdown、Writer/Background雙consumer、Windows/Linux全新安裝、update partial/resume/rollback、session/全域bindings。

本次未搬runtime、未修改installed插件/研究/金鑰/排程/DB、未啟動API或遠端模型實驗。文件檢查回執见[DESIGN_VERIFICATION.md](DESIGN_VERIFICATION.md)，不得以其取代正式工程gates。

使用者澄清落實的資料邊界：工程維護經驗留在 Agent Relay；研究/Worker 經驗留在產生它的研究 project，不匯入共用工具 repo。共用軟體發布只同步工程來源/產物，不夾帶專案經驗、研究流程或把它們變成其他 project 的預設規則。
