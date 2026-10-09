import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref,watch,nextTick} from 'vue';
import {parse,compileScript,compileTemplate,compileStyle} from '@vue/compiler-sfc';

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
  assert.match(script,/等待响应头[\s\S]*等待可识别输出[\s\S]*后续接收/);
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

function chartReadoutHarness() {
  const beginning=script.indexOf('// Chart readout interactions:');
  const ending=script.indexOf('// End chart readout interactions.');
  assert.ok(beginning>0 && ending>beginning);
  const hover=script.slice(script.indexOf('function hoverIndex('),script.indexOf('function tooltipStyle('));
  const chart={width:100,left:0,chartWidth:100};
  const state={
    selectedReadout:ref({chart:'',metric:'',index:-1}),timelineRows:ref([{}, {}, {}, {}]),
    trendHoverIndex:ref(-1),qualityHoverIndex:ref(-1),thinkingHoverIndex:ref(-1),modelHover:ref({metric:'',index:-1}),heatHover:ref(null),
    trendChart:ref(chart),qualityChart:ref(chart),thinkingChart:ref(chart),modelCharts:ref({requests:chart}),
    data:ref(null),trendMetric:ref('tokens'),hiddenTrendSeries:ref([]),selectedModels:ref([]),selectedMetrics:ref(['requests']),watch,
  };
  const realWatch=script.match(/^watch\(\[data, trendMetric, hiddenTrendSeries, selectedModels, selectedMetrics\], clearChartReadout\);$/m)?.[0];
  assert.ok(realWatch);
  const functions=vm.runInNewContext(`let readoutPress=null;\n${hover}\n${script.slice(beginning,ending)}\nconst stopDataWatch = ${realWatch}\n({activeChartIndex,clearChartReadout,startChartReadout,stopChartReadout,cancelChartReadout,chartReadoutKeydown,dismissChartReadout,stopDataWatch})`,state);
  const svg={getBoundingClientRect:()=>({left:0,width:100})};
  svg.contains=target=>target===svg;
  const scroll={scrollTop:0};
  const currentTarget={querySelector:()=>svg,closest:()=>scroll};
  const event=(x=70,extras={})=>({currentTarget,target:svg,clientX:x,clientY:30,pointerId:1,pointerType:'touch',button:0,...extras});
  return {state,functions,event,scroll};
}

test('all four real chart handlers pin touch-up values, retain hover previews, and ignore scroll gestures',()=>{
  const {state,functions:h,event,scroll}=chartReadoutHarness();
  for(const [chart,metric] of [['trend',''],['quality',''],['model','requests'],['thinking','']]) {
    h.startChartReadout(event(),chart,metric);
    h.stopChartReadout(event(),chart,metric);
    assert.equal(h.activeChartIndex(chart,metric,-1),2,`${chart} selected bucket survives pointer leave`);
    assert.equal(state.selectedReadout.value.chart,chart);
    assert.equal(h.activeChartIndex(chart,metric,3),3,'mouse hover still previews another bucket');
    assert.equal(h.activeChartIndex(chart,metric,-1),2,'pointer leave restores pinned bucket');
  }
  h.startChartReadout(event(),'trend');
  h.stopChartReadout(event(98),'trend');
  assert.equal(h.activeChartIndex('thinking','',-1),2,'moving finger is not a tap or a new selection');
  h.startChartReadout(event(),'quality');
  scroll.scrollTop=8;
  h.stopChartReadout(event(),'quality');
  assert.equal(h.activeChartIndex('thinking','',-1),2,'vertical scroll cannot select a bucket');
  scroll.scrollTop=0;
  h.startChartReadout(event(),'trend');
  h.cancelChartReadout('trend');
  h.stopChartReadout(event(),'trend');
  assert.equal(h.activeChartIndex('thinking','',-1),2,'native pointercancel leaves previous selection intact');
  h.dismissChartReadout({target:{closest:()=>({})}});
  assert.equal(h.activeChartIndex('thinking','',-1),2,'synthetic click inside the SVG keeps the tap readout');
  h.dismissChartReadout({target:{closest:()=>null}});
  assert.equal(h.activeChartIndex('thinking','',-1),-1,'clicking outside the SVG clears pinned readout');
  h.stopDataWatch();
});

