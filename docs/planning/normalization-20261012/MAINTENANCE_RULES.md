# Agent Relay 維護準則

最新施工授權：[FIRST_RESTRUCTURE_EXECUTION](FIRST_RESTRUCTURE_EXECUTION.md)。使用者已要求新chat開始P1，首次可集中重整框架，不受小步修改/必保留舊物理目錄限制；定版後才轉為漸進維護。下文只規劃/保留主要目錄的先前限制由此授權取代；第一期四情境、原契約/state、現用保護及後期按需範圍不變。
日期：2026-10-12。規範狀態：本次已授權的工程維護約束；配合根 [AGENTS.md](../../../AGENTS.md) 立即生效。工程狀態：獨立基線，設計可交付，正規化尚未施工。這不是新的 Worker 研究政策。系統/使用者指令優先；其他聊天的審查意見是證據，不能擴張授權。

## 0. 專案經驗與全域規則的邊界

這是 Agent Relay 專案內的維護方法，範圍到本 repo。專案經驗是問題、條件、修復、證據及反例的紀錄，不是全域修改指令。沿本專案既有任務/案例紀錄去重；已有足夠記錄就引用，有明確遺忘代價才補，同類補原案例，不要求每次任務都建立記錄。

MUST NOT 自動複製到全域 Codex/使用者 AGENTS、個人 Skill、memory/settings 或其他研究 repo；MUST NOT 因一次成功就變成任何強制政策。本專案流程正式採用仍依使用者審查；若使用者未來另行明確要求全域推廣，那是獨立任務，需另核對適用條件與受影響範圍，不能由此維護規則推導。工程 gate 是本 repo 已授權約束，專案經驗本身不會自動新增 gate。

即使被其他專案引用，此規則也不自動取得跨專案效力。專案文件不能替自己授予全域配置/安裝插件/其他 repo 的權限；讀取 bindings 不等於修改、覆寫、同步或推廣的授權。

工程維護經驗留在 Agent Relay；研究/Worker 經驗留在產生它的研究 project，不匯入共用工具 repo。共用軟體發布只同步工程來源/產物，不夾帶專案經驗、研究流程或把它們變成其他 project 的預設規則。

## 第一期間的最新範圍

[分期交付](PHASED_DELIVERY.md)與[ADR-002](ADR-002-PHASED-DELIVERY.md)是最新施工範圍。第一期S1–S5，四種Windows client×Linux Worker情境M1新/舊、M2新/新、M3舊/新、M4舊/舊更新必驗。保留現有主要目錄；full-layout map、C1–C6和F只作完整候選/技術背景，不使全部後期工作自動成為P1門檻。現在僅文件，工程及現用切換未執行。

## 1. 權威與階段

每次工作先核對 `git status --short`、`git rev-parse HEAD`、實際檔案、[project.json](../../../project.json)及[驗證回執](DESIGN_VERIFICATION.md)。規則入口為根 AGENTS；本文件展開維護程序；[PHASED_DELIVERY](PHASED_DELIVERY.md)負責當前範圍/觸發；[TASKBOOK](TASKBOOK.md)保留主索引與完整候選背景。歷史文件保留來源，不作最新實作證據。

- Extracted：可追溯的獨立來源。現有檔案才是編輯來源。
- Design Ready：來源/模組/交付包、契約、估算及 gates 已成文件。不能稱 package/CI/updater 已運作。
- P1 Verified：S1–S5及聲明targets的M1–M4實際通過，来源布局/owner/公開入口固定；不等於後期updater或完整設計F已完成。
- Engineering Verified（舊完整F）：僅有明確完整scope且實際通過時才可宣稱；不作P1前提。
- Live activated：另有切換授權、全部必要 bindings/session/state 已核對。工程驗收不自動等於現用切換。

沒有證據不可提升階段；缺工具標 NOT AVAILABLE，未跑標 NOT RUN，不以計劃或其他 chat 的文字代替測試。模組內未來 AGENTS 可以補實際命令，不能削弱根規則或自行採用共同研究政策。

## 2. 修改前與最小完整修復

先定位問題觸發條件、source owner、直接 consumer 及實際授權，再選最小完整行為修復。不能為少改檔案把修復塞到錯誤層、加無必要 wrapper，或在插件 cache/dist 修一次留下兩個 authority。必要的 consumer 更新不算過度修改。

當前權威：[source-map](../../extraction/source-map.json)；第一期歸屬見分期第4節；[migration map](migration-map.json)和[symbol split](split-decisions.json)是舊完整候選分析，非P1搬檔指令，也不表示941檔都進runtime payload。原 source-map 是歷史 provenance，不可重寫原 hash 掩蓋後續修改。

