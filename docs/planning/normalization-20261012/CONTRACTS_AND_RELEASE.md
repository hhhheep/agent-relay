# 契約、方向性相容與發布規格

2026-10-12。設計規格，非已生成的schemas/已完成的updater。owner是維護責任，不指定另一個Agent或新資料庫。既有契約的版本/required/default/枚舉以原程式為準，C1導出fingerprint；文檔不擅自升protocol。

## 1. 單一契約權威

| ID/範圍 | 基線唯一編輯來源 | 消費者／衍生物 | 必須保留 |
| --- | --- | --- | --- |
| Harness MCP 11 tools | solution_agent/harness/mcp_server.py + manifests.py | plugin/client/lifecycle；工具manifest/fingerprint | stage_execute/status/collect；terminal_open/exec/send/read/close；artifact_inspect/store/apply；輸入、錯誤與副作用語義 |
| EndpointIdentity / ActivationGate；protocol 0.6 | solution_agent/harness/protocol.py、release_identity.py | client/worker/manager activation | release/source/schema身份；不同步不默認相容 |
| TaskCapsule / ArtifactRef / WriterLeaseRef / WaitRef | solution_agent/harness/task_capsule.py | runtime；需要跨語言時導出JSON Schema/TS | task/workunit/command/version、writer lease與CAS，不能重建新ID |
| ArtifactManifest / Selector / ReadReceipt | solution_agent/harness/artifact_runtime.py、artifact_apply.py | MCP/artifact consumers | hash、provenance、允許範圍、原task-scoped store |
| VerifiedReceipt / ProviderUsage / TokenBudget | solution_agent/harness/contracts.py | receipt/usage consumers | provider實測與估計分開、stream hashes、exit與timeout |
| WaitRunState / WaitEvent / WaitAcknowledgement / WaitDisposition | solution_agent/harness/wait_contracts.py | runtime wait/recovery | ACK不等於解決，身份/游標/去重語義 |
| UsageEvent / ContextManifest / PhaseBudget | solution_agent/harness/telemetry_contracts.py | budget/context consumers | 原始來源、可回查references、真實用量 |
| manager/review/goal routing envelope | components/lifecycle/manager-task-protocol.ts + goal-routing store/native-dispatch | lifecycle/App/XATS consumers | 原idempotency、原始失敗/異議、owner及completion來源；尚散落的檢查C1收成一份authority |
| Writer EvidencePacket / DraftResponse | plugins/research-report-writer/lib/core.mjs 的Zod schema | Writer MCP與remote adapter | schema_version 1.0、execution/scientific分開、claim/source/scope |
| Writer 6 tools | plugins/research-report-writer/server.mjs | plugin/remote caller | provider_status、remote_writer_status、validate_evidence_packet、draft_research_report、validate_remote_evidence_packet、draft_remote_research_report |
| Writer remote payload | Writer lib/remote.mjs + remote/report_writer.py | Node↔Python | C1標出每種payload的owner，從主authority生成/驗證，不能兩端各改 |
| Guard receipt/stage ledger | token_guard.py + stage_ledger.py + validate_stage_receipt.py | local/remote skill及Harness usage adapter | research-token-guard/v1、raw stream/hash、執行/驗收區分；並非新MCP |
| Background config/queue/output | tools/research-background/service.mjs + provider.mjs + rules.md | local background/CLI | 檔案穩定/hash/rules/provider版本、候選與審查、重啟去重 |
| 新project descriptor / release / update plan | schemas/authored，C1/C4才建立 | Python/TS validators與maintain工具 | 禁秘密值，未知字段按契約拒絕，scope/bindings明示 |

現有Python模型搬到src後維持模組路徑；原Zod模型仍由Writer擁有，不強制全部轉Python。每個contract ID有authority path、owner、版本、fingerprint、generation task、outputs、consumer fixtures。在C1建立機械清單；主表是其輸入規格，尚未假造schema hash。

Python authority → schemas/generated → schema-bindings；新schemas/authored → generated validators/bindings。runtime不得反向依賴自己的生成步驟。生成檔只讀、帶來源/工具版本/hash，CI重新生成檢查diff。禁止從生成檔反向修改authority。

## 2. 相容矩陣與拒絕條件

| sender／receiver組合 | 本輪政策 | 必測／拒絕 |
| --- | --- | --- |
| 同一正式release的精確組合 | C5通過後才支持 | tool/schema fingerprint、正負fixtures、實際載入身份 |
| 抽取基線 ↔ 正規化候選 | 遷移必測，現在未宣稱支持 | 每方向old/new capsule/receipt/tool/error；啟動與resource path |
| 新receiver接受舊payload | 按契約逐項驗證 | required/default/enum/validation不得意外收緊 |
| 舊receiver接受新payload | 不默認支持 | extra=forbid下新增optional字段也可拒絕；sender須只發peer確認能讀的格式 |
| 任意N/N−1或版本範圍 | 不支持推定 | 僅列出實測 exact component/contract tuples |
| peer capability未知／身份不符 | 禁有副作用操作 | 可安全唯讀診斷並報degraded；不得盲目submit/CAS/lease |
| command submission uncertain | lookup原ID／原task證據 | 不重新派研究command，也不假定第一次沒執行 |
| state回退不相容 | 拒絕自動rollback | 保留目前DB/receipts，提供明示處置；不覆蓋活DB |

維持strict extra=forbid。新增欄位按實際發送方向分類，不以optional或MINOR代替相容測試。必要sender投影只能是已知peer格式、且不丟關鍵身份/lease資訊；不建泛用降版框架。

## 3. Release與部署回執

