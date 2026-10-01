"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

type View = "總覽" | "健康" | "訓練" | "資產" | "系統";
const views: View[] = ["總覽", "健康", "訓練", "資產", "系統"];
const glyphs = ["⌂", "＋", "↗", "◒", "◇"];
const systems = [
  { name: "Garmin 健康", detail: "睡眠、步數、心率與身體電量" },
  { name: "Garmin 活動", detail: "跑步、訓練負荷與活動紀錄" },
  { name: "每日訓練微調", detail: "AI 教練與規則引擎" },
  { name: "投資組合", detail: "台股、美股與匯率" },
  { name: "週訓練建議", detail: "Garmin Coach 與週回顧" },
  { name: "Schwab 庫存", detail: "美股庫存整合" },
];

function EmptyState({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="empty-state"><span className="empty-symbol" aria-hidden="true">—</span><strong>{title}</strong><p>{children}</p></div>;
}

export default function Home() {
  const [view, setView] = useState<View>("總覽");
  const [date, setDate] = useState("");
  useEffect(() => {
    const updateDate = () => setDate(new Intl.DateTimeFormat("zh-TW", {
      timeZone: "Asia/Taipei", year: "numeric", month: "long", day: "numeric", weekday: "long",
    }).format(new Date()));
    updateDate();
    const timer = window.setInterval(updateDate, 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const show = (section: View) => view === "總覽" || view === section;
  const selectView = (next: View, focusMain = false) => {
    setView(next);
    window.scrollTo(0, 0);
    if (focusMain) window.requestAnimationFrame(() => document.getElementById("main-content")?.focus());
  };

  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">跳至主要內容</a>
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark" aria-hidden="true">L</div><div><strong>LifeOS</strong><span>CONTROL CENTER</span></div></div>
        <nav aria-label="主要導覽">
          {views.map((item, index) => <button key={item} className={`nav-item ${view === item ? "active" : ""}`}
            aria-current={view === item ? "page" : undefined} onClick={() => selectView(item)}>
            <span className="nav-glyph" aria-hidden="true">{glyphs[index]}</span><span>{item}</span></button>)}
        </nav>
        <div className="sidebar-spacer" />
        <div className="sync-card"><strong>資料連線待完成</strong><p>尚無即時運行狀態</p><small>網站目前僅提供唯讀介面</small></div>
        <div className="user-block"><div className="avatar" aria-hidden="true">L</div><div><strong>LifeOS</strong><span>示範工作區</span></div></div>
      </aside>
      <main className="workspace" id="main-content" tabIndex={-1}>
        <header className="topbar"><div><span className="eyebrow">{date || "Asia/Taipei"} · 檢視日期</span><h1>{view === "總覽" ? "生活總覽" : view}</h1></div><span className="privacy-badge">未連線示範</span></header>
        <div className="connection-notice" role="note"><span aria-hidden="true">◇</span><p><strong>尚未連接即時資料</strong>{" "}健康、資產與系統狀態皆待確認。此頁尚無最後同步時間。</p></div>
        <Link className="cloud-link" href="/review">專案檢討與雲端資料狀態 →</Link>

        <div className="hero-grid">
          {show("健康") && <article className="readiness-card" aria-labelledby="health-title">
            <span className="card-kicker">今日恢復</span>
            <div className="readiness-main"><div className="score-ring"><span>—</span><small>未連線</small></div><div className="readiness-copy"><h2 id="health-title">等待健康資料</h2><p>連接 Garmin 與 Notion 後，才能判斷今日恢復狀態。</p></div></div>
            <div className="signal-row"><div><span>睡眠</span><strong>—</strong></div><div><span>Body Battery</span><strong>—</strong></div><div><span>靜息心率</span><strong>—</strong></div></div>
          </article>}
          {show("訓練") && <article className="today-plan" aria-labelledby="training-title">
            <div className="card-top"><div><span className="card-kicker">今日訓練</span><h2 id="training-title">Garmin Coach 課表</h2></div><span className="pill">未連線</span></div>
            <div className="plan-metrics"><div><span>距離</span><strong>—</strong></div><div><span>配速</span><strong>—</strong></div><div><span>強度</span><strong>—</strong></div></div>
            <p className="coach-note">尚無今日訓練建議。連接資料後，這裡將顯示課表、補給、睡眠與規則引擎判斷。</p>
            <details className="details"><summary>訓練建議需要哪些資料？</summary><p>Garmin Coach 課表、近期活動、睡眠、心率與營養紀錄。缺少資料時須明確標示，避免把未知狀態當作可訓練。</p></details>
          </article>}
        </div>

        {(show("健康") || show("訓練")) && <section className="metric-strip" aria-label="待連線的關鍵指標">
          <div><span>步數</span><strong>—</strong><small>未連線</small></div><div><span>平均壓力</span><strong>—</strong><small>未連線</small></div><div><span>訓練負荷</span><strong>—</strong><small>未連線</small></div><div><span>本週遵循率</span><strong>—</strong><small>未連線</small></div>
        </section>}

        <div className="content-grid">
          {show("健康") && <article className="panel trend-panel" aria-labelledby="trend-title"><div className="panel-head"><div><span className="card-kicker">恢復趨勢</span><h2 id="trend-title">睡眠與身體電量</h2></div><span className="pill">未連線</span></div><EmptyState title="還沒有可顯示的趨勢">取得有日期的健康紀錄後，才能比較睡眠與恢復變化。</EmptyState></article>}
          {show("資產") && <article className="panel wealth-panel" aria-labelledby="wealth-title"><div className="panel-head"><div><span className="card-kicker">投資組合</span><h2 id="wealth-title">資產趨勢</h2></div><span className="pill">未連線</span></div><div className="wealth-total"><span>總資產估值</span><strong>—</strong></div><EmptyState title="尚無資產快照">台股、美股、匯率與估值日期將在資料連線後顯示。</EmptyState><div className="allocation"><div><span>台股</span><strong>—</strong></div><div><span>美股</span><strong>—</strong></div></div></article>}
          {show("系統") && <article className="panel systems-panel" aria-labelledby="systems-title"><div className="panel-head"><div><span className="card-kicker">自動化監控</span><h2 id="systems-title">資料管線</h2></div>{view === "總覽" && <button className="text-button" onClick={() => selectView("系統", true)}>全部系統 →</button>}</div><p className="panel-note">以下為專案管線清單；網站尚未讀取執行紀錄。</p><div className="system-list">{systems.map(system => <div className="system-row" key={system.name}><span className="status-dot" aria-hidden="true" /><div><strong>{system.name}</strong><span>{system.detail}</span></div><small>未確認</small></div>)}</div></article>}
          {show("系統") && <article className="panel alerts-panel" aria-labelledby="alerts-title"><div className="panel-head"><div><span className="card-kicker">事件與連線</span><h2 id="alerts-title">監控資料說明</h2></div></div><EmptyState title="尚無可查證的事件">需要讀取各管線的最後成功、失敗與通知送達紀錄，才能顯示告警。</EmptyState><details className="details"><summary>為什麼顯示「未確認」？</summary><p>此網站尚未連接專案的即時資料。既有 Python 腳本可能仍在執行，但網站無法據此宣稱系統正常。</p></details><details className="details"><summary>如何用手機開啟？</summary><p>部署後可使用手機瀏覽器開啟網站網址，並從瀏覽器選單加入主畫面。接入私人資料前須先完成登入與授權設定；公開程式碼本身不代表網站已部署或資料已受保護。</p></details></article>}
        </div>
        <footer><span>LifeOS Control Center · 唯讀介面</span><span>連線尚未完成 · 所有數據狀態待確認</span></footer>
      </main>
    </div>
  );
}
