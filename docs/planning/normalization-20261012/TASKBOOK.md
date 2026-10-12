# Agent Relay 正規化施工任務書

日期：2026-10-12。狀態：經跨聊天審查的設計包；未開始程式遷移，未切換現用安裝。這是本次正規化的最新主文件，取代 10/10–10/11 文件中未落實的結構、Nx 必選及工程估算建議。歷史盤點仍保留。

## 1. 交付目的與範圍

一次確立共用程式、插件、第三方來源、配置及狀態的責任邊界，之後沿公開接口、小範圍修復與增量功能維護。最小修正是完整解決問題所需的最小影響範圍，不是最少行數，也不是反覆新增補件。

已完成：獨立來源、隱私清理、私人 GitHub 基線及 Windows 離線乾淨 clone 驗證。基準 commit 為 `6337753ed436e9d230b50c2e1c2356f6c8bf4ee5`，941 tracked files，904 映射來源。基線 tag 為 `extraction-baseline-2026-10-12`。

本次 goal 交付文件、來源映射、接口與擴展邊界、逐批驗收及估算，並实际更新根 AGENTS 與維護準則。施工中的 package metadata、locks、validators、updater 仍是待做能力。未來獨立正規化工程與現用切換是不同工作範圍；不能從這份文件推導部署授權。

不把研究 Conda/GPU 環境、vault、真實 credentials、運行 DB、queue、leases、workplace 身份帶進共用程式 repo。研究文件沿原本權威來源保存；OS clipboard 不代替 Harness Artifact Clipboard。

## 2. 固定布局與責任

以下是目標布局。施工只建立有真實程式或契約消費者的模組；未來位置先以本文件預留，不建空 package。

```text
agent-relay/
  pyproject.toml / uv.lock
  package.json / pnpm-workspace.yaml / pnpm-lock.yaml
  apps/
    lifecycle-manager/
      src/                         # manager/router/mailbox/publication/resume/session
        integrations/              # goal-routing、App、Paperclip 衍生 adapter
      tests/
    xats-service/
      src/                         # 我方配置、啟停；vendor-port 為唯一上游內部接點
      tests/
    report-writer/
      server.mjs / lib/ / remote/ / tests/
    research-background/
      service.mjs / cli.mjs / provider.mjs / tests/
  packages/
    harness-runtime/
      pyproject.toml
      src/solution_agent/          # 原 namespace、子目錄、現有模型位置均保持
    report-provider/
      src/ / profiles/ / tests/    # 共用傳輸、配置、安全用量；非報告業務
    token-guard/
      pyproject.toml
      src/research_token_guard/ / tests/
    schema-bindings/               # 生成 consumer bindings，不擁有另一套模型
  plugins/
    solution-agent-harness/
    research-report-writer/
    research-token-guard/          # manifests、skills、薄 launcher、skill-only helpers
  schemas/
    authored/                     # 新 project/config/release 契約的唯一編輯來源
    generated/                    # 現有模型導出；不可手改
  templates/                      # 通用非秘密示例、rules 與 fixture 配置
  deployment/
    harness/ / background/        # 既有 builder/installer 的正式位置
    maintain.py                   # 既有 helpers 的單一維護入口，待實作
    project.py                    # 明確 init/check，待實作，不自動啟用服務
    compatibility/
  vendor/
    xats/                         # 固定 0.8.6 選定來源、既有 plugin、license
    paperclip-provenance/          # 原 commit/source hashes/license
  tests/runtime/ / tests/integration/ / tests/fixtures/
  scripts/extraction/ / scripts/architecture/
  docs/architecture/ / docs/planning/ / docs/extraction/
```