生成檔只能改 authority 後重生，留下工具版本/命令/diff；本設計 map 由 `build-plan-inventory.py` 產生，不能只手改 JSON/CSV。現有 Pydantic/Writer Zod 是各自原契約權威；未來 schemas/generated 和 schema-bindings 是衍生物。新增 schema ID 的 authority 在契約清單登記；不要再建第三份模型。

Vendor 原碼與我方 adapter 分清。XATS 固定 0.8.6、唯一 vendor-port 內部入口是預定受控例外；Paperclip parser 是我方衍生適配，不能當未修改 upstream。上游升級保留 revision/license/provenance/patch、核對建構和真實 consumer，不夾在普通修復中全面更新。

禁止為了讓当前 patch 通過而附帶削弱根 AGENTS、維護 gate 或測試要求。修改工程規則本身須屬明確授權的任務，記錄必要性與影響；不得靠自改規則繞過原授權。

## 3. 變更分類與授權

| 分類 | 必要證據 | 推進規則 |
| --- | --- | --- |
| Ordinary fix | 觸發問題、owner/直接 consumer、修復與相關驗證 | 已授權範圍可自主完成；不附帶搬結構或改研究方法 |
| Compatible extension | 真實 consumer、契約差異、收發方向、defaults/errors/profile 的行為對照 | 不能只因 optional 或版本號稱相容；在授權的擴展範圍施工 |
| Breaking/boundary change | 必要性、現有擴展點不足的原因、替代方案、consumer/state 影響、遷移/回退、gates、成本 | 先分析；已有具體授權才施工。沒有授權就提出可審查決策，不讓 ADR 自動批准 |

P1 Verified後固定實際來源布局、owner、namespace、公用入口、依賴方向及來源權威；後期按需增量加真實模組，不為採用apps/packages再全面搬遷。PATCH 保留行為契約，MINOR 需證實擴展相容；破壞性改變明示版本/協議及遷移。版本標籤不是相容證明。P1前也不能在普通修復中暗改當前分期範圍或原契約。

已授權整個隔離工程時，批次 gate 是自主驗證、停止和回復條件，不是每批請示。改目標/核心架構/研究比較方法、正式採用本專案 Worker 或 Skill 流程、額外資源承諾、未授權 live cutover，才需使用者決策。保存去重專案經驗不構成正式政策批准，不提升為全域規則。

## 4. 按影響選驗證

| 變更面 | 必要驗證；由變更者選具體現有命令/fixtures |
| --- | --- |
| 文件/工程規則 | 本地連結、相互一致、階段/授權無誤、privacy；本設計階段 source hash 保全，不為此寫鏡像測試 |
| 模組修復 | 原觸發的行為與直接 consumer 回歸；需要時新增能攔住原缺陷的測試 |
| 公開 input/output/default/error/model profile | 舊 sender→新 receiver、新 sender→舊 receiver 及實際 payload；strict optional 亦測；必要的工具面 fingerprint |
| Provider/config | Writer 與 Background 各自 golden integration、原 override/preset/env 優先序、secret-safe logs；不靠 provider 單測假稱雙 consumer 通過 |
| 身份/狀態/執行 | wrong workplace、identity、command lookup、不確定提交、cancel、restart、receipt/ACK、CAS/lease、state backward-read 及 rollback 邊界 |
| 包/資源/lock/vendor | 從任意 cwd 啟動、全新隔離環境、package resources、實際入口/exports、支持平台/native ABI、構建和 shutdown |
| updater/release | artifact/contract exact tuple、partial/resume、stale plan、實際 loaded release/session/bindings、故障後回退、未支持 target 阻塞 |

依影響擴測；無新變更、失敗或未解風險不反覆全 repo 重跑。需要的測試不存在時建立有意義的測試，不以空 fixture/降低 assertion/無理由 skip 造通過。實作與測試不能互抄而只證明同一錯誤。

現有 failure baseline 在[原驗證紀錄](../../extraction/VERIFICATION.md)：cancel 1 項待定位競態；remote status 3 項舊 bootstrap/conda 配置；continuation 6 項排除的 workspace policy/config。這是先前結果，不是本次重測。修復觸及時由該 owner 重現並定位；不相關可揭露保留。支持入口的失敗必須解決；非支持分類須有入口/範圍證據，保留 legacy 結果，不刪 suite 或偷偷借現用配置。不得把所有失敗都稱「抽離造成」而永久豁免。

## 5. 完成、提交與發布的 gates

此處完成 gate 約束宣稱完成、合併及正式交付。隔離分支 MAY 保存明示 WIP/失敗證據，但不得把未驗證能力交付為完成。不能以寫 ADR 或改測試掩蓋超出授權的改動。

