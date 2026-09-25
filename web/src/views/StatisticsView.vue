<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Api, apiError } from "../api";

const props = defineProps({ navigationObscured: { type: Boolean, default: false } });

const rangeDays = ref(30);
const folderUuid = ref("");
const comparePrevious = ref(true);
const trendMetric = ref("tokens");
const hiddenTrendSeries = ref([]);
const latencySource = ref("all");
const selectedModels = ref([]);
const selectedMetrics = ref(["requests", "latency", "tokens"]);
const trendHoverIndex = ref(-1);
const qualityHoverIndex = ref(-1);
const modelHover = ref({ metric: "", index: -1 });
const thinkingHoverIndex = ref(-1);
const modelCompareRef = ref(null);
const heatHover = ref(null);
const data = ref(null);
const loading = ref(true);
const refreshing = ref(false);
const error = ref("");
let requestSequence = 0;

const COLORS = ["#087af5", "#7656d6", "#16a36a", "#df8b0b", "#0e98a7", "#e5484d", "#4f6b87", "#b05fbe"];
const RANGE_OPTIONS = [{ days: 1, label: "今天" }, { days: 3, label: "3 天" }, { days: 7, label: "7 天" }, { days: 30, label: "30 天" }, { days: 90, label: "90 天" }];
const METRICS = {
  requests: { label: "请求数", unit: "次", value: (row) => Number(row?.modelCalls || 0), format: (value) => formatNumber(value) },
  latency: { label: "平均耗时", unit: "秒", value: (row) => Number(row?.avgTotalMs || 0) / 1000, format: (value) => `${formatDecimal(value, 1)}s` },
  tokens: { label: "Token 消耗", unit: "Token", value: (row) => tokenTotal(row), format: formatCompact },
  cache: { label: "缓存率", unit: "%", value: (row) => Number(row?.cacheRate || 0), format: (value) => `${formatDecimal(value, 1)}%` },
  cost: { label: "账本花费", unit: "USD", value: (row) => Number(row?.costUsd || 0), format: (value) => `$${formatDecimal(value, 2)}` },
};
const ERROR_LABELS = {
  server_error: "上游服务异常",
  unknown: "未知错误",
  rate_limit: "请求频率受限",
  timeout: "请求超时",
  timeouterror: "请求超时",
  "summary timeout": "摘要生成超时",
  overloaded: "上游负载过高",
  model_not_found: "模型不存在",
  context_overflow: "上下文超出限制",
  format: "响应格式异常",
  billing: "账户额度或计费异常",
  openbearllmerror: "模型调用异常",
  未分类: "未分类错误",
};
const TOOL_LABELS = {
  Bash: "执行脚本", Read: "读取文件", Write: "写入文件", Edit: "编辑文件", EditBatch: "批量编辑文件",
  Process: "管理进程", Memory: "全局记忆", TaskMemory: "会话记忆", History: "查询历史", UserInteraction: "用户交互",
  Agent: "启动 Agent", AgentContinue: "继续 Agent", AgentMessage: "指导 Agent", AgentInfo: "查看 Agent", AgentWait: "等待 Agent",
  AgentStop: "停止 Agent", AgentPlanDecision: "Agent 计划决策", OpenBearControl: "控制 OpenBear", WebSearch: "搜索网页",
  WebExtract: "提取网页", BashStatus: "查看脚本状态",
};
const PLAYWRIGHT_LABELS = {
  browser_navigate: "浏览器导航", browser_click: "点击页面", browser_snapshot: "读取页面", browser_find: "查找页面内容",
  browser_evaluate: "执行页面脚本", browser_run_code_unsafe: "运行浏览器脚本", browser_tabs: "管理浏览器标签页",
  browser_wait_for: "等待页面", browser_take_screenshot: "页面截图", browser_console_messages: "查看浏览器日志",
  browser_network_requests: "查看网络请求", browser_fill_form: "填写表单", browser_type: "输入文字", browser_resize: "调整浏览器窗口",
  browser_close: "关闭浏览器",
};

async function loadStatistics() {
  const sequence = ++requestSequence;
  if (!data.value) loading.value = true;
  refreshing.value = Boolean(data.value);
  error.value = "";
  try {
    const payload = await Api.statistics({ days: rangeDays.value, ...(folderUuid.value ? { folderUuid: folderUuid.value } : {}) });
    if (sequence !== requestSequence) return;
    data.value = payload;
    initializeModels();
  } catch (reason) {
    if (sequence !== requestSequence) return;
    error.value = apiError(reason);
    if (!data.value) ElMessage.error(`统计数据加载失败：${error.value}`);
  } finally {
    if (sequence === requestSequence) {
      loading.value = false;
      refreshing.value = false;
    }
  }
}
function initializeModels() {
  const available = new Set((data.value?.models || []).map((item) => item.model));
  const retained = selectedModels.value.filter((model) => available.has(model));
  selectedModels.value = retained.length ? retained : (data.value?.models || []).slice(0, 3).map((item) => item.model);
}
function changeFilter() {
  data.value = null;
  trendHoverIndex.value = -1;
  qualityHoverIndex.value = -1;
  modelHover.value = { metric: "", index: -1 };
  thinkingHoverIndex.value = -1;
  loadStatistics();
}
function selectRange(days) {
  if (rangeDays.value === days) return;
  rangeDays.value = days;
  changeFilter();
}
function toggleModel(model) {
  if (selectedModels.value.includes(model)) {
    if (selectedModels.value.length <= 1) return;
    selectedModels.value = selectedModels.value.filter((item) => item !== model);
    return;
  }
  selectedModels.value = [...selectedModels.value, model];
}
function toggleMetric(metric) {
  if (selectedMetrics.value.includes(metric)) {
    if (selectedMetrics.value.length > 1) selectedMetrics.value = selectedMetrics.value.filter((item) => item !== metric);
  } else {
    selectedMetrics.value = [...selectedMetrics.value, metric];
  }
}
function toggleTrendSeries(key) {
  const visible = trendDefinition.value.series.filter((item) => !hiddenTrendSeries.value.includes(item.key));
  if (!hiddenTrendSeries.value.includes(key) && visible.length <= 1) return;
  hiddenTrendSeries.value = hiddenTrendSeries.value.includes(key)
    ? hiddenTrendSeries.value.filter((item) => item !== key)
    : [...hiddenTrendSeries.value, key];
}

function formatNumber(value) { return Math.round(Number(value || 0)).toLocaleString("zh-CN"); }
function formatDecimal(value, digits = 1) { return Number(value || 0).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
function formatCompact(value) {
  const number = Number(value || 0);
  if (Math.abs(number) >= 1e9) return `${formatDecimal(number / 1e9, 1)}B`;
  if (Math.abs(number) >= 1e6) return `${formatDecimal(number / 1e6, 1)}M`;
  if (Math.abs(number) >= 1e3) return `${formatDecimal(number / 1e3, number >= 100000 ? 0 : 1)}K`;
  return formatNumber(number);
}
function formatMoney(value) { return `$${Number(value || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }
function formatDuration(value) {
  const ms = Number(value || 0);
  if (!ms) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${formatDecimal(ms / 1000, ms < 10000 ? 2 : 1)}s`;
  if (ms < 3600000) return `${formatDecimal(ms / 60000, 1)}m`;
  return `${formatDecimal(ms / 3600000, 1)}h`;
}
function inputTokenTotal(row) {
  return Number(row?.inputTokens || 0) + Number(row?.cacheReadTokens || 0) + Number(row?.cacheWriteTokens || 0);
}
function tokenTotal(row) { return inputTokenTotal(row) + Number(row?.outputTokens || 0); }
function shortDate(value) { return String(value || "").slice(5).replace("-", "/"); }
function fullDate(value) {
  if (!value) return "";
  return new Date(`${value}T00:00:00+08:00`).toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}
function updateTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" });
}
function bucketDate(value) { return new Date(value); }
function formatBucketTick(value) {
  const date = bucketDate(value);
  const hours = Number(data.value?.period?.bucketHours || 24);
  if (rangeDays.value === 1) return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" });
  if (hours < 24) return `${date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric", timeZone: "Asia/Shanghai" })} ${date.toLocaleTimeString("zh-CN", { hour: "2-digit", hour12: false, timeZone: "Asia/Shanghai" })}时`;
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric", timeZone: "Asia/Shanghai" });
}
function formatBucketRange(value) {
  if (!value) return "";
  const start = bucketDate(value);
  const hours = Number(data.value?.period?.bucketHours || 24);
  const end = new Date(start.getTime() + hours * 3600000 - 1);
  const dateLabel = start.toLocaleDateString("zh-CN", { month: "long", day: "numeric", timeZone: "Asia/Shanghai" });
  if (hours >= 24) return dateLabel;
  const startTime = start.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" });
  const endTime = end.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" });
  return `${dateLabel} ${startTime}–${endTime}`;
}
function median(values) {
  const sorted = values.map(Number).filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return 0;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}
