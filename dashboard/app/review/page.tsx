import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "LifeOS｜專案檢討與資料狀態",
  description: "2026-09-12 專案審查摘要、修正順序與雲端資料連線狀態。",
  openGraph: { title: "LifeOS 專案檢討", description: "2026-09-12 私人專案審查摘要", images: [] },
  twitter: { card: "summary", title: "LifeOS 專案檢討", description: "2026-09-12 私人專案審查摘要", images: [] },
};

const findings = [
  { id: "P1-1", title: "dry-run 仍可能發送真實通知", evidence: "AI 失敗的 fallback 先發送告警，之後才判斷 dry-run。受控測試攔下兩次對外發送企圖，未接觸真實服務。", action: "將禁止通知及正式寫入設為 dry-run 的一致規則，包含錯誤分支。", source: "training_advisor.py:647" },
  { id: "P1-2", title: "測試可能污染正式執行帳本", evidence: "測試缺少全域網路與 dotenv 隔離；部分 main 測試未替換帳本寫入。本次其中一組測試攔下九次正式帳本寫入企圖。", action: "測試預設阻擋外部網路、使用假憑證及暫存帳本。", source: "tests/conftest.py；test_health_tracker.py:286；test_investment_tracker.py:439" },
  { id: "P1-3", title: "通知失敗仍被記為成功", evidence: "通知回傳失敗或例外後，流程仍可能記錄成功；dry-run 也可能更新成功時間，使監控誤判。", action: "分開追蹤內容產生、資料寫入與通知送達；模擬執行不更新正式心跳。", source: "daily_adjust.py:213；weekly_review.py:105；training_advisor.py:665" },
  { id: "P1-4", title: "當天運動回報缺少補送機制", evidence: "Bot 找今天的活動，健康同步只抓昨天；找不到的 RPE、痠痛與補給回報寫入 pending 檔，但未找到讀取補送流程。", action: "活動同步後冪等補送，處理多活動日，並讓教練看見尚未補送的訊號。", source: "telegram_bot.py:285、368；health_tracker.py:729" },
  { id: "P1-5", title: "訓練強度限制可能漏掉矛盾建議", evidence: "關鍵字比對可放過 EASY 搭配 Z4，或 REST 搭配今天跑步、明天休息的文字，也可能誤判否定句。", action: "以程式確定休息與強度上限，或驗證結構化課表；AI 僅補充說明。", source: "daily_adjust.py:764" },
  { id: "P1-6", title: "投資報價與快照日期可能錯位", evidence: "最新收盤價未保留來源日期，固定寫為昨天。離線重現 9/8 報價被寫為 9/7；週一也可能跳過。", action: "保留各市場交易日期、估值時點與匯率日期，不能一律用今天減一天。", source: "investment_tracker.py:126、492" },
  { id: "P1-7", title: "缺價仍發布低估總額，補跑無法修正", evidence: "失敗持股被排除，其餘市值仍寫入並記成功；同日去重又會阻擋補跑更新。", action: "缺價時不宣稱完整估值；支援可修正且冪等的快照，不改動凍結的 Notion 欄位名。", source: "investment_tracker.py:233、385、527" },
  { id: "P1-8", title: "去重查詢失敗可能新增重複活動", evidence: "查詢例外被轉成空陣列，活動流程將其視為不存在，繼續新增。離線重現 timeout 後仍建立頁面。", action: "查詢失敗即停止該筆寫入，保留失敗原因並允許重試。", source: "health_tracker.py:558、593" },
  { id: "P2-1", title: "活動子管線不在 watchdog 範圍內", evidence: "健康成功但活動同步失敗時，既有監控可能無告警；健康資料全缺也會提前退出，使活動同步不開始。", action: "監控各子階段，區分正常零活動、讀取失敗及主流程早退。", source: "health_tracker.py:690；watchdog.py:42" },
  { id: "P2-2", title: "備份全部失敗仍可能回報成功結束", evidence: "備份入口未以 run_backup 的結果決定程式結束碼，執行完成不等於備份可用。", action: "以實際成功備份判定結果，並加入最小還原驗證。", source: "backup_notion.py:378" },
  { id: "P2-3", title: "文件、網站與驗證門檻不一致", evidence: "部分文件對活動寫入者、週回顧寫入資料庫的描述已過時；網站整包型別檢查另有既存 Cloudflare 型別缺漏。", action: "分開記錄程式完成、已啟用、最近實測成功；補齊型別及可重現驗證入口。", source: "docs/tasks/README.md；weekly_review.py:119；dashboard/db/index.ts" },
];

