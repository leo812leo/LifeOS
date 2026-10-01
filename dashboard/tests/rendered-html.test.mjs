import assert from "node:assert/strict";
import test from "node:test";

async function render(path = "/") {
  const { default: worker } = await import("../dist/server/index.js");
  return worker.fetch(new Request("http://localhost" + path, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} });
}

test("renders usable mobile metadata and named navigation on first load", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<html[^>]*lang="zh-Hant"/);
  assert.match(html, /<meta(?=[^>]*name="viewport")(?=[^>]*content="[^"]*width=device-width)(?=[^>]*content="[^"]*initial-scale=1)[^>]*>/);
  assert.doesNotMatch(html, /user-scalable=no|maximum-scale=1/);
  assert.match(html, /aria-label="主要導覽"/);
  for (const view of ["總覽", "健康", "訓練", "資產", "系統"]) {
    assert.match(html, new RegExp("<span>" + view + "</span>"));
  }
});

test("cloud review is reachable from the dashboard and works without local files", async () => {
  const home = await (await render()).text();
  assert.match(home, /href="\/review"/);
  const response = await render("/review");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /專案檢討與資料狀態/);
  assert.match(html, /2026-09-12/);
  assert.match(html, /432/);
  assert.match(html, /尚未同步健康與投資數據/);
  assert.match(html, /href="\/"/);
  for (const id of ["P1-1", "P1-2", "P1-3", "P1-4", "P1-5", "P1-6", "P1-7", "P1-8", "P2-1", "P2-2", "P2-3"]) {
    assert.match(html, new RegExp('id="' + id + '"'));
  }
  assert.doesNotMatch(html, /(?:href|src)="(?:file:|C:|http:\/\/localhost|http:\/\/127\.0\.0\.1)/i);
  assert.doesNotMatch(html, /C:[\\/]+Users[\\/]/i);
});

test("unconnected monitoring never claims live success or fabricates trends", async () => {
  const html = await (await render()).text();
  assert.match(html, /尚未連接即時資料/);
  assert.match(html, /此頁尚無最後同步時間/);
  assert.match(html, /還沒有可顯示的趨勢/);
  assert.doesNotMatch(html, /5 \/ 5 個主要流程正常|核心管線在線|最近成功|市場已收盤/);
  assert.doesNotMatch(html, /class="bar-chart"|class="spark-bars"|2026 年 8 月 11 日/);
  assert.doesNotMatch(html, /react-loading-skeleton|codex-preview|Your site is taking shape/);
});