function niceCeiling(value) {
  const amount = Math.max(1, Number(value || 0));
  const magnitude = 10 ** Math.floor(Math.log10(amount));
  const normalized = amount / magnitude;
  const step = [1, 2, 5, 10].find((candidate) => candidate >= normalized) || 10;
  return step * magnitude;
}
function smoothSparkline(values, width = 126, height = 34) {
  const rows = values.map(Number);
  const max = Math.max(1, ...rows);
  const pad = 2;
  const points = rows.map((value, index) => ({
    x: pad + index / Math.max(1, rows.length - 1) * (width - pad * 2),
    y: height - pad - value / max * (height - pad * 2),
  }));
  if (!points.length) return { width, height, path: "", area: "" };
  if (points.length === 1) points.push({ ...points[0], x: width - pad });
  let path = `M ${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`;
  for (let index = 0; index < points.length - 1; index += 1) {
    const p0 = points[Math.max(0, index - 1)], p1 = points[index], p2 = points[index + 1], p3 = points[Math.min(points.length - 1, index + 2)];
    const cp1x = p1.x + (p2.x - p0.x) / 6, cp1y = Math.max(pad, Math.min(height - pad, p1.y + (p2.y - p0.y) / 6));
    const cp2x = p2.x - (p3.x - p1.x) / 6, cp2y = Math.max(pad, Math.min(height - pad, p2.y - (p3.y - p1.y) / 6));
    path += ` C ${cp1x.toFixed(1)} ${cp1y.toFixed(1)}, ${cp2x.toFixed(1)} ${cp2y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
  }
  return { width, height, path, area: `${path} L ${points.at(-1).x.toFixed(1)} ${height - pad} L ${points[0].x.toFixed(1)} ${height - pad} Z` };
}
function summaryValue(row, key) { return key === "tokenTotal" ? tokenTotal(row) : Number(row?.[key] || 0); }
function deltaFor(key, lowerIsBetter = false) {
  const currentValue = summaryValue(data.value?.summary?.current, key);
  const previousValue = summaryValue(data.value?.summary?.previous, key);
  if (!comparePrevious.value || !previousValue) return null;
  const percent = (currentValue - previousValue) / previousValue * 100;
  return { value: `${percent >= 0 ? "↑" : "↓"} ${Math.abs(percent).toFixed(1)}%`, good: lowerIsBetter ? percent <= 0 : percent >= 0 };
}
function deltaClass(key, lowerIsBetter = false) { return deltaFor(key, lowerIsBetter)?.good === false ? "is-bad" : ""; }
function modelColor(model) { return COLORS[Math.max(0, selectedModels.value.indexOf(model)) % COLORS.length]; }
function modelChoiceColor(model) {
  const index = (data.value?.models || []).findIndex((item) => item.model === model);
  return COLORS[Math.max(0, index) % COLORS.length];
}
function metricColor(index) { return COLORS[index % COLORS.length]; }
function successRate(row) { return row?.successRate == null ? "—" : `${formatDecimal(row.successRate, 1)}%`; }
function errorLabel(type) { return ERROR_LABELS[String(type || "").trim().toLowerCase()] || `其他错误（${type || "未提供类型"}）`; }
function toolLabel(name) {
  const raw = String(name || "未知工具");
  let label = TOOL_LABELS[raw];
  if (!label && raw.startsWith("mcp__playwright__")) label = PLAYWRIGHT_LABELS[raw.slice("mcp__playwright__".length)] || "浏览器操作";
  if (!label && raw === "mcp__parrot__web_search") label = "搜索网页";
  if (!label && raw === "mcp__parrot__web_fetch") label = "读取网页";
  if (!label && raw.startsWith("mcp__parrot__image_")) label = "生成或编辑图片";
  if (!label && raw.startsWith("mcp__parrot__video_")) label = "生成或查询视频";
  if (!label && raw.startsWith("mcp__")) label = "MCP 扩展工具";
  return label ? `${raw}（${label}）` : raw;
}

const current = computed(() => data.value?.summary?.current || {});
const previous = computed(() => data.value?.summary?.previous || {});
const folders = computed(() => data.value?.folders || []);
const modelGroups = computed(() => {
  const groups = new Map();
  for (const item of data.value?.models || []) {
    const model = String(item.model || "未知模型");
    const separator = model.indexOf("/");
    const provider = separator > 0 ? model.slice(0, separator) : "其他";
    const name = separator > 0 ? model.slice(separator + 1) : model;
    if (!groups.has(provider)) groups.set(provider, []);
    groups.get(provider).push({ model, name });
  }
  return [...groups].map(([provider, models]) => ({ provider, models }));
});
const timelineRows = computed(() => data.value?.timeline || data.value?.daily || []);
const periodLabel = computed(() => {
  if (!data.value?.period) return "";
  if (rangeDays.value === 1) return `${fullDate(data.value.period.start)} · 00:00–23:59`;
  return `${fullDate(data.value.period.start)} – ${fullDate(data.value.period.end)}`;
});
const bucketCaption = computed(() => {
  const hours = Number(data.value?.period?.bucketHours || 24);
  return hours >= 24 ? "每天" : hours === 1 ? "每小时" : `每 ${hours} 小时`;
});
const dailyRows = computed(() => [...(data.value?.daily || [])].reverse());
const maxHeat = computed(() => Math.max(1, ...(data.value?.heatmap || []).flat().map(Number)));
const heatStyle = (count) => ({ background: `color-mix(in srgb, var(--stat-blue) ${Math.round(7 + Number(count || 0) / maxHeat.value * 88)}%, var(--stat-surface-soft))` });
const translatedErrors = computed(() => {
  const rows = (data.value?.errors || []).map((item) => ({ ...item, label: errorLabel(item.type) }));
  const listed = rows.reduce((sum, item) => sum + Number(item.count || 0), 0);
  const remaining = Math.max(0, Number(current.value.modelFailed || 0) - listed);
  if (remaining) rows.push({ type: "other", label: "其他未细分错误", count: remaining });
  return rows;
});

const kpis = computed(() => [
  { label: "活跃会话", value: formatNumber(current.value.activeConversations), unit: "", key: "activeConversations", note: "有用户提交的会话", accent: "blue", icon: "chat" },
  { label: "用户对话", value: formatNumber(current.value.userTurns), unit: "", key: "userTurns", note: "外部用户提交口径", accent: "cyan", icon: "message" },
  { label: "Token 用量", value: formatCompact(tokenTotal(current.value)), unit: "", key: "tokenTotal", note: `输入 ${formatCompact(inputTokenTotal(current.value))} · 输出 ${formatCompact(current.value.outputTokens)}`, accent: "violet", icon: "token" },
  { label: "账本花费", value: formatMoney(current.value.costUsd), unit: "", key: "costUsd", note: "模型调用账本", accent: "amber", icon: "money", lower: true },
  { label: "Token 缓存率", value: current.value.cacheRate == null ? "—" : formatDecimal(current.value.cacheRate, 1), unit: current.value.cacheRate == null ? "" : "%", key: "cacheRate", note: `缓存 ${formatCompact(current.value.cacheReadTokens)}`, accent: "cyan", icon: "cache" },
  { label: "成功请求", value: formatNumber(current.value.modelOk), unit: "", key: "modelOk", note: `${successRate(current.value)} · ${formatNumber(current.value.modelFailed)} 次失败`, accent: "green", icon: "check" },
  { label: "工具调用", value: formatNumber(current.value.toolCalls), unit: "", key: "toolCalls", note: `${successRate({ successRate: current.value.toolCalls ? current.value.toolOk / current.value.toolCalls * 100 : null })} 已记录成功`, accent: "blue", icon: "tool" },
  { label: "平均模型请求耗时", value: current.value.avgTotalMs ? formatDecimal(current.value.avgTotalMs / 1000, 1) : "—", unit: current.value.avgTotalMs ? "s" : "", key: "avgTotalMs", note: "调用模型 → 流式响应结束", accent: "red", icon: "clock", lower: true },
]);

const trendDefinition = computed(() => {
  if (trendMetric.value === "cost") return { subtitle: `${bucketCaption.value}已记录的模型调用账本花费`, series: [{ key: "costUsd", label: "账本花费", color: "#df8b0b", format: formatMoney }] };
  if (trendMetric.value === "requests") return { subtitle: `${bucketCaption.value}成功与失败的模型请求`, series: [{ key: "modelOk", label: "成功", color: "#16a36a", format: formatNumber }, { key: "modelFailed", label: "失败", color: "#e5484d", format: formatNumber }] };
  return { subtitle: `${bucketCaption.value}输入、输出与缓存率；输入包含缓存读写`, series: [
    { key: "inputTotalTokens", label: "输入", color: "#087af5", format: formatCompact, value: inputTokenTotal },
    { key: "outputTokens", label: "输出", color: "#7656d6", format: formatCompact },
    { key: "cacheRate", label: "缓存", color: "#0e98a7", format: (value) => `${formatDecimal(value, 1)}%`, axis: "right" },
  ] };
});
const activeTrendSeries = computed(() => trendDefinition.value.series.filter((item) => !hiddenTrendSeries.value.includes(item.key)));
const trendChart = computed(() => lineChart(timelineRows.value, activeTrendSeries.value, 760, 270));
const trendHoverRow = computed(() => trendHoverIndex.value >= 0 ? timelineRows.value[trendHoverIndex.value] : null);

function seriesValue(series, row) { return Number(series.value ? series.value(row) : row?.[series.key] || 0); }
function lineChart(rows, sourceSeries, width = 560, height = 210) {
  const hasPrimary = sourceSeries.some((item) => item.axis !== "right");
  const series = sourceSeries.map((item) => ({ ...item, chartAxis: hasPrimary ? (item.axis || "left") : "left" }));
  const hasRight = series.some((item) => item.chartAxis === "right");
  const left = 50, right = hasRight ? 45 : 16, top = 14, bottom = 31;
  const chartWidth = width - left - right, chartHeight = height - top - bottom;
  const primaryValues = series.filter((item) => item.chartAxis === "left").flatMap((item) => rows.map((row) => seriesValue(item, row)));
  const rightValues = series.filter((item) => item.chartAxis === "right").flatMap((item) => rows.map((row) => seriesValue(item, row)));
  const max = Math.max(1, ...primaryValues) * 1.08;
  const rightMax = Math.max(100, ...rightValues);
  const x = (index) => left + (rows.length <= 1 ? chartWidth / 2 : index / (rows.length - 1) * chartWidth);
  const y = (value, axis = "left") => top + chartHeight - Number(value || 0) / (axis === "right" ? rightMax : max) * chartHeight;
  const tickCount = Math.min(5, rows.length);
  const tickIndexes = [...new Set(Array.from({ length: tickCount }, (_, index) => Math.round(index * Math.max(0, rows.length - 1) / Math.max(1, tickCount - 1))))];
  const normalizedSeries = series.map((item) => {
    const values = rows.map((row) => seriesValue(item, row));
    const points = values.map((value, index) => ({ x: x(index), y: y(value, item.chartAxis), value, bucket: rows[index]?.bucket || rows[index]?.date }));
    const baseline = y(0, item.chartAxis);
    return {
      ...item,
      points: points.map((point) => ({ ...point, text: `${point.x.toFixed(1)},${point.y.toFixed(1)}` })),
      area: points.length ? `${points[0].x.toFixed(1)},${baseline.toFixed(1)} ${points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ")} ${points.at(-1).x.toFixed(1)},${baseline.toFixed(1)}` : "",
      peak: Math.max(0, ...values),
      median: median(values),
    };
  });
  return {
    width, height, left, right, top, bottom, chartWidth, chartHeight, max, rightMax, hasRight,
    grid: Array.from({ length: 5 }, (_, index) => ({ y: top + index / 4 * chartHeight, label: series.find((item) => item.chartAxis === "left")?.format(max * (4 - index) / 4) || "0" })),
    rightGrid: hasRight ? Array.from({ length: 5 }, (_, index) => ({ y: top + index / 4 * chartHeight, label: `${Math.round(rightMax * (4 - index) / 4)}%` })) : [],
    dates: tickIndexes.map((index) => ({ x: x(index), label: formatBucketTick(rows[index]?.bucket || rows[index]?.date), index })),
    series: normalizedSeries,
  };
}
function hoverIndex(event, chart, count) {
  const svg = event.currentTarget.querySelector("svg");
  if (!svg || count <= 0) return -1;
  const bounds = svg.getBoundingClientRect();
  const viewX = (event.clientX - bounds.left) / Math.max(1, bounds.width) * chart.width;
  const ratio = Math.max(0, Math.min(1, (viewX - chart.left) / chart.chartWidth));
  return Math.round(ratio * Math.max(0, count - 1));
}
function tooltipStyle(chart, index, count) {
  const point = count <= 1 ? chart.left + chart.chartWidth / 2 : chart.left + index / (count - 1) * chart.chartWidth;
  return { left: `${point / chart.width * 100}%` };
}
function updateTrendHover(event) { trendHoverIndex.value = hoverIndex(event, trendChart.value, timelineRows.value.length); }
function updateModelHover(event, metric) { modelHover.value = { metric, index: hoverIndex(event, modelCharts.value[metric], timelineRows.value.length) }; }
function modelHoverRow(metric) {
  const index = modelHover.value.metric === metric ? modelHover.value.index : -1;
  if (index < 0) return null;
  const chart = modelCharts.value[metric];
  return { index, bucket: timelineRows.value[index]?.bucket || timelineRows.value[index]?.date, series: chart.series.map((item) => ({ ...item, point: item.points[index] })).sort((a, b) => Number(b.point?.value || 0) - Number(a.point?.value || 0)) };
}

function modelMetricRows(metric) {
  const byKey = new Map((data.value?.modelTimeline || data.value?.modelDaily || []).map((row) => [`${row.bucket || row.date}\n${row.model}`, row]));
  return selectedModels.value.map((model) => ({
    model,
    color: modelColor(model),
    rows: timelineRows.value.map((bucket) => {
      const bucketKey = bucket.bucket || bucket.date;
      const source = byKey.get(`${bucketKey}\n${model}`) || {};
      return { bucket: bucketKey, metricValue: METRICS[metric].value(source) };
    }),
  }));
}
function buildModelChart(metric) {
  const modelSeries = modelMetricRows(metric);
  const transformed = timelineRows.value.map((bucket, index) => Object.fromEntries([["bucket", bucket.bucket || bucket.date], ...modelSeries.map((series) => [series.model, series.rows[index]?.metricValue || 0])]));
  return lineChart(transformed, modelSeries.map((series) => ({ key: series.model, label: series.model, color: series.color, format: METRICS[metric].format })), 620, 230);
}
const modelCharts = computed(() => Object.fromEntries(selectedMetrics.value.map((metric) => [metric, buildModelChart(metric)])));
function modelChartSummary(metric) {
  const chart = modelCharts.value[metric];
  const points = chart.series.flatMap((series) => series.points.map((point) => ({ model: series.label, value: point.value })));
  const peak = points.sort((a, b) => b.value - a.value)[0] || { model: "—", value: 0 };
  return { peak, median: median(points.map((point) => point.value).filter((value) => value > 0)) };
}
function aggregateModels(rows, label) {
  const result = { model: label, modelCalls: 0, modelOk: 0, modelFailed: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, costUsd: 0 };
  for (const row of rows) for (const key of Object.keys(result)) if (key !== "model") result[key] += Number(row?.[key] || 0);
  const known = result.modelOk + result.modelFailed;
  result.successRate = known ? result.modelOk / known * 100 : null;
  return result;
}
const modelRankRows = computed(() => {
  const models = data.value?.models || [];
  const top = models.slice(0, 6);
  const rest = models.slice(6);
  const baseRows = rest.length ? [...top, aggregateModels(rest, `其他 ${rest.length} 个模型`)] : top;
  const topNames = new Set(top.map((row) => row.model));
  const timeline = data.value?.modelTimeline || [];
  const byBucketModel = new Map(timeline.map((row) => [`${row.bucket}\n${row.model}`, row]));
  const otherByBucket = new Map();
  if (rest.length) for (const row of timeline) if (!topNames.has(row.model)) otherByBucket.set(row.bucket, Number(otherByBucket.get(row.bucket) || 0) + Number(row.modelCalls || 0));
  return baseRows.map((row, index) => {
    const isOther = rest.length > 0 && index === baseRows.length - 1;
    const values = timelineRows.value.map((bucket) => {
      const bucketKey = bucket.bucket || bucket.date;
      return isOther ? Number(otherByBucket.get(bucketKey) || 0) : Number(byBucketModel.get(`${bucketKey}\n${row.model}`)?.modelCalls || 0);
    });
    return { ...row, isOther, color: COLORS[index % COLORS.length], share: current.value.modelCalls ? Number(row.modelCalls || 0) / Number(current.value.modelCalls) * 100 : 0, sparkline: smoothSparkline(values) };
  });
});
const modelDistributionStyle = computed(() => {
  let cursor = 0;
  const stops = modelRankRows.value.map((row) => {
    const start = cursor;
    cursor += row.share;
    return `${row.color} ${start.toFixed(2)}% ${Math.min(100, cursor).toFixed(2)}%`;
  });
  return { background: stops.length ? `conic-gradient(${stops.join(",")})` : "var(--stat-surface-soft)" };
});
async function focusModel(model) {
  if (!model) return;
  selectedModels.value = [model];
  await nextTick();
  modelCompareRef.value?.scrollIntoView({ behavior: "smooth", block: "start" });
}

const quality = computed(() => {
  const ok = Number(current.value.modelOk || 0), failed = Number(current.value.modelFailed || 0), total = ok + failed;
  const rate = total ? ok / total * 100 : 0;
  return { ok, failed, total, rate, dash: `${Math.max(0, rate / 100 * 307.9)} 307.9` };
});
const qualityChart = computed(() => {
  const rows = timelineRows.value;
  const width = 350, height = 150, left = 37, right = 10, top = 18, bottom = 27;
  const chartWidth = width - left - right, chartHeight = height - top - bottom;
  const max = niceCeiling(Math.max(1, ...rows.map((row) => Number(row.modelOk || 0) + Number(row.modelFailed || 0))));
  const slot = chartWidth / Math.max(1, rows.length), barWidth = Math.max(3, Math.min(13, slot * .64));
  const bars = rows.map((row, index) => {
    const ok = Number(row.modelOk || 0), failed = Number(row.modelFailed || 0), total = ok + failed;
    const okHeight = ok / max * chartHeight, failedHeight = failed / max * chartHeight;
    return { x: left + index * slot + (slot - barWidth) / 2, slotX: left + index * slot, slotWidth: slot, width: barWidth, ok, failed, total, okHeight, failedHeight, okY: top + chartHeight - okHeight, failedY: top + chartHeight - okHeight - failedHeight, bucket: row.bucket || row.date, rate: total ? ok / total * 100 : null };
  });
  const peak = bars.reduce((best, item, index) => item.total > (best?.item.total || -1) ? { item, index } : best, null);
  const tickCount = Math.min(4, rows.length);
  const tickIndexes = [...new Set(Array.from({ length: tickCount }, (_, index) => Math.round(index * Math.max(0, rows.length - 1) / Math.max(1, tickCount - 1))))];
  return {
    width, height, left, right, top, bottom, chartWidth, chartHeight, max, bars, peak,
    grid: Array.from({ length: 4 }, (_, index) => ({ y: top + index / 3 * chartHeight, label: formatNumber(max * (3 - index) / 3) })),
    dates: tickIndexes.map((index) => ({ x: bars[index]?.x + barWidth / 2, label: formatBucketTick(rows[index]?.bucket || rows[index]?.date) })),
  };
});
const qualityHover = computed(() => qualityHoverIndex.value >= 0 ? qualityChart.value.bars[qualityHoverIndex.value] : null);
function updateQualityHover(event) { qualityHoverIndex.value = hoverIndex(event, qualityChart.value, timelineRows.value.length); }
const latency = computed(() => data.value?.latency?.[latencySource.value] || { calls: 0, buckets: [0, 0, 0, 0, 0, 0] });
const latencyStages = computed(() => {
  const item = latency.value;
  if (!item.timingSamples || item.avgFirstTokenMs == null || item.avgConnectMs == null) return { connect: null, wait: null, generate: null };
  return { connect: item.avgConnectMs, wait: Math.max(0, item.avgFirstTokenMs - item.avgConnectMs), generate: Math.max(0, Number(item.avgTimedTotalMs || 0) - item.avgFirstTokenMs) };
});
const latencyStageItems = computed(() => {
  const total = Number(latency.value.avgTimedTotalMs || 0);
  return [
    { key: "connect", index: "01", label: "建立连接", description: "请求发出 → 上游连接就绪", color: "var(--stat-cyan)", value: latencyStages.value.connect },
    { key: "wait", index: "02", label: "等待首字", description: "连接就绪 → 收到首个 Token", color: "var(--stat-violet)", value: latencyStages.value.wait },
    { key: "generate", index: "03", label: "生成回答", description: "首字出现 → 流式响应结束", color: "var(--stat-blue)", value: latencyStages.value.generate },
  ].map((item) => ({ ...item, share: item.value == null || !total ? null : Math.max(0, Number(item.value)) / total * 100 }));
});
const latencyBuckets = computed(() => {
  const labels = ["5 秒内", "5–15 秒", "15–30 秒", "30–60 秒", "1–2 分钟", "2 分钟以上"];
  const values = latency.value.buckets || [];
  const max = Math.max(1, ...values.map(Number));
  const total = Math.max(1, Number(latency.value.calls || 0));
  return labels.map((label, index) => ({ label, value: Number(values[index] || 0), width: Number(values[index] || 0) / max * 100, percent: Number(values[index] || 0) / total * 100 }));
});
const topLatencyBucket = computed(() => [...latencyBuckets.value].sort((a, b) => b.value - a.value)[0]);
const slowRequests = computed(() => latencyBuckets.value.slice(4).reduce((sum, item) => sum + item.value, 0));
const latencyPercentiles = computed(() => [
  { label: "总耗时", data: latency.value?.percentiles?.total || {} },
  { label: "首个 Token", data: latency.value?.percentiles?.firstToken || {} },
]);
const maxToolCalls = computed(() => Math.max(1, ...(data.value?.tools || []).slice(0, 7).map((item) => Number(item.calls || 0))));