test('keyboard select/navigation and dataset lifecycle clear readouts without breaking touch hit geometry',async()=>{
  const {state,functions:h,event}=chartReadoutHarness();
  const key=(value)=>({key:value,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}});
  const right=key('ArrowRight');h.chartReadoutKeydown(right,'model','requests');
  assert.equal(right.prevented,true);
  assert.equal(h.activeChartIndex('model','requests',-1),0);
  h.chartReadoutKeydown(key('ArrowRight'),'model','requests');
  assert.equal(h.activeChartIndex('model','requests',-1),1);
  h.chartReadoutKeydown(key('ArrowLeft'),'model','requests');
  assert.equal(h.activeChartIndex('model','requests',-1),0);
  const escape=key('Escape');h.chartReadoutKeydown(escape,'model','requests');
  assert.equal(escape.stopped,true,'dismissing the readout does not close an outer navigation layer');
  assert.equal(h.activeChartIndex('model','requests',-1),-1);
  h.startChartReadout(event(50,{target:{}}),'trend');
  h.stopChartReadout(event(),'trend');
  assert.equal(h.activeChartIndex('trend','',-1),-1,'non-SVG surface is not an accidental touch target');
  h.startChartReadout(event(),'trend');h.stopChartReadout(event(),'trend');
  state.trendMetric.value='cost';
  await nextTick();
  assert.equal(h.activeChartIndex('trend','',-1),-1,'changing a data condition dismisses the pinned readout');
  h.startChartReadout(event(),'trend');h.stopChartReadout(event(),'trend');
  state.data.value={timeline:[{bucket:'new'}]};
  await nextTick();
  assert.equal(h.activeChartIndex('trend','',-1),-1,'loading fresh data dismisses the prior readout');
  h.startChartReadout(event(),'trend');h.stopChartReadout(event(),'trend');
  state.timelineRows.value=[];
  assert.equal(h.activeChartIndex('trend','',-1),-1,'stale index cannot expose changed dataset');
  h.stopDataWatch();
  assert.match(script,/watch\(\[data, trendMetric, hiddenTrendSeries, selectedModels, selectedMetrics\], clearChartReadout\)/);
  assert.match(script,/onBeforeUnmount\(\(\) => \{ document\.removeEventListener\("click", dismissChartReadout, true\); \}\)/);
});

test('compiled charts wire tap, cancel, keyboard and ARIA; phone heatmap has precise 7×24 native selectors',()=>{
  const id='statistics-touch';
  const compiled=compileTemplate({source:template,filename:'StatisticsView.vue',id});
  const js=compileScript(descriptor,{id});
  const style=compileStyle({source:css,filename:'StatisticsView.vue',id,scoped:true});
  assert.equal(compiled.errors.length,0);assert.equal(js.errors?.length||0,0);assert.equal(style.errors.length,0);
  for(const chart of ['trend','quality','thinking']) {
    assert.match(template,new RegExp(`@pointerdown="startChartReadout\\(\\$event,'${chart}'\\)"[\\s\\S]*?@pointerup="stopChartReadout\\(\\$event,'${chart}'\\)"`));
    assert.match(template,new RegExp(`@pointercancel="cancelChartReadout\\('${chart}'\\)"`));
    assert.match(template,new RegExp(`@keydown="chartReadoutKeydown\\(\\$event,'${chart}'\\)"`));
  }
  assert.match(template,/@pointerdown="startChartReadout\(\$event,'model',metric\)"/);
  assert.match(template,/@keydown="chartReadoutKeydown\(\$event,'model',metric\)"/);
  assert.match(template,/class="heat-readout-controls"[\s\S]*v-model\.number="heatSelectionDay"[\s\S]*v-model\.number="heatSelectionHour"[\s\S]*data\?\.heatmap\?\.\[heatSelectionDay\]\?\.\[heatSelectionHour\]/);
  assert.match(template,/v-for="hour in 24"/);
  assert.match(css,/\.heat-scroll\{overflow-x:auto;overflow-y:hidden/);
  assert.match(css,/\.heat-readout-controls select\{[^}]*min-height:44px/);
  assert.doesNotMatch(css,/touch-action:\s*none/);
});
