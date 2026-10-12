# 第一期首次框架整理：施工授權與交接

2026-10-12。使用者已明確要求另開chat，開始第一期修改整理，並指示「第一次先不用小步修改，我們都要重新整理框架了」。此指令優先於先前只規劃、保留主要目錄及最小修復的施工限制。

## 授權範圍

- 在Agent Relay獨立repo執行第一期S1–S5，從現用入口/支持平台/舊包起點盤點開始，實際修改程式、整理結構、資源/依賴、build/install、相關有效測試與文檔；不能只交方案。
- 首次整理可集中改目錄、模組分工、共享部分和打包接線；不限制為少量檔案或最小diff。先选清楚且適合插件/MCP的compact布局，說明實際來源和consumer，再連貫實作。舊941 map僅候選分析，不要求機械照搬，也不限制一定保留現有物理目錄。
- 該首次施工的结构/owner調整已授權，不逐目錄請示。必要決策/實際移動映射/consumer遷移與驗證仍須留下。第一期驗收定版後，固定來源布局/owner/公開入口，轉為小範圍完整修復及漸進擴展，不繼續反覆全面改結構。
- 保持插件/MCP用法及既有solution_agent namespace、工具名、protocol 0.6 verify_pair、command/receipt/artifact/CAS/lease、狀態與研究權威。首次結構重整不授權破壞性資料遷移、研究方法改變、新治理agent、全域規則或自動推廣經驗。
- 新Win客戶端×Linux Worker的四情境：M1新/舊、M2新/新、M3舊/新、M4舊/舊更新，全部列入P1。舊端用支持舊包和隔離配置/state副本驗證；設備新舊與軟體相容分開。
- 可自主使用repo隔離環境、合成fixtures及實際可取得的非生產測試targets安裝/重建。新host尚未提供時先完成不依賴它的工程；Linux目標、舊包或機器缺失明示NOT RUN/NOT AVAILABLE，不假稱四列已通過。
- 本次「部署任務」是派工開始第一期；不是切換目前研究服務的授權。原solution-agent、研究資料、installed插件/cache、全域Codex/Skill/hooks、現用SSH/workplace/DB/排程不修改。不得自行尋找/輸出私人credentials來補安裝。新測試target連線需實際授權配置；不要對現用host安裝或重派研究命令。
- P2新功能、P3完整更新自動化、P4研究UI仍按實際需求另行範圍；第一期保留擴展位置和必要保全/回退，不因此做完整平台。

## 交接基線與必讀

- 文件修訂前來源HEAD：e906621de85dff0275b419920082dda08c21da37；原extraction基準6337753ed436e9d230b50c2e1c2356f6c8bf4ee5，904來源/941原tracked files。
- 根AGENTS、此授權、PHASED_DELIVERY、MAINTENANCE_RULES；TASKBOOK/CONTRACTS_AND_RELEASE/ENTRYPOINTS及原VERIFICATION供實際入口、契約和歷史結果查證。
- 904原hash未變，隱私/來源/計劃連結檢查已通過；這些是設計保全，不是P1工程/兩端安裝通過。
- Cancel 1項、legacy配置9項失敗及2skip已記錄。支持入口的失敗必處理，不能刪測試/降assertion造綠；非支持分類須有證據。
- 歷史source-map不可改原hash。改實作後留下變更mapping及新的release source/artifact provenance；別把正常施工造成hash差異當成不准修改，亦不可重寫原hash假裝源碼未變。

## 開始與完成要求

先確認Git狀態、入口/兩端支持與舊release起點；選定首次整理的簡潔布局，做實際code/build/config/resource重整，逐批驗證並自治修復。每次更新說明已做/未做、實際結果與下一步，不為每批增加批准。

最終交付：唯一編輯來源與固定模組邊界、可重建Windows client/Linux Worker包、外部配置與安裝/更新說明、M1–M4逐列真實結果、state保全/退回/loaded identity及有效回歸、支持fail處置、擴展入口及維護交接。缺機器或必要驗證就如實partial，繼續能做的範圍，不稱P1 Verified。

GitHub保持private。可將此範圍內的工程進度用清楚commit/分支保存；發布前privacy/staged diff及相應檢查。現用切換與破壞性操作仍依實際授權。
