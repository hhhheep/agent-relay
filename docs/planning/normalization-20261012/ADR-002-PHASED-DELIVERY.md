# ADR-002：先交付插件/MCP的整理與四情境安裝

2026-10-12。依使用者最新明確要求調整工程範圍；設計修訂，未執行施工或部署。取代ADR-001中「全面目標布局及C1–C6一次施工」的預設，保留唯一來源、owner、原公開入口/協議和專案經驗作用域。

## 決策與原因

第一期採[PHASED_DELIVERY](PHASED_DELIVERY.md) S1–S5，目標是整理來源、Windows/Linux可重建安裝、保留真實擴展點。以client×worker的M1新/舊、M2新/新、M3舊/新、M4舊/舊更新驗收，而非只看新設備乾淨安裝。

現有主要目錄保留並固定責任，僅為移植/唯一來源及真共享consumer作必要改動。未來沿此基礎增量擴展，不為採用apps/packages名稱再搬核心。舊migration-map/symbol-split是完整候選分析，不是必做移動清單。Provider雙consumer的必要解耦保留；不強制每功能拆包、通用schema-bindings、root workspace、Nx或完整updater。

舊設計同時要求可攜交付和完整部署體系，成本與現階段使用需求不匹配。按使用問題觸發P2功能擴展、P3多端更新自動化、P4研究UI；各方向可獨立進入，不自動全做。

## 不變式與驗證

M1–M4不代表任意版本可混搭。protocol 0.6身份等式、契約及state ownership不放寬。M4仍必須支持已列起點的配置/state保全及受控回退；故障自動化後延不等於取消回退。支持入口的既有fail仍必處理。

P1 Verified表示S1–S5與四列在聲明targets實際通過，來源owner/公開入口和有效物理布局固定；不表示舊完整F/所有updater功能已達成。改架構或資料權威依根AGENTS例外程序；普通修復在既定範圍自治。

此修訂由使用者要求分期，不是為讓某個失敗patch通過而自行削弱gate。本次只改文件，不授權現用安裝、讀live狀態或修改全域設定。
