# 獨立系統 project、成熟工具與同步升級方案

**最新執行順序（2026-10-11）：** 依[獨立組裝任務書](PROJECT_EXTRACTION_TASKBOOK_20261011.md)，先獨立來源及構建、上傳私人 GitHub 基線，再正式正規化並於最後切換現用系統。本文件保留工具選型與目標設計，不要求首次 push 前完成全部結構改造。

研究日期：2026-10-10（Asia/Taipei）。承接實際盤點 (local provenance omitted)與hash 證據 (local provenance omitted)。本輪查官方文件、核對少量本地入口，只新增／更新方案文件；未安裝框架、移動程式、修改運行配置、提交、push 或部署。

2026-10-11 補充：[結構定版建議與工程量](STRUCTURE_AND_ENGINEERING_ESTIMATE_20261011.md)具體列出目標目錄、各 runtime、構建工具、公開接口、部署元件及分階段人日估算。以該文件為較新的設計建議；本文件保留選型依據。

標記：**資料確認**是官方文件能力；**盤點確認**是本地檔案證據；**推薦／推論**是我們的設計；**待驗證**是在接受設計後需要完成的實測。成熟工具的文件能力與本系統已整合能力分開記錄。

## 建議決策

**推薦：將整套共用系統做成獨立 project，採單 repo 的模組化架構；以 Nx 管理開發任務／依賴圖，uv 管 Python，pnpm workspace 管 Node，沿用 Harness 的部署與恢復契約。** 在本次標準化中完成來源收斂、正式邊界及升級流程，日後 Codex 在這些接口內演進。

獨立 project 包含共用 core、Harness/MCP、local manager、remote worker、lifecycle/router、部署腳本、report-writer/provider、Token Guard、背景整理及 vault adapter。研究專案保存研究配置、原文、筆記、產物、SQLite 進度和 workplace identity；通用程式的維護來源不再放在研究 tools/experiments。研究實驗專用腳本仍歸研究專案。

現有 `C:/Users/example-user/Desktop/ai-tools/solution-agent` 已位於研究 folder 外，可作為收斂後的獨立 project 並保留 Git 歷史。是否另命名／另建乾淨 sibling checkout 是遷移選項，不影響架構。不能僅改名或新增 folder 就宣稱解耦完成：目前 launcher、venv、router build 和 provider 還有跨研究目錄依賴。

## 成熟方案比較

