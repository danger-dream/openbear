<script setup>
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import draggable from "vuedraggable";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import { copyTextToClipboard } from "../utils/clipboard.js";
import {
  buildCompressionOrderItems,
  compressionOrderFullnames,
} from "./compressionOrder.js";

const loading = ref(false);
const detailLoading = ref(false);
const providers = ref([]);
const overview = ref({ stats: {} });
const primaryModel = ref("");
const compressionModels = ref([]);
const compressionOrderItems = ref([]);
const compressionOrderSaving = ref(false);
const selectedName = ref("");
const detail = ref(null);
const providerDialog = ref(false);
const providerMode = ref("create");
const modelDialog = ref(false);
const modelMode = ref("create");
const providerSaving = ref(false);
const modelSaving = ref(false);
const fetchingModels = ref(false);
const testing = reactive({});
const defaultThinkingUpdating = reactive({});
const channelTestPollers = new Map();
const testDialog = ref(false);
const testTitle = ref("");
const testResults = ref([]);
const copiedModelMetadata = ref(null);
const copiedModelMetadataSource = ref("");
const modelsDev = ref({ available: false, refreshing: false });
const modelsDevProviders = ref([]);
const modelsDevModels = ref([]);
const modelsDevLoading = ref(false);
const modelsDevRefreshing = ref(false);
const modelsDevSyncing = ref(false);
const modelsDevMatchesLoading = ref(false);
const modelsDevMatchItems = ref([]);
const modelsDevSourceMode = ref("same-id");
const batchModelsDevDialog = ref(false);
const batchModelsDevItems = ref([]);
const batchModelsDevPreview = ref({ items: [] });
const batchModelsDevLoading = ref(false);
const batchModelsDevPreviewing = ref(false);
const batchModelsDevSyncing = ref(false);
let batchModelsDevPreviewRequest = 0;

const MODEL_METADATA_CLIPBOARD_KEY = "openbear:model-metadata:v1";

const providerForm = reactive({ name: "", baseUrl: "", apiKey: "", protocol: "chat", enabled: true, modelsText: "", modelsDevProviderId: "" });
const modelForm = reactive({
  id: "",
  oldId: "",
  name: "",
  capText: true,
  capImage: false,
  reasoning: false,
  reasoningOptions: [],
  thinkingLevels: "",
  defaultThinkingLevel: "",
  supportsFast: false,
  fastCost: {},
  fastRequest: null,
  compactTriggerTokens: 0,
  contextWindow: 128000,
  maxTokens: 8192,
  modelsDevProviderId: "",
  modelsDevModelId: "",
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, tiers: [] },
});

const selectedProvider = computed(() => detail.value?.provider || null);
const totalModels = computed(() => providers.value.reduce((sum, p) => sum + Number(p.modelCount || 0), 0));
const overviewStats = computed(() => overview.value?.stats || {});
const overviewCards = computed(() => {
  const stats = overviewStats.value;
  const calls = Number(stats.calls || 0);
  return [
    { label: "模型数量", value: fmtNum(totalModels.value), sub: "已配置" },
    buildTokenMetric(stats),
    { label: "成功率", value: successRate(stats), sub: `${fmtNum(calls)} 次调用` },
    { label: "TPS", value: fmtTps(stats.avg_tps), sub: `峰值 ${fmtTps(stats.peak_tps)}` },
    { label: "总花费", value: fmtMoney(stats.cost_usd), sub: "累计" },
  ];
});
const modelThinkingLevelOptions = computed(() => modelForm.reasoning ? parseThinkingLevelsText(modelForm.thinkingLevels) : []);
const modelSourceMatch = computed(() => {
  const id = String(modelForm.id || "").trim();
  return modelsDevMatchItems.value.find((item) => item?.modelId === id) || null;
});
const modelSourceCandidates = computed(() => {
  const candidates = modelSourceMatch.value?.candidates;
  return Array.isArray(candidates) ? candidates : [];
});
const modelDefaultSourceCandidate = computed(() => defaultSourceCandidate(
  modelSourceCandidates.value,
  modelSourceMatch.value?.defaultProviderId,
));
const batchModelsDevSelectedCount = computed(() => batchModelsDevItems.value.filter((item) => batchSourceFor(item)).length);
const batchModelsDevUnresolvedCount = computed(() => batchModelsDevItems.value.filter((item) => !item.currentSource && item.candidates?.length > 1 && !item.selectedProviderId).length);
const modelsDevStatusText = computed(() => {
  if (modelsDevRefreshing.value || modelsDev.value?.refreshing) return "刷新中";
  if (modelsDev.value?.available) return "已缓存";
  return "等待目录";
});