| 模組 owner | 責任與對外入口 | 禁止承擔 |
| --- | --- | --- |
| harness-runtime | Python 核心、11 tools MCP frontend、CLI、worker、artifact/CAS/lease/command/receipt | Writer/Background 業務、UI 或新的全域排程器 |
| lifecycle-manager | 現有 router/manager/session/mailbox、goal-routing、App 整合 | 冒充程序成功已通過科學驗收、直接改 Harness 私有 state |
| xats-service | 我方 config/token reference、loopback、health、shutdown | 重寫第三方 daemon、合併其 DB |
| report-writer | 6 tools MCP frontend、證據包/報告 schema、remote adapter、報告驗證 | 共用 provider 的另一份手寫副本 |
| report-provider | credential reference/endpoint 校驗、transport、request body、安全 usage；由 caller 選 preset | 自主選模型、fallback、研究結論、反向 import Writer |
| research-background | 本地穩定文件補查、去重、持久 queue、待審阅摘要、無閃窗啟動 | 隱含遠端收發件、直接批准節點/共同流程 |
| token-guard | bounded output、hash receipts、stage ledger、local/remote CLI payload | 第二個 MCP server、第二個 artifact 傳輸系統 |
| schema-bindings | 單向生成、消費端驗證及漂移檢查 | 手工重寫現有 Python 領域模型 |
| plugins | 安裝描述、skills、啟動 adapter | 人工維護核心邏輯的第二份來源 |
| deployment | 構建輸入、交付包、release/plan/receipts、啟用及回退 | 研究派工、state authority、背景追 latest |

Harness 的現有子系統彼此引用，先保持一個 distribution；現有循環記 baseline，禁止擴大，相關修復時可局部減少。不要為了目錄整齊把 178 個 Python 檔全面拆包。原 `contracts.py` 是檔案，不在本輪變成同名目錄。

goal-routing 已被 manager/router 多處引用，歸 lifecycle-manager 內部整合。Paperclip goal parser 是有改編的衍生程式，歸我方 integrations，保留原 header、MIT 與 provenance，不稱原封不動的 vendor。

XATS 只有 CLI/channel CLI 的正式構建入口，未宣告可直接使用的 startServer library export。本輪允許單一 `vendor-port` 引用固定上游內部入口，列明例外、hash 與 consumer tests；禁止其餘模組深層 import。將來可在此 adapter 內改接上游公開接口，無需改整體布局。既有上游 plugin 留在 vendor，從它構建；不另維護我方同名 plugin。

## 3. 工具、依賴與資源

Python 使用 uv 管 metadata/lock，Hatchling 產生 wheel；Node 使用 pnpm workspace/lock、既有 TS/ESM、esbuild。Nx 改為可後加的構建編排工具，不是本轮前提。固定 target IDs、inputs、dependencies、outputs，可日後包成 Nx targets而不搬目錄。發布權威始終是既有 builders + 唯一 release plan/manifest。

Python baseline 3.13.12、Node 24.19.0 是已觀察的 Windows 環境，非宣稱已驗證 Linux。工具精確 patch/build backend 版本於 C2 隔離安裝測試後鎖定；不追 latest。XATS 固定 0.8.6，native better-sqlite3 12.9.0 必須按 OS/arch/Node ABI 出包。Writer 與 Harness 不同 MCP SDK 代際各自鎖定，不為了共用 workspace 強升到同一版。

uv workspace 僅包含相容的系統 Python packages；有不相容依賴時使用獨立 project/lock，物理布局不變。pnpm 根 lock 管我方成員，vendor 原 lock 留 provenance；vendor 如需獨立 resolution，另鎖其可重建 closure，不把兩份 lock 當同一權威。

| Caller | 允許依賴／通信 |
| --- | --- |
| Writer、Background | report-provider 公開 exports；schema-bindings 的需要部分 |
| Lifecycle | schema-bindings；Harness MCP/公開 RPC；XATS transport；我方 App/Paperclip adapter |
| XATS service | 唯一 vendor-port → fixed vendor；native 平台依賴 |
| Harness、Guard | 自己的 Python package/明示共用依賴；不 import Node apps/plugins |
| Plugin launcher | 正式 package/交付入口；不讀研究 checkout 或另一插件 cache |
| Builders、schema generators | 明示 source/locks/contracts；build-only 依賴不成為 runtime 反向依賴 |

Python Import Linter、Node exports/ESLint 規則與構建輸入掃描聯合檢查；絕對檔案路徑可以繞過 exports，必須拒絕跨 module 路徑偷讀。動態 import/`provider_module` 另有 allowlist 與 runtime fixtures，不能聲稱 AST 捕捉所有依賴。

