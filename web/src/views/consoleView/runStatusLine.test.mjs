import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {register} from "node:module";
import vm from "node:vm";
import {parse} from "@vue/compiler-sfc";
import {compile} from "@vue/compiler-dom";
import * as Vue from "vue";
import {renderToString} from "vue/server-renderer";

const read = path => readFileSync(new URL(path, import.meta.url), "utf8");
// display.js as this test needs it, given in place of the file itself (nothing written beside it).
const displayUrl = new URL("./display.js", import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`
 let given;
 export function initialize(value) { given = value; }
 export function load(url, context, next) {
  if (url === given.url) return {format: 'module', source: given.source, shortCircuit: true};
  return next(url, context);
 }
`)}`, import.meta.url, {data: {url: displayUrl, source: read("./display.js")
  .replace('import ContextCompactionIcon from "./legacy/ContextCompactionIcon.vue";', "const ContextCompactionIcon = {};")
  .replace('import {plainText} from "./markdown.js";', 'const plainText = value => String(value || "");')}});
const display = await import(displayUrl);

const {descriptor} = parse(read("./RunStatusLine.vue"));
const script = descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, "");
const names = ["props", "open", "parts", "total", "hasUsage", "cacheRate", "cost", "avgTps", "peakTps", "liveTps", "speed", "exact", "usageLabel", "fmtTokens", "vElapsed"];
const {code} = compile(descriptor.template.content, {
  mode: "function", prefixIdentifiers: true,
  bindingMetadata: Object.fromEntries(names.map(name => [name, "setup-maybe-ref"])),
});
const render = new Function("Vue", code)(Vue);

async function html(stats) {
  const props = Vue.reactive({startedAt: Date.now() - 1211000, stats});
  const context = vm.createContext({...display, computed: Vue.computed, ref: Vue.ref, vElapsed: {}, defineProps: () => props});
  const setup = Vue.proxyRefs(vm.runInContext(`${script}\n;({${names.join(", ")}})`, context));
  const app = Vue.createSSRApp({render: () => render({}, [], {}, setup)});
  app.component("el-popover", {setup: (_, {slots}) => () => [slots.reference?.(), Vue.h("div", {class: "popover-body"}, slots.default?.())]});
  return renderToString(app);
}
const text = markup => markup.replace(/<!--[\s\S]*?-->/g, "").replace(/<[^>]*>/g, "");

const stats = {live: true, usage: {inputTokens: 170000, cacheReadTokens: 15690000, cacheWriteTokens: 0, outputTokens: 73000},
  costUsd: 11.7, avgTps: 14.2, peakTps: 31.6};

test("a running turn shows its runner, clock, tokens with cache rate, cost and speed", async () => {
  const markup = await html(stats);
  assert.match(markup, /class="run-status-runner"/);
  assert.match(markup, /class="run-status-clock"/);
  const compact = text(markup.match(/<span class="run-status-compact">[\s\S]*?<\/span>/)[0]);
  assert.equal(compact, "15.93M（98.9%） · $11.70");
  const wide = text(markup.match(/<span class="run-status-wide">[\s\S]*?<\/span>/)[0]);
  assert.equal(wide, "↑15.86M · ↓73K · 缓存 15.69M（98.9%） · $11.70");
  const tps = markup.match(/<span class="run-status-tps[^"]*"[^>]*>[\s\S]*?<\/span>/)[0];
  assert.equal(text(tps), "均 14t/s", "with nothing streaming the turn average is shown and marked");
  assert.match(tps, /class="run-status-tps is-idle"/);
  assert.match(markup, /aria-label="本轮 Tokens 15\.93M，缓存命中 98\.9%，点击查看明细"/);
});

test("the usage popover lists input, output, cache, hit rate, cost and speeds", async () => {
  const detail = text((await html(stats)).match(/<div class="popover-body">[\s\S]*<\/div>/)[0]);
  for (const row of ["输入 Tokens15,860,000", "输出 Tokens73,000", "缓存 Tokens15,690,000", "缓存命中率98.9%", "本轮金额$11.70", "当前速度没在输出", "平均速度14t/s", "最快一次32t/s"]) {
    assert.ok(detail.includes(row), `${row} in ${detail}`);
  }
});

test("the call streaming now adds its prompt and output and shows its live speed", async () => {
  const markup = await html({live: true, usage: {}, liveTps: 38.4, avgTps: 0,
    inflightUsage: {inputTokens: 2000, cacheReadTokens: 478000, cacheWriteTokens: 300, outputTokens: 2700}});
  const wide = text(markup.match(/<span class="run-status-wide">[\s\S]*?<\/span>/)[0]);
  assert.equal(wide, "↑480.3K · ↓2.7K · 缓存 478K（99.5%）");
  const tps = markup.match(/<span class="run-status-tps[^"]*"[^>]*>[\s\S]*?<\/span>/)[0];
  assert.equal(text(tps), "38t/s");
  assert.doesNotMatch(tps, /is-idle/);
  assert.ok(text(markup.match(/<div class="popover-body">[\s\S]*<\/div>/)[0]).includes("当前速度38t/s"));
});

test("before the first live report only the runner and clock are shown", async () => {
  const markup = await html(null);
  assert.match(markup, /class="run-status-clock"/);
  assert.doesNotMatch(markup, /run-status-usage|run-status-tps|popover-body/);
});

test("phones show the compact total, desktops the full line, and motion can be reduced", () => {
  const css = descriptor.styles[0].content;
  assert.match(css, /\.run-status-compact \{ display: none; \}/);
  assert.match(css, /@media \(max-width: 760px\) \{[\s\S]*?\.run-status-compact \{ display: inline; \}[\s\S]*?\.run-status-wide \{ display: none; \}/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?animation: none;/);
});

test("the composer shows the line only while running, fed by live stats alone", () => {
  const composer = read("./ConsoleComposer.vue");
  assert.match(composer, /<RunStatusLine v-if="props\.running" :started-at="props\.runStartedAt" :stats="props\.runStats"\/>\s*<div class="composer-box">/);
  const consoleView = read("./ConsoleView.vue");
  assert.match(consoleView, /const liveRunStats = computed\(\(\) => \(latestStatsPayload\.value\?\.live \? latestStatsPayload\.value : null\)\);/);
  const composerTag = consoleView.match(/<ConsoleComposer[\s\S]*?\/>/)[0];
  assert.match(composerTag, /:run-started-at="runClockStartedAt"/);
  assert.match(consoleView.match(/<ConsoleHeader[\s\S]*?\/>/)[0], /:run-started-at="runClockStartedAt"/);
  assert.match(consoleView, /const start = Date\.now\(\) - elapsed;\n\tif \(!liveClockStartedAt\.value \|\| start < liveClockStartedAt\.value\) liveClockStartedAt\.value = start;/);
  assert.match(composerTag, /:run-stats="liveRunStats"/);
});