唯一release plan由既有builder生成manifest。最少欄位：release_id/source_commit、module versions、OS/arch/Python/Node/native ABI、每artifact相對入口及SHA、contract IDs/versions/hashes、MCP tool surface fingerprint、允許compat tuples、activation前置條件/順序、state讀寫格式要求/migration、rollback目標/限制、licenses/vendor revision/patch/locks hashes。

artifact manifest不放真實本機路徑、host/account、金鑰或研究內容。部署配置與receipts在本機受管位置保存實際bindings，發布包只聲明所需角色/相對入口。private部署receipt不進GitHub。

activation的通過是運行中的session/process報出目標release和schema，不只是source pull/檔案替換。Windows client/manager/Background、Linux runtime/worker可逐端處理，不能聲稱跨host原子更新。只有另有明確共用安裝升級授權時，單一personal plugin影響的已註冊project才須核對；這是部署安全檢查，不是把專案經驗推廣全域的權限。

更新流程：status → plan → preflight → stage → activate/check → receipt/resume → verify。plan固定source/target hashes、scope和前置條件，執行前比對實際狀態；舊plan過期則重產。build失敗不啟用。apply每端保存existing deployment IDs/receipts；resume先查實際載入和原回執。

rollback用当前狀態產生反向plan、已驗證舊包及相容state；不可複用過期plan。活躍command保持原execution release/ID/receipts，舊runtime到無引用才清理。SQLite備份需一致性/WAL-aware方式，在隔離副本演練；不以複製活主檔宣稱可恢復。

## 4. 邏輯狀態與研究權威

Execution由原command/OS/receipt權威回答執行狀態；Review由既有task/review/event及owner證據回答驗收；Research decision由研究vault/typed relations/分支決策回答科學主張。這不是三個新DB，也不把科學主張塞到execution enum。

跨層只交換project/task/workunit/command/artifact/source revision引用、可核對證據及各自狀態。遠端程序exit不能消除未解問題；負面研究結果可以合法完成驗收。必要子問題由原交付依賴判斷，owner附理由，保留重要異議；沒有其他owner就記責任缺口。

目前已有restart/lease/CAS/ACK流程要回歸驗證。新的停滯檢測、模型賦權、自主策略、共同流程改善只在需求台帳記錄，不在此文檔悄悄改成強制規則。

## 5. 施工驗收分工

C1：authority/方向性fixtures/支持入口與10fail分類。C2：src/wheel資源及各launcher從任意cwd啟動。C3：Writer/Background同package雙consumer golden、原preset/body/output/usage、secret-safe錯誤；兩folder隔離。

C4：manifest/tamper/stale plan/partial/reentrant resume/binary與state rollback前置檢查。C5：Windows/Linux fresh install/native；manager/XATS/runtime聯動；PID前cancel、重啟、lease/CAS、submission uncertain、session reload。C6：必要gates機械化。

合成測試不呼叫付費provider、不連現用SSH或讀live DB。原baseline checks可作對照，不能代替新package的消費端驗收。F前各支持target完整通過，未測平台保持unsupported/pending，不宣稱所有existing tests皆通過。

## 6. 第三輪收斂後的精確配置與停止規則

共用preset固定記錄現行 `model=deepseek-v4-flash-0731`、`enable_thinking=false`、`temperature=0.2`、`response_format={type:json_object}`、Writer `max_tokens=8192`。Background目前顯式覆寫 `max_tokens=4096`，transport `maxRetries=0`、`timeout=120000`，重試由原queue負責。這些是本次讀到的程式預設，C3用golden fixtures验证；不代表本次付費API驗收。

Preset是受版本控制的資料，正式版本/檔案hash在C3/C4固定。解析規格是「caller按既有契約允許的顯式字段 > caller選定版本化preset > 明示兼容預設」，不新增目前禁止的模型override，也不讓preset覆蓋有效caller設定。credentials維持目前 environment 值優先於所選env-file內容；Background對env-file reference的選擇保持原config precedence並列fixture。實際端點由外部配置引用供給並保留原校驗，不在preset寫入私人endpoint或key。

Compatibility exact tuple包括各component的source/artifact hash、contract ID/version/fingerprint、tool surface fingerprint、state格式讀写範圍及平台/ABI條件；真實path/account不進tuple。未知、未測或狀態前置條件不滿足時不得啟用副作用入口。

批次內單純import/path/config/fixture缺陷若保持已定owner/契約/研究條件，且在原授權與預算內，可自主修復重試。需要改資料權威、破壞性state遷移、研究共同規則、架構邊界、資源上限或未授權正式切換時，停止相應操作、保存證據並提交具體取捨；繼續不依賴它的授權內工作。這些條件不增加逐批批准。

## 7. 原協議限制與分拆細節

原 `protocol.py:verify_pair` 要求protocol_version、release_id、payload_sha256、mcp_schema_sha256相等，並核對workplace。正規化不得因新增system manifest而放寬這個0.6 gate。current client/remote runtime仍須同一核心identity；Writer/Guard可有各自component版本，但不由此推導跨release Harness網路互通。old/new directional fixtures是離線資料相容驗證，通過也不能繞過原activation拒絕。更改此協議屬明示後續scope/ADR。

逐檔map提供primary authority path；少數混合檔的符號分拆由[split-decisions.json](split-decisions.json)補全。`draftWithProvider`及報告prompt歸Writer；通用transport/config/schema-as-input helpers歸Provider。Writer core的safe transport status下沉，模型family/default值改由唯一版本化preset供caller顯式選；原schema/claim/evidence業務仍留Writer，不手工複製兩套常數。