從 src layout/installed wheel 啟動不能依賴 `__file__.parents[n]` 找 repo、研究 `.solution` 或相鄰 scripts。C2 明定 code/resource/state/profile roots，resources 用 package data/標準資源 API；舊 SOLUTION_AGENT_ROOT 等 override 可受測保留，但不再借原研究環境。RPC helpers 歸 runtime 的 `harness/integration_rpc`，legacy filepath wrapper 只由 builder 生成。

Provider 拆包先保持原 endpoint、thinking/body、model preset、schema、usage 和錯誤語義。共用 preset 是唯一版本化配置資料，Writer/Background 各自顯式選它；provider 不替 caller 決策。Writer 的 Draft schema、evidence checking 和 remote adapter 留在 Writer。Background 不再 import installed Writer cache；兼容 override 必須可核對來源且有明確範圍。

工具依據：[uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/)、[uv build](https://docs.astral.sh/uv/concepts/projects/build/)、[pnpm workspace](https://pnpm.io/workspaces)、[Node package exports](https://nodejs.org/docs/latest-v24.x/api/packages.html#package-entry-points)、[Nx tasks](https://nx.dev/docs/features/run-tasks)。它們提供工具能力，不代表本 repo 已完成接入。

## 4. 來源、接口與交付包

逐檔設計見 [migration-map.json](migration-map.json) 和 [migration-map.csv](migration-map.csv)。941 基線檔案全部有 owner/module/目的地/artifact disposition/維護方式；其中904有原抽取 provenance。映射是遷移計劃，不是已搬移或全功能驗收。

`harness-runtime` 的舊子目錄保全於原 namespace，列 legacy-candidate；不能默認全是現用，也不能自動刪除。只有已宣告 current entrypoint 和其經驗證的依賴 closure 才是正式支持範圍。研究案例 metadata/舊測試保留 historical/test 分類，不變成共用系統的新科學功能。

| Artifact ID | 正式來源與平台 | 最低驗收 |
| --- | --- | --- |
| harness-wheel | harness-runtime，Windows/Linux | clean installed wheel；公開 CLI/MCP；resources；依賴 closure |
| harness-client-plugin | plugin + 受管 Python runtime，Windows | 11 tools schema/啟動/身份；無原 venv |
| harness-worker | runtime + 原 worker payload，Linux | command 身份、cancel/recovery/lease；原 run root |
| lifecycle-bundle | lifecycle-manager + 衍生 Paperclip，Windows | 原 77 router/goal 檢查、session 接回、mailbox |
| xats-bundle | xats-service + vendor/native，Windows | config/auth/DB path/shutdown；native memory/持久 fixture |
| writer-plugin / writer-remote | Writer + provider/plugin；local Node/remote Python | 6 tools、schema/body/usage；remote resource 定位 |
| background-bundle | Background + provider + launcher，Windows | 12 mock 檢查、停啟/去重/兩 folder、無閃窗 |
| guard-wheel / guard-plugin | Guard + plugin；Windows/Linux | 37 檢查；同一來源 local/remote receipts |
| schema-bindings | authority → JSON Schema/consumer bindings | 生成無漂移、方向性 fixtures |
| source-only / test-only / provenance-only | 文件、基線 metadata、研究測試例 | 不因存在於 repo 就納入 runtime payload |

新版本保留入口名稱和既有協議。詳見 [契約與相容規格](CONTRACTS_AND_RELEASE.md)；未測組合不依 SemVer 大小推定相容。

## 5. 項目配置與狀態

一 folder 一研究 project，共用安裝可服務多 folder。每個 project 保留自己的 project identity、workplace、release lock、vault、state/profile/credential references。共享程式不等於共享研究資料。

新 descriptor 的優先級：明示 invocation 指定 project → 該 project descriptor → 明示 local override → release 的非秘密預設；credential 使用目前受支持的環境變數／env-file reference。既有入口實際優先級須先錄成 fixture，再轉譯到 descriptor；不得在搬移時靜默改 precedence。驗證按消費模組拒絕未知配置，保留既有有效 override 的明示兼容入口。

init/check 只建立模板、核對相對路徑及版本要求，不註冊插件/排程或自行部署遠端。只有另獲明確共用安裝升級授權時，personal plugin 升級才枚舉已註冊的受影響 project；此技術核對不代表專案維護可改全域規則。無法確認 bindings 時停止啟用，不能只核對當前 folder。

Execution、Review、Research decision 是三種語義，引用原 task/workunit/command/artifact/source revision；沿現有 receipts、review/events 和研究 vault，不造平行主 DB。程序退出、ACK、回答核對、科學主張成立各自不能代替另一項。新增自主行為和共同流程仍屬後續提案，不因正規化自動批准。

## 6. 分批施工與 gates

D0 是本次設計包完成條件：來源映射完整、owner/接口/擴展/交付/gates/估算明確，完成實質回讀審查，未決問題有責任批次。D0 不等於已完成實作的 Architecture Freeze。

未來使用者授權整個隔離正規化工程後，依此範圍自主逐批推進；每批 gate 是技術驗收與停止條件，不新增逐批批准。擴大架構/共同研究流程/成本/存取或未授權 live cutover 才交使用者判斷。

| 批次 | 工作與不可變條件 | 輸出與通過條件 | 停止／回復 |
| --- | --- | --- | --- |
| C1 | 鎖入口/支持範圍、API/schema authority；只讀 baseline | 941 mapping、支持/legacy 列表、tool fingerprints、方向性 fixtures、現行根路徑與動態入口清單 | 未知現用入口先保全；不刪除；新必要範圍重估 |
| C2 | package/layout/locks/resources/邊界；namespace 不變 | wheel/bundles、package exports、明示 targets、全新 env 及從任意 cwd 啟動；非法依賴被拒 | import/resource缺失則停；回上批 source commit，不碰 live |
| C3 | Provider 雙 consumer、project config；不改預設研究行為 | Writer+Background golden integration、credential redaction、override、兩 folder 隔離與 init/check | output/model/usage/precedence 改變即停；恢復前一包 |
| C4 | release/update/receipts；原 manifest 是唯一權威 | maintain status/plan/apply/resume/verify/rollback；部分完成及 stale plan 拒絕；原command不重派 | uncertain先lookup；保留原版本/state；禁止未相容回退 |
| C5 | Windows/Linux clean install/native及跨元件隔離演練 | 平台閉包、MCP contracts、cancel、restart/ACK/lease/CAS、局部失敗及回退結果；第二synthetic project | 已知 fail 未處理不宣稱全面通過；缺平台則該target不能發布 |
| C6 | CI、effective AGENTS、文檔/發布輸入 | privacy/contract/boundary/build/required tests gates、單一最新入口、版本一致 | 未實作 gate 不寫已過；runtime邏輯不藏於生成包 |
| F | Architecture Freeze | 所有本次 current consumers/支持targets通過，布局/owner/公開契約/生成權威/hash鎖定，剩餘legacy與擴展明示 | 未達 F 僅候選 release，不能宣稱結構已驗收定版 |
| D | 可選現用切換 | 在另有部署授權時核對全部bindings/session/runtime/state並驗證回退 | 任一target未完成保留partial；不覆蓋state；舊活動命令留原execution release |

C2–C6 的建議命令規格為 `uv sync --locked`、`uv build`、`pnpm install --frozen-lockfile`、成員 `build/test`、schema regeneration/diff、boundary check。這些要在 metadata/locks 建好後成為實際命令；當前不能照這份文檔假稱已執行。

CI cache 僅用於已宣告輸入的確定性構建/合成測試。API/SSH/live health/state migration/研究command不cache。

必測失敗情境：bad artifact hash；未知peer；wrong project/workplace；stale release plan；native ABI錯；中斷後resume；某端更新另一端失敗；session仍舊；command submission uncertain；cancel在PID登記前；expired lease；CAS mismatch；daemon/manager/background restart；ACK後原問題仍未解；回退不能讀新state。各測試歸屬C4或C5，不重複虛增工時。

現存10個失敗分類：cancel 1項為命令啟動與立即取消的待定位競態，C5重現並修復或證明語義後驗證；remote_status_worker_agnostic 3項為缺舊bootstrap/conda配置；continuation_three_agent_flow 6項為缺被排除的workspace policy/config。後9項在C1界定支持範圍，再以合成配置修正測試假設；若非支持入口，保留明示legacy-suite及證據，不能抹除失敗或導入live設定。移出required suite須有支持範圍理由，若入口仍支持就必須修到通過。

## 7. 剩餘工程預算

以下是在來源獨立/GitHub 已完成後的規劃人日（每人日6–8工作小時），不包含未來新擴展，非 Codex 日曆時間承諾。

| 預算 | 工作 | 人日 |
| --- | --- | --- |
| A 正規化與可交付構建 | C1來源/契約3–4；C2包/根路徑/resources3–5；C3provider/config雙consumer2–4；C6 CI/docs1–2 | 9–15 |
| B 發布與隔離驗收 | C4release/update/receipts3–5；C5平台fresh install/native2–4；跨元件失敗演練2–3 | 7–12 |
| A+B 基礎 | 不切換現用系統 | 16–27 |
| A+B 加約25%餘量 | 候選交付與Architecture Freeze | 約20–34 |
| D 可選現用切換 | 全部bindings/活躍session/state接回 | 2–4 |
| 含D的總額與餘量 | 基礎18–31；加約25% | 約23–39 |

單模組驗證計入其工作包；C5只計平台及跨元件演練，不再全數重算C3golden或C4rollback單元檢查。每批結束以實際證據修正後續估算。

前提：state format無破壞性遷移；XATS固定入口/native可構建；10項fail不需廣泛產品重寫。任一前提失效另列必要工程和預算，不能只靠餘量或把它包成普通小修。私人版本管理不要求公共產品化、多OS長期支持或新的agent框架。

## 8. 定版後更新規則

F之後固定頂層布局、模組owner、公開入口與來源權威。普通修復只改受影響模組及必要consumer；禁止附帶改namespace/目錄/框架、全面升依賴或增加人工副本。

PATCH維持既有契約；MINOR新增能力需實際consumer相容測試；破壞性行為明示MAJOR或協議轉換。strict schema新增optional字段也可能使旧receiver拒绝，不能機械當MINOR安全。改模組責任/依賴方向是ADR例外，需說明為何現有擴展點不足、對消費端與遷移成本的影響。

每次更新回到唯一repo來源，生成payload和manifest，逐端核對磁碟hash及實際載入release。維護入口 `python deployment/maintain.py status|plan|apply|resume|verify|rollback` 待C4實作，沿既有IDs/receipts，不另建部署資料庫。舊版本在無活動引用且回退已驗證前保留，保留期限於C4按容量/長任務需求定值。

本專案 Worker/Skill 流程改變正式採用前交使用者審查；已授權的普通修復不逐次詢問。專案經驗沿本專案既有紀錄去重、保留適用條件，不自動變成強制規則或提升全域 Codex/個人 Skill/其他 repo；全域推廣須另有明確獨立任務，不屬此正規化工程。

## 9. 文件入口

- [擴展需求與證據台帳](EXTENSION_REQUIREMENTS.md)：已提出的能力、現況、固定接點及後延事項。
- [契約、相容與發布](CONTRACTS_AND_RELEASE.md)：authority、directional matrix及activation/rollback。
- [架構決策](ADR-001-STABLE-BOUNDARIES.md)：穩定結構及例外規則。
- [跨聊天討論決策](DISCUSSION_DECISIONS.md)：完整回讀後採納、修正與分歧。
- [mapping摘要](migration-summary.json)、[逐檔JSON](migration-map.json)、[CSV](migration-map.csv)、[import證據](dependency-evidence.json)。

混合provider/core檔案的primary map之外，具體symbol歸屬見[split-decisions.json](split-decisions.json)。每個symbol只保留一個編輯authority；生成兼容入口不另手寫業務。

立即生效的工程維護約束見[根 AGENTS](../../../AGENTS.md)及[維護準則](MAINTENANCE_RULES.md)。R1–R8、按影響驗證、完成/發布阻塞、WIP保存、交接與架構例外在同一入口維護；本規則不新增 Worker 研究政策、不擴張施工/部署授權。Design Ready 驗收也需核對維護文件一致性及八條根規則。

專案經驗的資料歸屬：工程維護經驗留在 Agent Relay；研究/Worker 經驗留在產生它的研究 project，不匯入共用工具 repo。共用軟體發布只同步工程來源/產物，不夾帶專案經驗、研究流程或把它們變成其他 project 的預設規則。