const thinkingChart = computed(() => {
  const rows = timelineRows.value;
  const width = 520, height = 164, left = 45, right = 12, top = 12, bottom = 31;
  const chartWidth = width - left - right, chartHeight = height - top - bottom;
  const values = rows.map((row) => Number(row.reasoningMs || 0));
  const max = Math.max(1, ...values);
  const xStep = chartWidth / Math.max(1, rows.length);
  const barWidth = Math.max(2, Math.min(14, xStep * .62));
  const bars = rows.map((row, index) => ({ x: left + index * xStep + (xStep - barWidth) / 2, y: top + chartHeight - values[index] / max * chartHeight, width: barWidth, height: Math.max(values[index] ? 3 : 1, values[index] / max * chartHeight), value: values[index], bucket: row.bucket || row.date, observedRuns: Number(row.observedRuns || 0) }));
  const tickCount = Math.min(5, rows.length);
  const tickIndexes = [...new Set(Array.from({ length: tickCount }, (_, index) => Math.round(index * Math.max(0, rows.length - 1) / Math.max(1, tickCount - 1))))];
  return {
    width, height, left, right, top, bottom, chartWidth, chartHeight, max, bars,
    grid: Array.from({ length: 4 }, (_, index) => ({ y: top + index / 3 * chartHeight, label: formatDuration(max * (3 - index) / 3) })),
    dates: tickIndexes.map((index) => ({ x: bars[index]?.x + barWidth / 2, label: formatBucketTick(rows[index]?.bucket || rows[index]?.date) })),
    peak: Math.max(0, ...values), median: median(values.filter((value) => value > 0)),
  };
});
const thinkingHover = computed(() => thinkingHoverIndex.value >= 0 ? thinkingChart.value.bars[thinkingHoverIndex.value] : null);
function updateThinkingHover(event) { thinkingHoverIndex.value = hoverIndex(event, thinkingChart.value, timelineRows.value.length); }

const weekLabels = ["一", "二", "三", "四", "五", "六", "日"];
function updateHeatHover(event, weekday, hour, count) {
  const cellBounds = event.currentTarget.getBoundingClientRect();
  const viewportWidth = document.documentElement.clientWidth;
  const viewportHeight = document.documentElement.clientHeight;
  const pointerX = Number.isFinite(event.clientX) && event.clientX > 0 ? event.clientX : cellBounds.left + cellBounds.width / 2;
  const showAbove = cellBounds.bottom + 64 > viewportHeight;
  heatHover.value = {
    left: Math.max(86, Math.min(viewportWidth - 86, pointerX)),
    top: showAbove ? cellBounds.top - 8 : cellBounds.bottom + 8,
    above: showAbove,
    label: `周${weekLabels[weekday]} ${String(hour).padStart(2, "0")}:00–${String(hour).padStart(2, "0")}:59`,
    count: Number(count || 0),
  };
}

onMounted(() => loadStatistics());
watch(() => props.navigationObscured, async () => { await nextTick(); });
watch(trendMetric, () => { hiddenTrendSeries.value = []; trendHoverIndex.value = -1; });
</script>