function items(data) { return Array.isArray(data?.providers) ? data.providers : []; }
function compressionListFrom(data) {
  return Array.isArray(data?.compressionModels) ? data.compressionModels.filter(Boolean).map(String) : [];
}
function applyCompressionData(data) {
  const models = compressionListFrom(data);
  compressionModels.value = models;
  compressionOrderItems.value = buildCompressionOrderItems(models, data?.compressionCandidates);
}
function okOrThrow(data) { if (data?.ok === false) throw new Error(data.error || "操作失败"); return data; }
function fmtMoney(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}
function fmtNum(value) { return Number(value || 0).toLocaleString(); }
function fmtCompact(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1_000) return String(Math.round(n));

  const units = [
    { size: 1_000, suffix: "K" },
    { size: 1_000_000, suffix: "M" },
    { size: 1_000_000_000, suffix: "B" },
    { size: 1_000_000_000_000, suffix: "T" },
  ];
  const decimalsFor = (index, amount) => index === 0
    ? (amount >= 100 ? 0 : 1)
    : (amount >= 100 ? 0 : amount >= 10 ? 1 : 2);
  let unitIndex = units.findLastIndex((unit) => n >= unit.size);
  let scaled = n / units[unitIndex].size;
  let decimals = decimalsFor(unitIndex, scaled);

  if (Number(scaled.toFixed(decimals)) >= 1_000 && unitIndex < units.length - 1) {
    unitIndex += 1;
    scaled = n / units[unitIndex].size;
    decimals = decimalsFor(unitIndex, scaled);
  }
  return `${scaled.toFixed(decimals)}${units[unitIndex].suffix}`;
}
function fmtExactTokens(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0";
  return Math.round(n).toLocaleString("en-US");
}
function tokenDisplay(label, value) {
  return {
    compact: fmtCompact(value),
    title: `${label}：${fmtExactTokens(value)}`,
  };
}
function buildTokenMetric(stats = {}) {
  const totals = tokenTotals(stats);
  return {
    kind: "tokens",
    label: "Tokens",
    total: tokenDisplay("总 Tokens", totals.input + totals.output),
    input: tokenDisplay("输入 Tokens", totals.input),
    output: tokenDisplay("输出 Tokens", totals.output),
    cache: tokenDisplay("缓存 Tokens", totals.cache),
    pct: totals.pct,
  };
}
function fmtTps(value) {
  const n = Number(value || 0);
  if (!n) return "—";
  const formatted = n >= 100 ? n.toFixed(0) : n.toFixed(1);
  return `${formatted} t/s`;
}
function protocolLabel(value) {
  return ({ chat: "OpenAI Chat", responses: "OpenAI Responses", anthropic: "Anthropic" })[value] || value || "—";
}
function successRate(stats = {}) {
  const ok = Number(stats.ok_count || 0);
  const fail = Number(stats.fail_count || 0);
  const total = ok + fail;
  return total ? `${(ok / total * 100).toFixed(1)}%` : "—";
}
function tokenTotals(stats = {}) {
  const input = Number(stats.input_tokens || 0) + Number(stats.cache_read_tokens || 0) + Number(stats.cache_write_tokens || 0);
  const output = Number(stats.output_tokens || 0);
  const cache = Number(stats.cache_read_tokens || 0) + Number(stats.cache_write_tokens || 0);
  const pct = input ? `${(cache / input * 100).toFixed(1)}%` : "—";
  return { input, output, cache, pct };
}
function providerMetrics(provider) {
  const stats = provider?.stats || {};
  return [
    buildTokenMetric(stats),
    { label: "成功率", value: successRate(stats), sub: `${Number(stats.calls || 0)} 次调用` },
    { label: "TPS", value: fmtTps(stats.avg_tps), sub: `峰值 ${fmtTps(stats.peak_tps)}` },
    { label: "总花费", value: fmtMoney(stats.cost_usd), sub: "累计" },
  ];
}
function modelMetrics(model) {
  const stats = model?.stats || {};
  return [
    buildTokenMetric(stats),
    { label: "成功率", value: successRate(stats), sub: `${Number(stats.calls || 0)} 次` },
    { label: "TPS", value: fmtTps(stats.avg_tps), sub: `峰值 ${fmtTps(stats.peak_tps)}` },
    { label: "花费", value: fmtMoney(stats.cost_usd), sub: "模型累计" },
  ];
}
function modelCost(model) {
  const c = model?.cost || {};
  return [
    { label: "输入", value: Number(c.input || 0) },
    { label: "输出", value: Number(c.output || 0) },
    { label: "缓存读", value: Number(c.cacheRead || 0) },
    { label: "缓存写", value: Number(c.cacheWrite || 0) },
  ];
}
function modelTiers(model) {
  const tiers = model?.cost?.tiers;
  return Array.isArray(tiers) ? tiers.slice().sort((a, b) => Number(a?.contextTokens || 0) - Number(b?.contextTokens || 0)) : [];
}
function tierRate(tier) {
  const parts = [];
  if (tier?.input !== undefined) parts.push(`入 $${Number(tier.input)}/1M`);
  if (tier?.output !== undefined) parts.push(`出 $${Number(tier.output)}/1M`);
  if (tier?.cacheRead !== undefined) parts.push(`读 $${Number(tier.cacheRead)}/1M`);
  if (tier?.cacheWrite !== undefined) parts.push(`写 $${Number(tier.cacheWrite)}/1M`);
  return parts.join(" · ");
}
const MODALITY_LABELS = { text: "文本", image: "图片", audio: "音频", video: "视频", pdf: "PDF" };
const THINKING_LEVEL_LABELS = { off: "关闭", minimal: "极低", low: "低", medium: "中", high: "高", xhigh: "极高", max: "最大" };
function fmtTokenValue(value, fallback = "未设置") {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? fmtCompact(n) : fallback;
}
function formatModalities(value) {
  if (!Array.isArray(value) || !value.length) return "未设置";
  return value.map((item) => MODALITY_LABELS[String(item || "").toLowerCase()] || String(item)).join("、");
}
function formatThinking(value) {
  if (!Array.isArray(value) || !value.length) return "无可选档位";
  return value.map((item) => THINKING_LEVEL_LABELS[String(item || "").toLowerCase()] || String(item)).join("、");
}
function thinkingLevelLabel(value) {
  const normalized = normalizeThinkingLevel(value);
  return THINKING_LEVEL_LABELS[normalized] || String(value || "");
}
function formatReasoning(metadata = {}) {
  const enabled = Boolean(metadata?.reasoning);
  if (!enabled) return "不支持";
  return `支持（${formatThinking(metadata?.thinkingLevels)}）`;
}
function formatRate(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `$${n}/1M` : "未设置";
}
function formatCost(value) {
  if (!value || typeof value !== "object") return "未设置";
  const labels = [["input", "输入"], ["output", "输出"], ["cacheRead", "缓存读"], ["cacheWrite", "缓存写"]];
  const base = labels
    .filter(([key]) => value[key] !== undefined)
    .map(([key, label]) => `${label} ${formatRate(value[key])}`);
  const tiers = Array.isArray(value.tiers) ? value.tiers : [];
  const tierText = tiers.map((tier) => {
    const rates = labels
      .filter(([key]) => tier?.[key] !== undefined)
      .map(([key, label]) => `${label} ${formatRate(tier[key])}`);
    return rates.length ? `超过 ${fmtTokenValue(tier?.contextTokens)}：${rates.join(" · ")}` : "";
  }).filter(Boolean);
  return [base.length ? base.join(" · ") : "基础费率未提供", ...tierText].join("；");
}
function formatFastRequest(request) {
  if (!request || typeof request !== "object") return "";
  const briefValue = (value) => {
    if (value === null) return "null";
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
    return "已设置";
  };
  const body = request.body && typeof request.body === "object" ? request.body : {};
  const headers = request.headers && typeof request.headers === "object" ? request.headers : {};
  const bodyItems = Object.entries(body).map(([key, value]) => `参数 ${key}=${briefValue(value)}`);
  const headerItems = Object.entries(headers).map(([key, value]) => `请求头 ${key}=${briefValue(value)}`);
  return [...bodyItems, ...headerItems].join(" · ");
}
function formatFastMode(metadata = {}) {
  if (!metadata?.supportsFast) return "不支持";
  const parts = ["支持"];
  const request = formatFastRequest(metadata?.fastRequest);
  const cost = formatCost(metadata?.fastCost);
  if (request) parts.push(request);
  if (cost !== "未设置") parts.push(cost);
  return parts.join("；");
}
function humanPreviewRows(preview = {}) {
  const current = preview?.current || {};
  const proposed = preview?.metadata || {};
  const changed = new Set((preview?.changes || []).map((item) => item?.field));
  const rows = [];
  if (["reasoning", "reasoningOptions", "thinkingLevels"].some((field) => changed.has(field))) {
    rows.push({ label: "推理与思考档位", current: formatReasoning(current), proposed: formatReasoning(proposed) });
  }
  if (["supportsFast", "fastCost", "fastRequest"].some((field) => changed.has(field))) {
    rows.push({ label: "Fast 模式", current: formatFastMode(current), proposed: formatFastMode(proposed) });
  }
  const formatters = {
    name: (value) => String(value || "未设置"),
    input: formatModalities,
    contextWindow: (value) => fmtTokenValue(value),
    maxTokens: (value) => fmtTokenValue(value),
    compactTriggerTokens: (value) => fmtTokenValue(value, "按全局比例"),
    cost: formatCost,
  };
  const labels = {
    name: "显示名称",
    input: "输入能力",
    contextWindow: "上下文窗口",
    maxTokens: "最大输出",
    compactTriggerTokens: "压缩触发 Token",
    cost: "费率与阶梯价",
  };
  for (const field of ["name", "input", "contextWindow", "maxTokens", "compactTriggerTokens", "cost"]) {
    if (!changed.has(field)) continue;
    rows.push({ label: labels[field], current: formatters[field](current[field]), proposed: formatters[field](proposed[field]) });
  }
  return rows;
}
function modelsDevPreviewNode(preview) {
  const source = preview?.source || {};
  const rows = humanPreviewRows(preview);
  const nodeRows = rows.length
    ? rows.map((item) => h("li", { class: "models-dev-preview-row" }, [
      h("strong", item.label),
      h("span", `${item.current} → ${item.proposed}`),
    ]))
    : [h("li", { class: "models-dev-preview-row" }, "公共字段与当前配置无差异；本次仅记录来源绑定。")];
  return h("div", { class: "models-dev-preview" }, [
    h("p", [h("strong", "来源 · "), `${source.providerId || ""} · ${source.name || source.modelId || ""}`]),
    h("ul", nodeRows),
    h("p", { class: "models-dev-preview-note" }, "首个价格阶梯会同步为压缩触发 Token；Fast 会同步来源给出的请求参数、请求头和有效费率，但不会自动开启当前会话的 Fast。不会修改上游模型 ID、Base URL、主力/压缩模型。"),
  ]);
}
function modelDisplayName(model) {
  const name = String(model?.name || "").trim();
  const id = String(model?.id || "").trim();
  return name || id;
}
function hasCustomModelName(model) {
  const name = String(model?.name || "").trim();
  const id = String(model?.id || "").trim();
  return Boolean(name && id && name !== id);
}
function modelFeatures(model) {
  const rows = [];
  if (Array.isArray(model?.input) && model.input.some((x) => String(x).toLowerCase() !== "text")) rows.push({ kind: "multimodal", label: "多模态" });
  if (model?.supportsFast) rows.push({ kind: "fast", label: "支持 Fast" });
  return rows;
}
function canPasteMetadataTo(row) {
  return Boolean(copiedModelMetadata.value && row?.fullname && copiedModelMetadataSource.value !== row.fullname);
}
function providerInitial(provider) {
  return String(provider?.name || "?").slice(0, 2).toUpperCase();
}
function providerTone(provider) {
  if (!provider?.enabled) return "is-disabled";
  if (provider?.primary) return "is-primary";
  if (provider?.compression) return "is-compression";
  return "is-enabled";
}
function modelsTextFromFetched(rows = []) {
  return rows.map((m) => `${m.id}:${m.name || m.id}`).join("\n");
}
function normalizeThinkingLevel(raw) {
  const key = String(raw || "").trim().toLowerCase().replace(/[^a-z0-9]/g, "");
  const map = {
    off: "off", false: "off", disabled: "off", disable: "off", no: "off", 0: "off",
    min: "minimal", minimal: "minimal",
    low: "low", thinkhard: "low",
    mid: "medium", med: "medium", medium: "medium", thinkharder: "medium", harder: "medium",
    high: "high", ultra: "high", ultrathink: "high", thinkhardest: "high", highest: "high",
    xhigh: "xhigh", extrahigh: "xhigh",
    max: "max", maximum: "max", maxeffort: "max",
  };
  return map[key] || "";
}
function parseThinkingLevelsText(text) {
  const out = [];
  const seen = new Set();
  String(text || "").split(/[,;，；\n\r\t ]+/).forEach((part) => {
    const lv = normalizeThinkingLevel(part);
    if (lv && !seen.has(lv)) {
      seen.add(lv);
      out.push(lv);
    }
  });
  return out;
}
function syncThinkingLevelsFromText({ forceLast = false } = {}) {
  if (!modelForm.reasoning) {
    modelForm.thinkingLevels = "";
    modelForm.defaultThinkingLevel = "";
    return [];
  }
  const levels = parseThinkingLevelsText(modelForm.thinkingLevels);
  modelForm.thinkingLevels = levels.join(",");
  if (!levels.length) {
    modelForm.defaultThinkingLevel = "";
  } else if (forceLast || !levels.includes(modelForm.defaultThinkingLevel)) {
    modelForm.defaultThinkingLevel = levels[levels.length - 1];
  }
  return levels;
}
function onReasoningChanged() {
  if (!modelForm.reasoning) {
    modelForm.thinkingLevels = "";
    modelForm.defaultThinkingLevel = "";
  } else {
    syncThinkingLevelsFromText({ forceLast: true });
  }
}
function numericValue(label, value, { min = 0, integer = false } = {}) {
  const n = Number(value);
  if (!Number.isFinite(n)) throw new Error(`${label} 必须是数字`);
  if (n < min) throw new Error(`${label} 不能小于 ${min}`);
  if (integer && !Number.isInteger(n)) throw new Error(`${label} 必须是整数`);
  return n;
}
function validateModelForm() {
  if (!String(modelForm.id || "").trim()) throw new Error("模型 ID 不能为空");
  if (!modelForm.capText && !modelForm.capImage) throw new Error("模型能力至少选择文本或图片之一");
  const contextWindow = numericValue("上下文窗口", modelForm.contextWindow, { min: 1, integer: true });
  const maxTokens = numericValue("最大输出", modelForm.maxTokens, { min: 1, integer: true });
  const compactTriggerTokens = numericValue("压缩触发 Token", modelForm.compactTriggerTokens, { min: 0, integer: true });
  const cost = {
    input: numericValue("输入费用", modelForm.cost.input, { min: 0 }),
    output: numericValue("输出费用", modelForm.cost.output, { min: 0 }),
    cacheRead: numericValue("缓存读费用", modelForm.cost.cacheRead, { min: 0 }),
    cacheWrite: numericValue("缓存写费用", modelForm.cost.cacheWrite, { min: 0 }),
    tiers: Array.isArray(modelForm.cost?.tiers) ? modelForm.cost.tiers.map((tier) => ({ ...tier })) : [],
  };
  const levels = modelForm.reasoning ? syncThinkingLevelsFromText({ forceLast: false }) : [];
  if (modelForm.reasoning && modelForm.defaultThinkingLevel && !levels.includes(modelForm.defaultThinkingLevel)) throw new Error("默认思考强度必须来自支持思考强度列表");
  return { contextWindow, maxTokens, compactTriggerTokens, cost, thinkingLevels: levels.join(","), defaultThinkingLevel: modelForm.reasoning ? (modelForm.defaultThinkingLevel || "") : "" };
}
function cloneCostTable(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const cloned = { ...value };
  if (Array.isArray(value.tiers)) cloned.tiers = value.tiers.map((tier) => ({ ...tier }));
  return cloned;
}
function cloneFastRequest(value) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "object" || Array.isArray(value)) return null;
  let body = {};
  if (value.body && typeof value.body === "object" && !Array.isArray(value.body)) {
    try { body = JSON.parse(JSON.stringify(value.body)); } catch { body = {}; }
  }
  const headers = value.headers && typeof value.headers === "object" && !Array.isArray(value.headers)
    ? { ...value.headers }
    : {};
  return { body, headers };
}
function hasCompleteFastMetadata(data) {
  const own = (key) => Object.prototype.hasOwnProperty.call(data, key);
  const request = data?.fastRequest;
  return Boolean(
    data && typeof data === "object"
    && own("supportsFast")
    && own("fastCost")
    && own("fastRequest")
    && data.fastCost && typeof data.fastCost === "object" && !Array.isArray(data.fastCost)
    && (request === null || (typeof request === "object" && !Array.isArray(request))),
  );
}
function metadataFromModel(row) {
  const inputs = Array.isArray(row?.input) ? row.input.map((x) => String(x).toLowerCase()) : ["text"];
  return {
    version: 2,
    metadata: {
      capText: inputs.includes("text") || inputs.length === 0,
      capImage: inputs.some((x) => x !== "text"),
      reasoning: Boolean(row?.reasoning),
      reasoningOptions: Array.isArray(row?.reasoningOptions) ? row.reasoningOptions : [],
      thinkingLevels: Array.isArray(row?.thinkingLevels) ? row.thinkingLevels.join(",") : "",
      defaultThinkingLevel: row?.defaultThinkingLevel || "",
      supportsFast: Boolean(row?.supportsFast),
      fastCost: cloneCostTable(row?.fastCost),
      fastRequest: cloneFastRequest(row?.fastRequest),
      compactTriggerTokens: Number(row?.compactTriggerTokens || 0),
      contextWindow: Number(row?.contextWindow || 128000),
      maxTokens: Number(row?.maxTokens || 8192),
      cost: { input: row?.cost?.input || 0, output: row?.cost?.output || 0, cacheRead: row?.cost?.cacheRead || 0, cacheWrite: row?.cost?.cacheWrite || 0, tiers: Array.isArray(row?.cost?.tiers) ? row.cost.tiers.map((tier) => ({ ...tier })) : [] },
    },
  };
}
function applyModelMetadata(meta) {
  const data = meta?.metadata || meta || {};
  const patch = {
    capText: Boolean(data.capText ?? true),
    capImage: Boolean(data.capImage ?? false),
    reasoning: Boolean(data.reasoning),
    reasoningOptions: Array.isArray(data.reasoningOptions) ? data.reasoningOptions : [],
    thinkingLevels: String(data.thinkingLevels || ""),
    defaultThinkingLevel: String(data.defaultThinkingLevel || ""),
    compactTriggerTokens: Number(data.compactTriggerTokens || 0),
    contextWindow: Number(data.contextWindow || 128000),
    maxTokens: Number(data.maxTokens || 8192),
    cost: { input: data.cost?.input || 0, output: data.cost?.output || 0, cacheRead: data.cost?.cacheRead || 0, cacheWrite: data.cost?.cacheWrite || 0, tiers: Array.isArray(data.cost?.tiers) ? data.cost.tiers.map((tier) => ({ ...tier })) : [] },
  };
  // Fast support, request additions, and effective cost are one atomic unit.
  // Old clipboard payloads did not include fastRequest, so preserve the target's
  // complete Fast configuration rather than applying only two of the three.
  if (hasCompleteFastMetadata(data)) {
    patch.supportsFast = Boolean(data.supportsFast);
    patch.fastCost = cloneCostTable(data.fastCost);
    patch.fastRequest = cloneFastRequest(data.fastRequest);
  }
  Object.assign(modelForm, patch);
  syncThinkingLevelsFromText({ forceLast: !modelForm.defaultThinkingLevel });
}
async function copyModelMetadata(row) {
  const payload = metadataFromModel(row);
  const text = JSON.stringify(payload, null, 2);
  copiedModelMetadata.value = payload;
  copiedModelMetadataSource.value = row?.fullname || "";
  localStorage.setItem(MODEL_METADATA_CLIPBOARD_KEY, text);
  try { await copyTextToClipboard(text); } catch { /* local metadata clipboard remains available */ }
  ElMessage.success("模型元数据已复制");
}
function payloadWithMetadataForRow(row, meta) {
  const data = meta?.metadata || meta || {};
  const input = [];
  if (data.capText ?? true) input.push("text");
  if (data.capImage) input.push("image");
  const reasoning = Boolean(data.reasoning);
  const fast = hasCompleteFastMetadata(data) ? data : row;
  return {
    id: row.id,
    name: row.name || row.id,
    reasoning,
    reasoningOptions: Array.isArray(data.reasoningOptions) ? data.reasoningOptions : [],
    thinkingLevels: reasoning ? String(data.thinkingLevels || "") : "",
    defaultThinkingLevel: reasoning ? String(data.defaultThinkingLevel || "") : "",
    supportsFast: Boolean(fast?.supportsFast),
    fastCost: cloneCostTable(fast?.fastCost),
    fastRequest: cloneFastRequest(fast?.fastRequest),
    compactTriggerTokens: Number(data.compactTriggerTokens || 0),
    input: input.length ? input : ["text"],
    contextWindow: Number(data.contextWindow || 128000),
    maxTokens: Number(data.maxTokens || 8192),
    cost: { input: data.cost?.input || 0, output: data.cost?.output || 0, cacheRead: data.cost?.cacheRead || 0, cacheWrite: data.cost?.cacheWrite || 0, tiers: Array.isArray(data.cost?.tiers) ? data.cost.tiers.map((tier) => ({ ...tier })) : [] },
  };
}
async function pasteCopiedMetadataToModel(row) {
  if (!selectedName.value || !row?.id || !copiedModelMetadata.value || copiedModelMetadataSource.value === row.fullname) return;
  await ElMessageBox.confirm(
    `把已复制的模型元数据粘贴到「${modelDisplayName(row)}」？不会修改模型 ID 和显示名称。`,
    "确认粘贴元数据",
    { type: "warning", confirmButtonText: "确认粘贴", cancelButtonText: "取消" },
  );
  try {
    okOrThrow(await Api.updateChannelModel(selectedName.value, row.id, payloadWithMetadataForRow(row, copiedModelMetadata.value)));
    await loadList(selectedName.value);
    ElMessage.success("模型元数据已粘贴");
  } catch (error) { ElMessage.error(apiError(error)); }
}
async function pasteModelMetadata() {
  let text = "";
  try { text = await navigator.clipboard?.readText?.() || ""; } catch { text = ""; }
  if (!text.trim()) text = localStorage.getItem(MODEL_METADATA_CLIPBOARD_KEY) || "";
  if (!text.trim()) {
    ElMessage.warning("没有可粘贴的模型元数据");
    return;
  }
  try {
    applyModelMetadata(JSON.parse(text));
    ElMessage.success("模型元数据已粘贴");
  } catch {
    ElMessage.error("剪贴板内容不是有效的模型元数据");
  }
}

