# Design Ready 驗證回執

2026-10-12。結果：設計包 D0 可交付；工程 C1–C6/F/D 尚未執行。本文件記錄本次實際檢查，不把跨聊天審查當 runtime 驗收。

## 實際完成的檢查

| 命令/檢查 | 本次結果 |
| --- | --- |
| `python docs/planning/normalization-20261012/build-plan-inventory.py --check` | passed；基線941檔、來源904檔、預定destination941唯一、JSON/CSV一致、來源hash對映一致 |
| 同一checker的基線保全 | runtime及非policy基線未變；明確允許本次授權的 README.md/AGENTS.md 文件更新；docs不視為runtime變更 |
| `python scripts/extraction/verify-source.py` | 904檔，source mismatches 0；internal junctions 686，external links 0；原四份歷史計劃hash亦保全 |
| `python scripts/extraction/check-publication.py` | 955個Git候選檔，findings 0，passed true；不是私人目錄全文公開 |
| Markdown相對連結與維護入口的一次性檢查 | 根AGENTS/README、planning索引及7份設計Markdown，共10檔；本地連結全部存在；根R1–R8、MUST/MUST NOT/MAY、stage、verify_pair和未驗項目標示存在 |
| 指定聊天討論回读 | 四輪已完成；逐輪完整回讀瀏覽器body，已核對第三輪撤回逐批批准、第四輪維護約束；完整IDs保留於忽略的本地provenance，不進公開來源 |
| Git變更範圍 | 根AGENTS/README、最新planning索引、設計文件及設計inventory工具；沒有runtime/source move、installed plugin/live state/研究資料變更 |

941 mapping 指全部原 tracked files（904 source +37 publication metadata），不代表全部進交付payload。依賴掃描只覆蓋Python absolute imports及Node literal imports，另記Background動態依賴；完整relative/dynamic/runtime閉包留C1/C2，不假稱已證明。

八條維護約束現在僅由本專案的人/agent遵守。最新使用者澄清已落實：專案經驗僅在本專案保存/去重，不自動上升全域 Codex/個人 Skill/其他 repo，也不自動成為強制政策。Boundary lint、schema generation drift、正式 locks/manifest updater、平台fixture及自動CI仍planned。根AGENTS不能只是索引，也禁止agent為通過patch自行削弱gate；規則變更本身須明確授權。

## 未跑及已知限制

- 本次是文件/維護規則/設計工具變更；沒有重跑全repo runtime tests、模型/API/SSH實驗，沒有假稱新工程驗收通過。
- Cancel 1項及legacy配置9項失敗、2項skip沿原驗證紀錄披露。未在此goal修復或重測；影響它們的後續工程須定位，不可永久豁免或删測試。
- C1–C6：契約機械ownership/fingerprint、包/locks/resources、Provider雙consumer、updater、Windows/Linux fresh install/native ABI、失敗注入/session/bindings/rollback均NOT RUN。
- Architecture Freeze F未達；live cutover D未授權/未執行。當前project.json仍independent-extraction，formal modularization/live installation仍false。
- 本設計採uv/Hatchling、pnpm/esbuild、Nx後延；工具文檔能支持选型，不能证明repo已经接入。
- 剩餘規劃基線A+B約20–34人日（含25%餘量），不含新UI/DSH等候選功能；可選現用切換另2–4基礎人日，前提失效需重估。

本輪完成只表示：來源到固定模組/交付包、公開契約、擴展位置、工程gates與估算、立即生效維護/交接規則已形成一致設計。正式源碼正規化仍依TASKBOOK另行施工。

追加檢查：生成工具重生4個inventory輸出與已驗版本byte-identical；新增的project-local澄清、經驗所屬project及禁止共享發布夾帶經驗已核對；staged whitespace check通過。工程維護經驗留在 Agent Relay；研究/Worker 經驗留在產生它的研究 project，不匯入共用工具 repo。共用軟體發布只同步工程來源/產物，不夾帶專案經驗、研究流程或把它們變成其他 project 的預設規則。
