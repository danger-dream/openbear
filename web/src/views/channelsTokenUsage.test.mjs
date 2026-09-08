import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import * as Vue from "vue";
import { compile } from "@vue/compiler-dom";
import { renderToString } from "vue/server-renderer";

const source = readFileSync(new URL("./ChannelsView.vue", import.meta.url), "utf8");
const start = source.indexOf("function fmtMoney(");
const end = source.indexOf("function modelCost(", start);
assert.ok(start >= 0 && end > start);
const helpers = vm.runInNewContext(`${source.slice(start, end)};({
  fmtMoney, fmtCompact, tokenTotals, buildTokenMetric, providerMetrics, modelMetrics
})`);

// Compile the actual model-card markup: a correct but unused helper would not
// catch this regression, since the card previously had its own incorrect sum.
function tileRenderer(label) {
  const markup = source.match(new RegExp(`<div class="model-stat-tile">\\s*<span class="stat-k">${label}</span>[\\s\\S]*?</div>\\s*</div>`))?.[0];
  assert.ok(markup, `actual ${label} tile exists`);
  const { code } = compile(markup, { mode: "function" });
  return new Function("Vue", code)(Vue);
}
const renderTokens = tileRenderer("Tokens 消耗");
const renderCost = tileRenderer("累计消费");
async function renderTile(render, stats) {
  const context = { ...helpers, row: { stats } };
  return renderToString(Vue.createSSRApp({ render: () => render(context, []) }));
}

const cases = [
  { name: "cache reads and writes are both included", stats: { input_tokens: 100, output_tokens: 50, cache_read_tokens: 600, cache_write_tokens: 200 }, total: "950", input: "900", pct: "88.9%" },
  { name: "cache-read-only usage", stats: { cache_read_tokens: 600 }, total: "600", input: "600", pct: "100.0%" },
  { name: "cache-write-only usage", stats: { cache_write_tokens: 200 }, total: "200", input: "200", pct: "100.0%" },
  { name: "uncached usage remains unchanged", stats: { input_tokens: 100, output_tokens: 50 }, total: "150", input: "100", pct: "0.0%" },
  { name: "output-only usage", stats: { output_tokens: 50 }, total: "50", input: "0", pct: "—" },
  { name: "zero usage", stats: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 }, total: "0", input: "0", pct: "—" },
  { name: "missing stats", stats: undefined, total: "0", input: "0", pct: "—" },
  { name: "numeric strings use numeric addition", stats: { input_tokens: "100", output_tokens: "50", cache_read_tokens: "600", cache_write_tokens: "200" }, total: "950", input: "900", pct: "88.9%" },
  { name: "reported large-usage regression", stats: { input_tokens: 22553108, output_tokens: 2692527, cache_read_tokens: 581175424, cache_write_tokens: 0 }, total: "606M", input: "604M", pct: "96.3%" },
];
for (const fixture of cases) {
  test(`model Tokens tile: ${fixture.name}`, async () => {
    const original = JSON.stringify(fixture.stats);
    const html = await renderTile(renderTokens, fixture.stats);
    assert.ok(html.includes(`<strong class="stat-v">${fixture.total}</strong>`), html);
    assert.ok(html.includes(`入 ${fixture.input} · 缓 ${fixture.pct}`), html);
    const shared = helpers.providerMetrics({ stats: fixture.stats })[0];
    assert.equal(shared.total.compact, fixture.total);
    assert.equal(shared.input.compact, fixture.input);
    assert.equal(JSON.stringify(fixture.stats), original, "rendering does not change recorded usage");
  });
}
test("cached token correction does not recalculate the separate cost field", async () => {
  const stats = { input_tokens: 100, output_tokens: 50, cost_usd: 12.34 };
  const withoutCache = await renderTile(renderCost, stats);
  const withCache = await renderTile(renderCost, { ...stats, cache_read_tokens: 1000000, cache_write_tokens: 2000000 });
  assert.equal(withCache, withoutCache);
  assert.ok(withCache.includes("$12.34"));
});