export default function Review() {
  return (
    <main className="review-page" id="review-content">
      <Link className="cloud-link" href="/">← 返回生活總覽</Link>
      <header className="topbar">
        <div><span className="eyebrow">LifeOS · 審查日期 <time dateTime="2026-09-12">2026-09-12</time></span><h1>專案檢討與資料狀態</h1></div>
        <span className="privacy-badge">未連線示範</span>
      </header>
      <section className="panel review-intro" aria-labelledby="cloud-status">
        <span className="card-kicker">跨裝置查看</span>
        <h2 id="cloud-status">網站原始碼已備妥，部署與實際數據待串接</h2>
        <p>部署到託管平台後，可從手機或公司電腦使用相同網址。此公開原始碼不含正式存取控制；接入私人資料前，必須配置並驗證登入與授權。</p>
        <div className="connection-notice"><p><strong>尚未同步健康與投資數據。</strong>此報告是日期固定的審查摘要，不是即時監控。Notion 雖是專案既有的雲端資料來源，網站目前尚未連線讀取。</p></div>
        <p>公開版本不含 API 金鑰、登入 cookie 或原始健康與持股紀錄。公開程式碼不代表網站已部署，也不保證私人存取限制；在公司裝置使用時，仍需遵守公司的帳號與個人資料使用政策。</p>
      </section>
      <section className="panel review-intro" aria-labelledby="review-summary">
        <span className="card-kicker">審查結論</span><h2 id="review-summary">先修好資料可信度，再接入監控</h2>
        <p>目前的主要風險是任務是否真的完成、通知是否送達，以及資料是否完整。後端問題本次僅審查，尚未修正；不能因單元測試通過就推論正式系統正常。</p>
        <ol className="review-steps"><li>隔離測試與 dry-run 的副作用。</li><li>修正成功判定、通知送達與子管線監控。</li><li>補送運動回報，強化訓練強度限制。</li><li>修正交易日期、缺價處理與活動去重。</li><li>接入受保護的唯讀資料，顯示來源、業務日期、最後成功、完整性及過期狀態。</li></ol>
      </section>
      <section aria-labelledby="findings-title"><h2 id="findings-title">待修正問題 · 11 項</h2>
        <div className="review-findings">{findings.map(finding => <article className="panel" id={finding.id} key={finding.id}>
          <span className="card-kicker">{finding.id} · {finding.id.startsWith("P1") ? "優先處理" : "後續改善"}</span>
          <h2>{finding.title}</h2><p>{finding.evidence}</p>
          <details className="details"><summary>修正方向與程式位置</summary><p>{finding.action}</p><p className="review-source">審查位置：{finding.source}</p></details>
        </article>)}</div>
      </section>
      <section className="panel review-intro" aria-labelledby="validation-title">
        <h2 id="validation-title">驗證與限制</h2>
        <p>2026-09-12 以假憑證、網路阻擋與帳本隔離執行兩組 Python 測試，共 432 項通過，並另外重現高影響問題。沒有向真實 Telegram、Garmin、Notion 或 OpenRouter 發送測試流量，沒有操作 Windows 排程。</p>
        <p>手機版介面已檢查 8 種視窗寬度與 200% 字體放大；這是桌面瀏覽器模擬，不是實體手機或公司網路的端到端測試。審查不是逐行證明所有模組無缺陷，也未據此斷言正式資料已損壞。</p>
      </section>
      <footer><Link className="cloud-link" href="/">返回生活總覽</Link><span>LifeOS · 唯讀審查摘要 · 2026-09-12</span></footer>
    </main>
  );
}
