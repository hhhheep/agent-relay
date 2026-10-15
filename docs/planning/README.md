# 正規化計劃入口

當前範圍：[第一期與按需分期](normalization-20261012/PHASED_DELIVERY.md)，以整理、Windows×Linux新/舊四組合、舊/舊更新及真實擴展基礎為準。[ADR-002](normalization-20261012/ADR-002-PHASED-DELIVERY.md)已取代全面搬目錄/一次做完全部工程的預設。

主文件入口：[2026-10-12 Agent Relay 正規化施工任務書](normalization-20261012/TASKBOOK.md)。已完成四輪指定聊天討論，配套來源/模組/交付包映射、擴展需求、契約/相容、維護準則、根AGENTS、ADR及工程預算。

| 歷史文件 | 目前定位 |
| --- | --- |
| [框架與獨立project研究](FRAMEWORK_AND_STANDALONE_PROJECT_20261010.md) | 保留選型背景；Nx必選等建議由10/12決策取代 |
| [結構與初估](STRUCTURE_AND_ENGINEERING_ESTIMATE_20261011.md) | 保留初盤點；目標樹/工程量以10/12為準 |
| [更新維護原則](UPDATE_AND_MAINTENANCE_POLICY_20261011.md) | 原則沿用，最新版補directional compatibility、工程自治、preset/state責任 |
| [獨立組裝任務書](PROJECT_EXTRACTION_TASKBOOK_20261011.md) | A獨立、B私人GitHub已完成；C/D由最新分批任務書接續 |

歷史檔不改寫，以保留source-map與原決策追溯。第一期已形成工程候選版，共用Provider、依賴重建與打包已有實作。Linux Worker隔離安裝及M1–M3的Harness檢查已完成，M4隔離Harness更新回退亦已通過；完整四情境、後期updater與正式切換仍未完成。最新實際結果見[工程狀態](../engineering/phase-one/STATUS.md)及[workplace驗收](../engineering/phase-one/WORKPLACE_ACCEPTANCE.md)。
