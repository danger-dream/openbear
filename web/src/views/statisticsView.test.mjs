import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {parse} from '@vue/compiler-sfc';

const source=fs.readFileSync(new URL('./StatisticsView.vue',import.meta.url),'utf8');
const descriptor=parse(source).descriptor;
const template=descriptor.template.content;
const script=descriptor.scriptSetup.content;
const css=descriptor.styles.filter(style=>style.scoped).map(style=>style.content).join('\n');

test('statistics ranges use direct tags and keep dense 3/7-day timelines',()=>{
  assert.match(template,/class="range-tags" role="group"/);
  assert.match(script,/const RANGE_OPTIONS = \[\{ days: 1, label: "今天" \}[\s\S]*\{ days: 90, label: "90 天" \}\]/);
  assert.match(template,/@click="selectRange\(item\.days\)"/);
  assert.doesNotMatch(template,/class="select-shell range-select"/);
  assert.match(script,/const timelineRows = computed/);
  assert.match(script,/hours === 1 \? "每小时" : `每 \$\{hours\} 小时`/);
});

test('token chart keeps total input while its tooltip exposes the exact split and bucket cost',()=>{
  assert.match(template,/<th>输入<\/th><th>缓存率<\/th>/);
  assert.match(template,/class="tooltip-section">输入拆分/);
  assert.match(template,/Fresh 输入[\s\S]*缓存读取[\s\S]*缓存写入[\s\S]*当前桶花费/);
  assert.match(script,/inputTokens \|\| 0\) \+ Number\(row\?\.cacheReadTokens \|\| 0\) \+ Number\(row\?\.cacheWriteTokens/);
  assert.match(script,/key: "cacheRate", label: "缓存"/);
});

test('charts expose centered selectors, peak, median and readable hover details',()=>{
  assert.match(template,/class="chart-stat-strip"/);
  assert.match(template,/峰值 \{\{ series\.format\(series\.peak\) \}\}/);
  assert.match(template,/中位数 \{\{ series\.format\(series\.median\) \}\}/);
  assert.match(template,/class="chart-legend legend-buttons"/);
  assert.match(template,/class="metric-insights"/);
  assert.match(template,/class="chart-tooltip model-tooltip"[\s\S]*<em :title="series\.label">/);
  assert.match(css,/\.chart-legend,\.mini-legend\{justify-content:center;flex-wrap:wrap\}/);
});

test('quality, heatmap, tools and reasoning use contextual translated tooltips',()=>{
  assert.doesNotMatch(template,/class="error-breakdown"|失败归因/);
  assert.match(template,/class="failure-tooltip"/);
  assert.match(template,/class="quality-timeline"/);
  assert.match(template,/class="quality-bar-ok"/);
  assert.match(template,/class="quality-bar-failed"/);
  assert.match(template,/成功率/);
  assert.match(script,/server_error: "上游服务异常"/);
  assert.match(template,/class="heat-scroll"[\s\S]*<\/div><div v-if="heatHover" class="heat-tooltip"/);
  assert.match(template,/@pointerenter="updateHeatHover/);
  assert.match(css,/\.heat-scroll\{[^}]*overflow-y:hidden/);
  assert.match(css,/\.heat-tooltip\{position:fixed/);
  assert.match(script,/Bash: "执行脚本", Read: "读取文件"/);
  assert.match(template,/\{\{ toolLabel\(item\.name\) \}\}/);
  assert.match(template,/class="thinking-chart"/);
  assert.match(template,/class="chart-tooltip thinking-tooltip"/);
});

test('model overview keeps top models plus other, aligned sparklines, exact values and drill-down',()=>{
  assert.match(template,/模型分布与近期走势/);
  assert.match(template,/class="distribution-donut"/);
  assert.match(template,/class="model-sparkline"/);
  assert.match(template,/class="spark-col"/);
  assert.match(template,/<th>请求<\/th><th>占比<\/th><th>Token<\/th><th>花费<\/th><th>成功率<\/th>/);
  assert.match(css,/\.distribution-table\{[^}]*table-layout:fixed/);
  assert.match(css,/\.distribution-table th:nth-child\(2\),\.distribution-table td:nth-child\(2\)\{text-align:center\}/);
  assert.match(template,/@click="focusModel/);
  assert.match(script,/aggregateModels\(rest, `其他 \$\{rest\.length\} 个模型`\)/);
  assert.match(script,/scrollIntoView\(\{ behavior: "smooth", block: "start" \}\)/);
});

test('latency uses a proportional three-stage journey and explains backend-provided request percentiles',()=>{
  assert.match(template,/class="latency-overview"/);
  assert.match(template,/class="latency-composition"[\s\S]*v-for="item in latencyStageItems"/);
  assert.match(template,/class="latency-step"[\s\S]*item\.share[\s\S]*item\.description/);
  assert.match(script,/const latencyStageItems = computed/);
  assert.match(script,/建立连接[\s\S]*等待首字[\s\S]*生成回答/);
  assert.match(script,/Math\.max\(0, Number\(item\.value\)\) \/ total \* 100/);
  assert.match(template,/class="latency-percentiles"/);
  assert.match(template,/P50[\s\S]*P90[\s\S]*P95[\s\S]*P99[\s\S]*最大/);
  assert.match(template,/P99 口径：[\s\S]*99% 的逐请求样本不超过该耗时/);
  assert.match(script,/latency\.value\?\.percentiles\?\.total/);
  assert.match(script,/latency\.value\?\.percentiles\?\.firstToken/);
});

test('dashboard cards size to their content and reflow from actual content width',()=>{
  assert.match(css,/\.statistics-scroll\{[^}]*container-type:inline-size/);
  assert.match(css,/\.dashboard-grid\{[^}]*align-items:start/);
  assert.match(css,/\.panel\{[^}]*align-self:start/);
  assert.match(css,/\.model-compare-panel,\.latency-panel,\.models-panel,\.heat-panel,\.table-panel\{grid-column:1\/-1/);
  assert.match(css,/\.metric-chart-card:last-child:nth-child\(odd\)\{grid-column:1\/-1\}/);
  assert.match(css,/@container\(max-width:1050px\)\{\.dashboard-grid\{grid-template-columns:1fr\}/);
  assert.doesNotMatch(css,/@media\(max-width:1180px\)\{\.kpi-grid\{grid-template-columns:repeat\(3/);
  assert.match(css,/\.bottom-grid\{[^}]*align-items:stretch/);
  assert.match(css,/\.bottom-grid>\.panel\{align-self:stretch\}/);
});

test('comparison controls use a refined period switch and provider-grouped model cards',()=>{
  assert.match(script,/const modelGroups = computed/);
  assert.match(script,/function modelChoiceColor\(model\)/);
  assert.match(template,/class="compare-glyph"/);
  assert.match(template,/class="compare-copy"[\s\S]*已叠加/);
  assert.match(template,/class="compare-switch"/);
  assert.match(template,/class="selection-summary"/);
  assert.match(template,/class="provider-models"/);
  assert.match(template,/class="provider-cluster"/);
  assert.match(template,/class="model-choice"/);
  assert.match(template,/class="model-choice-check"/);
  assert.match(template,/:aria-pressed="selectedModels\.includes\(item\.model\)"/);
  assert.match(template,/@click="toggleModel\(item\.model\)"/);
  assert.doesNotMatch(template,/class="add-model"|添加模型<\/option>/);
  assert.match(css,/\.model-choice\.active\{[^}]*--model-color/);
  assert.match(css,/\.latency-overview\{[^}]*linear-gradient/);
});
