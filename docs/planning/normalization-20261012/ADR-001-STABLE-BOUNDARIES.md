# ADR-001：Agent Relay 穩定模組邊界

日期：2026-10-12。狀態：經討論收斂的設計決策；待授權施工和驗收，尚未實際 Architecture Freeze。

最新修訂：[ADR-002](ADR-002-PHASED-DELIVERY.md)與[分期交付](PHASED_DELIVERY.md)已取代本文件的全面布局/C1–C6一次施工預設。以下保留完整候選及當時理由；來源權威/原接口/身份與專案作用域不變，apps/packages全面搬遷不再是必做承諾。

## 決策

採用單repo，apps/packages/plugins/schemas/templates/deployment/vendor/tests/docs的固定分工。現有Python solution_agent保留一個distribution及原內部namespace；Harness MCP實作留runtime；lifecycle吸收goal-routing；XATS第三方daemon與我方入口分開；Writer業務/remote adapter留Writer，共用provider下沉；Guard以實際CLI內容成立package；schema-bindings只生成consumer。

uv/Hatchling、pnpm/esbuild為工具鏈。Nx後延，未來只增加targets，不重構布局或發布權威。既有builder/manifest與task/artifact/command/state identities保留。

## 理由與實查

Harness→orchestrator/工具，orchestrator→remote_materializer，remote_materializer→Harness，強拆會新增跨包循環。goal store/native-dispatch被lifecycle多處引用。Background動態使用Writer provider，provider目前反向依賴Writer core，需把傳輸/安全配置與報告schema分開。Paperclip parser有明示本地改編，不可當未修改vendor。XATS沒有startServer公開library export，依賴隔離於單一vendor-port並按版本/hash測試。

這些支持真實責任邊界，而不是為每個函數建package。詳見migration表、dependency-evidence及主任務書。

## 擴展方式

有新consumer/獨立生命周期時，在既定apps/packages位置新增具體模組。研究工作台、來源adapter、context policy、backend adapter、review改善先記需求/公共接口；不預造pluginbus、generic scheduler、仲裁Agent或第二DB。

為現有功能抽公共接口時，保留方向性相容fixtures和唯一owner。未實作能力不因文檔出現就視為已授權實作或支持。

## 結構凍結與例外

D0完成設計包；F是施工完成、current consumers與平台gates通過後的結構凍結。F後普通修復不搬目錄/改namespace/換框架。例外ADR需附實際需求、現有接口不足的證據、必要改動、consumer遷移、驗收及回退成本；使用者决定擴大架構或共同流程範圍。

未來使用者授權完整隔離施工後，各批自主驗收推进，不逐批再請批准；技術失敗按stop/recovery處理。livecutover與本專案流程正式採用依其實際授權另行判斷。維護經驗只屬此專案，不自動升成全域 Codex/個人 Skill/其他 repo 規則；全域推廣需使用者另有明確獨立任務。

## 代價與限制

保留舊Python內部依賴意味著需要明示legacy邊界，不能承諾整包舊功能全驗收。XATS入口仍是固定版本的內部耦合，風險集中而非消失。跨平台locks/native/online安裝還未完成。模型preset、state format與全域plugin bindings需要施工驗證；不靠目錄設計宣稱已解决。