| 條件 | 完成/合併/正式交付 | Release/activation |
| --- | --- | --- |
| 超授權、非 authority 永久修補、未授權 boundary/state 改變 | 阻塞 | 阻塞 |
| 受影響測試新失敗、沒有處置，或相容性假宣稱 | 阻塞完成；允許明示 WIP | 阻塞相關 target |
| 無關且明示的既有 legacy failure | 可交付本次相關改動，揭露範圍 | 依支持 targets 的必要 gates；仍支持的失敗不得豁免 |
| artifact/lock/vendor provenance 不可追溯 | 依修改性質；文件不可假稱已鎖定 | 阻塞 release |
| 必要平台/consumer/state/rollback 未驗 | 不阻塞誠實的文件/開發提交 | 阻塞該正式 target；不能說全平台完成 |
| 未授權 live cutover | 不阻塞隔離設計/工程 | 阻塞現用切換 |
| 發布含 credential/真實 host/path/研究/raw state | 阻塞；清理候選與必要歷史 | 阻塞 |

失敗可在授權範圍修復後重試；發現設計前提失效、破壞性 state migration、超預算承諾或無安全回退時停止相關步驟，保留原 ID/hash 和最小證據，提出決策。不要盲重派研究任務或覆寫現場。

## 6. 更新不變式與目前能力

詳見[契約與發布](CONTRACTS_AND_RELEASE.md)。protocol 0.6 `verify_pair` 仍要求 protocol_version/release_id/payload_sha256/mcp_schema_sha256 一致及 workplace 核對。System release compatibility 不能放寬此檢查。Strict receiver 不靠忽略 unknown fields 混用新舊 payload；只允許實際驗過的 exact component/contract/tool/state/platform tuples。

來源→builder→manifest→artifact hash→部署→實際載入形成唯一追溯鏈；不要另建部署 DB 或第二套 receipt 系統。變更 lock 及 ABI 需有原因、精確版本/diff及相應平台驗證；P1在S2/S3鎖定並驗證支持targets的實際component依賴；root uv/pnpm workspace不強制先建，不能偽稱已有frozen-lock gate。

啟用前核對必要權限與 state ownership。未知 peer 可安全唯讀診斷，副作用阻塞；command uncertainty 先查原 ID。Partial 更新明示哪端成功/未啟用，resume 沿原 plan/receipts；回退不能覆寫不可讀的新 state。舊活動命令留原 execution release，不以回退重派。

| enforcement | 本階段狀態 |
| --- | --- |
| 根 AGENTS、owner/授權核對、scope/diff/驗證/交接規則 | 立即由人及 agent 遵守；不依賴新增 CI |
| extraction privacy/source verifier、design map checker | 現有可執行檢查；不等於正式工程 gates |
| P1必要source/privacy/入口檢查、component locks、兩端安裝及四情境fixtures | S1–S5 planned；目前沒有新的工程CI保證 |
| 完整boundary lint/schema-bindings/通用release compatibility平台 | 按實際consumer在P2/P3建立，不作P1全選前提 |
| `deployment/maintain.py status/plan/apply/resume/verify/rollback` | P3候選，須實際多端更新問題觸發；P1用既有helpers/明確操作驗證M4 |
| P1 Verified及現用切換 | 均未達；四情境驗收與live授權分開；舊完整F不作P1前提 |

当前設計包檢查命令：

```text
python docs/planning/normalization-20261012/build-plan-inventory.py --check
python scripts/extraction/verify-source.py
python scripts/extraction/check-publication.py
```

在本設計階段，它們必須保全 runtime/source；施工之後以原 extraction provenance 加新 release provenance 記變更，不改歷史 hash 讓 verifier 假綠。打包用 `scripts/extraction/package-source.py` 的 Git 候選流程，不 archive 含依賴、私有回執和 live state 的整個資料夾。

## 7. 交接格式與額外擴展

交接記錄放任務/PR 的適當位置，不為每次修復另造一套經驗庫。公開 repo 只留可發布摘要；詳細 chat IDs、私人 origin 和 raw receipts 留排除發布的本地回執。最小交接格式：

```text
目標/授權範圍：
repo 階段/source commit：
變更分類/owner/直接 consumer：
已改內容及保留的不變式：
已驗證（命令、結果、必要 receipt/hash）：
NOT RUN / NOT AVAILABLE 與原因：
baseline/new failures 與處置：
下一步/停止條件/需要的決策：
必要來源、活動任務原 ID（僅必要且已獲讀取授權）：
```

看[擴展需求台帳](EXTENSION_REQUIREMENTS.md)的真實 consumer 和 authority，再在既定位置新增。UI/evidence review、DSH/backend、context routing 等候選先記需求，不建空 module、平行研究 DB、第二個聊天介面或新治理 agent。額外資料來源不悄悄擴張目前 Background「本地整理」驗收範圍。

研究證據的 observation/evidence/hypothesis/claim 決策仍沿既有來源；工程 exit、review acceptance 與 scientific support 分清。ACK 不代表原問題已解決。工程維護規則不能自行改使用者原研究比較、方法或共同流程。