| 方案 | 官方文件確認的能力 | 對我們的適配與限制 | 結論 |
| --- | --- | --- | --- |
| Nx + uv + pnpm | Nx 以 project graph 組織任意語言的命令，官方展示 Python + uv；提供 affected tasks、release groups、version plans 和工具 migrations。[多語言](https://nx.dev/docs/features/multi-language-support)、[發布分組](https://nx.dev/docs/guides/nx-release/release-groups)、[版本計畫](https://nx.dev/docs/guides/nx-release/file-based-versioning-version-plans) | 能接既有 Python/TS/PS1/C# builder，不必改寫 Harness runtime。Python graph 邊、鎖檔輸入與發布 adapter 必須明確接入；Nx 不自動理解本系統身份、SQLite、SSH 部署及 worker 恢復 | **推薦／推論：首選**。正式採用前做下方隔離驗證，鎖定相容版本 |
| uv + pnpm + 既有腳本 | uv 管 Python metadata/lock；pnpm 管 Node workspace 及本地 package 依賴。[uv](https://docs.astral.sh/uv/concepts/projects/workspaces/)、[pnpm](https://pnpm.io/workspaces) | 依賴管理完整，但跨語言影響分析、統一任務圖及發布協調需由我們的腳本補足。若 Nx 驗證不合適，仍可保持相同模組邊界 | **推論：可行備選**。降低工具依賴，維護流程的自訂工作較多 |
| Turborepo + pnpm + uv | 建立在 JavaScript package-manager workspace 上，按其結構组织多包。[官方結構文件](https://turborepo.dev/docs/crafting-your-repository/structuring-a-repository) | 可用 package scripts 包裝 Python，但文件模型以 JS workspace 為中心；本系統跨語言接口、schema 與部署需另接，不能據此宣稱 Python 原生發布已解決 | **推論：次選**。我們需要的整體 graph／演進治理較適合先評估 Nx |
| Pants | Python resolve/lockfile 鎖 transitive 版本和 artifact hashes；JavaScript backend 文件仍使用 experimental namespace。[Python locks](https://www.pantsbuild.org/stable/docs/python/overview/lockfiles)、[JS backend](https://www.pantsbuild.org/dev/docs/javascript/overview/enabling-javascript-support) | 官方 Windows 說明使用 WSL，且 project 應位於 Linux VM；我們的 Windows hook、原生 SQLite 和 GUI launcher 有額外銜接成本。[平台前提](https://www.pantsbuild.org/stable/docs/getting-started/prerequisites) | **推論：本次不優先**。不為套用 build system 改變現用 Windows 運行方式 |

以上是文件與實際依賴的比較，未做工具性能 benchmark，也未在本機安裝 Nx/Pants/uv。**盤點確認：** 本輪 PATH 可找到 Node 與 pnpm，未找到 uv；C 未找到 nx.json、pnpm-workspace.yaml、pyproject.toml、uv.lock、turbo.json 或 pants.toml。推薦不等於已完成導入。

## 工具各管哪一層

- **推薦：Nx 只管開發工作。** 每模組固定 `lint`、`test`、`contract-test`、`build`、`package` 等 target，明示依賴、inputs、outputs。Python/Node 各用自己的工具。Nx 的開發任務不是 Harness 的研究 task；不替換 command ID、capsule、lease、CAS、研究排程或等待流程。自訂命令是官方支持的接法。[Nx command targets](https://nx.dev/docs/kb/run-commands-executor)
- **推薦：uv 鎖定共用 Python 工具鏈。** 以 pyproject.toml 描述正式 package／CLI、uv.lock 保存依賴；CI/構建使用 locked 模式。不要對現用 venv 直接執行同步來驗證：uv sync 預設會移除未在 lock 的 packages，先在新隔離環境驗證。[uv lock/sync](https://docs.astral.sh/uv/concepts/projects/sync/)
- **推薦：pnpm workspace 管 Node 模組。** report-provider、writer、background、router 用顯式 package 依賴及 workspace protocol；不再 import sibling node_modules/.pnpm 的具體路徑。其 protocol 限制開發時解析為本地 workspace package；出包後依賴仍需按具體版本安裝／打包，不能把 symlink 當交付。[pnpm workspace](https://pnpm.io/workspaces)
- **推薦：框架升級與系統升級分開。** nx migrate 更新它支持的工具／插件 metadata、配置與程式 migration；我們仍需審 diff、測試並锁定結果。它不遷移 Harness 工作資料或將 plugin 更新到遠端。[Nx migrations](https://nx.dev/docs/features/automate-updating-dependencies)

**資料確認：** uv workspace 共用一份 lock；不同成員需要衝突依賴／各自 venv 時，官方建議獨立 project + path dependency。**推薦：** 不把研究的 Conda/GPU 環境加入系統 uv workspace。Python 的 core/Harness 可先維持一個可安裝 distribution、內部有強制模組邊界；Token Guard 是否為 workspace member 取決於打包與相容性，不為目錄好看拆出彼此難以獨立安裝的 Python packages。[uv workspace 適用範圍](https://docs.astral.sh/uv/concepts/projects/workspaces/)

**推薦：缓存只適用於輸入完整宣告的確定性構建／隔離測試。** API、live health、SSH install、服務 Start/Stop、研究派工、狀態 migration 的 target 不可因缓存跳過或回放，部署不使用 affected 推導省略相容性核對。初期本地 cache 僅包含程式及合成 fixture；研究正文、憑證和正式 runtime state 不作 cache inputs/outputs。背景服務運行不依賴 Nx daemon 或開發 checkout。

## 能被檢查的模組架構

以下是**推薦／擬議正式邊界**，不是現有目錄的搬移回執。部署元件與來源模組不必一對一；目前相互耦合的 Harness Python 模組可以同包發布。

| 來源模組 | 責任與公開接口 | 允許的依賴／擴充位置 |
| --- | --- | --- |
| contracts | 身份／artifact／receipt／配置 schema 與 protocol versions | 不依賴研究專案、provider、router 或服務 runtime；通用 schema 有一份權威 |
| core + Harness runtime | capsule、lease/CAS、OS command supervision、worker、恢復、MCP | 依賴 contracts；SSH/worker/report adapter 經明確接口注入；繼續維護原命令語義 |
| lifecycle-router | manager、publication/help routing、App/XATS adapters、SessionStart | 透過 Harness client/CLI/RPC 契約，不 import 遠端 runtime 的私有實作；XATS vendor/provenance 明示 |
| report-provider | 固定 DS profile、credential loading、structured request helper、安全 usage receipt | 不依賴 writer 的正文 schema、background queue 或研究筆記；writer/background 都引用此維護來源 |
| report-writer | 已核准 evidence packet 的報告輸出與 claims schema | 使用 provider；保留獨立 draftWithProvider/API adapter 邊界 |
| research-background | 穩定補查、SQLite jobs、DS 摘要、PROPOSED 輸出、Windows lifecycle | 使用 provider、摘要/delivery schema；vault 根由 project config 指定；不擁有科學完成判斷 |
| Token Guard | command stdout/stderr、hash receipt、bounded excerpt、stage ledger | 自己的正式入口与 receipt 契約；Harness adapter 消費它，避免反向 import 全套 core |
| research/vault adapters | 路徑與資料格式映射、只讀比較、已授權交付、Obsidian 導覽模板 | 原文權威在研究專案；不用研究名稱／個人路徑實現通用邏輯，不新增遠端傳輸系統 |
| deployment | source/release manifest、打包、既有 install/overlay/rollback helpers | 描述各部署元件，不持有研究 queue；project/workplace 選擇與計画核對沿既有規則 |

**推薦：把邊界放進檢查，不只寫 AGENTS.md。** TS/JS 使用 Nx ESLint boundary 規則和 package exports；Python 使用 Import Linter 的 forbidden/layers contracts，為跨語言 schema/provider 增加 producer-consumer 測試。Nx 的全語言 conformance 是 Enterprise 功能，不能把它說成免费已涵盖 Python；Import Linter 是另一個成熟選項。[Nx 邊界能力／限制](https://nx.dev/docs/features/enforce-module-boundaries)、[Import Linter forbidden](https://import-linter.readthedocs.io/en/stable/contract_types/forbidden/)、[layers](https://import-linter.readthedocs.io/en/stable/contract_types/layers/)

**推薦：擴充接口預先定好。** 增加 provider、worker backend、report format 或 source adapter 應新增實作並透過明確註冊／配置接入；契約宣告 api_version、輸入／輸出、錯誤與相容範圍。需要第三方 Python package 動態發現時採標準 entry points；固定內建 adapter 直接註冊即可，不引入新的插件總線。這是 Python package 的擴充接口，和 Codex personal plugin 的安裝机制分開。[Python plugin discovery](https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/)

## 來源、安裝與研究 folder 的三個位置

**推薦：三者分開管理。** 以下是邏輯位置，實際本機／遠端 root 由 profile 宣告；不先決定要移走現用 SQLite。

```text
獨立 solution-agent project（Git 維護來源）
  Python package／Node packages／plugin payload source
  contracts／tests／build/deploy scripts／locks／AGENTS／docs

本機或遠端受管安裝位置
  releases/<release-id>/...     # 版本化程式與依賴，按平台構建
  受管 launcher／入口 binding  # 固定入口解析已驗證 release
  installation receipts       # 記實際 hash／配置綁定

研究 folder（一 folder 一 project）
  project descriptor／local overrides／release lock
  research-vault／研究程式／資料／產物
  .solution-agent 的身份、SQLite、queue、progress、回執
```

研究專案從固定入口使用 release，升級時更新 binding／lock，不複製 source。來源可以在開發 project 修正，打包後更新兩端。研究狀態與原文不因 source checkout/pull 改變。

**盤點確認：** 現有 personal plugin 是每主機共用入口，且 launcher 取 workplace profile；不能假設每個研究 folder 能同時任意安裝不同版本的全域 plugin。**推薦：** 共用 client 支持宣告的 protocol/runtime 相容範圍，project lock 指定所需 runtime／adapter 組合；升級全域 client 前檢查該主機全部受影響 bindings。如果接口無法共存，協調一次版本切換，拒絕不相容綁定，不能默默修改其他研究專案的鎖。既有活躍 App session 的 plugin reload 也須列為啟用條件，不把 source 更新當熱更新成功。

## 同步更新的完整流程

**推薦：同步的單位是一份經過驗證的系統 release manifest，包含各 component 的確切版本／hash／相容要求。** core/Harness client/worker/router 若有必須一起變更的契約，按相容組發布；writer/guard 可独立版本，但系統 release 記錄測過的組合。Nx 支持 fixed/independent release groups；Python metadata、Codex plugin payload、跨語言版本鎖仍須接 release adapter，不能宣稱 Nx 原生發布任意 ZIP 就完成了這一層。[Nx release groups](https://nx.dev/docs/guides/nx-release/release-groups)

1. **來源修改與版本計畫。** 在獨立 project 修改唯一來源；版本計畫列受影響模組、API/schema 是否變動、必要 migration。Nx 的 version plan 可保存具體升版意圖，通過審查後消费；它是開發發布資料，不是研究任務隊列。[Nx version plans](https://nx.dev/docs/guides/nx-release/file-based-versioning-version-plans)
2. **構建及集成檢查。** 以鎖定工具／依賴構建各平台包，跑受影響檢查及整個相容組的集成檢查。release adapter 核對 Python metadata、Node metadata、plugin version、來源 commit、payload SHA、完整 MCP/schema hash、資料 schema 範圍与 migration IDs；metadata 不一致不能发布。
3. **發布共用交付。** 私人 GitHub 的 tag/release 附 package、manifest、changelog 和去識別化驗收資料。原始研究資料與 runtime DB 不在 release。GitHub Release 支持 tag 對應版本與下載附件，但上傳附件不等於部署。[GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
4. **更新計畫。** 由一個維護入口列出明確 targets，讀其 installed manifest/project locks，沿現有 deployment helper 建 dry-run：預期舊 SHA、新 SHA、衝突、依賴、活動工作与需要的 migration。聚合結果只引用既有 deployment IDs／receipts；不新增競爭任務或 transport。
5. **逐端受管安裝與啟用。** 共用同一份已驗證 manifest，依原 helper 分別套用 Windows client/manager、Linux runtime/worker，以及需要的背景服务。預先 staging 不代表已啟用；保持活躍命令的執行 release/provenance，查原 command ID 接回。profile 綁定不變，不能為更新重派未知工作。更新操作的持久進度引用既有回執，中斷後先查每端 hash 再繼續。
6. **一致性結果與回退。** 顯示每端 desired/installed release、hash/schema check、activation/health 及待更新 session。任一端失敗報部分完成，不宣稱跨兩台主機原子更新。回退沿既有反向 dry-run；若曾遷移資料，要先確認 schema 向後相容或使用已驗證恢復程序，不直接還原活躍 SQLite。

這套流程應將「同一組版本、一次發起更新計畫、可逐端接續」做成常規功能。自動偵測新版本／無人值守升級的頻率與授權另定；本輪沒有新增 monitor、scheduler 或自動部署。便利性以明確可核對的更新入口實現，不以背景 `git pull` 更新正在運行的程式。

## 框架落地的驗證關卡

**待驗證：在隔離開發環境完成，下列是選型驗證，不是補件後就當整套標準化完成。** 不修改現用 plugin、venv、研究 DB 或 OS 排程。

| 關卡 | 完成證據／不合適時的判斷 |
| --- | --- |
| Nx + Python/Node/C# 開發任務 | 現用 Python module、router、provider→background、Windows launcher 可從宣告 target 構建；沒有研究路徑直連；不引入 WSL 執行依賴 |
| graph／影響分析 | 改 provider helper 會納入 writer/background consumer 檢查；改 schema 會納入雙語 consumer；跨語言 dependency edges 明確，不能漏測 |
| dependency locks／原生模組 | 隔離環境以正式 Python 版本、Node 24、pnpm/uv locks 重建；Windows/Linux 的 native SQLite 路徑與 ABI 經驗證；研究 Conda/GPU 環境不混入 |
| 邊界守護 | 非法跨模組 import、私有 package export 及無宣告 vendor 路徑被檢查拒絕；正常 extension/adapter 可以增加而不改核心目錄 |
| 統一發布 | 一份版本計畫產生一致 Python/Node/plugin metadata、系統 manifest與包；只 bump 需要的相容組；release 不需公開 npm/PyPI |
| 更新／恢復演練 | 合成 state 的雙 target 更新可 dry-run、檢查衝突、模擬部分失败、中斷接續及回退；不重派研究、不重建 workplace identity |

如果 Nx 發布 adapter 的複雜度超出收益，保留已設計的 packages／schemas／uv/pnpm，發布層使用受測試的现有 builder與manifest流程。這是選型階段的明確退出條件；正式標準化驗收後不在普通 bug fix 反覆換框架。

下一步應產出正式模組圖、目錄/入口清單、schema/API 契約、release/更新設計與逐檔遷移清單，完成隔離驗證後定版；再按完整標準化驗收條件遷移。所有需要一次結構重整的工作在這個阶段處理，之後一般功能/修復受已建立的邊界約束。