<template>
  <div class="statistics-view">
    <header class="statistics-header">
      <div class="statistics-title">
        <div class="title-line">
          <h1>数据统计</h1>
          <span v-if="refreshing" class="refreshing-chip"><i></i>更新中</span>
        </div>
        <p>掌握会话成本、请求质量与 Agent 工作效率</p>
      </div>
      <div class="statistics-filters">
        <label class="select-shell">
          <span class="sr-only">统计目录</span>
          <select v-model="folderUuid" @change="changeFilter">
            <option value="">全部目录</option>
            <option value="__temporary__">临时会话</option>
            <option v-for="folder in folders" :key="folder.uuid" :value="folder.uuid">{{ `${'　'.repeat(folder.depth)}${folder.name}` }}</option>
          </select>
        </label>
        <div class="range-tags" role="group" aria-label="统计时间范围"><button v-for="item in RANGE_OPTIONS" :key="item.days" type="button" :class="{active:rangeDays===item.days}" :aria-pressed="rangeDays===item.days" @click="selectRange(item.days)">{{ item.label }}</button></div>
        <button class="compare-toggle" :class="{ on: comparePrevious }" type="button" :aria-pressed="comparePrevious" :aria-label="`上周期对比，当前${comparePrevious ? '已开启' : '已关闭'}`" @click="comparePrevious = !comparePrevious">
          <span class="compare-glyph" aria-hidden="true"><svg viewBox="0 0 24 24"><path class="current-line" d="M3 16.5 8.2 11l4 3.1L21 5.7"/><path class="previous-line" d="M3 10.2 7.8 7l4.8 2.5 4.1-3.2"/></svg></span>
          <span class="compare-copy"><b>上周期对比</b><small>{{ comparePrevious ? '已叠加' : '未叠加' }}</small></span>
          <span class="compare-switch" aria-hidden="true"><i></i></span>
        </button>
      </div>
    </header>

    <div v-if="loading && !data" class="statistics-loading" aria-label="正在加载统计数据">
      <div class="loading-line"><span></span><i></i></div>
      <div class="loading-kpis"><i v-for="index in 8" :key="index"></i></div>
      <div class="loading-panels"><i></i><i></i></div>
    </div>

    <div v-else-if="error && !data" class="statistics-error">
      <span class="error-mark">!</span><h2>统计数据暂时无法读取</h2><p>{{ error }}</p><button type="button" @click="loadStatistics()">重新加载</button>
    </div>

    <div v-else class="statistics-scroll">
      <div class="summary-line">
        <div class="summary-copy"><strong>{{ periodLabel }}</strong> · {{ folderUuid ? '所选目录' : '所有会话' }}与 Agent 请求</div>
        <div class="live-chip">最后更新于 {{ updateTime(data?.generatedAt) }}</div>
      </div>

      <section class="kpi-grid" aria-label="核心指标">
        <article v-for="item in kpis" :key="item.label" class="kpi-card" :data-accent="item.accent">
          <div class="kpi-top"><span>{{ item.label }}</span><span class="kpi-icon" aria-hidden="true">
            <svg v-if="item.icon === 'chat'" viewBox="0 0 24 24"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3 1.8-5A7 7 0 0 1 3 12V8a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/></svg>
            <svg v-else-if="item.icon === 'message'" viewBox="0 0 24 24"><path d="M5 5h14v11H9l-4 3Z"/><path d="M9 9h6M9 12h4"/></svg>
            <svg v-else-if="item.icon === 'token'" viewBox="0 0 24 24"><path d="M12 3v18M7 6.5h8.2a3.3 3.3 0 0 1 0 6.5H9a3.5 3.5 0 0 0 0 7h8"/></svg>
            <svg v-else-if="item.icon === 'money'" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M15.5 8.5c-.6-.8-1.7-1.3-3.2-1.3-1.7 0-3 .8-3 2.1 0 3.1 6.2 1.3 6.2 4.6 0 1.4-1.3 2.4-3.4 2.4-1.5 0-2.8-.5-3.6-1.4M12 5.5v13"/></svg>
            <svg v-else-if="item.icon === 'cache'" viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>
            <svg v-else-if="item.icon === 'check'" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>
            <svg v-else-if="item.icon === 'tool'" viewBox="0 0 24 24"><path d="m14 7 3-3 3 3-3 3M4 17l6-6 3 3-6 6H4Z"/></svg>
            <svg v-else viewBox="0 0 24 24"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l3 2M9 3h6"/></svg>
          </span></div>
          <div class="kpi-value">{{ item.value }}<small>{{ item.unit }}</small></div>
          <div class="kpi-foot"><span v-if="deltaFor(item.key, item.lower)" class="delta" :class="deltaClass(item.key, item.lower)">{{ deltaFor(item.key, item.lower).value }}</span><span>{{ item.note }}</span></div>
        </article>
      </section>

      <section class="dashboard-grid">
        <article class="panel trend-panel">
          <div class="panel-head"><div class="panel-title"><h2>用量趋势</h2><p>{{ trendDefinition.subtitle }}</p></div><div class="segmented"><button v-for="item in [{key:'tokens',label:'Tokens'},{key:'cost',label:'花费'},{key:'requests',label:'请求'}]" :key="item.key" type="button" :class="{active:trendMetric===item.key}" @click="trendMetric=item.key">{{ item.label }}</button></div></div>
          <div class="chart-stat-strip">
            <span v-for="series in trendChart.series" :key="series.key"><i :style="{background:series.color}"></i><b>{{ series.label }}</b><em>峰值 {{ series.format(series.peak) }}</em><em>中位数 {{ series.format(series.median) }}</em></span>
          </div>
          <div class="chart-wrap" @pointermove="updateTrendHover" @pointerleave="trendHoverIndex=-1">
            <svg :viewBox="`0 0 ${trendChart.width} ${trendChart.height}`" role="img" aria-label="用量趋势图">
              <g v-for="line in trendChart.grid" :key="line.y"><line :x1="trendChart.left" :x2="trendChart.left+trendChart.chartWidth" :y1="line.y" :y2="line.y" class="chart-grid-line"/><text :x="trendChart.left-7" :y="line.y+3" class="chart-axis-label" text-anchor="end">{{ line.label }}</text></g>
              <text v-for="line in trendChart.rightGrid" :key="`right-${line.y}`" :x="trendChart.left+trendChart.chartWidth+7" :y="line.y+3" class="chart-axis-label" text-anchor="start">{{ line.label }}</text>
              <text v-for="tick in trendChart.dates" :key="tick.x" :x="tick.x" :y="trendChart.height-5" class="chart-axis-label" :text-anchor="tick.x===trendChart.left?'start':tick.x===trendChart.left+trendChart.chartWidth?'end':'middle'">{{ tick.label }}</text>
              <line v-if="trendHoverIndex>=0" :x1="trendChart.series[0]?.points[trendHoverIndex]?.x" :x2="trendChart.series[0]?.points[trendHoverIndex]?.x" :y1="trendChart.top" :y2="trendChart.top+trendChart.chartHeight" class="chart-hover-line"/>
              <g v-for="series in trendChart.series" :key="series.key"><polygon :points="series.area" :fill="series.color" class="chart-area"/><polyline :points="series.points.map(point=>point.text).join(' ')" fill="none" :stroke="series.color" stroke-width="2.7" stroke-linecap="round" stroke-linejoin="round"/><circle v-for="point in series.points" :key="`${series.key}-${point.bucket}`" :cx="point.x" :cy="point.y" r="2.8" :fill="series.color" class="chart-point"><title>{{ `${formatBucketRange(point.bucket)} · ${series.label} ${series.format(point.value)}` }}</title></circle><circle v-if="trendHoverIndex>=0" :cx="series.points[trendHoverIndex]?.x" :cy="series.points[trendHoverIndex]?.y" r="4.6" :fill="series.color" class="chart-active-point"/></g>
            </svg>
            <div v-if="trendHoverRow" class="chart-tooltip" :class="{'is-start':trendHoverIndex<timelineRows.length*.18,'is-edge':trendHoverIndex>timelineRows.length*.72}" :style="tooltipStyle(trendChart,trendHoverIndex,timelineRows.length)"><strong>{{ formatBucketRange(trendHoverRow.bucket || trendHoverRow.date) }}</strong><span v-for="series in trendChart.series" :key="series.key"><i :style="{background:series.color}"></i><em>{{ series.label }}</em><b>{{ series.format(seriesValue(series,trendHoverRow)) }}</b></span><template v-if="trendMetric==='tokens'"><small class="tooltip-section">输入拆分</small><span><i class="fresh-dot"></i><em>Fresh 输入</em><b>{{ formatCompact(trendHoverRow.inputTokens) }}</b></span><span><i class="cache-read-dot"></i><em>缓存读取</em><b>{{ formatCompact(trendHoverRow.cacheReadTokens) }}</b></span><span><i class="cache-write-dot"></i><em>缓存写入</em><b>{{ formatCompact(trendHoverRow.cacheWriteTokens) }}</b></span><span><i class="cost-dot"></i><em>当前桶花费</em><b>{{ formatMoney(trendHoverRow.costUsd) }}</b></span></template></div>
          </div>
          <div class="chart-legend legend-buttons"><button v-for="series in trendDefinition.series" :key="series.key" type="button" :class="{active:!hiddenTrendSeries.includes(series.key)}" :aria-pressed="!hiddenTrendSeries.includes(series.key)" @click="toggleTrendSeries(series.key)"><i :style="{background:series.color}"></i>{{ series.label }}</button></div>
        </article>

        <article class="panel quality-panel">
          <div class="panel-head"><div class="panel-title"><h2>请求质量</h2><p>已判定成功与失败的模型请求</p></div><span class="health-badge" :class="{warn:quality.rate<95}">{{ quality.rate >= 95 ? '健康' : '需关注' }}</span></div>
          <div class="quality-body">
            <div class="donut"><svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="49" class="donut-base"/><circle cx="60" cy="60" r="49" class="donut-ok" :stroke-dasharray="quality.dash"/></svg><div><strong>{{ formatDecimal(quality.rate,1) }}%</strong><span>请求成功率</span></div></div>
            <div class="outcome-list"><div><i class="ok"></i><span>成功模型请求</span><b>{{ formatNumber(quality.ok) }}</b></div><div class="failure-outcome"><i class="failed"></i><span>失败模型请求</span><span class="failure-trigger" tabindex="0" aria-label="查看失败原因"><b>{{ formatNumber(quality.failed) }}</b><span class="failure-tooltip"><strong>失败原因 · {{ formatNumber(quality.failed) }} 次</strong><span v-for="item in translatedErrors" :key="item.type"><em>{{ item.label }}</em><b>{{ formatNumber(item.count) }}</b></span><span v-if="!translatedErrors.length" class="failure-empty">当前范围没有失败明细</span><small>悬浮内容来自模型调用记录中的错误分类</small></span></span></div><div><i class="retry"></i><span>发生重试</span><b>{{ formatNumber(current.modelRetries) }}</b></div><div><i class="muted"></i><span>全部模型调用</span><b>{{ formatNumber(current.modelCalls) }}</b></div></div>
          </div>
          <div class="quality-timeline" @pointermove="updateQualityHover" @pointerleave="qualityHoverIndex=-1">
            <div class="quality-chart-legend"><span><i class="ok"></i>成功 <b>{{ formatNumber(quality.ok) }}</b></span><span><i class="failed"></i>失败 <b>{{ formatNumber(quality.failed) }}</b></span></div>
            <svg :viewBox="`0 0 ${qualityChart.width} ${qualityChart.height}`" role="img" :aria-label="`${bucketCaption}成功与失败模型请求`"><g v-for="line in qualityChart.grid" :key="line.y"><line :x1="qualityChart.left" :x2="qualityChart.left+qualityChart.chartWidth" :y1="line.y" :y2="line.y" class="chart-grid-line"/><text :x="qualityChart.left-6" :y="line.y+3" class="chart-axis-label" text-anchor="end">{{ line.label }}</text></g><text v-for="tick in qualityChart.dates" :key="tick.x" :x="tick.x" :y="qualityChart.height-5" class="chart-axis-label" text-anchor="middle">{{ tick.label }}</text><g v-for="(bar,index) in qualityChart.bars" :key="bar.bucket"><rect :x="bar.x" :y="bar.okY" :width="bar.width" :height="bar.okHeight" rx="2" class="quality-bar-ok"/><rect :x="bar.x" :y="bar.failedY" :width="bar.width" :height="bar.failedHeight" rx="2" class="quality-bar-failed"/><rect :x="bar.slotX" :y="qualityChart.top" :width="bar.slotWidth" :height="qualityChart.chartHeight" fill="transparent" class="quality-hit-slot"/><text v-if="qualityChart.peak?.index===index && bar.total" :x="bar.x+bar.width/2" :y="Math.max(10,bar.failedY-5)" class="quality-peak-label" text-anchor="middle">{{ formatNumber(bar.total) }}</text></g></svg>
            <div v-if="qualityHover" class="chart-tooltip quality-tooltip" :class="{'is-start':qualityHoverIndex<timelineRows.length*.2,'is-edge':qualityHoverIndex>timelineRows.length*.7}" :style="tooltipStyle(qualityChart,qualityHoverIndex,timelineRows.length)"><strong>{{ formatBucketRange(qualityHover.bucket) }}</strong><span><i class="ok-dot"></i><em>成功请求</em><b>{{ formatNumber(qualityHover.ok) }}</b></span><span><i class="failed-dot"></i><em>失败请求</em><b>{{ formatNumber(qualityHover.failed) }}</b></span><span><i class="rate-dot"></i><em>成功率</em><b>{{ qualityHover.rate==null?'—':`${formatDecimal(qualityHover.rate,1)}%` }}</b></span></div>
          </div>
        </article>

        <article ref="modelCompareRef" class="panel model-compare-panel">
          <div class="panel-head"><div class="panel-title"><h2>模型趋势对比</h2><p>按提供商选择模型；不同单位保持独立图表，读数不互相干扰</p></div><span class="selection-summary"><i></i>{{ selectedModels.length }} 模型 · {{ selectedMetrics.length }} 指标</span></div>
          <div class="compare-controls">
            <div class="compare-group models-control">
              <div class="control-heading"><div><span>COMPARE SET</span><strong>要对比的模型</strong></div><small>点击加入或移出，至少保留一个</small></div>
              <div class="provider-models">
                <section v-for="group in modelGroups" :key="group.provider" class="provider-cluster">
                  <header class="provider-identity"><span>{{ group.provider.slice(0,1).toUpperCase() }}</span><div><strong :title="group.provider">{{ group.provider }}</strong><small>{{ group.models.length }} 个可用模型</small></div></header>
                  <div class="model-choice-list"><button v-for="item in group.models" :key="item.model" type="button" class="model-choice" :class="{active:selectedModels.includes(item.model)}" :style="{'--model-color':modelChoiceColor(item.model)}" :aria-pressed="selectedModels.includes(item.model)" :title="selectedModels.length<=1&&selectedModels.includes(item.model)?'至少保留一个模型':item.model" @click="toggleModel(item.model)"><i class="model-choice-swatch" aria-hidden="true"></i><span><b>{{ item.name }}</b><small>{{ selectedModels.includes(item.model) ? '已加入对比' : '未选择' }}</small></span><i class="model-choice-check" aria-hidden="true"><svg viewBox="0 0 12 12"><path d="m2.3 6.2 2.2 2.2 5.2-5"/></svg></i></button></div>
                </section>
              </div>
            </div>
            <div class="compare-group metrics-control">
              <div class="control-heading"><div><span>MEASURES</span><strong>显示指标</strong></div><small>每项独立成图</small></div>
              <div class="metric-choices"><button v-for="(meta,key) in METRICS" :key="key" type="button" :class="{active:selectedMetrics.includes(key)}" :aria-pressed="selectedMetrics.includes(key)" @click="toggleMetric(key)"><i aria-hidden="true"></i><span>{{ meta.label }}</span></button></div>
            </div>
          </div>
          <div class="model-chart-grid">
            <div v-for="metric in selectedMetrics" :key="metric" class="metric-chart-card" @pointermove="updateModelHover($event,metric)" @pointerleave="modelHover={metric:'',index:-1}">
              <div class="metric-chart-head"><strong>{{ METRICS[metric].label }}</strong><span>{{ METRICS[metric].unit }}</span></div>
              <div class="metric-insights"><span title="图中全部模型与时间桶的最高值"><b>峰值</b><em class="peak-model" :title="modelChartSummary(metric).peak.model">{{ modelChartSummary(metric).peak.model }}</em><strong>{{ METRICS[metric].format(modelChartSummary(metric).peak.value) }}</strong></span><span><b>中位数</b><strong>{{ METRICS[metric].format(modelChartSummary(metric).median) }}</strong></span></div>
              <svg :viewBox="`0 0 ${modelCharts[metric].width} ${modelCharts[metric].height}`" role="img" :aria-label="`${METRICS[metric].label}模型对比图`">
                <g v-for="line in modelCharts[metric].grid" :key="line.y"><line :x1="modelCharts[metric].left" :x2="modelCharts[metric].left+modelCharts[metric].chartWidth" :y1="line.y" :y2="line.y" class="chart-grid-line"/><text :x="modelCharts[metric].left-7" :y="line.y+3" class="chart-axis-label" text-anchor="end">{{ line.label }}</text></g>
                <text v-for="tick in modelCharts[metric].dates" :key="tick.x" :x="tick.x" :y="modelCharts[metric].height-5" class="chart-axis-label" :text-anchor="tick.x===modelCharts[metric].left?'start':tick.x===modelCharts[metric].left+modelCharts[metric].chartWidth?'end':'middle'">{{ tick.label }}</text>
                <line v-if="modelHoverRow(metric)" :x1="modelCharts[metric].series[0]?.points[modelHoverRow(metric).index]?.x" :x2="modelCharts[metric].series[0]?.points[modelHoverRow(metric).index]?.x" :y1="modelCharts[metric].top" :y2="modelCharts[metric].top+modelCharts[metric].chartHeight" class="chart-hover-line"/>
                <g v-for="series in modelCharts[metric].series" :key="series.key"><polygon :points="series.area" :fill="series.color" class="chart-area model-area"/><polyline :points="series.points.map(point=>point.text).join(' ')" fill="none" :stroke="series.color" stroke-width="2.65" stroke-linecap="round" stroke-linejoin="round"/><circle v-for="point in series.points" :key="`${series.key}-${point.bucket}`" :cx="point.x" :cy="point.y" r="2.5" :fill="series.color" class="chart-point"><title>{{ `${formatBucketRange(point.bucket)} · ${series.label} ${series.format(point.value)}` }}</title></circle><circle v-if="modelHoverRow(metric)" :cx="series.points[modelHoverRow(metric).index]?.x" :cy="series.points[modelHoverRow(metric).index]?.y" r="4.4" :fill="series.color" class="chart-active-point"/></g>
              </svg>
              <div v-if="modelHoverRow(metric)" class="chart-tooltip model-tooltip" :class="{'is-start':modelHoverRow(metric).index<timelineRows.length*.18,'is-edge':modelHoverRow(metric).index>timelineRows.length*.72}" :style="tooltipStyle(modelCharts[metric],modelHoverRow(metric).index,timelineRows.length)"><strong>{{ formatBucketRange(modelHoverRow(metric).bucket) }}</strong><span v-for="series in modelHoverRow(metric).series" :key="series.key"><i :style="{background:series.color}"></i><em :title="series.label">{{ series.label }}</em><b>{{ series.format(series.point?.value) }}</b></span></div>
              <div class="mini-legend"><span v-for="model in selectedModels" :key="model" :title="model"><i :style="{background:modelColor(model)}"></i>{{ model }}</span></div>
            </div>
          </div>
        </article>

        <article class="panel latency-panel">
          <div class="panel-head"><div class="panel-title"><h2>一次模型请求的时间花在哪</h2><p>从调用模型到流式响应结束；不含后续工具和再次调用</p></div><div class="segmented"><button v-for="item in [{key:'all',label:'全部'},{key:'main',label:'主会话'},{key:'agent',label:'Agent'}]" :key="item.key" type="button" :class="{active:latencySource===item.key}" @click="latencySource=item.key">{{ item.label }}</button></div></div>
          <div class="latency-stages">
            <div class="latency-overview">
              <div class="latency-overview-copy"><span><i aria-hidden="true"></i>{{ latency.timingSamples ? '完整计时样本平均耗时' : '平均单次模型请求耗时' }}</span><p>{{ latency.timingSamples ? '端到端时间被拆成三个连续阶段' : '当前来源缺少可拆分的完整阶段样本' }}</p></div>
              <div class="latency-overview-value"><strong>{{ formatDuration(latency.timingSamples ? latency.avgTimedTotalMs : latency.avgTotalMs) }}</strong><small v-if="latency.timingSamples">{{ formatNumber(latency.timingSamples) }} / {{ formatNumber(latency.calls) }} 次完整样本</small><small v-else>{{ formatNumber(latency.calls) }} 次请求</small></div>
            </div>
            <div v-if="latency.timingSamples" class="latency-composition" aria-label="平均耗时阶段占比"><span v-for="item in latencyStageItems" :key="item.key" :style="{'--stage-color':item.color,width:`${item.share || 0}%`}" :title="`${item.label} ${formatDecimal(item.share || 0,1)}%`"><i></i></span></div>
            <div class="latency-steps"><div v-for="item in latencyStageItems" :key="item.key" class="latency-step" :style="{'--stage-color':item.color}"><div class="stage-meta"><span>{{ item.index }}</span><em>{{ item.share == null ? '暂无占比' : `${formatDecimal(item.share,1)}%` }}</em></div><div class="stage-name"><i aria-hidden="true"></i><span>{{ item.label }}</span></div><strong>{{ formatDuration(item.value) }}</strong><small>{{ item.description }}</small></div></div>
            <p v-if="latency.timingSamples" class="latency-note"><i aria-hidden="true"></i><span>总耗时与三个阶段均来自同一组完整计时样本，阶段占比可以直接相加。</span></p><p v-else class="latency-note"><i aria-hidden="true"></i><span>该来源没有完整的连接与首字阶段样本，只显示总耗时分布。</span></p>
          </div>
          <div class="latency-percentiles"><div v-for="group in latencyPercentiles" :key="group.label" class="percentile-group"><div><strong>{{ group.label }}</strong><span>{{ formatNumber(group.data.samples) }} 条逐请求样本</span></div><dl><div><dt title="50% 的请求不超过该耗时">P50</dt><dd>{{ formatDuration(group.data.p50Ms) }}</dd></div><div><dt title="90% 的请求不超过该耗时">P90</dt><dd>{{ formatDuration(group.data.p90Ms) }}</dd></div><div><dt title="95% 的请求不超过该耗时">P95</dt><dd>{{ formatDuration(group.data.p95Ms) }}</dd></div><div><dt title="99% 的请求不超过该耗时">P99</dt><dd>{{ formatDuration(group.data.p99Ms) }}</dd></div><div><dt>最大</dt><dd>{{ formatDuration(group.data.maxMs) }}</dd></div></dl></div><p class="percentile-explanation"><b>P99 口径：</b>99% 的逐请求样本不超过该耗时；长尾请求会让它明显高于 P50。当前 {{ formatDecimal(latencyBuckets[5]?.percent,1) }}% 的请求超过 2 分钟。</p></div>
          <div class="speed-head"><strong>{{ formatNumber(latency.calls) }} 次模型请求分别用了多久</strong><span>每次请求只计入一个区间</span></div>
          <div class="speed-summary"><span><b>最多的一组：</b>{{ formatNumber(topLatencyBucket?.value) }} 次在 {{ topLatencyBucket?.label }}</span><span><b>较慢请求：</b>{{ formatNumber(slowRequests) }} 次超过 1 分钟，占 {{ latency.calls ? formatDecimal(slowRequests/latency.calls*100,1) : '0.0' }}%</span></div>
          <div class="speed-list"><div v-for="bucket in latencyBuckets" :key="bucket.label" class="speed-row"><span>{{ bucket.label }}</span><span class="speed-track"><i :style="{width:`${bucket.width}%`}"></i></span><span>{{ formatNumber(bucket.value) }} 次 · {{ formatDecimal(bucket.percent,1) }}%</span></div></div>
        </article>

        <article class="panel models-panel">
          <div class="panel-head"><div class="panel-title"><h2>模型分布与近期走势</h2><p>Top 6 模型及其他模型；点击模型可聚焦上方趋势图</p></div><span class="panel-unit">请求 · Token · USD</span></div>
          <div v-if="modelRankRows.length" class="model-distribution-body"><div class="distribution-donut" :style="modelDistributionStyle"><div><strong>{{ formatNumber(current.modelCalls) }}</strong><span>模型请求</span></div></div><div class="distribution-table-scroll"><table class="distribution-table"><colgroup><col class="model-col"><col class="spark-col"><col class="request-col"><col class="share-col"><col class="token-col"><col class="cost-col"><col class="rate-col"></colgroup><thead><tr><th>模型</th><th>近期走势</th><th>请求</th><th>占比</th><th>Token</th><th>花费</th><th>成功率</th></tr></thead><tbody><tr v-for="row in modelRankRows" :key="row.model"><td><button type="button" class="distribution-model" :disabled="row.isOther" :title="row.isOther?'聚合后的其他模型':`聚焦 ${row.model}`" @click="focusModel(row.isOther?'':row.model)"><i :style="{background:row.color}"></i><span>{{ row.model }}</span><b v-if="!row.isOther">↗</b></button></td><td><svg class="model-sparkline" :viewBox="`0 0 ${row.sparkline.width} ${row.sparkline.height}`" aria-hidden="true"><path :d="row.sparkline.area" :fill="row.color" class="spark-area"/><path :d="row.sparkline.path" fill="none" :stroke="row.color" class="spark-line"/></svg></td><td>{{ formatNumber(row.modelCalls) }}</td><td>{{ formatDecimal(row.share,1) }}%</td><td>{{ formatCompact(tokenTotal(row)) }}</td><td><strong>{{ formatMoney(row.costUsd) }}</strong></td><td><span class="status-pill" :class="{muted:row.successRate==null}">{{ successRate(row) }}</span></td></tr></tbody></table></div></div><div v-else class="empty-copy">当前范围没有模型调用</div>
        </article>

        <article class="panel heat-panel">
          <div class="panel-head"><div class="panel-title"><h2>活跃时段</h2><p>按星期与小时聚合的用户提交密度 · 北京时间</p></div><div class="heat-legend"><span>少</span><i></i><i></i><i></i><span>多</span></div></div>
          <div class="heat-scroll" @pointerleave="heatHover=null"><div class="heatmap"><span></span><span v-for="hour in 24" :key="`h-${hour}`" class="hour-label">{{ (hour-1)%3===0?String(hour-1).padStart(2,'0'):'' }}</span><template v-for="(label,weekday) in weekLabels" :key="label"><span class="heat-label">周{{ label }}</span><span v-for="hour in 24" :key="`${weekday}-${hour}`" class="heat-cell" :style="heatStyle(data?.heatmap?.[weekday]?.[hour-1])" tabindex="0" @pointerenter="updateHeatHover($event,weekday,hour-1,data?.heatmap?.[weekday]?.[hour-1])" @pointermove="updateHeatHover($event,weekday,hour-1,data?.heatmap?.[weekday]?.[hour-1])" @focus="updateHeatHover($event,weekday,hour-1,data?.heatmap?.[weekday]?.[hour-1])" @blur="heatHover=null"></span></template></div></div><div v-if="heatHover" class="heat-tooltip" :class="{above:heatHover.above}" :style="{left:`${heatHover.left}px`,top:`${heatHover.top}px`}"><strong>{{ heatHover.label }}</strong><span>{{ formatNumber(heatHover.count) }} 次用户提交</span></div>
        </article>

        <div class="bottom-grid">
          <article class="panel tools-panel"><div class="panel-head"><div class="panel-title"><h2>工具调用</h2><p>已完成调用数、平均耗时与账本状态</p></div><span class="panel-unit">共 {{ formatNumber(current.toolCalls) }} 次</span></div><div class="tools-list"><div v-for="item in (data?.tools || []).slice(0,7)" :key="item.name" class="tool-row"><div class="tool-name" :title="toolLabel(item.name)"><span>{{ item.name.slice(0,1).toUpperCase() }}</span>{{ toolLabel(item.name) }}</div><div class="tool-bar"><i :style="{width:`${item.calls/maxToolCalls*100}%`}"></i></div><div class="number">{{ formatNumber(item.calls) }}</div><div class="duration">{{ formatDuration(item.avgDurationMs) }}</div><div class="rate" :class="{good:item.successRate>=95}">{{ item.successRate==null?'—':`${formatDecimal(item.successRate,1)}%` }}</div></div><div v-if="!(data?.tools || []).length" class="empty-copy">当前范围没有工具调用</div></div></article>
          <article class="panel thinking-panel"><div class="panel-head"><div class="panel-title"><h2>模型思考</h2><p>{{ bucketCaption }}可观测 reasoning 累计时长</p></div><span class="experimental-badge">实验</span></div><div class="thinking-body"><div class="thinking-summary"><span><b>总计</b><strong>{{ data?.thinking?.available ? formatDuration(data.thinking.totalMs) : '—' }}</strong></span><span><b>可观测运行</b><strong>{{ formatNumber(data?.thinking?.observedRuns) }}</strong></span><span><b>峰值</b><strong>{{ formatDuration(thinkingChart.peak) }}</strong></span><span><b>中位数</b><strong>{{ formatDuration(thinkingChart.median) }}</strong></span></div><div class="thinking-chart" @pointermove="updateThinkingHover" @pointerleave="thinkingHoverIndex=-1"><svg :viewBox="`0 0 ${thinkingChart.width} ${thinkingChart.height}`" role="img" aria-label="模型思考时长趋势"><g v-for="line in thinkingChart.grid" :key="line.y"><line :x1="thinkingChart.left" :x2="thinkingChart.left+thinkingChart.chartWidth" :y1="line.y" :y2="line.y" class="chart-grid-line"/><text :x="thinkingChart.left-6" :y="line.y+3" class="chart-axis-label" text-anchor="end">{{ line.label }}</text></g><text v-for="tick in thinkingChart.dates" :key="tick.x" :x="tick.x" :y="thinkingChart.height-5" class="chart-axis-label" text-anchor="middle">{{ tick.label }}</text><rect v-for="(bar,index) in thinkingChart.bars" :key="bar.bucket" :x="bar.x" :y="bar.y" :width="bar.width" :height="bar.height" rx="3" class="thinking-bar" :class="{active:index===thinkingHoverIndex}"/></svg><div v-if="thinkingHover" class="chart-tooltip thinking-tooltip" :class="{'is-start':thinkingHoverIndex<timelineRows.length*.18,'is-edge':thinkingHoverIndex>timelineRows.length*.72}" :style="tooltipStyle(thinkingChart,thinkingHoverIndex,timelineRows.length)"><strong>{{ formatBucketRange(thinkingHover.bucket) }}</strong><span><i></i><em>思考时长</em><b>{{ formatDuration(thinkingHover.value) }}</b></span><span><i></i><em>可观测运行</em><b>{{ formatNumber(thinkingHover.observedRuns) }}</b></span></div></div><div class="note-strip"><b>口径提示</b><span>仅覆盖 Web 主控制器持久化的 reasoning 时长；悬浮柱形可查看具体时间与数值，缺失值不按 0 推断。</span></div></div></article>
        </div>

        <article class="panel table-panel">
          <div class="panel-head"><div class="panel-title"><h2>每日明细</h2><p>同一北京时间桶内的会话、请求、Token、缓存、成本和性能口径</p></div><span class="panel-unit">北京时间 · 完成时间归属</span></div>
          <div class="table-scroll"><table><thead><tr><th>日期</th><th>活跃会话</th><th>用户对话</th><th>模型请求</th><th>成功率</th><th>输入</th><th>缓存率</th><th>输出</th><th>账本花费</th><th>首个 Token</th><th>平均耗时</th></tr></thead><tbody><tr v-for="row in dailyRows" :key="row.date"><td><strong>{{ shortDate(row.date) }}</strong></td><td>{{ formatNumber(row.activeConversations) }}</td><td>{{ formatNumber(row.userTurns) }}</td><td>{{ formatNumber(row.modelCalls) }}</td><td><span class="status-pill" :class="{muted:row.successRate==null}">{{ successRate(row) }}</span></td><td>{{ formatCompact(inputTokenTotal(row)) }}</td><td>{{ row.cacheRate==null?'—':`${formatDecimal(row.cacheRate,1)}%` }}</td><td>{{ formatCompact(row.outputTokens) }}</td><td><strong>{{ formatMoney(row.costUsd) }}</strong></td><td>{{ formatDuration(row.avgFirstTokenMs) }}</td><td>{{ formatDuration(row.avgTotalMs) }}</td></tr></tbody></table></div>
        </article>
      </section>
    </div>
  </div>