async function loadModelsDevProviders() {
  modelsDevLoading.value = true;
  try {
    const data = okOrThrow(await Api.modelsDevProviders());
    modelsDevProviders.value = Array.isArray(data.items) ? data.items : [];
    if (data.catalog) modelsDev.value = data.catalog;
  } catch { /* catalog status remains visible; do not block normal channel editing */ }
  finally { modelsDevLoading.value = false; }
}
async function loadModelsDevModels(providerId = modelForm.modelsDevProviderId) {
  const id = String(providerId || "").trim();
  if (!id) { modelsDevModels.value = []; return; }
  modelsDevLoading.value = true;
  try {
    const data = okOrThrow(await Api.modelsDevProviderModels(id));
    modelsDevModels.value = Array.isArray(data.items) ? data.items : [];
    if (data.catalog) modelsDev.value = data.catalog;
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { modelsDevLoading.value = false; }
}
async function loadModelsDevMatches() {
  if (!selectedName.value) return;
  modelsDevMatchesLoading.value = true;
  try {
    const data = okOrThrow(await Api.channelModelsDevMatches(selectedName.value));
    modelsDevMatchItems.value = Array.isArray(data.items) ? data.items : [];
    if (data.catalog) modelsDev.value = data.catalog;
    if (modelDialog.value && modelsDevSourceMode.value === "same-id") {
      const id = String(modelForm.id || "").trim();
      const match = modelsDevMatchItems.value.find((item) => item?.modelId === id) || null;
      const candidates = Array.isArray(match?.candidates) ? match.candidates : [];
      const automatic = candidates.length === 1
        ? candidates[0]
        : defaultSourceCandidate(candidates, match?.defaultProviderId);
      if (!modelForm.modelsDevProviderId && automatic) {
        modelForm.modelsDevProviderId = automatic.providerId;
        modelForm.modelsDevModelId = id;
      }
    }
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { modelsDevMatchesLoading.value = false; }
}
function sourceCandidateForProvider(providerId) {
  const id = String(providerId || "").trim();
  return modelSourceCandidates.value.find((candidate) => candidate?.providerId === id) || null;
}
async function onSameIdSourceProviderChanged() {
  const candidate = sourceCandidateForProvider(modelForm.modelsDevProviderId);
  modelForm.modelsDevModelId = candidate ? String(modelForm.id || "").trim() : "";
  // Choosing a same-ID provider is the complete source decision.  Go straight
  // to the readable confirmation instead of making the user find a second
  // unrelated footer action.
  if (candidate && modelMode.value === "edit" && !modelsDevSyncing.value) await syncModelFormModelsDev();
}
function useSameIdSourceMode() {
  modelsDevSourceMode.value = "same-id";
  const candidate = sourceCandidateForProvider(modelForm.modelsDevProviderId);
  if (!candidate) {
    modelForm.modelsDevProviderId = "";
    modelForm.modelsDevModelId = "";
  } else {
    modelForm.modelsDevModelId = String(modelForm.id || "").trim();
  }
}
function useManualSourceMode() {
  modelsDevSourceMode.value = "manual";
  if (modelForm.modelsDevProviderId) void loadModelsDevModels(modelForm.modelsDevProviderId);
}
async function onManualSourceProviderChanged() {
  modelForm.modelsDevModelId = "";
  await loadModelsDevModels(modelForm.modelsDevProviderId);
}
async function onManualSourceModelChanged() {
  if (modelMode.value === "edit" && modelFormSource() && !modelsDevSyncing.value) await syncModelFormModelsDev();
}
function batchSourceFor(item) {
  if (!item) return null;
  if (item.currentSource && item.currentSource.modelId !== item.modelId) return { ...item.currentSource };
  const providerId = String(item.selectedProviderId || "").trim();
  const candidate = Array.isArray(item.candidates)
    ? item.candidates.find((entry) => entry?.providerId === providerId)
    : null;
  return candidate ? { providerId: candidate.providerId, modelId: candidate.modelId } : null;
}
function batchPreviewFor(modelId) {
  return (batchModelsDevPreview.value?.items || []).find((item) => item?.localModelId === modelId) || null;
}
function batchPreviewRows(item) {
  return humanPreviewRows(batchPreviewFor(item?.modelId));
}
function modelsDevProviderLabel(provider) {
  return String(provider?.name || provider?.id || "").trim();
}
function batchCandidateLabel(candidate) {
  return String(candidate?.providerName || candidate?.providerId || "").trim();
}
function defaultSourceCandidate(candidates, defaultProviderId = "") {
  const entries = Array.isArray(candidates) ? candidates : [];
  const id = String(defaultProviderId || "").trim();
  return entries.find((candidate) => candidate?.providerId === id)
    || entries.find((candidate) => Boolean(candidate?.isDefault))
    || null;
}
function batchDefaultCandidateFor(item) {
  return defaultSourceCandidate(item?.candidates, item?.defaultProviderId);
}
function batchSourceUsesDefault(item) {
  const source = batchSourceFor(item);
  const defaultCandidate = batchDefaultCandidateFor(item);
  return Boolean(source && defaultCandidate && source.providerId === defaultCandidate.providerId);
}
function batchSourceLabel(item) {
  const source = batchSourceFor(item);
  if (!source) return item?.candidates?.length ? "请选择提供者" : "目录中没有同名模型";
  const candidate = (item?.candidates || []).find((entry) => entry?.providerId === source.providerId);
  return candidate ? batchCandidateLabel(candidate) : `${source.providerId} · ${source.modelId}`;
}
function batchPreviewPayload() {
  return batchModelsDevItems.value
    .map((item) => ({ item, source: batchSourceFor(item) }))
    .filter(({ source }) => source)
    .map(({ item, source }) => ({ localModelId: item.modelId, source }));
}
async function refreshBatchModelsDevPreview() {
  const requestId = ++batchModelsDevPreviewRequest;
  const items = batchPreviewPayload();
  if (!items.length) {
    batchModelsDevPreview.value = { items: [] };
    return;
  }
  batchModelsDevPreviewing.value = true;
  try {
    const data = okOrThrow(await Api.previewChannelModelsDevBatch(selectedName.value, { items }));
    if (requestId === batchModelsDevPreviewRequest) batchModelsDevPreview.value = data;
  } catch (error) {
    if (requestId === batchModelsDevPreviewRequest) {
      batchModelsDevPreview.value = { items: [] };
      ElMessage.error(apiError(error));
    }
  } finally {
    if (requestId === batchModelsDevPreviewRequest) batchModelsDevPreviewing.value = false;
  }
}
function onBatchSourceProviderChanged() {
  void refreshBatchModelsDevPreview();
}
async function openModelsDevBatch() {
  if (!selectedName.value) return;
  batchModelsDevDialog.value = true;
  batchModelsDevLoading.value = true;
  batchModelsDevPreview.value = { items: [] };
  try {
    const data = okOrThrow(await Api.channelModelsDevMatches(selectedName.value));
    if (data.catalog) modelsDev.value = data.catalog;
    batchModelsDevItems.value = (Array.isArray(data.items) ? data.items : []).map((item) => {
      const candidates = Array.isArray(item.candidates) ? item.candidates : [];
      const currentSource = item.currentSource || null;
      const sameIdCurrent = currentSource?.modelId === item.modelId ? currentSource : null;
      const automatic = !currentSource
        ? (candidates.length === 1 ? candidates[0] : defaultSourceCandidate(candidates, item.defaultProviderId))
        : null;
      return {
        ...item,
        candidates,
        currentSource,
        selectedProviderId: sameIdCurrent?.providerId || automatic?.providerId || "",
      };
    });
    await refreshBatchModelsDevPreview();
  } catch (error) {
    ElMessage.error(apiError(error));
    batchModelsDevDialog.value = false;
  } finally { batchModelsDevLoading.value = false; }
}
async function syncBatchModelsDev() {
  const selections = batchPreviewPayload();
  if (!selections.length) {
    ElMessage.warning("请至少选择一个同名元数据来源");
    return;
  }
  if (batchModelsDevPreviewing.value || (batchModelsDevPreview.value?.items || []).length !== selections.length) {
    ElMessage.warning("正在生成最新预览，请稍后确认同步");
    return;
  }
  const previewById = new Map((batchModelsDevPreview.value?.items || []).map((item) => [item.localModelId, item]));
  const items = selections.map((selection) => {
    const preview = previewById.get(selection.localModelId);
    return { ...selection, metadataSha256: preview?.metadataSha256 || "" };
  });
  if (items.some((item) => !item.metadataSha256)) {
    ElMessage.warning("有模型缺少最新预览，请重新选择来源");
    return;
  }
  batchModelsDevSyncing.value = true;
  try {
    okOrThrow(await Api.syncChannelModelsDevBatch(selectedName.value, { items }));
    batchModelsDevDialog.value = false;
    await loadList(selectedName.value);
    ElMessage.success(`已同步 ${items.length} 个模型的元数据`);
  } catch (error) {
    if (error?.response?.status === 409) {
      ElMessage.warning("公共目录刚刚更新，已重新生成预览，请再次确认");
      await refreshBatchModelsDevPreview();
    } else {
      ElMessage.error(apiError(error));
    }
  } finally { batchModelsDevSyncing.value = false; }
}

async function refreshModelsDev() {
  modelsDevRefreshing.value = true;
  try {
    modelsDev.value = okOrThrow(await Api.refreshModelsDev());
    await loadModelsDevProviders();
    if (modelForm.modelsDevProviderId) await loadModelsDevModels();
    ElMessage.success(modelsDev.value.status === "not_modified" ? "公共模型目录已是最新" : "公共模型目录已更新");
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { modelsDevRefreshing.value = false; }
}
async function loadList(preferred = selectedName.value) {
  loading.value = true;
  try {
    const data = okOrThrow(await Api.channels());
    providers.value = items(data);
    overview.value = data.overview || { stats: {} };
    primaryModel.value = data.primaryModel || "";
    applyCompressionData(data);
    if (data.modelsDev) modelsDev.value = data.modelsDev;
    const next = preferred && providers.value.some((p) => p.name === preferred) ? preferred : providers.value[0]?.name || "";
    if (next) await loadProvider(next);
    else { selectedName.value = ""; detail.value = null; }
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally { loading.value = false; }
}
async function loadProvider(name) {
  if (!name) return;
  selectedName.value = name;
  detailLoading.value = true;
  try {
    const data = okOrThrow(await Api.channel(name));
    detail.value = data;
    primaryModel.value = data.primaryModel || primaryModel.value;
    applyCompressionData(data);
    if (data.modelsDev) modelsDev.value = data.modelsDev;
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally { detailLoading.value = false; }
}
function openCreateProvider() {
  providerMode.value = "create";
  Object.assign(providerForm, { name: "", baseUrl: "", apiKey: "", protocol: "chat", enabled: true, modelsText: "", modelsDevProviderId: "" });
  void loadModelsDevProviders();
  providerDialog.value = true;
}
function openEditProvider() {
  const p = selectedProvider.value;
  if (!p) return;
  providerMode.value = "edit";
  Object.assign(providerForm, { name: p.name, baseUrl: p.baseUrl, apiKey: "", protocol: p.protocol, enabled: p.enabled, modelsText: (p.models || []).map((m) => `${m.id}:${m.name || m.id}`).join("\n"), modelsDevProviderId: p.modelsDevProviderId || "" });
  void loadModelsDevProviders();
  providerDialog.value = true;
}
async function fetchProviderModels() {
  fetchingModels.value = true;
  try {
    const data = okOrThrow(await Api.fetchChannelModels({
      name: providerMode.value === "edit" ? selectedName.value : "",
      baseUrl: providerForm.baseUrl,
      apiKey: providerForm.apiKey,
      protocol: providerForm.protocol,
    }));
    providerForm.modelsText = modelsTextFromFetched(data.models || []);
    ElMessage.success(`已获取 ${data.count || 0} 个模型`);
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    fetchingModels.value = false;
  }
}
async function saveProvider() {
  providerSaving.value = true;
  try {
    if (providerMode.value === "create") {
      const data = okOrThrow(await Api.createChannel({ ...providerForm, models: providerForm.modelsText }));
      providerDialog.value = false;
      await loadList(data.provider?.name || providerForm.name);
      ElMessage.success("渠道已创建");
    } else {
      const body = { name: providerForm.name, baseUrl: providerForm.baseUrl, protocol: providerForm.protocol, enabled: providerForm.enabled, models: providerForm.modelsText, modelsDevProviderId: providerForm.modelsDevProviderId };
      if (providerForm.apiKey) body.apiKey = providerForm.apiKey;
      okOrThrow(await Api.updateChannel(selectedName.value, body));
      providerDialog.value = false;
      await loadList(providerForm.name);
      ElMessage.success("渠道已保存");
    }
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { providerSaving.value = false; }
}
async function removeProvider() {
  const name = selectedName.value;
  if (!name) return;
  await ElMessageBox.confirm(`删除渠道 ${name}？如果它承载主力/压缩模型会被后端拒绝。`, "确认删除", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  try {
    okOrThrow(await Api.deleteChannel(name));
    ElMessage.success("渠道已删除");
    await loadList("");
  } catch (error) { ElMessage.error(apiError(error)); }
}
function openCreateModel() {
  modelMode.value = "create";
  modelsDevSourceMode.value = "same-id";
  const defaultProviderId = selectedProvider.value?.modelsDevProviderId || "";
  Object.assign(modelForm, { id: "", oldId: "", name: "", capText: true, capImage: false, reasoning: false, reasoningOptions: [], thinkingLevels: "", defaultThinkingLevel: "", supportsFast: false, fastCost: {}, fastRequest: null, compactTriggerTokens: 0, contextWindow: 128000, maxTokens: 8192, modelsDevProviderId: defaultProviderId, modelsDevModelId: "", cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, tiers: [] } });
  void loadModelsDevProviders();
  if (defaultProviderId) void loadModelsDevModels(defaultProviderId);
  modelDialog.value = true;
}
function openEditModel(row) {
  modelMode.value = "edit";
  const inputs = Array.isArray(row.input) ? row.input.map((x) => String(x).toLowerCase()) : ["text"];
  const source = row.modelsDev?.bound ? row.modelsDev : {};
  // A channel default is only a picker hint.  An unbound local model must not
  // acquire a metadata binding merely by opening and saving this form.
  const sourceProviderId = source.providerId || "";
  modelsDevSourceMode.value = source.modelId && source.modelId !== row.id ? "manual" : "same-id";
  Object.assign(modelForm, {
    id: row.id,
    oldId: row.id,
    name: row.name || row.id,
    capText: inputs.includes("text") || inputs.length === 0,
    capImage: inputs.some((x) => x !== "text"),
    reasoning: Boolean(row.reasoning),
    reasoningOptions: Array.isArray(row.reasoningOptions) ? row.reasoningOptions : [],
    thinkingLevels: Array.isArray(row.thinkingLevels) ? row.thinkingLevels.join(",") : "",
    defaultThinkingLevel: row.defaultThinkingLevel || "",
    supportsFast: Boolean(row.supportsFast),
    fastCost: cloneCostTable(row.fastCost),
    fastRequest: cloneFastRequest(row.fastRequest),
    compactTriggerTokens: Number(row.compactTriggerTokens || 0),
    contextWindow: row.contextWindow || 128000,
    maxTokens: row.maxTokens || 8192,
    modelsDevProviderId: sourceProviderId,
    modelsDevModelId: source.modelId || "",
    cost: { input: row.cost?.input || 0, output: row.cost?.output || 0, cacheRead: row.cost?.cacheRead || 0, cacheWrite: row.cost?.cacheWrite || 0, tiers: Array.isArray(row.cost?.tiers) ? row.cost.tiers.map((tier) => ({ ...tier })) : [] },
  });
  void loadModelsDevProviders();
  if (sourceProviderId && modelsDevSourceMode.value === "manual") void loadModelsDevModels(sourceProviderId);
  syncThinkingLevelsFromText({ forceLast: false });
  modelDialog.value = true;
  void loadModelsDevMatches();
}
function modelPayload(validated = validateModelForm()) {
  const input = [];
  if (modelForm.capText) input.push("text");
  if (modelForm.capImage) input.push("image");
  const payload = {
    id: modelForm.id,
    name: modelForm.name || modelForm.id,
    reasoning: Boolean(modelForm.reasoning),
    reasoningOptions: Array.isArray(modelForm.reasoningOptions) ? modelForm.reasoningOptions : [],
    thinkingLevels: validated.thinkingLevels,
    defaultThinkingLevel: validated.defaultThinkingLevel,
    supportsFast: Boolean(modelForm.supportsFast),
    fastCost: cloneCostTable(modelForm.fastCost),
    fastRequest: cloneFastRequest(modelForm.fastRequest),
    compactTriggerTokens: validated.compactTriggerTokens,
    input: input.length ? input : ["text"],
    contextWindow: validated.contextWindow,
    maxTokens: validated.maxTokens,
    cost: validated.cost,
  };
  // Source selection is applied together with its catalog metadata through the
  // dedicated sync action.  An ordinary model save must not create a pending or
  // guessed binding as a side effect.
  return payload;
}
function modelFormSource() {
  const providerId = String(modelForm.modelsDevProviderId || "").trim();
  if (modelsDevSourceMode.value === "same-id") {
    const candidate = sourceCandidateForProvider(providerId);
    const localModelId = String(modelForm.id || "").trim();
    return candidate && localModelId ? { providerId: candidate.providerId, modelId: localModelId } : null;
  }
  const modelId = String(modelForm.modelsDevModelId || "").trim();
  return providerId && modelId ? { providerId, modelId } : null;
}
async function syncModelFromModelsDev(row, source = row?.modelsDev?.bound ? { providerId: row.modelsDev.providerId, modelId: row.modelsDev.modelId } : null) {
  if (!selectedName.value || !row?.id) return;
  if (!source) {
    ElMessage.warning("请先在模型编辑页选择元数据来源");
    openEditModel(row);
    return;
  }
  modelsDevSyncing.value = true;
  try {
    const preview = okOrThrow(await Api.previewChannelModelModelsDev(selectedName.value, row.id, source));
    await ElMessageBox.confirm(
      modelsDevPreviewNode(preview),
      "应用元数据",
      { type: "warning", confirmButtonText: "应用并同步", cancelButtonText: "取消" },
    );
    const metadataSha256 = String(preview.metadataSha256 || "").trim();
    if (!metadataSha256) throw new Error("元数据预览缺少版本标识，请重新预览");
    okOrThrow(await Api.syncChannelModelModelsDev(selectedName.value, row.id, { ...source, metadataSha256 }));
    modelDialog.value = false;
    await loadList(selectedName.value);
    ElMessage.success("元数据已同步");
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(apiError(error));
  } finally { modelsDevSyncing.value = false; }
}
async function syncModelFormModelsDev() {
  if (modelMode.value !== "edit") {
    ElMessage.warning("请先保存新模型，再确认同步公共元数据");
    return;
  }
  await syncModelFromModelsDev({ id: modelForm.oldId, modelsDev: { bound: true, ...modelFormSource() } }, modelFormSource());
}
async function saveModel() {
  if (!selectedName.value) return;
  modelSaving.value = true;
  try {
    const validated = validateModelForm();
    const body = modelPayload(validated);
    if (modelMode.value === "create") okOrThrow(await Api.createChannelModel(selectedName.value, body));
    else okOrThrow(await Api.updateChannelModel(selectedName.value, modelForm.oldId, body));
    modelDialog.value = false;
    await loadList(selectedName.value);
    ElMessage.success("模型已保存");
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { modelSaving.value = false; }
}
function defaultThinkingUpdateKey(row) {
  return String(row?.fullname || `${selectedName.value}/${row?.id || ""}`);
}
function isDefaultThinkingLevel(row, level) {
  const normalized = normalizeThinkingLevel(level);
  return Boolean(normalized && normalized === normalizeThinkingLevel(row?.defaultThinkingLevel));
}
function isDefaultThinkingUpdating(row) {
  return Boolean(defaultThinkingUpdating[defaultThinkingUpdateKey(row)]);
}
async function confirmSetDefaultThinkingLevel(row, rawLevel) {
  const level = normalizeThinkingLevel(rawLevel);
  const supportedLevels = Array.isArray(row?.thinkingLevels)
    ? row.thinkingLevels.map((item) => normalizeThinkingLevel(item)).filter(Boolean)
    : [];
  if (!selectedName.value || !row?.id || !level || !supportedLevels.includes(level) || isDefaultThinkingLevel(row, level)) return;

  const key = defaultThinkingUpdateKey(row);
  if (defaultThinkingUpdating[key]) return;
  defaultThinkingUpdating[key] = true;
  try {
    await ElMessageBox.confirm(
      `将「${modelDisplayName(row)}」的默认思考档位设为「${thinkingLevelLabel(level)}」？未在会话中手动选择档位时，会使用这个默认值。`,
      "确认默认思考档位",
      { type: "warning", confirmButtonText: "设为默认", cancelButtonText: "取消" },
    );
    okOrThrow(await Api.updateChannelModel(selectedName.value, row.id, { defaultThinkingLevel: level }));
    await loadList(selectedName.value);
    ElMessage.success(`已将默认思考档位设为「${thinkingLevelLabel(level)}」`);
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(apiError(error));
  } finally {
    delete defaultThinkingUpdating[key];
  }
}
async function removeModel(row) {
  await ElMessageBox.confirm(
    `确认删除模型「${row.fullname}」？这个操作会修改渠道配置。`,
    "确认删除模型",
    { type: "warning", confirmButtonText: "确认删除", cancelButtonText: "取消" },
  );
  try {
    okOrThrow(await Api.deleteChannelModel(selectedName.value, row.id));
    await loadList(selectedName.value);
    ElMessage.success("模型已删除");
  } catch (error) { ElMessage.error(apiError(error)); }
}
async function setPrimary(row) {
  try { okOrThrow(await Api.setPrimaryModel(row.fullname)); await loadList(selectedName.value); ElMessage.success("主力模型已更新"); }
  catch (error) { ElMessage.error(apiError(error)); }
}
async function confirmSetPrimary(row) {
  if (row?.primary) return;
  await ElMessageBox.confirm(`将「${modelDisplayName(row)}」设为主力模型？`, "确认设置主力", { type: "warning", confirmButtonText: "设为主力", cancelButtonText: "取消" });
  await setPrimary(row);
}
async function setCompressionModels(models = []) {
  if (compressionOrderSaving.value) return;
  compressionOrderSaving.value = true;
  try {
    okOrThrow(await Api.setCompressionModel(models));
    await loadList(selectedName.value);
    ElMessage.success(models.length ? "压缩模型已更新" : "压缩模型已清空");
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { compressionOrderSaving.value = false; }
}
async function persistCompressionOrder() {
  if (compressionOrderSaving.value) return;
  const ordered = compressionOrderFullnames(compressionOrderItems.value);
  const previous = compressionModels.value.slice();
  if (ordered.length === previous.length && ordered.every((item, index) => item === previous[index])) return;
  compressionOrderSaving.value = true;
  try {
    okOrThrow(await Api.setCompressionModel(ordered));
    compressionModels.value = ordered;
    ElMessage.success("压缩执行顺序已更新");
  } catch (error) {
    compressionModels.value = previous;
    compressionOrderItems.value = buildCompressionOrderItems(previous, compressionOrderItems.value);
    ElMessage.error(apiError(error));
  } finally { compressionOrderSaving.value = false; }
}
async function confirmRemoveCompressionCandidate(item) {
  if (compressionOrderSaving.value || !item?.fullname) return;
  try {
    await ElMessageBox.confirm(
      `将「${item.name || item.id || item.fullname}」移出压缩候选？这不会删除模型。`,
      "确认移出压缩候选",
      { type: "warning", confirmButtonText: "移出候选", cancelButtonText: "取消" },
    );
  } catch (action) {
    if (action === "cancel" || action === "close") return;
    throw action;
  }
  await setCompressionModels(compressionModels.value.filter((fullname) => fullname !== item.fullname));
}
async function clearCompressionModels() {
  await setCompressionModels([]);
}
async function toggleCompression(row) {
  if (!row?.fullname) return;
  const current = compressionModels.value.slice();
  const exists = current.includes(row.fullname);
  const next = exists ? current.filter((item) => item !== row.fullname) : [...current, row.fullname];
  await setCompressionModels(next);
}
async function confirmSetCompression(row) {
  if (!row?.fullname) return;
  const exists = compressionModels.value.includes(row.fullname);
  if (exists) {
    await ElMessageBox.confirm(`从压缩候选中移除「${modelDisplayName(row)}」？`, "确认移除压缩模型", { type: "warning", confirmButtonText: "移除", cancelButtonText: "取消" });
  } else {
    await ElMessageBox.confirm(`将「${modelDisplayName(row)}」追加为压缩候选？候选会按设置顺序尝试，全部失败后回退主模型。`, "确认设置压缩模型", { type: "warning", confirmButtonText: "追加为压缩", cancelButtonText: "取消" });
  }
  await toggleCompression(row);
}
async function testModel(row) {
  if (!selectedName.value || !row?.id) return;
  const key = `model:${row.fullname}`;
  testing[key] = true;
  try {
    const data = okOrThrow(await Api.testChannelModel(selectedName.value, row.id));
    testTitle.value = `模型测试：${row.fullname}`;
    testResults.value = data.result ? [data.result] : [];
    testDialog.value = true;
    ElMessage[data.result?.ok ? 'success' : 'warning'](data.result?.ok ? '模型测试通过' : '模型测试返回异常');
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { testing[key] = false; }
}
function clearChannelTestPoller(key) {
  const timer = channelTestPollers.get(key);
  if (timer) window.clearTimeout(timer);
  channelTestPollers.delete(key);
}
function scheduleChannelTestPoll(provider, jobUuid, key) {
  clearChannelTestPoller(key);
  channelTestPollers.set(key, window.setTimeout(async () => {
    try {
      const data = okOrThrow(await Api.channelTestStatus(provider, jobUuid));
      testResults.value = data.results || [];
      const done = data.status === "completed" || data.status === "failed" || data.status === "cancelled";
      if (!done) {
        scheduleChannelTestPoll(provider, jobUuid, key);
        return;
      }
      testing[key] = false;
      clearChannelTestPoller(key);
      ElMessage[data.okAll ? 'success' : 'warning'](data.okAll ? '渠道全部模型测试通过' : '渠道测试存在异常');
    } catch (error) {
      testing[key] = false;
      clearChannelTestPoller(key);
      ElMessage.error(apiError(error));
    }
  }, 1200));
}
async function testChannel() {
  if (!selectedName.value) return;
  const provider = selectedName.value;
  const key = `channel:${provider}`;
  testing[key] = true;
  clearChannelTestPoller(key);
  try {
    const data = okOrThrow(await Api.testChannel(provider));
    testTitle.value = `渠道测试：${provider}`;
    testResults.value = [];
    testDialog.value = true;
    ElMessage.success(`渠道测试已开始（0/${data.total || 0}）`);
    scheduleChannelTestPoll(provider, data.jobUuid, key);
  } catch (error) {
    testing[key] = false;
    ElMessage.error(apiError(error));
  }
}
async function persistProviderOrder() {
  try { okOrThrow(await Api.reorderChannels(providers.value.map((p) => p.name))); await loadList(selectedName.value); }
  catch (error) { ElMessage.error(apiError(error)); await loadList(selectedName.value); }
}
async function persistModelOrder() {
  const models = selectedProvider.value?.models || [];
  try { okOrThrow(await Api.reorderChannelModels(selectedName.value, models.map((m) => m.id))); await loadList(selectedName.value); }
  catch (error) { ElMessage.error(apiError(error)); await loadList(selectedName.value); }
}
onMounted(() => { void loadModelsDevProviders(); void loadList(); });
onBeforeUnmount(() => {
  for (const key of channelTestPollers.keys()) clearChannelTestPoller(key);
});
</script>

<template>
  <div class="h-full flex flex-col bg-macbg" v-loading="loading">
    <header class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
      <div class="flex items-center gap-2">
        <h1 class="text-base font-semibold">渠道管理</h1>
        <span class="text-xs text-macsub">模型渠道、主力/压缩模型、测试与费用配置</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="mini-chip" :class="{ 'is-muted': !modelsDev.available }" :title="modelsDev.lastError || '元数据目录会自动刷新；变更需在模型页确认同步'">元数据 · {{ modelsDevStatusText }}</span>
        <button class="mac-circle-button" :disabled="modelsDevRefreshing" title="立即刷新元数据目录" @click="refreshModelsDev">{{ modelsDevRefreshing ? '…' : '↻' }}</button>
        <button class="mac-circle-button" :disabled="loading" title="刷新渠道" @click="loadList()">↻</button>
        <button class="mac-toolbar-button" @click="openCreateProvider">＋ 添加渠道</button>
      </div>
    </header>

    <section class="px-6 pt-5 shrink-0" aria-label="渠道累计统计">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <div v-for="card in overviewCards" :key="card.label" class="mac-panel mac-shadow min-w-0 px-4 py-3">
          <div class="text-[11px] font-medium tracking-wide text-macsub">{{ card.label }}</div>
          <template v-if="card.kind === 'tokens'">
            <div class="overview-token-value" :title="card.total.title">{{ card.total.compact }}</div>
            <div class="overview-token-lines">
              <div class="overview-token-line">
                <span :title="card.input.title">输入 <b>{{ card.input.compact }}</b></span><i>·</i><span :title="card.output.title">输出 <b>{{ card.output.compact }}</b></span>
              </div>
              <div class="overview-token-line">
                <span :title="card.cache.title">缓存 <b>{{ card.cache.compact }}</b></span><i>·</i><span>占输入 <b>{{ card.pct }}</b></span>
              </div>
            </div>
          </template>
          <template v-else>
            <div class="mt-1 text-xl font-semibold tabular-nums text-zinc-900">{{ card.value }}</div>
            <div class="mt-1 truncate text-[10px] tabular-nums text-zinc-400" :title="card.sub">{{ card.sub }}</div>
          </template>
        </div>
      </div>
    </section>

    <section class="compression-strategy-wrap px-6 pt-4 shrink-0" aria-label="压缩执行顺序">
      <div class="mac-panel mac-shadow compression-strategy-panel">
        <div class="compression-strategy-head">
          <div class="compression-strategy-heading">
            <span class="compression-strategy-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M5.8 4.2h12.4A1.8 1.8 0 0 1 20 6v12a1.8 1.8 0 0 1-1.8 1.8H5.8A1.8 1.8 0 0 1 4 18V6a1.8 1.8 0 0 1 1.8-1.8Zm2 3v2h8.4v-2H7.8Zm0 4.2v2h6.8v-2H7.8Zm0 4.2v2h5v-2h-5Z"/></svg>
            </span>
            <div class="min-w-0">
              <div class="compression-strategy-title"><h2>压缩执行顺序</h2><span class="mini-chip">{{ compressionOrderItems.length }} 个候选</span></div>
              <p>从左到右依次尝试；第一个产出合格摘要的模型结束本次压缩。</p>
            </div>
          </div>
          <button class="mac-small-button" :disabled="compressionOrderSaving || !compressionOrderItems.length" @click="clearCompressionModels">清空压缩候选</button>
        </div>
        <div class="compression-strategy-body">
          <div class="compression-order-scroll">
            <div class="compression-order-track">
              <draggable
                v-if="compressionOrderItems.length"
                :list="compressionOrderItems"
                item-key="fullname"
                handle=".compression-drag"
                ghost-class="compression-drag-ghost"
                class="compression-candidate-list"
                :disabled="compressionOrderSaving"
                @end="persistCompressionOrder"
              >
                <template #item="{ element, index }">
                  <div class="compression-candidate-wrap">
                    <article class="compression-candidate">
                      <button class="compression-drag" title="拖动调整压缩优先级">⋮⋮</button>
                      <span class="compression-rank">{{ index + 1 }}</span>
                      <span class="compression-candidate-copy">
                        <strong>{{ element.name || element.id }}</strong>
                        <code>{{ element.fullname }}</code>
                        <em>第 {{ index + 1 }} 个压缩候选</em>
                      </span>
                      <span class="compression-remove-control">
                        <button
                          title="移出压缩候选，不会删除模型"
                          :aria-label="`将 ${element.name || element.id} 移出压缩候选`"
                          :disabled="compressionOrderSaving"
                          @click="confirmRemoveCompressionCandidate(element)"
                        >×</button>
                      </span>
                    </article>
                    <span v-if="index < compressionOrderItems.length - 1" class="compression-arrow" aria-hidden="true">→</span>
                  </div>
                </template>
              </draggable>
              <div v-else class="compression-empty">当前没有专用压缩候选，将直接使用当前会话或 Agent 模型。</div>
              <span class="compression-arrow compression-arrow--fallback" aria-hidden="true"><i>→</i></span>
              <article class="compression-fallback">
                <span class="compression-fallback-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><path d="M12 2.8a5.2 5.2 0 0 1 5.2 5.2v2h.7a1.8 1.8 0 0 1 1.8 1.8v7.1a1.8 1.8 0 0 1-1.8 1.8H6.1a1.8 1.8 0 0 1-1.8-1.8v-7.1A1.8 1.8 0 0 1 6.1 10h.7V8A5.2 5.2 0 0 1 12 2.8Zm0 2A3.2 3.2 0 0 0 8.8 8v2h6.4V8A3.2 3.2 0 0 0 12 4.8Z"/></svg>
                </span>
                <span class="compression-fallback-copy">
                  <em>全部候选失败后</em>
                  <strong>当前会话 / Agent 模型</strong>
                  <code>默认：{{ primaryModel || '未配置主力模型' }}</code>
                </span>
                <span class="mini-chip">固定回退</span>
              </article>
            </div>
          </div>
          <div class="compression-strategy-foot"><i></i><span>拖动候选调整优先级；右侧按钮只会移出压缩候选，不会删除模型。下方模型列表排序不会影响这里的执行顺序。</span></div>
        </div>
      </div>
    </section>

    <div class="flex-1 min-h-0 grid grid-cols-[320px_minmax(0,1fr)] gap-4 p-6 pb-7">
      <aside class="mac-panel mac-shadow min-h-0 overflow-hidden flex flex-col">
        <div v-if="!providers.length" class="flex-1 min-h-0 p-6 text-center text-sm text-macsub">暂无渠道</div>
        <draggable v-else :list="providers" item-key="name" handle=".drag-handle" ghost-class="drag-ghost" class="channel-list-scroll" @end="persistProviderOrder">
          <template #item="{ element: p }">
            <div class="provider-row mb-1 rounded-xl transition-colors" :class="selectedName === p.name ? 'is-active' : ''">
              <button class="drag-handle" title="拖动排序">⋮⋮</button>
              <button class="provider-main" @click="loadProvider(p.name)">
                <span class="provider-avatar" :class="providerTone(p)"><span>{{ providerInitial(p) }}</span></span>
                <span class="min-w-0 flex-1">
                  <span class="flex min-w-0 items-center gap-2">
                    <span class="truncate text-sm font-medium text-zinc-950">{{ p.name }}</span>
                    <span v-if="!p.enabled" class="mini-chip is-muted">停用</span>
                    <span v-if="p.primary" class="mini-chip">主力</span>
                    <span v-if="p.compression" class="mini-chip">压缩</span>
                  </span>
                  <span class="mt-0.5 block truncate text-[11px] text-zinc-500">{{ protocolLabel(p.protocol) }} · {{ p.modelCount }} 模型</span>
                  <span class="mt-1 flex items-center justify-between gap-2 text-[11px] text-zinc-400">
                    <span>{{ p.stats?.calls || 0 }} 调用 · {{ successRate(p.stats) }}</span>
                    <span>{{ fmtMoney(p.stats?.cost_usd) }}</span>
                  </span>
                </span>
              </button>
            </div>
          </template>
        </draggable>
      </aside>

      <section class="min-h-0 flex flex-col" v-loading="detailLoading">
        <div v-if="!selectedProvider" class="mac-panel p-10 text-center text-sm text-macsub">选择左侧渠道查看详情</div>
        <div v-else class="h-full min-h-0 flex flex-col gap-4">
          <div class="mac-panel mac-shadow p-5 shrink-0">
            <div class="flex items-start justify-between gap-4">
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="provider-avatar provider-avatar--lg" :class="providerTone(selectedProvider)"><span>{{ providerInitial(selectedProvider) }}</span></span>
                  <h2 class="text-lg font-semibold">{{ selectedProvider.name }}</h2>
                  <span class="mini-chip" :class="selectedProvider.enabled ? '' : 'is-muted'">{{ selectedProvider.enabled ? '启用' : '停用' }}</span>
                  <span class="mini-chip">{{ protocolLabel(selectedProvider.protocol) }}</span>
                  <span v-if="selectedProvider.primary" class="mini-chip">承载主力</span>
                  <span v-if="selectedProvider.compression" class="mini-chip">承载压缩</span>
                </div>
                <div class="mt-3 grid grid-cols-1 gap-1 text-xs text-zinc-600">
                  <div class="channel-kv truncate"><span>URL</span><code>{{ selectedProvider.baseUrl }}</code></div>
                  <div class="channel-kv"><span>Key</span><code>{{ selectedProvider.apiKeyMasked || '未配置' }}</code></div>
                </div>
                <div class="metric-strip provider-metrics mt-4">
                  <div v-for="metric in providerMetrics(selectedProvider)" :key="metric.label" class="metric-card" :class="{ 'metric-card--tokens': metric.kind === 'tokens' }">
                    <template v-if="metric.kind === 'tokens'">
                      <span>{{ metric.label }}</span>
                      <div class="provider-token-pair">
                        <div class="provider-token-value"><em>输入</em><strong :title="metric.input.title">{{ metric.input.compact }}</strong></div>
                        <div class="provider-token-value"><em>输出</em><strong :title="metric.output.title">{{ metric.output.compact }}</strong></div>
                      </div>
                      <div class="provider-token-cache">
                        <em>缓存</em><strong :title="metric.cache.title">{{ metric.cache.compact }}</strong><i>·</i><em>占输入</em><strong>{{ metric.pct }}</strong>
                      </div>
                    </template>
                    <template v-else>
                      <span>{{ metric.label }}</span>
                      <strong>{{ metric.value }}</strong>
                      <em>{{ metric.sub }}</em>
                    </template>
                  </div>
                </div>
              </div>
              <div class="flex flex-wrap justify-end gap-2">
                <button class="mac-small-button" :disabled="testing[`channel:${selectedName}`]" @click="testChannel">{{ testing[`channel:${selectedName}`] ? '测试中…' : '测试渠道' }}</button>
                <button class="mac-small-button" @click="openEditProvider">编辑渠道</button>
                <button class="mac-small-button is-danger" @click="removeProvider">删除渠道</button>
              </div>
            </div>
          </div>

          <div class="mac-panel mac-shadow overflow-hidden flex flex-col min-h-0 flex-1">
            <div class="flex items-center justify-between border-b border-macborder px-5 py-3 shrink-0">
              <div>
                <h3 class="text-sm font-semibold">模型列表</h3>
                <p class="text-xs text-macsub">拖动左侧把手排序；测试只在手动点击时调用上游。</p>
              </div>
              <div class="flex gap-2">
                <button class="mac-small-button" :disabled="!modelsDev.available || batchModelsDevLoading" @click="openModelsDevBatch">{{ batchModelsDevLoading ? '匹配中…' : '批量同步元数据' }}</button>
                <button class="mac-small-button" @click="clearCompressionModels">压缩跟随主力</button>
                <button class="mac-small-button" @click="openCreateModel">＋ 添加模型</button>
              </div>
            </div>

            <div v-if="!(selectedProvider.models || []).length" class="flex-1 min-h-0 p-8 text-center text-sm text-macsub">暂无模型</div>
            <draggable v-else :list="selectedProvider.models" item-key="id" handle=".model-drag" ghost-class="drag-ghost" class="model-list-scroll grid grid-cols-1 xl:grid-cols-2 gap-3 p-4" @end="persistModelOrder">
              <template #item="{ element: row }">
                <article class="model-card">
                  <div class="model-head">
                    <button class="model-drag" title="拖动排序">⋮⋮</button>
                    <div class="model-title-block">
                      <div class="model-title-row">
                        <button class="model-title model-title-button" :title="hasCustomModelName(row) ? row.id : modelDisplayName(row)" @click="openEditModel(row)">{{ modelDisplayName(row) }}</button>
                        <div class="model-badges" aria-label="模型状态">
                          <button class="feature-icon status-icon feature-primary" :class="{ 'is-inactive': !row.primary }" :title="row.primary ? '当前主力模型' : '设为主力模型'" @click="confirmSetPrimary(row)">
                            <svg viewBox="0 0 24 24"><path d="M12 3.2l2.5 5.1 5.6.8-4 3.9.9 5.5-5-2.6-5 2.6.9-5.5-4-3.9 5.6-.8L12 3.2z"/></svg>
                          </button>
                          <button class="feature-icon status-icon feature-compression" :class="{ 'is-inactive': !(row.compression) }" :title="(row.compression) ? '当前压缩模型' : '设为压缩模型'" @click="confirmSetCompression(row)">
                            <svg viewBox="0 0 24 24"><path d="M4.8 6.5c0-1 .8-1.8 1.8-1.8h10.8c1 0 1.8.8 1.8 1.8v11c0 1-.8 1.8-1.8 1.8H6.6c-1 0-1.8-.8-1.8-1.8v-11zm3 1.2v1.8h8.4V7.7H7.8zm0 4v1.8h6.7v-1.8H7.8zm0 4v1.8h4.9v-1.8H7.8z"/></svg>
                          </button>
                          <span v-for="feature in modelFeatures(row)" :key="feature.kind" class="feature-icon" :class="`feature-${feature.kind}`" :title="feature.label">
                            <svg v-if="feature.kind === 'multimodal'" viewBox="0 0 24 24"><path d="M5 5h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zm1 2v8.3l3.2-3.1 2.4 2.2 3.8-4.4L19 14.2V7H6z"/></svg>
                            <svg v-else viewBox="0 0 24 24"><path d="M13.4 2.8 5.2 13h5.5l-1 8.2 8.1-10.6h-5.4l1-7.8z"/></svg>
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div class="mt-3 grid grid-cols-3 gap-2 text-xs">
                    <div class="soft-stat"><span>上下文</span><strong>{{ fmtCompact(row.contextWindow) }}</strong></div>
                    <div class="soft-stat"><span>最大输出</span><strong>{{ fmtCompact(row.maxTokens) }}</strong></div>
                    <div class="soft-stat"><span>压缩触发</span><strong>{{ row.compactTriggerTokens ? fmtCompact(row.compactTriggerTokens) : '按比例' }}</strong></div>
                  </div>
                  <div class="metric-strip model-metrics mt-3">
                    <div v-for="metric in modelMetrics(row)" :key="metric.label" class="metric-card" :class="{ 'metric-card--tokens': metric.kind === 'tokens' }">
                      <template v-if="metric.kind === 'tokens'">
                        <span class="model-token-label">{{ metric.label }}</span>
                        <div class="model-token-value"><em>输入</em><strong :title="metric.input.title">{{ metric.input.compact }}</strong></div>
                        <div class="model-token-value"><em>输出</em><strong :title="metric.output.title">{{ metric.output.compact }}</strong></div>
                        <div class="model-token-value"><em>缓存</em><strong :title="metric.cache.title">{{ metric.cache.compact }}</strong><small>· {{ metric.pct }}</small></div>
                      </template>
                      <template v-else>
                        <span>{{ metric.label }}</span>
                        <strong>{{ metric.value }}</strong>
                        <em>{{ metric.sub }}</em>
                      </template>
                    </div>
                  </div>
                  <div class="rate-row mt-3">
                    <span v-for="cost in modelCost(row)" :key="cost.label"><em>{{ cost.label }}</em><strong>${{ cost.value }}/1M</strong></span>
                  </div>
                  <div v-if="modelTiers(row).length" class="tier-row">
                    <span>上下文阶梯价</span>
                    <em v-for="tier in modelTiers(row)" :key="tier.contextTokens">&gt;{{ fmtCompact(tier.contextTokens) }}：{{ tierRate(tier) }}</em>
                  </div>
                  <div class="models-dev-row">
                    <template v-if="row.modelsDev?.bound">
                      <span :class="{ 'is-update': row.modelsDev.updateAvailable }">元数据 · {{ row.modelsDev.providerId }}/{{ row.modelsDev.modelId }}{{ row.modelsDev.needsSync ? '（待同步）' : (row.modelsDev.updateAvailable ? '（有更新）' : '') }}</span>
                      <button class="model-action" :disabled="modelsDevSyncing" @click="syncModelFromModelsDev(row)">{{ row.modelsDev.needsSync ? '确认首次同步' : (row.modelsDev.updateAvailable ? '确认同步更新' : '同步元数据') }}</button>
                    </template>
                    <template v-else>
                      <span>未绑定元数据来源</span>
                      <button class="model-action" @click="openEditModel(row)">绑定元数据</button>
                    </template>
                  </div>

                  <div v-if="(row.thinkingLevels || []).length" class="thinking-row thinking-row-bottom">
                    <span>思考档位</span>
                    <div>
                      <button
                        v-for="lv in row.thinkingLevels"
                        :key="lv"
                        type="button"
                        class="thinking-level-chip"
                        :class="{ 'is-default': isDefaultThinkingLevel(row, lv) }"
                        :disabled="isDefaultThinkingUpdating(row)"
                        :title="isDefaultThinkingLevel(row, lv) ? '当前默认思考档位' : `设为默认思考档位：${thinkingLevelLabel(lv)}`"
                        @click="confirmSetDefaultThinkingLevel(row, lv)"
                      >{{ lv }}<b v-if="isDefaultThinkingLevel(row, lv)">默认</b></button>
                    </div>
                  </div>

                  <div class="model-action-row">
                    <div class="model-action-group">
                      <button class="model-action" :disabled="testing[`model:${row.fullname}`]" @click="testModel(row)">{{ testing[`model:${row.fullname}`] ? '测试中…' : '测试' }}</button>
                      <button class="model-action" @click="openEditModel(row)">编辑</button>
                      <button class="model-action is-danger" @click="removeModel(row)">删除</button>
                    </div>
                    <div class="model-action-group is-meta">
                      <button class="model-action" @click="copyModelMetadata(row)">复制元数据</button>
                      <button v-if="canPasteMetadataTo(row)" class="model-action" @click="pasteCopiedMetadataToModel(row)">粘贴元数据</button>
                    </div>
                  </div>
                </article>
              </template>
            </draggable>
          </div>
        </div>
      </section>
    </div>

    <el-dialog v-model="providerDialog" class="mac-dialog" :title="providerMode === 'create' ? '添加渠道' : '编辑渠道'" width="720px">
      <div class="dialog-grid">
        <label class="mac-field"><span>渠道名称</span><input v-model="providerForm.name" class="mac-input" placeholder="openai" /></label>
        <label class="mac-field"><span>协议</span><select v-model="providerForm.protocol" class="mac-input"><option value="chat">OpenAI Chat</option><option value="responses">OpenAI Responses</option><option value="anthropic">Anthropic</option></select></label>
        <label class="mac-field span-2"><span>Base URL</span><input v-model="providerForm.baseUrl" class="mac-input" placeholder="https://api.example.com/v1" /></label>
        <label class="mac-field span-2"><span>API Key</span><input v-model="providerForm.apiKey" class="mac-input" type="password" :placeholder="providerMode === 'edit' ? '留空表示不修改；获取模型时会复用已保存 Key' : '可留空'" /></label>
        <label class="mac-field span-2"><span>默认元数据提供者（仅作来源筛选）</span><select v-model="providerForm.modelsDevProviderId" class="mac-input"><option value="">不设置默认来源</option><option v-for="item in modelsDevProviders" :key="item.id" :value="item.id">{{ modelsDevProviderLabel(item) }}（{{ item.modelCount }}）</option></select><p class="field-help">聚合渠道仍需在每个模型上确认准确的提供者/模型来源，不能按名称自动猜测。</p></label>
        <div class="mac-field span-2">
          <div class="field-row"><span>模型列表</span><button class="mac-inline-button" :disabled="fetchingModels" @click="fetchProviderModels">{{ fetchingModels ? '获取中…' : '获取模型列表' }}</button></div>
          <textarea v-model="providerForm.modelsText" class="mac-textarea" rows="7" placeholder="gpt:GPT\nclaude:Claude"></textarea>
          <p class="field-help">每行一个模型，格式：<code>id:显示名</code></p>
        </div>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <label class="mac-checkbox"><input v-model="providerForm.enabled" type="checkbox" /><span>启用渠道</span></label>
          <div class="dialog-actions">
            <button class="mac-dialog-button" @click="providerDialog = false">取消</button>
            <button class="mac-dialog-button is-primary" :disabled="providerSaving" @click="saveProvider">{{ providerSaving ? '保存中…' : '保存' }}</button>
          </div>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="testDialog" class="mac-dialog" :title="testTitle" width="720px">
      <div class="space-y-3">
        <div v-for="result in testResults" :key="result.model" class="test-result" :class="result.ok ? 'is-ok' : 'is-bad'">
          <div class="flex items-center justify-between gap-3">
            <div class="font-mono text-sm font-semibold">{{ result.ok ? '通过' : '失败' }} · {{ result.model }}</div>
            <div class="text-xs opacity-70">{{ result.elapsedMs }}ms</div>
          </div>
          <div v-if="result.snippet" class="mt-2 rounded bg-white/60 p-2 text-xs">回复：{{ result.snippet }}</div>
          <div v-if="result.error" class="mt-2 rounded bg-white/60 p-2 text-xs font-mono break-all">{{ result.error }}</div>
        </div>
        <div v-if="!testResults.length" class="text-sm text-macsub">暂无测试结果。</div>
      </div>
      <template #footer><div class="dialog-footer is-right"><div class="dialog-actions"><button class="mac-dialog-button is-primary" @click="testDialog = false">知道了</button></div></div></template>
    </el-dialog>

    <el-dialog v-model="modelDialog" class="mac-dialog" :title="modelMode === 'create' ? '添加模型' : '编辑模型'" width="820px">
      <div class="dialog-grid">
        <label class="mac-field"><span>模型 ID</span><input v-model="modelForm.id" class="mac-input" placeholder="gpt-4.1" /></label>
        <label class="mac-field"><span>显示名</span><input v-model="modelForm.name" class="mac-input" placeholder="留空则同 ID" /></label>
        <div class="mac-field span-2"><span>模型能力</span><div class="capability-row"><label><input v-model="modelForm.capText" type="checkbox" />文本</label><label><input v-model="modelForm.capImage" type="checkbox" />图片</label><label><input v-model="modelForm.reasoning" type="checkbox" @change="onReasoningChanged" />推理</label><label><input v-model="modelForm.supportsFast" type="checkbox" />Fast</label></div></div>
        <div class="mac-field span-2 models-dev-source-field">
          <div class="field-row">
            <span>元数据来源</span>
            <div class="source-toolbar">
              <button class="mac-inline-button" :disabled="modelsDevMatchesLoading" @click="loadModelsDevMatches">{{ modelsDevMatchesLoading ? '匹配中…' : '按同名模型 ID 匹配' }}</button>
              <button v-if="modelsDevSourceMode === 'same-id'" class="mac-inline-button" @click="useManualSourceMode">手动映射</button>
              <button v-else class="mac-inline-button" @click="useSameIdSourceMode">使用同名匹配</button>
            </div>
          </div>
          <template v-if="modelsDevSourceMode === 'same-id'">
            <div class="source-match-grid">
              <div class="source-model-lock"><em>同名模型 ID</em><strong>{{ modelForm.id || '请先填写模型 ID' }}</strong></div>
              <label><em>提供者</em><select v-model="modelForm.modelsDevProviderId" class="mac-input" :disabled="modelsDevMatchesLoading || !modelForm.id" @change="onSameIdSourceProviderChanged"><option value="">{{ modelsDevMatchesLoading ? '正在匹配…' : (modelSourceCandidates.length ? '请选择提供者' : '暂无同名来源') }}</option><option v-for="candidate in modelSourceCandidates" :key="candidate.providerId" :value="candidate.providerId">{{ batchCandidateLabel(candidate) }}</option></select></label>
            </div>
            <p class="field-help" v-if="modelSourceCandidates.length === 1">已找到唯一同名来源：{{ batchCandidateLabel(modelSourceCandidates[0]) }}。点击“预览并同步”后会一次完成来源绑定与公共元数据同步。</p>
            <p class="field-help" v-else-if="modelSourceCandidates.length > 1"><template v-if="modelDefaultSourceCandidate">官方默认提供者 {{ batchCandidateLabel(modelDefaultSourceCandidate) }} 已优先选中；</template>同名模型在多个提供者下存在；只需选择实际提供者，模型 ID 固定为本地 ID，选择后会立即展示变更确认，不会猜测来源。</p>
            <p class="field-help" v-else>目录中没有同名记录时，再使用“手动映射”选择 provider 与 model ID。</p>
          </template>
          <template v-else>
            <div class="dialog-grid compact-grid source-manual-grid">
              <label><em>Provider</em><select v-model="modelForm.modelsDevProviderId" class="mac-input" @change="onManualSourceProviderChanged"><option value="">请选择提供者</option><option v-for="item in modelsDevProviders" :key="item.id" :value="item.id">{{ modelsDevProviderLabel(item) }}</option></select></label>
              <label><em>Model ID</em><select v-model="modelForm.modelsDevModelId" class="mac-input" :disabled="modelsDevLoading || !modelForm.modelsDevProviderId" @change="onManualSourceModelChanged"><option value="">{{ modelsDevLoading ? '加载中…' : '请选择模型 ID' }}</option><option v-for="item in modelsDevModels" :key="item.id" :value="item.id">{{ item.name }} · {{ item.id }}</option></select></label>
            </div>
            <p class="field-help">仅在本地模型 ID 与目录不一致时使用手动映射；两个选择框使用相同控件，来源仍不会改变上游模型 ID 或渠道路由。</p>
          </template>
          <div class="source-action-row">
            <span>来源不会随着普通“保存”单独写入；同名提供者选定后会直接进入变更确认。</span>
            <button v-if="modelMode === 'edit'" class="mac-dialog-button is-primary" :disabled="modelsDevSyncing || !modelFormSource()" @click="syncModelFormModelsDev">{{ modelsDevSyncing ? '同步中…' : '预览并同步' }}</button>
          </div>
        </div>
        <div v-if="modelForm.reasoning" class="dialog-grid span-2 compact-grid">
          <label class="mac-field"><span>支持思考强度</span><input v-model="modelForm.thinkingLevels" class="mac-input" placeholder="high,xhigh,max" @blur="syncThinkingLevelsFromText({ forceLast: true })" /></label>
          <label class="mac-field"><span>默认思考强度</span><select v-model="modelForm.defaultThinkingLevel" class="mac-input" :disabled="!modelThinkingLevelOptions.length"><option value="">{{ modelThinkingLevelOptions.length ? '自动=最后一档' : '先填写支持思考强度' }}</option><option v-for="lv in modelThinkingLevelOptions" :key="lv" :value="lv">{{ lv }}</option></select></label>
        </div>
        <div class="dialog-grid span-2 triple-grid">
          <label class="mac-field"><span>上下文窗口</span><input v-model.number="modelForm.contextWindow" class="mac-input" type="number" min="1" /></label>
          <label class="mac-field"><span>压缩触发 Token</span><input v-model.number="modelForm.compactTriggerTokens" class="mac-input" type="number" min="0" placeholder="0=按全局比例" /></label>
          <label class="mac-field"><span>最大输出</span><input v-model.number="modelForm.maxTokens" class="mac-input" type="number" min="1" /></label>
        </div>
        <div class="mac-field span-2"><span>费用 / 1M tokens</span><div class="price-grid"><label><em>输入</em><input v-model.number="modelForm.cost.input" type="number" min="0" step="0.0001" /></label><label><em>输出</em><input v-model.number="modelForm.cost.output" type="number" min="0" step="0.0001" /></label><label><em>缓存读</em><input v-model.number="modelForm.cost.cacheRead" type="number" min="0" step="0.0001" /></label><label><em>缓存写</em><input v-model.number="modelForm.cost.cacheWrite" type="number" min="0" step="0.0001" /></label></div></div>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <div class="dialog-actions">
            <button v-if="copiedModelMetadata && copiedModelMetadataSource !== `${selectedName}/${modelForm.oldId || modelForm.id}`" class="mac-dialog-button" @click="pasteModelMetadata">粘贴元数据</button>
          </div>
          <div class="dialog-actions">
            <button class="mac-dialog-button" @click="modelDialog = false">取消</button>
            <button class="mac-dialog-button is-primary" :disabled="modelSaving" @click="saveModel">{{ modelSaving ? '保存中…' : '保存' }}</button>
          </div>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="batchModelsDevDialog" class="mac-dialog batch-models-dev-dialog" width="1060px" :show-close="false">
      <template #header="{ close, titleId, titleClass }">
        <div class="batch-dialog-header">
          <div class="batch-dialog-title-group">
            <span class="batch-dialog-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6.5 12 3l8 3.5-8 3.5-8-3.5Z"/><path d="M4 12 12 15.5 20 12M4 17.5 12 21l8-3.5"/></svg>
            </span>
            <div>
              <h2 :id="titleId" :class="titleClass">批量同步元数据</h2>
              <p>已按本地模型 ID 精确匹配；有歧义时只需改选提供者。</p>
            </div>
          </div>
          <div class="batch-dialog-header-state">
            <span class="batch-dialog-ready-count">{{ batchModelsDevSelectedCount }} 项已就绪<span v-if="batchModelsDevUnresolvedCount"> · 待选 {{ batchModelsDevUnresolvedCount }} 项</span></span>
            <button class="batch-dialog-close" type="button" title="关闭" aria-label="关闭" @click="close">×</button>
          </div>
        </div>
      </template>
      <div class="batch-sync-shell" v-loading="batchModelsDevLoading">
        <div class="batch-sync-quiet-intro">
          <span><strong>一键同步当前预览。</strong> 唯一匹配与官方默认已自动选好。</span>
          <span class="batch-sync-fresh" :class="{ 'is-loading': batchModelsDevPreviewing }"><i></i>{{ batchModelsDevPreviewing ? '正在更新预览' : '预览已是最新' }}</span>
        </div>
        <div v-if="!batchModelsDevItems.length && !batchModelsDevLoading" class="batch-empty">该渠道暂无可匹配的模型。</div>
        <div v-else class="batch-sync-list">
          <article v-for="item in batchModelsDevItems" :key="item.modelId" class="batch-sync-card">
            <div class="batch-sync-card-top">
              <div class="batch-sync-model">
                <span class="batch-sync-model-label">本地模型</span>
                <strong>{{ item.name || item.modelId }}</strong>
                <code>{{ item.modelId }}</code>
              </div>
              <div class="batch-source-panel">
                <div class="batch-source-panel-head">
                  <span class="batch-source-label">{{ item.currentSource && item.currentSource.modelId !== item.modelId ? '元数据来源' : '元数据提供者' }}</span>
                  <span v-if="item.currentSource && item.currentSource.modelId !== item.modelId" class="batch-source-tag is-manual">手动映射</span>
                  <span v-else-if="item.candidates.length === 1" class="batch-source-tag is-unique">唯一匹配</span>
                  <span v-else-if="item.candidates.length > 1 && batchSourceUsesDefault(item)" class="batch-source-tag">官方默认</span>
                </div>
                <div class="batch-source-control">
                  <template v-if="item.currentSource && item.currentSource.modelId !== item.modelId">
                    <span class="batch-source-static is-manual"><i class="batch-source-static-mark">↗</i>{{ item.currentSource.providerId }} · {{ item.currentSource.modelId }}</span>
                  </template>
                  <template v-else-if="item.candidates.length === 1">
                    <span class="batch-source-static"><i class="batch-source-static-mark">✓</i>{{ batchCandidateLabel(item.candidates[0]) }}</span>
                  </template>
                  <template v-else-if="item.candidates.length > 1">
                    <el-select v-model="item.selectedProviderId" class="batch-source-select" filterable placeholder="选择提供者" @change="onBatchSourceProviderChanged">
                      <el-option v-for="candidate in item.candidates" :key="candidate.providerId" :label="batchCandidateLabel(candidate)" :value="candidate.providerId" />
                    </el-select>
                  </template>
                  <span v-else class="batch-source-missing">未找到同名记录；可在单模型中手动映射</span>
                </div>
              </div>
            </div>
            <div class="batch-sync-change-block">
              <template v-if="batchPreviewFor(item.modelId)">
                <div class="batch-sync-change-heading">
                  {{ batchPreviewRows(item).length ? '将同步' : '来源已就绪' }}
                  <span>{{ batchPreviewRows(item).length ? `${batchPreviewRows(item).length} 项公共元数据` : '本次不改公共字段' }}</span>
                </div>
                <div v-if="batchPreviewRows(item).length" class="batch-sync-change-grid">
                  <div v-for="change in batchPreviewRows(item)" :key="change.label" class="batch-sync-change">
                    <span class="batch-sync-change-name">{{ change.label }}</span>
                    <span class="batch-sync-change-value"><span class="old" :title="change.current">{{ change.current }}</span><span class="arrow">→</span><span class="new" :title="change.proposed">{{ change.proposed }}</span></span>
                  </div>
                </div>
                <div v-else class="batch-sync-no-change">将只记录来源绑定，不会覆盖现有配置。</div>
              </template>
              <template v-else>
                <div class="batch-sync-change-heading">变更预览<span>{{ batchModelsDevPreviewing && batchSourceFor(item) ? '正在更新元数据预览' : '等待选择来源' }}</span></div>
                <div class="batch-sync-no-change">{{ batchModelsDevPreviewing && batchSourceFor(item) ? '正在根据已选提供者生成差异。' : batchSourceLabel(item) }}</div>
              </template>
            </div>
          </article>
        </div>
        <p class="batch-sync-note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z"/><path d="M12 10v5M12 7h.01"/></svg>同步会写入本次预览的公共元数据，包括 Fast 支持、请求配置与有效费率；不会修改上游模型 ID、渠道地址或主力/压缩模型。首个价格阶梯的 Token 阈值会同步为压缩触发 Token。</p>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <span class="batch-footer-status">{{ batchModelsDevPreviewing ? '正在更新预览…' : `将同步 ${batchModelsDevSelectedCount} 项；未选择的模型不会改动` }}</span>
          <div class="dialog-actions"><button class="mac-dialog-button" @click="batchModelsDevDialog = false">取消</button><button class="mac-dialog-button is-primary" :disabled="batchModelsDevSyncing || batchModelsDevPreviewing || !batchModelsDevSelectedCount || (batchModelsDevPreview.items || []).length !== batchModelsDevSelectedCount" @click="syncBatchModelsDev">{{ batchModelsDevSyncing ? '同步中…' : `确认同步 ${batchModelsDevSelectedCount} 项` }}</button></div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mac-circle-button,
.mac-toolbar-button,
.mac-small-button,
.model-action,
.mac-dialog-button,
.mac-inline-button {
  border: 1px solid rgba(212, 212, 216, 0.9);
  border-radius: 999px;
  background: linear-gradient(180deg, rgba(255,255,255,.94), rgba(244,244,245,.88));
  color: #3f3f46;
  font-size: 12px;
  font-weight: 500;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.9), 0 1px 2px rgba(24,24,27,.04);
  transition: transform .14s ease, box-shadow .14s ease, border-color .14s ease, opacity .14s ease;
}
.mac-circle-button { width: 32px; height: 32px; }
.mac-toolbar-button { height: 32px; padding: 0 13px; }
.mac-small-button { height: 30px; padding: 0 12px; }
.model-action { height: 27px; padding: 0 11px; font-size: 11px; }
.mac-inline-button { height: 24px; padding: 0 9px; font-size: 11px; }
.mac-dialog-button { height: 30px; padding: 0 15px; }
.mac-circle-button:hover:not(:disabled),
.mac-toolbar-button:hover:not(:disabled),
.mac-small-button:hover:not(:disabled),
.model-action:hover:not(:disabled),
.mac-dialog-button:hover:not(:disabled),
.mac-inline-button:hover:not(:disabled) { transform: translateY(-1px); box-shadow: inset 0 1px 0 rgba(255,255,255,.95), 0 6px 16px rgba(24,24,27,.08); }
.mac-dialog-button.is-primary { border-color: rgba(82,82,91,.22); background: linear-gradient(180deg, #3f3f46, #27272a); color: #fff; }
.is-danger { color: #a61b1b; border-color: rgba(239,68,68,.22); }
button:disabled { cursor: not-allowed; opacity: .48; }
.compression-strategy-panel { overflow: hidden; }
.compression-strategy-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; border-bottom: 1px solid rgba(228,228,231,.86); padding: 13px 16px 11px; }
.compression-strategy-heading { display: flex; min-width: 0; align-items: center; gap: 11px; }
.compression-strategy-mark { display: grid; width: 34px; height: 34px; flex: 0 0 auto; place-items: center; border: 1px solid #e4e4e7; border-radius: 12px; background: linear-gradient(145deg, #fff, #f4f4f5); color: #52525b; box-shadow: 0 2px 6px rgba(24,24,27,.06); }
.compression-strategy-mark svg { width: 18px; height: 18px; fill: currentColor; }
.compression-strategy-title { display: flex; align-items: center; gap: 8px; }
.compression-strategy-title h2 { margin: 0; color: #1d1d1f; font-size: 14px; font-weight: 680; letter-spacing: -.01em; }
.compression-strategy-heading p { margin: 3px 0 0; color: #86868b; font-size: 11px; line-height: 1.4; }
.compression-strategy-body { background: linear-gradient(180deg, rgba(250,250,251,.58), rgba(255,255,255,.92)); padding: 13px 16px 10px; }
.compression-order-scroll { overflow-x: auto; overscroll-behavior-x: contain; padding: 2px 0; }
.compression-order-track { display: flex; min-width: 960px; align-items: stretch; }
.compression-candidate-list { display: flex; min-width: 0; flex: 1 1 auto; align-items: stretch; }
.compression-candidate-wrap { display: flex; min-width: 255px; flex: 1 1 270px; align-items: stretch; }
.compression-candidate { display: grid; min-width: 0; min-height: 76px; flex: 1 1 auto; grid-template-columns: 18px 30px minmax(0,1fr) 24px; align-items: center; gap: 6px; border: 1px solid rgba(228,228,231,.96); border-radius: 15px; background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(250,250,250,.92)); padding: 9px 6px; box-shadow: inset 0 1px 0 rgba(255,255,255,.9), 0 4px 14px rgba(24,24,27,.045); transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease, opacity .16s ease; }
.compression-candidate:hover { transform: translateY(-1px); border-color: #a1a1aa; box-shadow: 0 8px 22px rgba(24,24,27,.07); }
.compression-drag { width: 18px; height: 100%; border: 0; background: transparent; color: #a1a1aa; cursor: grab; font-size: 14px; letter-spacing: -3px; }
.compression-drag:active { cursor: grabbing; }
.compression-drag-ghost { opacity: .45; }
.compression-rank { display: grid; width: 31px; height: 31px; place-items: center; border-radius: 11px; background: linear-gradient(180deg, #f4f4f5, #e4e4e7); color: #52525b; font-size: 12px; font-weight: 760; box-shadow: inset 0 0 0 1px rgba(255,255,255,.72); }
.compression-candidate-copy { display: block; min-width: 0; }
.compression-candidate-copy strong { display: block; overflow: hidden; color: #27272a; font-size: 13px; font-weight: 690; text-overflow: ellipsis; white-space: nowrap; }
.compression-candidate-copy code { display: block; margin-top: 3px; overflow: hidden; color: #8b8b91; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 9.5px; text-overflow: ellipsis; white-space: nowrap; }
.compression-candidate-copy em { display: inline-flex; margin-top: 5px; color: #71717a; font-size: 9.5px; font-style: normal; font-weight: 620; }
.compression-remove-control { display: grid; place-items: center; }
.compression-remove-control button { display: grid; width: 24px; height: 24px; place-items: center; border: 1px solid #e4e4e7; border-radius: 8px; background: rgba(255,255,255,.84); color: #71717a; font-size: 16px; line-height: 1; transition: border-color .14s ease, color .14s ease, background .14s ease, transform .14s ease; }
.compression-remove-control button:hover:not(:disabled) { transform: translateY(-1px); border-color: #a1a1aa; background: #f4f4f5; color: #3f3f46; }
.compression-arrow { display: grid; width: 30px; flex: 0 0 30px; place-items: center; color: #bbb8c2; font-size: 17px; }
.compression-arrow--fallback { position: relative; width: 38px; flex-basis: 38px; }
.compression-arrow--fallback::before { width: 100%; border-top: 1px dashed #d4d4d8; content: ""; }
.compression-arrow--fallback i { position: absolute; background: #fff; padding: 0 5px; color: #a1a1aa; font-style: normal; }
.compression-fallback { display: grid; width: 260px; min-width: 260px; min-height: 76px; grid-template-columns: 35px minmax(0,1fr) auto; align-items: center; gap: 8px; border: 1px dashed #d4d4d8; border-radius: 15px; background: rgba(250,250,250,.86); padding: 9px; }
.compression-fallback-icon { display: grid; width: 34px; height: 34px; place-items: center; border-radius: 12px; background: linear-gradient(180deg, #f4f4f5, #e4e4e7); color: #52525b; box-shadow: inset 0 0 0 1px rgba(212,212,216,.72); }
.compression-fallback-icon svg { width: 16px; height: 16px; fill: currentColor; }
.compression-fallback-copy { display: block; min-width: 0; }
.compression-fallback-copy em { display: block; color: #a1a1aa; font-size: 9px; font-style: normal; font-weight: 700; letter-spacing: .07em; text-transform: uppercase; }
.compression-fallback-copy strong { display: block; margin-top: 3px; overflow: hidden; color: #3f3f46; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.compression-fallback-copy code { display: block; margin-top: 3px; overflow: hidden; color: #8b8b91; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.compression-empty { display: grid; min-width: 420px; min-height: 76px; flex: 1 1 auto; place-items: center; border: 1px dashed #d4d4d8; border-radius: 15px; background: rgba(250,250,250,.72); color: #86868b; font-size: 11px; }
.compression-strategy-foot { display: flex; align-items: center; gap: 7px; margin-top: 9px; color: #8a8990; font-size: 10px; }
.compression-strategy-foot i { width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: #a1a1aa; box-shadow: 0 0 0 3px rgba(161,161,170,.12); }
.provider-row { display: flex; align-items: stretch; gap: 3px; }
.provider-row:hover { background: rgba(244,244,245,.78); }
.provider-row.is-active { background: rgba(228,228,231,.78); }
.provider-main { display: flex; flex: 1; min-width: 0; align-items: center; gap: 12px; padding: 10px 9px 10px 3px; text-align: left; }
.drag-handle,
.model-drag { width: 25px; flex: 0 0 auto; color: #a1a1aa; cursor: grab; font-size: 14px; letter-spacing: -3px; }
.drag-handle:active,
.model-drag:active { cursor: grabbing; }
.drag-ghost { opacity: .45; }
.provider-avatar { position: relative; display: grid; width: 36px; height: 36px; flex: 0 0 auto; place-items: center; border-radius: 13px; background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(244,244,245,.9)); color: #52525b; font-size: 11px; font-weight: 700; letter-spacing: .03em; box-shadow: inset 0 1px 0 rgba(255,255,255,.95), 0 1px 3px rgba(24,24,27,.08); }
.provider-avatar::after { content: ""; position: absolute; right: -1px; bottom: -1px; width: 10px; height: 10px; border: 2px solid #fff; border-radius: 999px; background: #a1a1aa; }
.provider-avatar.is-enabled::after { background: #34c759; }
.provider-avatar.is-primary::after { background: #007aff; }
.provider-avatar.is-compression::after { background: #8e8e93; }
.provider-avatar.is-disabled { opacity: .62; }
.provider-avatar.is-disabled::after { background: #ff3b30; }
.provider-avatar--lg { width: 38px; height: 38px; border-radius: 14px; }
.mini-chip { display: inline-flex; align-items: center; height: 19px; padding: 0 7px; border-radius: 999px; background: rgba(244,244,245,.9); color: #52525b; font-size: 11px; box-shadow: inset 0 0 0 1px rgba(228,228,231,.9); }
.mini-chip.is-muted { color: #a1a1aa; }
.channel-kv { display: flex; min-width: 0; gap: 8px; align-items: baseline; }
.channel-kv > span { width: 48px; flex: 0 0 auto; color: #a1a1aa; font-size: 11px; letter-spacing: .02em; }
.channel-kv > code { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #3f3f46; }
.overview-token-value { margin-top: 4px; color: #18181b; font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums; }
.overview-token-lines { display: grid; gap: 1px; margin-top: 3px; color: #a1a1aa; font-size: 10px; font-variant-numeric: tabular-nums; }
.overview-token-line { display: flex; min-width: 0; align-items: center; gap: 5px; white-space: nowrap; }
.overview-token-line span { min-width: 0; }
.overview-token-line b { color: #71717a; font-weight: 620; }
.overview-token-line i { color: #d4d4d8; font-style: normal; }
.metric-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.provider-metrics { grid-template-columns: minmax(0, 2fr) repeat(3, minmax(0, 1fr)); }
.metric-card { min-width: 0; border: 1px solid rgba(228,228,231,.8); border-radius: 14px; background: linear-gradient(180deg, rgba(255,255,255,.92), rgba(248,248,249,.86)); padding: 9px 10px; }
.metric-card span { display: block; color: #a1a1aa; font-size: 10px; letter-spacing: .04em; text-transform: uppercase; }
.metric-card strong { display: block; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #27272a; font-size: 14px; font-weight: 650; }
.metric-card em { display: block; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #a1a1aa; font-size: 10px; font-style: normal; }
.provider-token-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 5px; }
.provider-token-value { min-width: 0; }
.provider-token-value em { margin: 0; color: #a1a1aa; font-size: 9px; letter-spacing: .03em; }
.provider-token-value strong { margin-top: 1px; color: #27272a; font-size: 14px; font-weight: 650; font-variant-numeric: tabular-nums; }
.provider-token-cache { display: flex; min-width: 0; align-items: baseline; gap: 4px; margin-top: 4px; border-top: 1px solid rgba(228,228,231,.62); padding-top: 4px; white-space: nowrap; }
.provider-token-cache em { display: inline; margin: 0; overflow: visible; color: #a1a1aa; font-size: 9px; }
.provider-token-cache strong { display: inline; margin: 0; overflow: visible; color: #71717a; font-size: 10px; font-weight: 630; font-variant-numeric: tabular-nums; }
.provider-token-cache i { color: #d4d4d8; font-size: 9px; font-style: normal; }
@media (max-width: 1399px) {
  .provider-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .provider-metrics .metric-card--tokens { grid-column: 1 / -1; }
}
@media (min-width: 1280px) and (max-width: 1399px) {
  .model-list-scroll { grid-template-columns: minmax(0, 1fr); }
}
.channel-list-scroll,
.model-list-scroll { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.channel-list-scroll { padding: 8px; }
.model-list-scroll { align-content: start; }
.model-card { border: 1px solid rgba(228,228,231,.9); border-radius: 18px; background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(250,250,250,.9)); padding: 14px; transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease; }
.model-card:hover { border-color: rgba(161,161,170,.85); box-shadow: 0 8px 24px rgba(24,24,27,.06); transform: translateY(-1px); }
.model-head { display: flex; min-width: 0; align-items: flex-start; gap: 9px; }
.model-title-block { min-width: 0; flex: 1; }
.model-title-row { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; gap: 12px; }
.model-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #18181b; font-size: 14px; font-weight: 720; letter-spacing: -.01em; }
.model-title-button { display: block; max-width: 100%; border: 0; background: transparent; padding: 0; text-align: left; cursor: pointer; transition: color .14s ease, text-decoration-color .14s ease; }
.model-title-button:hover { color: #2563eb; text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 3px; }
.model-badges { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 6px; padding-top: 1px; }
.feature-icon { display: inline-grid; width: 23px; height: 23px; place-items: center; border: 0; border-radius: 999px; box-shadow: inset 0 0 0 1px rgba(255,255,255,.48), 0 4px 10px rgba(24,24,27,.08); }
.feature-icon svg { width: 13px; height: 13px; fill: currentColor; }
.status-icon { cursor: pointer; transition: transform .14s ease, filter .14s ease, box-shadow .14s ease; }
.status-icon:hover { transform: translateY(-1px); filter: saturate(1.08); box-shadow: inset 0 0 0 1px rgba(255,255,255,.6), 0 7px 16px rgba(24,24,27,.12); }
.status-icon.is-inactive { color: #a1a1aa; background: linear-gradient(180deg, rgba(244,244,245,.95), rgba(228,228,231,.9)); box-shadow: inset 0 0 0 1px rgba(212,212,216,.9); }
.feature-primary:not(.is-inactive) { color: #075985; background: linear-gradient(180deg, #e0f2fe, #bae6fd); }
.feature-compression:not(.is-inactive) { color: #6d28d9; background: linear-gradient(180deg, #ede9fe, #ddd6fe); }
.feature-multimodal { color: #047857; background: linear-gradient(180deg, #d1fae5, #a7f3d0); }
.feature-fast { color: #b45309; background: linear-gradient(180deg, #fef3c7, #fde68a); }
.thinking-row { display: flex; min-width: 0; align-items: center; gap: 8px; margin-top: 8px; color: #71717a; font-size: 11px; }
.thinking-row-bottom { margin-top: 12px; }
.thinking-row > span { flex: 0 0 auto; color: #a1a1aa; letter-spacing: .04em; }
.thinking-row > div { display: flex; min-width: 0; flex-wrap: wrap; gap: 5px; }
.thinking-level-chip { display: inline-flex; align-items: center; gap: 4px; border: 0; border-radius: 999px; background: rgba(244,244,245,.82); padding: 2px 7px; color: #52525b; font: inherit; font-style: normal; line-height: 1.35; cursor: pointer; box-shadow: inset 0 0 0 1px rgba(228,228,231,.72); transition: background .14s ease, box-shadow .14s ease, color .14s ease, transform .14s ease; }
.thinking-level-chip:hover:not(:disabled):not(.is-default) { background: rgba(228,228,231,.92); color: #27272a; transform: translateY(-1px); box-shadow: inset 0 0 0 1px rgba(161,161,170,.56), 0 3px 8px rgba(24,24,27,.08); }
.thinking-level-chip:focus-visible { outline: 2px solid rgba(37,99,235,.45); outline-offset: 2px; }
.thinking-level-chip.is-default { background: linear-gradient(180deg, rgba(239,246,255,.98), rgba(219,234,254,.9)); color: #1d4ed8; cursor: default; box-shadow: inset 0 0 0 1px rgba(147,197,253,.6); }
.thinking-level-chip:disabled { cursor: wait; }
.thinking-level-chip b { border-radius: 999px; background: rgba(255,255,255,.7); padding: 0 4px; font-size: 9px; font-weight: 700; letter-spacing: .04em; }
.soft-stat { border-radius: 13px; background: rgba(244,244,245,.68); padding: 8px 10px; }
.soft-stat span { display: block; color: #a1a1aa; font-size: 10px; }
.soft-stat strong { display: block; margin-top: 2px; color: #3f3f46; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
.model-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
.model-metrics .metric-card { border-radius: 11px; padding: 6px 7px; background: linear-gradient(180deg, rgba(255,255,255,.86), rgba(248,248,249,.72)); }
.model-metrics .metric-card span { font-size: 9px; letter-spacing: .035em; }
.model-metrics .metric-card strong { margin-top: 2px; font-size: 11px; font-weight: 650; }
.model-metrics .metric-card em { margin-top: 1px; font-size: 9px; }
.model-metrics .metric-card--tokens { display: grid; grid-column: 1 / -1; grid-template-columns: 50px repeat(3, minmax(0, 1fr)); align-items: center; gap: 0; padding: 7px 9px; }
.model-metrics .model-token-label { color: #a1a1aa; font-size: 9px; letter-spacing: .035em; }
.model-token-value { min-width: 0; border-left: 1px solid rgba(228,228,231,.72); padding-left: 9px; }
.model-metrics .model-token-value em { display: block; margin: 0; color: #a1a1aa; font-size: 8.5px; letter-spacing: .02em; }
.model-metrics .model-token-value strong { display: inline; margin: 0; overflow: visible; color: #3f3f46; font-size: 11px; font-weight: 650; font-variant-numeric: tabular-nums; }
.model-token-value small { margin-left: 3px; color: #a1a1aa; font-size: 8.5px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.model-action-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px 12px; margin-top: 12px; border-top: 1px solid rgba(228,228,231,.9); padding-top: 12px; }
.model-action-group { display: inline-flex; flex-wrap: wrap; gap: 8px; }
.model-action-group.is-meta { margin-left: auto; justify-content: flex-end; }
.rate-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; }
.rate-row span { min-width: 0; border-radius: 11px; background: rgba(250,250,250,.9); padding: 6px 7px; box-shadow: inset 0 0 0 1px rgba(228,228,231,.68); }
.rate-row em { display: block; color: #a1a1aa; font-size: 10px; font-style: normal; }
.rate-row strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #52525b; font-size: 10px; font-weight: 600; }
.tier-row { display: flex; flex-wrap: wrap; gap: 5px 8px; margin-top: 8px; color: #71717a; font-size: 10px; }
.tier-row > span { color: #a1a1aa; letter-spacing: .04em; }
.tier-row em { border-radius: 999px; background: rgba(255,247,237,.88); padding: 3px 7px; color: #9a3412; font-style: normal; box-shadow: inset 0 0 0 1px rgba(251,146,60,.18); }
.models-dev-row { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 8px; margin-top: 10px; border-radius: 10px; background: rgba(240,249,255,.66); padding: 6px 7px; color: #64748b; font-size: 10px; box-shadow: inset 0 0 0 1px rgba(186,230,253,.72); }
.models-dev-row > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.models-dev-row > span.is-update { color: #b45309; font-weight: 650; }
:deep(.models-dev-preview) { display: grid; gap: 10px; color: #52525b; font-size: 13px; line-height: 1.45; }
:deep(.models-dev-preview p) { margin: 0; }
:deep(.models-dev-preview ul) { display: grid; max-height: 240px; gap: 7px; overflow: auto; margin: 0; padding: 0; list-style: none; }
:deep(.models-dev-preview-row) { display: grid; gap: 3px; border-radius: 8px; background: rgba(244,244,245,.82); padding: 7px 8px; }
:deep(.models-dev-preview-row strong) { color: #27272a; font-size: 12px; }
:deep(.models-dev-preview-row span) { overflow-wrap: anywhere; color: #71717a; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
:deep(.models-dev-preview-note) { color: #71717a; font-size: 11px; }
:deep(.mac-dialog.el-dialog) { --el-dialog-padding-primary: 0; padding: 0 !important; border-radius: 22px; background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(246,246,247,.96)); box-shadow: 0 24px 80px rgba(24,24,27,.22), inset 0 1px 0 rgba(255,255,255,.9); overflow: hidden; }
:deep(.mac-dialog .el-dialog__header) { margin: 0; padding: 18px 20px 12px; border-bottom: 1px solid rgba(228,228,231,.82); }
:deep(.mac-dialog .el-dialog__title) { color: #18181b; font-size: 15px; font-weight: 650; }
:deep(.mac-dialog .el-dialog__body) { padding: 16px 20px; }
:deep(.mac-dialog .el-dialog__footer) { padding: 12px 20px 16px; border-top: 1px solid rgba(228,228,231,.75); }
:deep(.batch-models-dev-dialog.el-dialog) { max-width: calc(100vw - 32px); border-radius: 20px; }
:deep(.batch-models-dev-dialog .el-dialog__header) { padding: 0; border-bottom: 1px solid rgba(228,228,231,.86); }
:deep(.batch-models-dev-dialog .el-dialog__title) { color: #1d232d; font-size: 17px; font-weight: 680; letter-spacing: -.015em; }
:deep(.batch-models-dev-dialog .el-dialog__body) { padding: 18px 26px 14px; }
:deep(.batch-models-dev-dialog .el-dialog__footer) { padding: 15px 26px 18px; background: rgba(252,252,253,.74); }
.dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; width: 100%; }
.dialog-footer.is-right { justify-content: flex-end; }
.dialog-actions { display: inline-flex; align-items: center; justify-content: flex-end; gap: 10px; }
.dialog-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.compact-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.triple-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.span-2 { grid-column: span 2 / span 2; }
.mac-field { display: flex; flex-direction: column; gap: 7px; border: 1px solid rgba(228,228,231,.78); border-radius: 15px; background: rgba(255,255,255,.72); padding: 9px 10px; }
.mac-field > span, .field-row > span { color: #71717a; font-size: 12px; font-weight: 520; }
.mac-field .dialog-grid > label { display: flex; min-width: 0; flex-direction: column; gap: 5px; }
.mac-field .dialog-grid > label em { color: #a1a1aa; font-size: 10px; font-style: normal; }
.field-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.mac-input, .mac-textarea, .price-grid input { width: 100%; border: 1px solid rgba(212,212,216,.62); border-radius: 12px; background: rgba(244,244,245,.72); color: #27272a; outline: none; }
.mac-input { height: 34px; padding: 0 10px; }
.mac-textarea { padding: 9px 10px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.mac-input:focus, .mac-textarea:focus, .price-grid input:focus { border-color: rgba(0,122,255,.38); background: #fff; box-shadow: 0 0 0 3px rgba(0,122,255,.08); }
.mac-checkbox, .capability-row label { display: inline-flex; align-items: center; gap: 8px; color: #52525b; font-size: 12px; }
.mac-checkbox { min-height: 30px; }
.field-help { margin-top: 6px; color: #a1a1aa; font-size: 11px; }
.models-dev-source-field { background: linear-gradient(180deg, rgba(240,249,255,.82), rgba(255,255,255,.74)); box-shadow: inset 0 0 0 1px rgba(186,230,253,.55); }
.source-toolbar { display: inline-flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 6px; }
.source-match-grid { display: grid; grid-template-columns: minmax(0,.8fr) minmax(0,1.2fr); gap: 9px; }
.source-match-grid > label, .source-manual-grid > label { display: flex; min-width: 0; flex-direction: column; gap: 5px; }
.source-match-grid em, .source-manual-grid em, .source-model-lock em { color: #64748b; font-size: 10px; font-style: normal; }
.source-model-lock { display: flex; min-width: 0; flex-direction: column; justify-content: center; gap: 4px; border: 1px dashed rgba(125,211,252,.84); border-radius: 12px; background: rgba(255,255,255,.68); padding: 7px 10px; }
.source-model-lock strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #0f172a; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; font-weight: 600; }
.source-action-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; border-top: 1px solid rgba(186,230,253,.72); padding-top: 9px; }
.source-action-row > span { color: #64748b; font-size: 11px; line-height: 1.4; }
.batch-dialog-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 20px 26px 18px; }
.batch-dialog-title-group { display: flex; min-width: 0; align-items: center; gap: 12px; }
.batch-dialog-mark { display: grid; width: 31px; height: 31px; flex: 0 0 auto; place-items: center; border: 1px solid #d4e7ff; border-radius: 10px; color: #1677ff; background: linear-gradient(145deg, #fff, #eef6ff); box-shadow: 0 2px 5px rgba(22,119,255,.08); }
.batch-dialog-mark svg { width: 17px; height: 17px; }
.batch-dialog-title-group h2 { margin: 0; color: #1d232d; font-size: 17px; font-weight: 680; letter-spacing: -.015em; }
.batch-dialog-title-group p { margin: 3px 0 0; color: #79808c; font-size: 12px; line-height: 1.45; }
.batch-dialog-header-state { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 10px; }
.batch-dialog-ready-count { border: 1px solid #cdeedc; border-radius: 999px; background: #eefaf4; padding: 7px 11px; color: #16825d; font-size: 12px; font-weight: 650; font-variant-numeric: tabular-nums; white-space: nowrap; }
.batch-dialog-close { display: grid; width: 28px; height: 28px; place-items: center; border: 0; border-radius: 8px; color: #8a919d; background: transparent; font-size: 19px; line-height: 1; transition: color .14s ease, background .14s ease; }
.batch-dialog-close:hover { color: #374151; background: rgba(244,244,245,.9); }
.batch-sync-shell { display: grid; gap: 14px; }
.batch-sync-quiet-intro { display: flex; align-items: center; justify-content: space-between; gap: 16px; color: #66707e; font-size: 12px; line-height: 1.5; }
.batch-sync-quiet-intro strong { color: #4b5563; font-weight: 650; }
.batch-sync-fresh { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 7px; color: #7c8490; white-space: nowrap; }
.batch-sync-fresh i { width: 6px; height: 6px; border-radius: 50%; background: #49b78a; box-shadow: 0 0 0 3px rgba(73,183,138,.13); }
.batch-sync-fresh.is-loading i { background: #e5a51b; box-shadow: 0 0 0 3px rgba(229,165,27,.14); }
.batch-empty { padding: 34px; color: #71717a; font-size: 14px; text-align: center; }
.batch-sync-list { display: grid; grid-auto-rows: max-content; align-content: start; max-height: min(58vh, 620px); gap: 10px; overflow: auto; padding-right: 3px; }
.batch-sync-card { overflow: hidden; border: 1px solid #e5e8ed; border-radius: 14px; background: #fff; box-shadow: 0 1px 2px rgba(31,41,55,.025); }
.batch-sync-card-top { display: grid; grid-template-columns: minmax(248px,.82fr) minmax(340px,1.18fr); align-items: stretch; gap: 30px; min-height: 118px; padding: 18px 20px; background: linear-gradient(108deg, #fff 0%, #fff 63%, #fcfdff 100%); }
.batch-sync-model { display: grid; min-width: 0; align-content: center; gap: 5px; border-right: 1px solid #edf0f3; padding: 3px 30px 3px 2px; }
.batch-sync-model-label { color: #98a1ad; font-size: 10px; font-weight: 680; letter-spacing: .07em; line-height: 1.2; text-transform: uppercase; }
.batch-sync-model strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #252c36; font-size: 15px; font-weight: 670; letter-spacing: -.012em; line-height: 1.35; }
.batch-sync-model code { overflow: hidden; color: #858f9d; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 11.5px; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
.batch-source-panel { display: grid; min-width: 0; align-content: center; gap: 9px; padding: 2px 0; }
.batch-source-panel-head { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 12px; }
.batch-source-control { display: block; min-width: 0; }
.batch-source-label { min-width: 0; color: #687383; font-size: 12px; font-weight: 620; letter-spacing: .01em; }
.batch-source-select { display: block; width: 100%; max-width: none; }
:deep(.batch-source-select .el-select__wrapper) { min-height: 42px; border-radius: 11px; background: #fff; box-shadow: 0 0 0 1px #d9dfe7 inset, 0 1px 2px rgba(31,41,55,.025) !important; }
:deep(.batch-source-select .el-select__wrapper:hover) { box-shadow: 0 0 0 1px #b9c4d2 inset, 0 1px 2px rgba(31,41,55,.03) !important; }
:deep(.batch-source-select .el-select__wrapper.is-focused) { box-shadow: 0 0 0 1px #1677ff inset, 0 0 0 3px rgba(22,119,255,.10) !important; }
:deep(.batch-source-select .el-select__selected-item) { font-size: 13px; font-weight: 590; }
.batch-source-static { display: inline-flex; width: 100%; min-height: 42px; min-width: 0; max-width: 100%; align-items: center; gap: 9px; overflow: hidden; border: 1px solid #dfe4ea; border-radius: 11px; background: #fafbfc; padding: 8px 12px; color: #47515e; font-size: 13px; font-weight: 590; line-height: 1.35; white-space: nowrap; text-overflow: ellipsis; }
.batch-source-static.is-manual { color: #596473; }
.batch-source-static-mark { display: grid; width: 17px; height: 17px; flex: 0 0 auto; place-items: center; border-radius: 50%; background: #e7f8ef; color: #16825d; font-size: 10px; font-style: normal; font-weight: 800; }
.batch-source-static.is-manual .batch-source-static-mark { background: #eef2f7; color: #64748b; }
.batch-source-tag { display: inline-flex; flex: 0 0 auto; align-items: center; border: 1px solid #d4e7ff; border-radius: 999px; background: #eef6ff; padding: 4px 9px; color: #3578c4; font-size: 11px; font-weight: 630; line-height: 1.25; white-space: nowrap; }
.batch-source-tag.is-unique { border-color: #cdeedc; background: #eefaf4; color: #16825d; }
.batch-source-tag.is-manual { border-color: #e1e6ed; background: #f6f8fa; color: #64748b; }
.batch-source-missing { display: flex; min-height: 42px; align-items: center; border: 1px dashed #d9dee6; border-radius: 11px; background: #fbfcfd; padding: 8px 12px; color: #87919e; font-size: 12px; line-height: 1.45; }
.batch-sync-change-block { display: grid; grid-template-columns: 122px minmax(0,1fr); gap: 12px; border-top: 1px solid #eff1f4; background: linear-gradient(90deg, #fbfcfd, #fff); padding: 12px 16px 13px; }
.batch-sync-change-heading { padding-top: 4px; color: #87909d; font-size: 11px; font-weight: 650; letter-spacing: .04em; text-transform: uppercase; }
.batch-sync-change-heading span { display: block; margin-top: 4px; color: #a1a8b2; font-size: 11px; font-weight: 480; letter-spacing: 0; text-transform: none; }
.batch-sync-change-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 7px 10px; }
.batch-sync-change { display: grid; grid-template-columns: 82px minmax(0,1fr); align-items: center; gap: 8px; min-width: 0; border-radius: 9px; background: rgba(246,248,250,.82); padding: 7px 9px; }
.batch-sync-change-name { color: #7d8693; font-size: 11px; white-space: nowrap; }
.batch-sync-change-value { display: flex; min-width: 0; align-items: center; gap: 7px; color: #5d6774; font-size: 12px; white-space: nowrap; }
.batch-sync-change-value .old, .batch-sync-change-value .new { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.batch-sync-change-value .old { color: #9aa2ad; text-decoration: line-through; }
.batch-sync-change-value .arrow { flex: 0 0 auto; color: #9ca5b0; font-size: 13px; }
.batch-sync-change-value .new { color: #2f5f9a; font-weight: 630; }
.batch-sync-no-change { display: flex; align-items: center; gap: 7px; color: #818a96; font-size: 12px; line-height: 1.45; }
.batch-sync-no-change::before { width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: #b2bdc8; content: ""; }
.batch-sync-note { display: flex; align-items: flex-start; gap: 8px; margin: 1px 0 0; color: #89919c; font-size: 11px; line-height: 1.5; }
.batch-sync-note svg { width: 14px; height: 14px; flex: 0 0 auto; margin-top: 1px; color: #8ba5c4; }
.batch-footer-status { color: #747d89; font-size: 12px; }
@media (max-width: 780px) { .source-match-grid, .batch-sync-card-top, .batch-sync-change-block { grid-template-columns: 1fr; } .batch-dialog-header, .batch-sync-quiet-intro, .source-action-row { align-items: flex-start; flex-direction: column; } .batch-dialog-header-state { width: 100%; justify-content: space-between; } .batch-sync-card-top > .batch-source-tag { justify-self: start; } .batch-sync-change-heading { padding-top: 0; } .batch-sync-change-grid { grid-template-columns: 1fr; } .batch-sync-list { max-height: 52vh; } }
.capability-row { display: flex; flex-wrap: wrap; gap: 8px; }
.capability-row label { height: 30px; padding: 0 11px; border: 1px solid rgba(228,228,231,.9); border-radius: 999px; background: rgba(244,244,245,.72); }
.price-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.price-grid label { display: flex; flex-direction: column; gap: 5px; }
.price-grid em { color: #a1a1aa; font-size: 10px; font-style: normal; }
.price-grid input { height: 32px; padding: 0 8px; }
.test-result { border: 1px solid rgba(228,228,231,.8); border-radius: 15px; padding: 12px; background: rgba(255,255,255,.72); }
.test-result.is-ok { box-shadow: inset 3px 0 0 rgba(52,199,89,.68); }
.test-result.is-bad { box-shadow: inset 3px 0 0 rgba(255,59,48,.68); }
</style>