</template>

<style scoped>
.statistics-view {
  --stat-bg:var(--ob-bg); --stat-surface:var(--ob-surface); --stat-surface-soft:var(--ob-surface-soft); --stat-raised:var(--ob-surface-raised);
  --stat-border:var(--ob-border); --stat-border-soft:var(--ob-border-soft); --stat-text:var(--ob-text); --stat-strong:var(--ob-text-strong);
  --stat-sub:var(--ob-text-subtle); --stat-muted:var(--ob-text-muted); --stat-blue:var(--ob-blue); --stat-blue-soft:var(--ob-blue-soft);
  --stat-green:var(--ob-success); --stat-green-soft:var(--ob-success-soft); --stat-amber:var(--ob-warning); --stat-amber-soft:var(--ob-warning-soft);
  --stat-red:var(--ob-danger); --stat-red-soft:var(--ob-danger-soft); --stat-violet:var(--ob-violet); --stat-violet-soft:var(--ob-violet-soft); --stat-cyan:#0e98a7;
  --stat-shadow:var(--ob-shadow-panel);
  min-width:0; min-height:0; flex:1; display:flex; flex-direction:column; overflow:hidden; background:var(--stat-bg); color:var(--stat-text); font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Microsoft YaHei",sans-serif;
}
*{box-sizing:border-box}.statistics-view button,.statistics-view select{font:inherit;color:inherit}.statistics-view button{cursor:pointer}.statistics-view svg{display:block}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.mobile-only{display:none}
.statistics-header{z-index:5;display:flex;min-height:72px;flex:0 0 auto;align-items:center;justify-content:space-between;gap:20px;padding:12px 24px;border-bottom:1px solid var(--stat-border);background:color-mix(in srgb,var(--stat-surface) 76%,transparent);backdrop-filter:blur(18px)}
.title-line{display:flex;align-items:center;gap:9px}.statistics-title h1{margin:0;color:var(--stat-strong);font-size:18px;line-height:1.25;letter-spacing:-.02em}.statistics-title p{margin:4px 0 0;color:var(--stat-sub);font-size:12.5px}.refreshing-chip{display:inline-flex;align-items:center;gap:5px;padding:3px 7px;border:1px solid color-mix(in srgb,var(--stat-blue) 25%,var(--stat-border));border-radius:999px;background:var(--stat-blue-soft);color:var(--stat-blue);font-size:11px;font-weight:650}.refreshing-chip i,.live-chip:before{width:6px;height:6px;border-radius:50%;background:currentColor}.refreshing-chip i{animation:pulse 1s ease-in-out infinite}@keyframes pulse{50%{opacity:.25}}
.statistics-filters{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:8px}.select-shell{position:relative}.select-shell:after{content:"";position:absolute;right:13px;top:50%;width:6px;height:6px;border-right:1.5px solid var(--stat-sub);border-bottom:1.5px solid var(--stat-sub);pointer-events:none;transform:translateY(-68%) rotate(45deg)}.select-shell select{height:36px;max-width:190px;appearance:none;padding:0 34px 0 12px;border:1px solid var(--stat-border);border-radius:10px;outline:0;background:var(--stat-surface);font-size:12px;font-weight:550;box-shadow:0 1px 2px rgb(var(--ob-shadow-rgb) / .03)}.range-tags{display:flex;align-items:center;gap:2px;padding:3px;border:1px solid var(--stat-border-soft);border-radius:10px;background:var(--stat-surface-soft)}.range-tags button{height:28px;padding:0 10px;border:0;border-radius:7px;background:transparent;color:var(--stat-sub);font-size:11.5px;white-space:nowrap}.range-tags button:hover{color:var(--stat-text)}.range-tags button.active{background:var(--stat-surface);color:var(--stat-blue);font-weight:680;box-shadow:0 1px 4px rgb(var(--ob-shadow-rgb) / .1)}.compare-toggle{display:grid;min-height:42px;grid-template-columns:30px auto 28px;align-items:center;gap:7px;padding:4px 8px 4px 5px;border:1px solid var(--stat-border);border-radius:12px;background:linear-gradient(145deg,var(--stat-surface),var(--stat-surface-soft));box-shadow:0 1px 2px rgb(var(--ob-shadow-rgb) / .04),inset 0 1px 0 color-mix(in srgb,var(--ob-surface) 72%,transparent);color:var(--stat-sub);transition:border-color .18s,box-shadow .18s,transform .18s}.compare-toggle:hover{border-color:color-mix(in srgb,var(--stat-blue) 38%,var(--stat-border));box-shadow:0 5px 15px rgb(var(--ob-shadow-rgb) / .08);transform:translateY(-1px)}.compare-toggle:focus-visible{outline:0;box-shadow:0 0 0 3px color-mix(in srgb,var(--stat-blue) 20%,transparent)}.compare-glyph{display:grid;width:30px;height:30px;place-items:center;border:1px solid var(--stat-border-soft);border-radius:9px;background:var(--stat-surface);box-shadow:0 1px 3px rgb(var(--ob-shadow-rgb) / .06)}.compare-glyph svg{width:19px;height:19px;fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-width:1.7}.compare-glyph .current-line{stroke:var(--stat-blue)}.compare-glyph .previous-line{stroke:var(--stat-muted);stroke-dasharray:2.2 2.2;opacity:.65}.compare-copy{display:grid;min-width:68px;gap:1px;text-align:left;line-height:1.05}.compare-copy b{color:var(--stat-text);font-size:11.5px;font-weight:650;white-space:nowrap}.compare-copy small{color:var(--stat-muted);font-size:9.5px}.compare-switch{position:relative;width:28px;height:16px;border:1px solid color-mix(in srgb,var(--stat-muted) 28%,transparent);border-radius:999px;background:var(--stat-border);box-shadow:inset 0 1px 2px rgb(var(--ob-shadow-rgb) / .12);transition:background .18s,border-color .18s}.compare-switch i{position:absolute;left:2px;top:2px;width:10px;height:10px;border-radius:50%;background:var(--ob-surface);box-shadow:0 1px 3px rgb(var(--ob-shadow-rgb) / 0.22);transition:transform .2s cubic-bezier(.2,.8,.2,1)}.compare-toggle.on{border-color:color-mix(in srgb,var(--stat-blue) 36%,var(--stat-border));background:linear-gradient(145deg,var(--stat-surface),color-mix(in srgb,var(--stat-blue-soft) 70%,var(--stat-surface)));box-shadow:0 5px 18px color-mix(in srgb,var(--stat-blue) 10%,transparent),inset 0 1px 0 color-mix(in srgb,var(--ob-surface) 72%,transparent)}.compare-toggle.on .compare-glyph{border-color:color-mix(in srgb,var(--stat-blue) 24%,var(--stat-border));background:var(--stat-blue-soft)}.compare-toggle.on .previous-line{stroke:var(--stat-violet);opacity:.95}.compare-toggle.on .compare-copy small{color:var(--stat-blue)}.compare-toggle.on .compare-switch{border-color:var(--stat-blue);background:var(--stat-blue)}.compare-toggle.on .compare-switch i{transform:translateX(12px)}
.statistics-scroll{min-height:0;flex:1;overflow:auto;padding:18px 24px 30px;container-type:inline-size}.summary-line{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 13px}.summary-copy{color:var(--stat-sub);font-size:12.5px}.summary-copy strong{color:var(--stat-text);font-weight:600}.live-chip{display:flex;align-items:center;gap:6px;color:var(--stat-sub);font-size:11px}.live-chip:before{color:var(--stat-green);box-shadow:0 0 0 3px var(--stat-green-soft)}
.kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(142px,1fr));gap:10px;margin-bottom:10px}.kpi-card{position:relative;min-height:116px;overflow:hidden;padding:14px 14px 11px;border:1px solid var(--stat-border);border-radius:13px;background:var(--stat-surface);box-shadow:var(--stat-shadow)}.kpi-card:after{content:"";position:absolute;right:-28px;top:-31px;width:86px;height:86px;border-radius:50%;background:var(--tone,var(--stat-blue-soft));opacity:.8}.kpi-card[data-accent="blue"]{--accent:var(--stat-blue);--tone:var(--stat-blue-soft)}.kpi-card[data-accent="cyan"]{--accent:var(--stat-cyan);--tone:color-mix(in srgb,var(--stat-cyan) 13%,transparent)}.kpi-card[data-accent="violet"]{--accent:var(--stat-violet);--tone:var(--stat-violet-soft)}.kpi-card[data-accent="amber"]{--accent:var(--stat-amber);--tone:var(--stat-amber-soft)}.kpi-card[data-accent="green"]{--accent:var(--stat-green);--tone:var(--stat-green-soft)}.kpi-card[data-accent="red"]{--accent:var(--stat-red);--tone:var(--stat-red-soft)}.kpi-top{display:flex;align-items:center;justify-content:space-between;gap:6px;color:var(--stat-sub);font-size:12.5px}.kpi-icon{z-index:1;display:grid;width:24px;height:24px;place-items:center;border-radius:7px;background:var(--tone);color:var(--accent)}.kpi-icon svg{width:13px;height:13px;fill:none;stroke:currentColor;stroke-width:1.9}.kpi-value{margin-top:8px;color:var(--stat-strong);font-size:23px;font-weight:680;line-height:1;letter-spacing:-.035em;font-variant-numeric:tabular-nums}.kpi-value small{margin-left:2px;color:var(--stat-sub);font-size:12px;font-weight:550;letter-spacing:0}.kpi-foot{display:flex;min-width:0;align-items:center;gap:5px;margin-top:9px;color:var(--stat-sub);font-size:12px;white-space:nowrap}.kpi-foot>span:last-child{min-width:0;overflow:hidden;text-overflow:ellipsis}.delta{display:inline-flex;padding:2px 5px;border-radius:5px;background:var(--stat-green-soft);color:var(--stat-green);font-weight:650}.delta.is-bad{background:var(--stat-red-soft);color:var(--stat-red)}
.dashboard-grid{display:grid;grid-template-columns:minmax(0,1.62fr) minmax(340px,.9fr);align-items:start;gap:10px}.panel{min-width:0;align-self:start;border:1px solid var(--stat-border);border-radius:14px;background:var(--stat-surface);box-shadow:var(--stat-shadow)}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:15px 16px 10px}.panel-title h2{margin:0;color:var(--stat-strong);font-size:15px;font-weight:650;line-height:1.25}.panel-title p{margin:4px 0 0;color:var(--stat-sub);font-size:12.5px;line-height:1.45}.panel-unit{color:var(--stat-sub);font-size:10px;white-space:nowrap}.segmented{display:flex;flex:0 0 auto;gap:2px;padding:3px;border:1px solid var(--stat-border-soft);border-radius:9px;background:var(--stat-surface-soft)}.segmented button{height:26px;padding:0 8px;border:0;border-radius:6px;background:transparent;color:var(--stat-sub);font-size:12px;white-space:nowrap}.segmented button.active{background:var(--stat-surface);color:var(--stat-strong);font-weight:650;box-shadow:0 1px 3px rgb(var(--ob-shadow-rgb) / .08)}
.chart-wrap{position:relative;height:276px;padding:3px 10px 6px}.chart-wrap svg,.metric-chart-card svg{width:100%;height:100%;overflow:visible}.chart-grid-line{stroke:var(--stat-border-soft);stroke-width:1}.chart-axis-label{fill:var(--stat-muted);font-size:9.5px;font-family:inherit}.chart-point{stroke:var(--stat-surface);stroke-width:1.5;opacity:0;transition:opacity .12s,r .12s}.chart-point:hover{opacity:1;r:4}.chart-hover-line{stroke:color-mix(in srgb,var(--stat-text) 26%,transparent);stroke-width:1;stroke-dasharray:3 3;pointer-events:none}.chart-active-point{stroke:var(--stat-surface);stroke-width:2;pointer-events:none}.chart-tooltip{position:absolute;z-index:4;top:13px;min-width:158px;max-width:240px;padding:9px 10px;border:1px solid var(--stat-border);border-radius:9px;background:color-mix(in srgb,var(--stat-raised) 95%,transparent);box-shadow:0 12px 30px rgb(var(--ob-shadow-rgb) / .16);backdrop-filter:blur(12px);color:var(--stat-sub);font-size:10.5px;pointer-events:none;transform:translateX(-50%)}.chart-tooltip.is-start{transform:translateX(0)}.chart-tooltip.is-edge{transform:translateX(-100%)}.chart-tooltip strong{display:block;margin-bottom:6px;color:var(--stat-strong);font-size:11px}.chart-tooltip span{display:grid;grid-template-columns:7px minmax(76px,1fr) auto;align-items:center;gap:6px;margin:4px 0;white-space:nowrap}.chart-tooltip span i{width:7px;height:7px;border-radius:50%}.chart-tooltip span b{color:var(--stat-text);font-weight:650;text-align:right;font-variant-numeric:tabular-nums}.chart-legend,.mini-legend{display:flex;align-items:center;gap:14px;color:var(--stat-sub);font-size:12px}.chart-legend{padding:0 17px 14px}.chart-legend span,.mini-legend span{display:flex;align-items:center;gap:5px}.chart-legend i,.mini-legend i{width:7px;height:7px;flex:0 0 auto;border-radius:50%}
.quality-body{display:grid;grid-template-columns:128px minmax(0,1fr);align-items:center;gap:12px;padding:5px 16px 10px}.health-badge,.interactive-badge,.experimental-badge{display:inline-flex;padding:3px 7px;border:1px solid color-mix(in srgb,var(--stat-green) 28%,var(--stat-border));border-radius:999px;background:var(--stat-green-soft);color:var(--stat-green);font-size:11px;font-weight:650}.health-badge.warn{border-color:color-mix(in srgb,var(--stat-amber) 28%,var(--stat-border));background:var(--stat-amber-soft);color:var(--stat-amber)}.interactive-badge{border-color:color-mix(in srgb,var(--stat-blue) 28%,var(--stat-border));background:var(--stat-blue-soft);color:var(--stat-blue)}.experimental-badge{border-color:color-mix(in srgb,var(--stat-violet) 28%,var(--stat-border));background:var(--stat-violet-soft);color:var(--stat-violet)}.donut{position:relative;width:128px;height:128px}.donut svg{width:100%;height:100%;transform:rotate(-90deg)}.donut circle{fill:none;stroke-width:12}.donut-base{stroke:var(--stat-surface-soft)}.donut-ok{stroke:var(--stat-green);stroke-linecap:round}.donut>div{position:absolute;inset:0;display:grid;place-content:center;text-align:center}.donut strong{font-size:24px;line-height:1;letter-spacing:-.04em}.donut span{margin-top:5px;color:var(--stat-sub);font-size:12px}.outcome-list{width:100%;min-width:0;margin:0}.outcome-list>div{display:grid;grid-template-columns:9px 1fr auto;align-items:center;gap:7px;padding:7px 0;border-bottom:1px solid var(--stat-border-soft);color:var(--stat-sub);font-size:12px}.outcome-list>div:last-child{border-bottom:0}.outcome-list i{width:7px;height:7px;border-radius:50%}.outcome-list i.ok{background:var(--stat-green)}.outcome-list i.failed{background:var(--stat-red)}.outcome-list i.retry{background:var(--stat-amber)}.outcome-list i.muted{background:var(--stat-muted)}.outcome-list b{color:var(--stat-text);font-weight:600}.error-breakdown{display:flex;align-items:center;gap:6px;overflow-x:auto;margin:0 16px 14px;padding-top:10px;border-top:1px solid var(--stat-border-soft);color:var(--stat-muted);font-size:10.5px;white-space:nowrap}.error-chip{display:inline-flex;gap:4px;padding:4px 6px;border-radius:6px;background:var(--stat-red-soft);color:var(--stat-red)}
.model-compare-panel,.latency-panel,.models-panel,.heat-panel,.table-panel{grid-column:1/-1;overflow:hidden}.selection-summary{display:inline-flex;align-items:center;gap:6px;padding:5px 9px;border:1px solid color-mix(in srgb,var(--stat-blue) 22%,var(--stat-border));border-radius:999px;background:color-mix(in srgb,var(--stat-blue-soft) 62%,var(--stat-surface));color:var(--stat-blue);font-size:10.5px;font-weight:650;white-space:nowrap}.selection-summary i{width:6px;height:6px;border-radius:50%;background:var(--stat-blue);box-shadow:0 0 0 3px color-mix(in srgb,var(--stat-blue) 13%,transparent)}.compare-controls{display:grid;grid-template-columns:minmax(0,1fr) 216px;align-items:start;gap:12px;padding:3px 16px 15px;border-bottom:1px solid var(--stat-border-soft)}.compare-group{min-width:0}.control-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:8px}.control-heading>div{display:grid;gap:2px}.control-heading span{color:var(--stat-muted);font-size:8.5px;font-weight:720;letter-spacing:.12em}.control-heading strong{color:var(--stat-strong);font-size:12px;font-weight:650}.control-heading small{color:var(--stat-muted);font-size:9.5px;white-space:nowrap}.provider-models{display:grid;gap:7px}.provider-cluster{display:grid;grid-template-columns:112px minmax(0,1fr);align-items:stretch;gap:8px;padding:7px;border:1px solid var(--stat-border-soft);border-radius:12px;background:linear-gradient(120deg,var(--stat-surface-soft),color-mix(in srgb,var(--stat-surface-soft) 34%,transparent))}.provider-identity{display:flex;min-width:0;align-items:center;gap:8px;padding:4px 5px}.provider-identity>span{display:grid;width:28px;height:28px;flex:0 0 auto;place-items:center;border:1px solid var(--stat-border);border-radius:8px;background:var(--stat-surface);box-shadow:0 2px 6px rgb(var(--ob-shadow-rgb) / .05);color:var(--stat-text);font-size:11px;font-weight:740}.provider-identity>div{display:grid;min-width:0;gap:2px}.provider-identity strong{overflow:hidden;color:var(--stat-text);font-size:10.5px;font-weight:650;text-overflow:ellipsis;white-space:nowrap}.provider-identity small{color:var(--stat-muted);font-size:8.5px;white-space:nowrap}.model-choice-list{display:grid;min-width:0;grid-template-columns:repeat(auto-fill,minmax(142px,1fr));gap:5px}.model-choice{position:relative;display:grid;min-width:0;min-height:42px;grid-template-columns:7px minmax(0,1fr) 18px;align-items:center;gap:7px;padding:5px 7px 5px 8px;border:1px solid var(--stat-border-soft);border-radius:9px;background:color-mix(in srgb,var(--stat-surface) 88%,transparent);color:var(--stat-sub);text-align:left;transition:border-color .16s,background .16s,box-shadow .16s,transform .16s}.model-choice:hover{z-index:1;border-color:color-mix(in srgb,var(--model-color) 42%,var(--stat-border));background:var(--stat-surface);box-shadow:0 4px 12px rgb(var(--ob-shadow-rgb) / .07);transform:translateY(-1px)}.model-choice:focus-visible{z-index:2;outline:0;box-shadow:0 0 0 3px color-mix(in srgb,var(--model-color) 18%,transparent)}.model-choice-swatch{width:7px;height:7px;border-radius:50%;background:var(--model-color);box-shadow:0 0 0 3px color-mix(in srgb,var(--model-color) 11%,transparent)}.model-choice>span{display:grid;min-width:0;gap:2px}.model-choice b{overflow:hidden;color:var(--stat-text);font-size:10.5px;font-weight:600;text-overflow:ellipsis;white-space:nowrap}.model-choice small{color:var(--stat-muted);font-size:8.5px}.model-choice-check{display:grid;width:17px;height:17px;place-items:center;border:1px solid var(--stat-border);border-radius:50%;background:var(--stat-surface-soft);color:transparent;transition:border-color .16s,background .16s,color .16s}.model-choice-check svg{width:10px;height:10px;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round;stroke-width:1.8}.model-choice.active{border-color:color-mix(in srgb,var(--model-color) 45%,var(--stat-border));background:color-mix(in srgb,var(--model-color) 8%,var(--stat-surface));box-shadow:inset 2px 0 var(--model-color),0 3px 10px color-mix(in srgb,var(--model-color) 9%,transparent)}.model-choice.active b{font-weight:680}.model-choice.active small{color:color-mix(in srgb,var(--model-color) 82%,var(--stat-sub))}.model-choice.active .model-choice-check{border-color:var(--model-color);background:var(--model-color);color:var(--ob-surface)}.metrics-control{padding:10px;border:1px solid var(--stat-border-soft);border-radius:12px;background:linear-gradient(145deg,var(--stat-surface-soft),color-mix(in srgb,var(--stat-blue-soft) 24%,var(--stat-surface)))}.metrics-control .control-heading{align-items:start}.metrics-control .control-heading small{display:none}.metric-choices{display:grid;gap:5px}.metric-choices button{display:flex;min-height:32px;align-items:center;gap:8px;padding:0 9px;border:1px solid transparent;border-radius:8px;background:color-mix(in srgb,var(--stat-surface) 72%,transparent);color:var(--stat-sub);font-size:10.5px;text-align:left;transition:border-color .15s,background .15s,color .15s}.metric-choices button i{width:6px;height:6px;border:1px solid var(--stat-muted);border-radius:50%;background:transparent}.metric-choices button:hover{border-color:var(--stat-border);background:var(--stat-surface);color:var(--stat-text)}.metric-choices button.active{border-color:color-mix(in srgb,var(--stat-blue) 28%,var(--stat-border));background:var(--stat-surface);box-shadow:0 2px 7px rgb(var(--ob-shadow-rgb) / .05);color:var(--stat-blue);font-weight:650}.metric-choices button.active i{border-color:var(--stat-blue);background:var(--stat-blue);box-shadow:0 0 0 3px color-mix(in srgb,var(--stat-blue) 12%,transparent)}.model-chart-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;gap:10px;padding:12px 16px 16px}.metric-chart-card:last-child:nth-child(odd){grid-column:1/-1}.metric-chart-card{position:relative;min-width:0;padding:12px;border:1px solid var(--stat-border-soft);border-radius:12px;background:linear-gradient(180deg,var(--stat-surface),var(--stat-surface-soft));box-shadow:inset 0 1px 0 color-mix(in srgb,var(--ob-surface) 55%,transparent)}.metric-chart-head{display:flex;align-items:center;justify-content:space-between;gap:9px;margin-bottom:5px}.metric-chart-head strong{color:var(--stat-strong);font-size:13px;font-weight:650}.metric-chart-head span{padding:3px 6px;border:1px solid var(--stat-border-soft);border-radius:6px;background:var(--stat-surface);color:var(--stat-muted);font-size:9px}.metric-chart-card svg{height:190px}.model-tooltip{top:39px}.mini-legend{gap:9px;overflow:hidden;margin-top:4px;font-size:10px}.mini-legend span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.latency-stages{padding:2px 16px 12px}.latency-overview{position:relative;display:grid;min-height:86px;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:18px;overflow:hidden;padding:14px 16px;border:1px solid color-mix(in srgb,var(--stat-blue) 18%,var(--stat-border));border-radius:13px;background:linear-gradient(118deg,color-mix(in srgb,var(--stat-blue-soft) 66%,var(--stat-surface)),var(--stat-surface) 52%,color-mix(in srgb,var(--stat-violet-soft) 36%,var(--stat-surface)));box-shadow:inset 0 1px 0 color-mix(in srgb,var(--ob-surface) 65%,transparent)}.latency-overview:after{content:"";position:absolute;right:116px;top:-36px;width:120px;height:120px;border:1px solid color-mix(in srgb,var(--stat-blue) 10%,transparent);border-radius:50%;box-shadow:0 0 0 18px color-mix(in srgb,var(--stat-blue) 3%,transparent),0 0 0 38px color-mix(in srgb,var(--stat-violet) 2%,transparent);pointer-events:none}.latency-overview-copy{z-index:1;min-width:0}.latency-overview-copy>span{display:flex;align-items:center;gap:7px;color:var(--stat-text);font-size:11.5px;font-weight:680}.latency-overview-copy>span i{width:7px;height:7px;border-radius:50%;background:var(--stat-green);box-shadow:0 0 0 4px var(--stat-green-soft)}.latency-overview-copy p{margin:7px 0 0;color:var(--stat-sub);font-size:10.5px}.latency-overview-value{z-index:1;display:grid;justify-items:end;gap:4px;padding-left:18px;border-left:1px solid color-mix(in srgb,var(--stat-border) 75%,transparent)}.latency-overview-value strong{color:var(--stat-strong);font-size:27px;font-weight:690;line-height:1;letter-spacing:-.04em;font-variant-numeric:tabular-nums}.latency-overview-value small{color:var(--stat-muted);font-size:9.5px;white-space:nowrap}.latency-composition{display:flex;height:8px;gap:2px;overflow:hidden;margin:9px 2px 10px;padding:2px;border-radius:999px;background:var(--stat-surface-soft);box-shadow:inset 0 1px 2px rgb(var(--ob-shadow-rgb) / .08)}.latency-composition span{min-width:3px;flex-shrink:1;overflow:hidden;border-radius:999px}.latency-composition i{display:block;width:100%;height:100%;border-radius:inherit;background:var(--stage-color)}.latency-steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.latency-step{position:relative;min-width:0;padding:11px 12px 12px;border:1px solid color-mix(in srgb,var(--stage-color) 17%,var(--stat-border));border-radius:11px;background:linear-gradient(145deg,color-mix(in srgb,var(--stage-color) 5%,var(--stat-surface)),var(--stat-surface));box-shadow:inset 0 1px 0 color-mix(in srgb,var(--ob-surface) 62%,transparent)}.latency-step:not(:last-child):after{content:"";position:absolute;z-index:2;right:-10px;top:50%;width:10px;border-top:1px dashed color-mix(in srgb,var(--stat-muted) 45%,transparent)}.stage-meta{display:flex;align-items:center;justify-content:space-between;gap:8px}.stage-meta span{color:var(--stat-muted);font-size:8.5px;font-weight:720;letter-spacing:.13em}.stage-meta em{padding:2px 5px;border-radius:999px;background:color-mix(in srgb,var(--stage-color) 9%,var(--stat-surface-soft));color:color-mix(in srgb,var(--stage-color) 80%,var(--stat-text));font-size:8.5px;font-style:normal;font-weight:650}.stage-name{display:flex;align-items:center;gap:7px;margin-top:11px;color:var(--stat-sub)}.stage-name i{width:7px;height:7px;border-radius:2px;background:var(--stage-color);box-shadow:0 0 0 3px color-mix(in srgb,var(--stage-color) 11%,transparent)}.stage-name span{font-size:11px;font-weight:620}.latency-step>strong{display:block;margin-top:8px;color:var(--stat-strong);font-size:21px;font-weight:680;letter-spacing:-.025em;font-variant-numeric:tabular-nums}.latency-step>small{display:block;margin-top:6px;overflow:hidden;color:var(--stat-muted);font-size:9.5px;text-overflow:ellipsis;white-space:nowrap}.latency-note{display:flex;align-items:flex-start;gap:7px;margin:8px 2px 0;color:var(--stat-muted);font-size:9.5px;line-height:1.45}.latency-note>i{position:relative;width:13px;height:13px;flex:0 0 auto;margin-top:1px;border:1px solid var(--stat-border);border-radius:50%;background:var(--stat-surface-soft)}.latency-note>i:after{content:"i";position:absolute;inset:0;display:grid;place-items:center;color:var(--stat-muted);font-size:8px;font-style:normal;font-weight:700}.speed-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 16px 5px}.speed-head strong,.speed-head span{font-size:12px}.speed-head span{color:var(--stat-muted)}.speed-summary{display:grid;grid-template-columns:1fr 1fr;gap:7px;padding:0 16px 7px}.speed-summary span{padding:7px 8px;border-radius:7px;background:var(--stat-surface-soft);color:var(--stat-sub);font-size:12px}.speed-summary b{color:var(--stat-text)}.speed-list{padding:0 16px 14px}.speed-row{display:grid;grid-template-columns:76px minmax(80px,1fr) 102px;align-items:center;gap:9px;min-height:25px;color:var(--stat-sub);font-size:12px}.speed-row>span:first-child{color:var(--stat-text);font-weight:550}.speed-row>span:last-child{text-align:right;white-space:nowrap}.speed-track,.bar-track,.tool-bar{height:6px;overflow:hidden;border-radius:99px;background:var(--stat-surface-soft)}.speed-track i,.bar-track i,.tool-bar i{display:block;height:100%;border-radius:inherit;background:var(--stat-blue)}
.rank-list{padding:1px 16px 15px}.rank-row{margin-top:12px}.rank-meta{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px;color:var(--stat-sub);font-size:12px}.rank-name{display:flex;min-width:0;align-items:center;gap:7px;overflow:hidden;color:var(--stat-text);text-overflow:ellipsis;white-space:nowrap}.rank-name i,.tool-name>span{display:grid;width:21px;height:21px;flex:0 0 auto;place-items:center;border-radius:6px;background:var(--stat-surface-soft);color:var(--stat-sub);font-style:normal;font-weight:700}.rank-meta b{color:var(--stat-text)}.bar-track i{background:linear-gradient(90deg,var(--stat-blue),color-mix(in srgb,var(--stat-blue) 55%,var(--stat-violet)))}
.heat-scroll{overflow-x:auto;overflow-y:hidden;padding:3px 16px 15px}.heatmap{display:grid;grid-template-columns:38px repeat(24,minmax(18px,1fr));gap:3px;min-width:780px;align-items:center}.heat-label{color:var(--stat-muted);font-size:11px}.hour-label{text-align:center;color:var(--stat-muted);font-size:10px}.heat-cell{height:18px;border-radius:4px;transition:transform .12s ease,filter .12s ease}.heat-cell:hover{z-index:2;transform:scale(1.22);filter:saturate(1.25)}.heat-legend{display:flex;align-items:center;gap:5px;color:var(--stat-muted);font-size:9px}.heat-legend i{width:12px;height:12px;border-radius:3px;background:var(--stat-surface-soft)}.heat-legend i:nth-of-type(2){background:color-mix(in srgb,var(--stat-blue) 38%,var(--stat-surface-soft))}.heat-legend i:nth-of-type(3){background:var(--stat-blue)}
.bottom-grid{grid-column:1/-1;display:grid;grid-template-columns:minmax(0,1.34fr) minmax(300px,.76fr);align-items:stretch;gap:10px}.bottom-grid>.panel{align-self:stretch}.tools-list{padding:2px 16px 15px}.tool-row{display:grid;grid-template-columns:minmax(110px,.72fr) minmax(100px,1fr) 58px 58px 58px;align-items:center;gap:10px;min-height:43px;border-bottom:1px solid var(--stat-border-soft);font-size:12px}.tool-row:last-child{border-bottom:0}.tool-name{display:flex;min-width:0;align-items:center;gap:8px;overflow:hidden;color:var(--stat-text);font-weight:550;text-overflow:ellipsis;white-space:nowrap}.tool-name>span{width:23px;height:23px}.tool-row .number,.tool-row .duration,.tool-row .rate{text-align:right;color:var(--stat-sub);font-variant-numeric:tabular-nums}.tool-row .duration{color:var(--stat-muted)}.tool-row .rate.good{color:var(--stat-green)}.thinking-body{padding:5px 16px 16px}.thinking-hero{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;padding:7px 0 14px}.thinking-hero strong{font-size:27px;line-height:1}.thinking-hero strong small{font-size:12px;color:var(--stat-sub)}.thinking-hero span{color:var(--stat-sub);font-size:10px;text-align:right}.thinking-bars{display:flex;height:88px;align-items:flex-end;gap:7px;padding:4px 2px 0;border-bottom:1px solid var(--stat-border)}.thinking-day{display:flex;min-width:0;flex:1;flex-direction:column;align-items:center;justify-content:flex-end;gap:5px;height:100%}.thinking-day i{width:100%;max-width:19px;border-radius:5px 5px 2px 2px;background:linear-gradient(180deg,var(--stat-violet),color-mix(in srgb,var(--stat-violet) 52%,var(--stat-blue)));opacity:.9}.thinking-day span{color:var(--stat-muted);font-size:10px}.note-strip{display:flex;align-items:flex-start;gap:8px;margin-top:13px;padding:9px 10px;border-radius:9px;background:var(--stat-violet-soft);color:var(--stat-sub);font-size:12px;line-height:1.45}.note-strip b{color:var(--stat-violet);white-space:nowrap}
.table-scroll{overflow-x:auto;padding:0 16px 15px}.table-scroll table{width:100%;min-width:920px;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}.table-scroll th{padding:9px 10px;border-bottom:1px solid var(--stat-border);color:var(--stat-muted);font-size:11.5px;font-weight:600;text-align:right;white-space:nowrap}.table-scroll th:first-child,.table-scroll td:first-child{text-align:left}.table-scroll td{padding:10px;border-bottom:1px solid var(--stat-border-soft);color:var(--stat-sub);text-align:right;white-space:nowrap}.table-scroll tr:last-child td{border-bottom:0}.table-scroll td strong{color:var(--stat-text)}.status-pill{display:inline-flex;padding:3px 6px;border-radius:5px;background:var(--stat-green-soft);color:var(--stat-green);font-weight:650}.status-pill.muted{background:var(--stat-surface-soft);color:var(--stat-muted)}.empty-copy{padding:24px;color:var(--stat-muted);text-align:center;font-size:12px}
.chart-stat-strip{display:flex;justify-content:center;flex-wrap:wrap;gap:6px;padding:0 16px 4px}.chart-stat-strip>span{display:flex;align-items:center;gap:6px;padding:5px 8px;border:1px solid var(--stat-border-soft);border-radius:8px;background:var(--stat-surface-soft);color:var(--stat-sub);font-size:10.5px}.chart-stat-strip i{width:7px;height:7px;border-radius:50%}.chart-stat-strip b{color:var(--stat-text)}.chart-stat-strip em{font-style:normal;font-variant-numeric:tabular-nums}.chart-area{opacity:.075;pointer-events:none}.chart-area.model-area{opacity:.045}.chart-tooltip span em{min-width:0;overflow:hidden;color:var(--stat-sub);font-style:normal;text-overflow:ellipsis;white-space:nowrap}.chart-tooltip span{grid-template-columns:7px minmax(0,1fr) auto}.chart-legend,.mini-legend{justify-content:center;flex-wrap:wrap}.legend-buttons{gap:6px}.legend-buttons button{display:flex;align-items:center;gap:6px;padding:5px 9px;border:1px solid var(--stat-border);border-radius:999px;background:var(--stat-surface);color:var(--stat-muted);font-size:11px;opacity:.55}.legend-buttons button.active{border-color:color-mix(in srgb,var(--stat-blue) 28%,var(--stat-border));background:var(--stat-blue-soft);color:var(--stat-text);opacity:1}.legend-buttons button i{width:7px;height:7px;border-radius:50%}
.failure-outcome{position:relative}.failure-trigger{position:relative;display:inline-flex;justify-content:flex-end;padding:2px 3px;border-radius:5px;outline:0;cursor:help}.failure-trigger:hover,.failure-trigger:focus-visible{background:var(--stat-red-soft);color:var(--stat-red)}.failure-tooltip{position:absolute;z-index:20;right:-6px;bottom:calc(100% + 8px);display:none;width:min(310px,calc(100vw - 56px));padding:11px 12px;border:1px solid color-mix(in srgb,var(--stat-red) 24%,var(--stat-border));border-radius:11px;background:color-mix(in srgb,var(--stat-raised) 97%,transparent);box-shadow:0 16px 38px rgb(var(--ob-shadow-rgb) / .2);backdrop-filter:blur(16px);color:var(--stat-sub);font-size:11px}.failure-trigger:hover .failure-tooltip,.failure-trigger:focus .failure-tooltip{display:block}.failure-tooltip>strong{display:block;margin-bottom:7px;color:var(--stat-strong);font-size:12px}.failure-tooltip>span{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:5px 0;border-bottom:1px solid var(--stat-border-soft)}.failure-tooltip em{overflow:hidden;font-style:normal;text-overflow:ellipsis;white-space:nowrap}.failure-tooltip small{display:block;margin-top:8px;color:var(--stat-muted);font-size:9.5px;line-height:1.35}.failure-tooltip .failure-empty{display:block;border:0;padding:7px 0}
.metric-insights{display:flex;justify-content:center;flex-wrap:wrap;gap:6px;margin-bottom:3px}.metric-insights>span{display:flex;max-width:100%;align-items:center;gap:6px;padding:4px 7px;border-radius:7px;background:var(--stat-surface);color:var(--stat-sub);font-size:9.5px}.metric-insights b{color:var(--stat-muted)}.metric-insights strong{color:var(--stat-text);font-size:10px}.metric-insights em{max-width:120px;overflow:hidden;color:var(--stat-sub);font-style:normal;text-overflow:ellipsis;white-space:nowrap}.metric-chart-card svg{height:230px}.model-tooltip{top:72px;width:min(340px,calc(100% - 20px));min-width:260px;max-width:none}.mini-legend{overflow:visible;margin-top:7px}.mini-legend span{max-width:190px}
.heat-cell{outline:0}.heat-cell:focus-visible{box-shadow:0 0 0 2px var(--stat-surface),0 0 0 4px var(--stat-blue)}.heat-tooltip{position:fixed;z-index:80;display:grid;gap:3px;min-width:158px;padding:8px 10px;border:1px solid var(--stat-border);border-radius:9px;background:color-mix(in srgb,var(--stat-raised) 97%,transparent);box-shadow:0 12px 30px rgb(var(--ob-shadow-rgb) / .18);color:var(--stat-sub);font-size:10.5px;pointer-events:none;transform:translateX(-50%)}.heat-tooltip.above{transform:translate(-50%,-100%)}.heat-tooltip strong{color:var(--stat-strong);font-size:11px}
.thinking-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-bottom:6px}.thinking-summary span{display:grid;gap:4px;padding:7px 8px;border-radius:8px;background:var(--stat-surface-soft)}.thinking-summary b{color:var(--stat-muted);font-size:9.5px;font-weight:550}.thinking-summary strong{overflow:hidden;color:var(--stat-strong);font-size:12px;text-overflow:ellipsis;white-space:nowrap}.thinking-chart{position:relative;height:174px}.thinking-chart svg{width:100%;height:100%}.thinking-bar{fill:color-mix(in srgb,var(--stat-violet) 78%,var(--stat-blue));opacity:.72;transition:opacity .12s,filter .12s}.thinking-bar.active{opacity:1;filter:saturate(1.3)}.thinking-tooltip{top:8px}.thinking-tooltip span i{background:var(--stat-violet)}
.tooltip-section{display:block;margin:7px -2px 4px;padding:6px 2px 1px;border-top:1px solid var(--stat-border-soft);color:var(--stat-muted);font-size:9.5px;font-weight:650;letter-spacing:.03em}.chart-tooltip .fresh-dot{background:var(--stat-blue)}.chart-tooltip .cache-read-dot{background:var(--stat-cyan)}.chart-tooltip .cache-write-dot{background:var(--stat-violet)}.chart-tooltip .cost-dot{background:var(--stat-amber)}
.quality-timeline{position:relative;margin:0 13px 13px;padding:10px 8px 2px;border:1px solid var(--stat-border-soft);border-radius:11px;background:linear-gradient(180deg,var(--stat-surface-soft),color-mix(in srgb,var(--stat-surface-soft) 35%,transparent))}.quality-timeline>svg{width:100%;height:150px;overflow:visible}.quality-chart-legend{display:flex;align-items:center;justify-content:center;gap:14px;color:var(--stat-sub);font-size:10.5px}.quality-chart-legend span{display:flex;align-items:center;gap:5px}.quality-chart-legend i{width:7px;height:7px;border-radius:50%}.quality-chart-legend i.ok{background:var(--stat-green)}.quality-chart-legend i.failed{background:var(--stat-red)}.quality-chart-legend b{color:var(--stat-text);font-weight:650}.quality-bar-ok{fill:var(--stat-green);opacity:.82}.quality-bar-failed{fill:var(--stat-red);opacity:.9}.quality-hit-slot{cursor:crosshair}.quality-peak-label{fill:var(--stat-sub);font-family:inherit;font-size:9px;font-weight:650}.quality-tooltip{top:32px}.quality-tooltip .ok-dot{background:var(--stat-green)}.quality-tooltip .failed-dot{background:var(--stat-red)}.quality-tooltip .rate-dot{background:var(--stat-blue)}
.latency-percentiles{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:0 16px 10px}.percentile-explanation{grid-column:1/-1;margin:0;padding:7px 9px;border-radius:8px;background:var(--stat-blue-soft);color:var(--stat-sub);font-size:10.5px;line-height:1.4}.percentile-explanation b{color:var(--stat-blue)}.percentile-group{min-width:0;padding:10px;border:1px solid var(--stat-border-soft);border-radius:10px;background:var(--stat-surface-soft)}.percentile-group>div{display:flex;align-items:baseline;justify-content:space-between;gap:8px}.percentile-group>div strong{color:var(--stat-strong);font-size:12px}.percentile-group>div span{color:var(--stat-muted);font-size:9.5px;white-space:nowrap}.percentile-group dl{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px;margin:9px 0 0}.percentile-group dl>div{min-width:0;padding:6px 4px;border-radius:7px;background:var(--stat-surface)}.percentile-group dt{color:var(--stat-muted);font-size:9px;text-align:center}.percentile-group dd{margin:3px 0 0;overflow:hidden;color:var(--stat-text);font-size:10.5px;font-weight:650;text-align:center;text-overflow:ellipsis;white-space:nowrap;font-variant-numeric:tabular-nums}
.model-distribution-body{display:grid;grid-template-columns:156px minmax(0,1fr);align-items:center;gap:16px;padding:4px 16px 16px}.distribution-donut{position:relative;width:146px;height:146px;border-radius:50%;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--stat-border) 55%,transparent)}.distribution-donut:after{content:"";position:absolute;inset:25px;border:1px solid var(--stat-border-soft);border-radius:50%;background:var(--stat-surface);box-shadow:0 4px 16px rgb(var(--ob-shadow-rgb) / .08)}.distribution-donut>div{position:absolute;z-index:1;inset:25px;display:grid;place-content:center;text-align:center}.distribution-donut strong{color:var(--stat-strong);font-size:19px;line-height:1}.distribution-donut span{margin-top:5px;color:var(--stat-muted);font-size:9.5px}.distribution-table-scroll{min-width:0;overflow-x:auto}.distribution-table{width:100%;min-width:760px;table-layout:fixed;border-collapse:collapse;font-size:11px;font-variant-numeric:tabular-nums}.distribution-table .model-col{width:28%}.distribution-table .spark-col{width:18%}.distribution-table .request-col{width:10%}.distribution-table .share-col{width:8%}.distribution-table .token-col{width:11%}.distribution-table .cost-col{width:14%}.distribution-table .rate-col{width:11%}.distribution-table th,.distribution-table td{padding:7px 8px;border-bottom:1px solid var(--stat-border-soft);color:var(--stat-sub);text-align:right;white-space:nowrap}.distribution-table th{color:var(--stat-muted);font-size:10px;font-weight:600}.distribution-table tr:last-child td{border-bottom:0}.distribution-table th:first-child,.distribution-table td:first-child{text-align:left}.distribution-table th:nth-child(2),.distribution-table td:nth-child(2){text-align:center}.distribution-table td strong{color:var(--stat-text)}.distribution-model{display:flex;min-width:0;max-width:220px;align-items:center;gap:7px;padding:3px 4px;border:0;border-radius:6px;background:transparent;text-align:left}.distribution-model:not(:disabled):hover{background:var(--stat-blue-soft);color:var(--stat-blue)}.distribution-model:disabled{cursor:default}.distribution-model i{width:8px;height:8px;flex:0 0 auto;border-radius:50%}.distribution-model span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.distribution-model b{color:var(--stat-blue);font-size:10px}.model-sparkline{width:108px;height:28px;margin:auto;overflow:visible!important}.spark-area{opacity:.09}.spark-line{stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.statistics-loading{padding:18px 24px}.loading-line,.loading-kpis,.loading-panels{display:flex;gap:10px}.loading-line{justify-content:space-between;margin-bottom:13px}.loading-line span{width:180px;height:14px}.loading-line i{width:100px;height:14px}.loading-kpis{display:grid;grid-template-columns:repeat(4,1fr);margin-bottom:10px}.loading-kpis i{height:116px}.loading-panels i{height:360px;flex:1}.statistics-loading i,.statistics-loading span{display:block;border-radius:13px;background:linear-gradient(100deg,var(--stat-surface-soft) 20%,var(--stat-surface) 45%,var(--stat-surface-soft) 70%);background-size:220% 100%;animation:shimmer 1.4s linear infinite}@keyframes shimmer{to{background-position:-220% 0}}.statistics-error{display:grid;place-items:center;margin:auto;text-align:center}.error-mark{display:grid;width:40px;height:40px;place-items:center;border-radius:50%;background:var(--stat-red-soft);color:var(--stat-red);font-weight:800}.statistics-error h2{margin:12px 0 4px;font-size:16px}.statistics-error p{margin:0;color:var(--stat-sub);font-size:12px}.statistics-error button{margin-top:14px;padding:7px 13px;border:0;border-radius:8px;background:var(--stat-blue);color:white}
@container(max-width:1050px){.dashboard-grid{grid-template-columns:1fr}.trend-panel,.quality-panel,.model-compare-panel,.latency-panel,.models-panel,.heat-panel,.table-panel{grid-column:1}.bottom-grid{grid-column:1;grid-template-columns:1fr}.quality-body{grid-template-columns:154px minmax(0,1fr);gap:20px}.quality-body .donut{width:154px;height:154px}}
@container(max-width:820px){.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.compare-controls{grid-template-columns:1fr}.metrics-control .metric-choices{grid-template-columns:repeat(3,minmax(0,1fr))}.model-chart-grid{grid-template-columns:1fr}.metric-chart-card{grid-column:1!important}.model-distribution-body{display:block}.distribution-donut{margin:2px auto 13px}}
@media(max-width:980px){.statistics-header{padding-inline:18px}.statistics-scroll{padding-inline:18px}.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.dashboard-grid{grid-template-columns:1fr}.trend-panel,.quality-panel,.latency-panel,.models-panel,.heat-panel,.table-panel{grid-column:1}.bottom-grid{grid-column:1;grid-template-columns:1fr}.model-chart-grid{grid-template-columns:1fr}.metric-chart-card{grid-column:1!important}.compare-controls{display:block}.compare-group+.compare-group{margin-top:12px}.quality-body{grid-template-columns:154px minmax(0,1fr);gap:18px}.quality-body .donut{width:154px;height:154px}.latency-percentiles{grid-template-columns:1fr 1fr}}
@media(max-width:760px){
  .statistics-view{min-height:0}.statistics-header{display:block;min-height:0;padding:16px 14px 12px;background:var(--stat-bg);backdrop-filter:none}.statistics-title h1{font-size:19px}.statistics-title p{font-size:12px}.statistics-filters{margin-top:13px;align-items:stretch;gap:7px}.select-shell:first-child{min-width:0;flex:0 0 100%}.select-shell select{width:100%;height:38px;max-width:none}.range-tags{min-width:0;flex:1;overflow-x:auto;scrollbar-width:none}.range-tags::-webkit-scrollbar{display:none}.range-tags button{padding-inline:8px}.compare-toggle{flex:0 0 auto}.statistics-scroll{overflow:auto;padding:0 14px calc(28px + env(safe-area-inset-bottom,0px))}.summary-line{margin:0 1px 10px}.live-chip{display:none}.kpi-grid{display:flex;gap:8px;overflow-x:auto;margin:0 -14px 10px;padding:0 14px 4px;scroll-snap-type:x mandatory;scrollbar-width:none}.kpi-grid::-webkit-scrollbar{display:none}.kpi-card{min-width:180px;min-height:112px;scroll-snap-align:start}.dashboard-grid{display:block}.panel{margin-bottom:10px;border-radius:12px}.panel-head{padding:13px 13px 9px}.panel-title h2{font-size:14px}.panel-title p{font-size:11px}.chart-wrap{height:230px;padding-inline:4px}.chart-axis-label{font-size:9px}.quality-body{grid-template-columns:126px minmax(0,1fr);gap:13px;place-items:center;padding:0 13px 10px}.quality-body .donut{width:126px;height:126px}.outcome-list{margin:0}.quality-timeline{margin-inline:13px}.latency-percentiles{grid-template-columns:1fr;padding-inline:13px}.model-distribution-body{display:block;padding-inline:13px}.distribution-donut{width:134px;height:134px;margin:2px auto 13px}.distribution-table-scroll{margin-inline:-3px}.distribution-table{min-width:620px}.error-breakdown{margin-inline:13px}.compare-controls{padding-inline:13px}.metrics-control .metric-choices{grid-template-columns:repeat(2,minmax(0,1fr))}.model-chart-grid{display:block;padding:10px 13px 13px}.metric-chart-card{margin-top:8px;padding:9px}.metric-chart-card:first-child{margin-top:0}.metric-chart-card svg{height:180px}.mini-legend{overflow-x:auto}.latency-steps{grid-template-columns:1fr}.latency-step:not(:last-child):after{right:auto;left:20px;top:auto;bottom:-10px;width:1px;height:10px;border:0;border-left:1px dashed color-mix(in srgb,var(--stat-muted) 45%,transparent)}.latency-step small{white-space:normal}.speed-summary{grid-template-columns:1fr}.speed-row{grid-template-columns:66px minmax(70px,1fr) 92px;font-size:11px}.heat-scroll,.table-scroll{padding-inline:13px}.bottom-grid{display:block}.tool-row{grid-template-columns:minmax(95px,1fr) 46px 52px 52px}.tool-row .tool-bar{display:none}.thinking-hero span{max-width:150px}.statistics-loading{padding:14px}.loading-kpis{display:flex;overflow:hidden}.loading-kpis i{min-width:180px}.loading-panels{display:block}.loading-panels i{height:300px;margin-bottom:10px}.panel-unit{display:none}
}
@media(max-width:760px){.chart-stat-strip{padding-inline:10px}.chart-stat-strip>span{flex-wrap:wrap;justify-content:center}.chart-wrap{height:270px}.model-tooltip{width:calc(100% - 14px);min-width:0}.metric-chart-card svg{height:215px}.mini-legend{overflow:visible;gap:7px}.mini-legend span{max-width:145px}.thinking-summary{grid-template-columns:1fr 1fr}.thinking-chart{height:190px}.failure-tooltip{right:-4px}.tool-name{font-size:11px}}
@media(max-width:430px){.statistics-filters{gap:5px}.range-tags button{padding-inline:6px;font-size:10.5px}.compare-toggle{grid-template-columns:28px auto 28px;padding:4px 7px 4px 4px}.compare-glyph{width:28px;height:28px}.compare-copy{min-width:60px}.compare-copy b{font-size:10.5px}.provider-cluster{grid-template-columns:1fr}.provider-identity{padding:2px}.latency-overview{grid-template-columns:1fr;gap:10px}.latency-overview:after{right:-20px}.latency-overview-value{justify-items:start;padding:10px 0 0;border-top:1px solid color-mix(in srgb,var(--stat-border) 75%,transparent);border-left:0}.quality-body{grid-template-columns:112px minmax(0,1fr)}.quality-body .donut{width:112px;height:112px}.outcome-list>div{font-size:11px}.segmented button{padding-inline:6px}.latency-panel .panel-head{display:block}.latency-panel .segmented{width:max-content;margin-top:9px}.chart-stat-strip>span{font-size:9.5px}.thinking-summary strong{font-size:11px}}
@media(prefers-reduced-motion:reduce){.refreshing-chip i,.statistics-loading i,.statistics-loading span{animation:none}.heat-cell,.chart-point,.compare-toggle,.compare-switch i,.model-choice{transition:none}.compare-toggle:hover,.model-choice:hover{transform:none}}
</style>
