<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";

const TOOL_PREVIEW_LIMIT = 3;

const loading = ref(false);
const reloading = ref(false);
const mcpToggling = ref(false);
const serverTogglingKeys = ref(new Set());
const status = ref({ ok: true, enabled: false, summary: {}, servers: [], tools: [], prompts: [], note: "" });
const query = ref("");
const drawerOpen = ref(false);
const drawerKind = ref("");
const drawerItem = ref(null);
const settingsSpecLoaded = ref(false);
const mcpSettingsAvailable = ref(false);
const mcpSettingPaths = ref([]);

const STATUS_TEXT = {
  connected: "已连接",
  failed: "连接失败",
  disabled: "已停用",
  pending: "连接中",
  stopped: "已停止",
  unknown: "未知状态",
};
const RISK_TEXT = {
  read: "只读",
  write: "写入",
  destructive: "高风险",
  unknown: "未知",
  external: "外部交互",
  secret: "敏感信息",
  "": "未知",
};
const APPROVAL_TEXT = {
  allow: "允许",
  ask: "询问",
  deny: "拒绝",
};
const TRANSPORT_TEXT = {
  stdio: "本地进程",
  sse: "远程事件流",
  http: "HTTP 服务",
  streamable_http: "HTTP 流式服务",
  "streamable-http": "HTTP 流式服务",
  websocket: "WebSocket 服务",
};
const FILTER_REASON_TEXT = {
  allowed: "可用",
  global_disabled: "全局 MCP 已停用",
  server_disabled: "此 MCP 已停用",
  global_deny: "命中全局拒绝规则",
  server_deny: "命中此 MCP 的拒绝规则",
  global_allow_not_matched: "不在全局允许范围内",
  server_allow_not_matched: "不在此 MCP 的允许范围内",
  name_conflict: "接口名称冲突",
};

function okOrThrow(data) {
  if (data?.ok === false) throw new Error(data.error || "操作失败");
  return data;
}
function asArray(value) {
  return Array.isArray(value) ? value : [];
}
function numberValue(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n : 0;
}
function textValue(value, fallback = "—") {
  const text = String(value ?? "").trim();
  return text || fallback;
}
function fieldText(...values) {
  return values.map((x) => String(x ?? "").toLowerCase()).join("\n");
}
function statusText(value) {
  return STATUS_TEXT[String(value || "unknown").toLowerCase()] || "未知状态";
}
function riskText(value) {
  return RISK_TEXT[String(value || "unknown").toLowerCase()] || "未知";
}
function approvalText(value) {
  return APPROVAL_TEXT[String(value || "ask").toLowerCase()] || "询问";
}
function transportText(value) {
  const key = String(value || "").toLowerCase();
  return TRANSPORT_TEXT[key] || (key ? "其他连接方式" : "未填写");
}
function filterReasonText(value) {
  const key = String(value || "").toLowerCase();
  if (!key) return "—";
  return FILTER_REASON_TEXT[key] || "命中接口过滤规则";
}
function enabledText(value) {
  return value ? "已启用" : "已禁用";
}
function boolText(value) {
  return value ? "是" : "否";
}
function formatTime(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "—";
  const ms = n > 1_000_000_000_000 ? n : n * 1000;
  return new Date(ms).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
function serverKey(row) {
  return String(row?.key || row?.name || "").trim();
}
function serverName(row) {
  return textValue(row?.displayName || row?.name || row?.key, "未命名 MCP");
}
function toolName(row) {
  return textValue(row?.publicName || row?.originalToolName || row?.normalizedToolName, "未命名接口");
}
function promptName(row) {
  return textValue(row?.title || row?.name, "未命名提示词");
}
function promptArguments(prompt) {
  return asArray(prompt?.arguments)
    .filter((row) => row && typeof row === "object" && String(row.name || "").trim())
    .map((row) => ({
      name: String(row.name || "").trim(),
      description: String(row.description || "").trim(),
      required: Boolean(row.required),
    }));
}
function promptArgumentSummary(prompt, limit = 2) {
  const rows = promptArguments(prompt);
  if (!rows.length) return "无参数";
  const shown = rows.slice(0, limit).map((row) => `${row.name}（${row.required ? "必填" : "可选"}${row.description ? `）：${row.description}` : "）"}`);
  if (rows.length > limit) shown.push(`等 ${rows.length} 个参数`);
  return shown.join("；");
}
function statusType(value) {
  const statusValue = String(value || "");
  if (statusValue === "connected") return "success";
  if (statusValue === "failed") return "danger";
  if (statusValue === "pending") return "warning";
  if (statusValue === "disabled" || statusValue === "stopped") return "info";
  return "info";
}
function riskType(value) {
  const risk = String(value || "unknown");
  if (risk === "read") return "success";
  if (risk === "write" || risk === "external") return "warning";
  if (risk === "destructive" || risk === "secret") return "danger";
  return "info";
}
function approvalType(value) {
  const approval = String(value || "ask");
  if (approval === "allow") return "success";
  if (approval === "deny") return "danger";
  return "warning";
}
function toolAvailabilityType(row) {
  return row?.filtered ? "warning" : "success";
}
function toolAvailabilityText(row) {
  return row?.filtered ? "已过滤" : "可用";
}
function countBy(items, key) {
  const counts = {};
  for (const item of items || []) {
    const value = String(item?.[key] || "unknown").toLowerCase();
    counts[value] = intCount(counts[value]) + 1;
  }
  return counts;
}
function intCount(value) {
  return Number.isFinite(Number(value)) ? Number(value) : 0;
}
function countEntries(counts, labeler) {
  return Object.entries(counts || {})
    .filter(([, count]) => intCount(count) > 0)
    .map(([key, count]) => ({ key, label: labeler(key), count: intCount(count) }));
}
function riskCountItems(row) {
  return countEntries(row?.riskCounts || {}, riskText);
}
function approvalCountItems(row) {
  return countEntries(row?.approvalCounts || {}, approvalText);
}
function schemaTypeOf(schema) {
  if (!schema || typeof schema !== "object") return "";
  const raw = schema.type;
  if (typeof raw === "string") return raw;
  if (Array.isArray(raw)) return raw.map((x) => String(x || "").trim()).filter(Boolean).join("|");
  for (const key of ["anyOf", "oneOf", "allOf"]) {
    const items = Array.isArray(schema[key]) ? schema[key] : [];
    const nested = items.map(schemaTypeOf).filter(Boolean);
    if (nested.length) return [...new Set(nested)].join("|");
  }
  if (schema.items) return "array";
  if (schema.properties) return "object";
  return "";
}
function schemaTypeText(value) {
  const map = {
    string: "文本",
    number: "数字",
    integer: "整数",
    boolean: "布尔",
    object: "对象",
    array: "数组",
    null: "空",
    unknown: "未知",
  };
  const text = String(value || "").trim();
  if (!text) return "未知";
  return text.split("|").map((item) => map[item] || item).join(" / ");
}
function toolParameters(tool) {
  const direct = asArray(tool?.parameters)
    .filter((row) => row && typeof row === "object" && String(row.name || "").trim())
    .map((row) => ({
      name: String(row.name || "").trim(),
      rawType: String(row.type || "unknown").trim() || "unknown",
      description: String(row.description || row.desc || "").trim(),
      required: Boolean(row.required),
      enum: asArray(row.enum).slice(0, 8).map((x) => String(x ?? "").trim()).filter(Boolean),
    }));
  if (direct.length) return direct;
  const schema = tool?.inputSchema || tool?.input_schema || {};
  const properties = schema?.properties && typeof schema.properties === "object" ? schema.properties : {};
  const required = new Set(asArray(schema?.required).map((x) => String(x || "")));
  return Object.entries(properties)
    .filter(([name, prop]) => String(name || "").trim() && prop && typeof prop === "object")
    .map(([name, prop]) => ({
      name: String(name),
      rawType: schemaTypeOf(prop) || "unknown",
      description: String(prop.description || prop.title || "").trim(),
      required: required.has(String(name)),
      enum: asArray(prop.enum).slice(0, 8).map((x) => String(x ?? "").trim()).filter(Boolean),
    }));
}
function parameterLine(row) {
  const name = textValue(row?.name, "参数");
  const type = schemaTypeText(row?.rawType || row?.type);
  const attrs = [type, row?.required ? "必填" : "可选"].filter(Boolean).join(" · ");
  const desc = String(row?.description || "").trim();
  const enumText = asArray(row?.enum).length ? `；可选值：${asArray(row.enum).join("、")}` : "";
  return `${name}${attrs ? `（${attrs}）` : ""}${desc ? `：${desc}` : ""}${enumText}`;
}
function hasParameterMetadata(tool) {
  return Array.isArray(tool?.parameters) || Boolean(tool?.inputSchema || tool?.input_schema);
}
function noParameterText(tool) {
  return hasParameterMetadata(tool) ? "无参数" : "当前后端未返回参数信息，请重新加载服务后查看";
}
function toolParameterSummary(tool, limit = 2) {
  const rows = toolParameters(tool);
  if (!rows.length) return noParameterText(tool);
  const shown = rows.slice(0, limit).map(parameterLine);
  if (rows.length > limit) shown.push(`等 ${rows.length} 个参数`);
  return shown.join("；");
}
function toolSchemaPreview(tool) {
  const rows = toolParameters(tool);
  if (!rows.length) return noParameterText(tool);
  const schema = tool?.inputSchema || tool?.input_schema || {};
  return JSON.stringify({
    类型: schemaTypeText(schemaTypeOf(schema) || "object"),
    参数数量: rows.length,
    必填参数: rows.filter((row) => row.required).map((row) => row.name),
    参数: rows.map((row) => ({
      名称: row.name,
      类型: schemaTypeText(row.rawType),
      必填: row.required ? "是" : "否",
      说明: row.description || "",
      可选值: row.enum || [],
    })),
  }, null, 2);
}
function annotationItems(tool) {
  const annotations = tool?.annotations && typeof tool.annotations === "object" ? tool.annotations : {};
  const labels = {
    readOnlyHint: "只读提示",
    destructiveHint: "破坏性提示",
    idempotentHint: "幂等提示",
    openWorldHint: "外部世界访问",
    title: "标题",
  };
  return Object.entries(labels)
    .filter(([key]) => annotations[key] !== undefined && annotations[key] !== "")
    .map(([key, label]) => ({ key, label, value: typeof annotations[key] === "boolean" ? boolText(annotations[key]) : String(annotations[key]) }));
}
function serverIntro(card) {
  const explicit = String(card?.description || "").trim();
  if (explicit) return explicit;
  const allTools = asArray(card?.tools);
  const allPrompts = asArray(card?.prompts);
  const available = allTools.filter((tool) => !tool.filtered);
  const first = available[0] || allTools[0];
  const firstDescription = String(first?.description || "").trim();
  if (available.length === 1 && first) {
    return firstDescription
      ? `这个 MCP 当前已接入 1 个可用接口：${toolName(first)}。${firstDescription}`
      : `这个 MCP 当前已接入 1 个可用接口：${toolName(first)}。`;
  }
  if (available.length > 0) {
    return `这个 MCP 当前已接入 ${available.length} 个可用接口，OpenBear 可以按审批策略调用这些接口来扩展外部工具能力。`;
  }
  const total = numberValue(card?.totalTools ?? allTools.length);
  if (total > 0) return `这个 MCP 当前发现 ${total} 个接口，但暂无可用接口；可能被过滤、停用或等待连接恢复。`;
  if (allPrompts.length === 1) {
    const prompt = allPrompts[0];
    return `这个 MCP 当前提供 1 个提示词：${promptName(prompt)}。${prompt.description || "提示词可由支持 MCP Prompt 的宿主手动调用。"}`;
  }
  if (allPrompts.length > 1) return `这个 MCP 当前提供 ${allPrompts.length} 个提示词；提示词可由支持 MCP Prompt 的宿主手动调用。`;
  return "这个 MCP 当前还没有发现可用接口或提示词。连接成功后，这里会显示 OpenBear 可以调用的能力和参数。";
}
function compactServerIntro(card, maxChars = 220) {
  const text = serverIntro(card).replace(/\s+/g, " ").trim();
  return text.length > maxChars ? `${text.slice(0, maxChars).trimEnd()}…` : text;
}
function searchNeedle() {
  return query.value.trim().toLowerCase();
}
function matchesText(text, needle = searchNeedle()) {
  return !needle || String(text || "").toLowerCase().includes(needle);
}
function toolSearchText(tool) {
  const params = toolParameters(tool).map((row) => `${row.name} ${row.description} ${schemaTypeText(row.rawType)} ${row.required ? "必填" : ""}`).join("\n");
  return fieldText(
    tool?.publicName,
    tool?.originalToolName,
    tool?.normalizedToolName,
    tool?.serverKey,
    tool?.serverName,
    tool?.description,
    params,
    riskText(tool?.risk),
    approvalText(tool?.approval),
    toolAvailabilityText(tool),
    filterReasonText(tool?.filterReason),
  );
}
function toolMatchesQuery(tool, needle = searchNeedle()) {
  return matchesText(toolSearchText(tool), needle);
}
function promptSearchText(prompt) {
  const args = promptArguments(prompt).map((row) => `${row.name} ${row.description} ${row.required ? "必填" : "可选"}`).join("\n");
  return fieldText(prompt?.name, prompt?.title, prompt?.description, prompt?.serverKey, args);
}
function promptMatchesQuery(prompt, needle = searchNeedle()) {
  return matchesText(promptSearchText(prompt), needle);
}
function serverSearchText(card) {
  return fieldText(
    card?.key,
    card?.name,
    card?.displayName,
    serverName(card),
    card?.description,
    serverIntro(card),
    transportText(card?.transport),
    statusText(card?.status),
    enabledText(card?.enabled),
    approvalText(card?.approval),
  );
}
function cardMatchesQuery(card) {
  const needle = searchNeedle();
  if (!needle) return true;
  return matchesText(serverSearchText(card), needle)
    || asArray(card?.tools).some((tool) => toolMatchesQuery(tool, needle))
    || asArray(card?.prompts).some((prompt) => promptMatchesQuery(prompt, needle));
}

const servers = computed(() => asArray(status.value?.servers));
const tools = computed(() => asArray(status.value?.tools));
const prompts = computed(() => asArray(status.value?.prompts));
const serverCards = computed(() => {
  const byServer = new Map();
  const promptsByServer = new Map();
  for (const rawPrompt of prompts.value) {
    const key = String(rawPrompt?.serverKey || rawPrompt?.serverName || "").trim() || "__unknown__";
    const row = { ...rawPrompt, serverKey: key === "__unknown__" ? "" : key };
    if (!promptsByServer.has(key)) promptsByServer.set(key, []);
    promptsByServer.get(key).push(row);
  }
  for (const rawTool of tools.value) {
    const key = String(rawTool?.serverKey || rawTool?.serverName || "").trim() || "__unknown__";
    const row = {
      ...rawTool,
      serverKey: key === "__unknown__" ? "" : key,
      filtered: Boolean(rawTool?.filtered),
    };
    if (!byServer.has(key)) byServer.set(key, []);
    byServer.get(key).push(row);
  }

  const cards = [];
  const seen = new Set();
  const makeCard = (server, fallbackKey, index) => {
    const key = serverKey(server) || fallbackKey || `mcp-${index}`;
    const groupedTools = asArray(byServer.get(key)).sort((a, b) => {
      if (Boolean(a.filtered) !== Boolean(b.filtered)) return a.filtered ? 1 : -1;
      return toolName(a).localeCompare(toolName(b), "zh-CN");
    });
    const groupedPrompts = asArray(promptsByServer.get(key)).sort((a, b) => promptName(a).localeCompare(promptName(b), "zh-CN"));
    const computedVisible = groupedTools.filter((tool) => !tool.filtered).length;
    const computedFiltered = groupedTools.filter((tool) => tool.filtered).length;
    const visibleTools = numberValue(server?.visibleTools ?? server?.toolCount ?? computedVisible);
    const filteredTools = numberValue(server?.filteredTools ?? computedFiltered);
    const totalTools = numberValue(server?.totalTools ?? (visibleTools + filteredTools || groupedTools.length));
    return {
      ...server,
      key,
      name: server?.name || key,
      displayName: server?.displayName || server?.name || key,
      transport: server?.transport || "",
      status: server?.status || "unknown",
      enabled: Boolean(server?.enabled),
      approval: server?.approval || "ask",
      required: Boolean(server?.required),
      visibleTools,
      filteredTools,
      totalTools,
      tools: groupedTools,
      prompts: groupedPrompts,
      promptCount: groupedPrompts.length,
      riskCounts: Object.keys(server?.riskCounts || {}).length ? server.riskCounts : countBy(groupedTools, "risk"),
      approvalCounts: Object.keys(server?.approvalCounts || {}).length ? server.approvalCounts : countBy(groupedTools, "approval"),
      lastConnectedAt: server?.lastConnectedAt ?? server?.lastConnected,
      lastFailedAt: server?.lastFailedAt ?? server?.lastFailed,
      errorPresent: Boolean(server?.errorPresent),
      errorHidden: Boolean(server?.errorHidden ?? server?.errorPresent),
    };
  };

  servers.value.forEach((server, index) => {
    const key = serverKey(server) || `mcp-${index}`;
    seen.add(key);
    cards.push(makeCard(server, key, index));
  });

  for (const [key] of byServer.entries()) {
    if (key === "__unknown__" || seen.has(key)) continue;
    cards.push(makeCard({ key, name: key, displayName: key, status: "unknown", enabled: Boolean(status.value?.enabled), approval: "ask" }, key, cards.length));
  }
  return cards.sort((a, b) => Number(Boolean(b.enabled)) - Number(Boolean(a.enabled)));
});
const summary = computed(() => {
  const s = status.value?.summary || {};
  const connectedCount = s.connectedCount ?? serverCards.value.filter((server) => server.status === "connected").length;
  const visibleTools = s.visibleTools ?? tools.value.filter((tool) => !tool.filtered).length;
  const filteredTools = s.filteredTools ?? tools.value.filter((tool) => tool.filtered).length;
  return {
    enabled: Boolean(s.enabled ?? status.value?.enabled),
    serverCount: numberValue(s.serverCount ?? serverCards.value.length),
    connectedCount: numberValue(connectedCount),
    visibleTools: numberValue(visibleTools),
    filteredTools: numberValue(filteredTools),
    promptCount: numberValue(s.promptCount ?? prompts.value.length),
  };
});
const filteredServerCards = computed(() => serverCards.value.filter((card) => cardMatchesQuery(card)));
const settingsEntryAvailable = computed(() => Boolean(status.value?.settingsAvailable || mcpSettingsAvailable.value));
const drawerTitle = computed(() => {
  if (drawerKind.value === "server") return `MCP 详情 · ${serverName(drawerItem.value)}`;
  if (drawerKind.value === "tool") return `接口详情 · ${toolName(drawerItem.value)}`;
  return "MCP 详情";
});

function isServerToggling(row) {
  return serverTogglingKeys.value.has(serverKey(row));
}
function setServerToggling(key, value) {
  const next = new Set(serverTogglingKeys.value);
  if (value) next.add(key);
  else next.delete(key);
  serverTogglingKeys.value = next;
}
function toolsMatchedForCard(card) {
  const needle = searchNeedle();
  const allTools = asArray(card?.tools);
  if (!needle) return allTools;
  const matched = allTools.filter((tool) => toolMatchesQuery(tool, needle));
  return matched.length ? matched : allTools;
}
function toolsForCard(card) {
  return toolsMatchedForCard(card).slice(0, TOOL_PREVIEW_LIMIT);
}
function toolHiddenCount(card) {
  return Math.max(0, toolsMatchedForCard(card).length - TOOL_PREVIEW_LIMIT);
}
function compactToolName(tool) {
  return textValue(tool?.originalToolName || tool?.normalizedToolName || tool?.publicName, "未命名接口");
}
function applyStatusPayload(data) {
  if (Array.isArray(data?.servers) || Array.isArray(data?.tools) || Array.isArray(data?.prompts)) {
    status.value = { ...status.value, ...data };
  } else if (data?.summary) {
    status.value = {
      ...status.value,
      enabled: Boolean(data.enabled ?? data.summary?.enabled ?? status.value.enabled),
      summary: { ...status.value.summary, ...data.summary },
    };
  }
  syncDrawerItem();
}
function setServerEnabledLocal(key, enabled) {
  const rows = servers.value.map((row) => (serverKey(row) === key ? { ...row, enabled } : row));
  status.value = { ...status.value, servers: rows };
  if (drawerKind.value === "server" && serverKey(drawerItem.value) === key) {
    drawerItem.value = { ...drawerItem.value, enabled };
  }
}
function setServerApprovalLocal(key, approval) {
  const rows = servers.value.map((row) => (serverKey(row) === key ? { ...row, approval } : row));
  status.value = { ...status.value, servers: rows };
  if (drawerKind.value === "server" && serverKey(drawerItem.value) === key) {
    drawerItem.value = { ...drawerItem.value, approval };
  }
}
function syncDrawerItem() {
  if (!drawerOpen.value || !drawerItem.value) return;
  if (drawerKind.value === "server") {
    const key = serverKey(drawerItem.value);
    const next = serverCards.value.find((card) => serverKey(card) === key);
    if (next) drawerItem.value = next;
  } else if (drawerKind.value === "tool") {
    const publicName = String(drawerItem.value?.publicName || "").trim();
    const originalName = String(drawerItem.value?.originalToolName || "").trim();
    const server = String(drawerItem.value?.serverKey || drawerItem.value?.serverName || "").trim();
    const next = tools.value.find((tool) => (
      (publicName && String(tool?.publicName || "") === publicName)
      || (originalName && server && String(tool?.originalToolName || "") === originalName && String(tool?.serverKey || tool?.serverName || "") === server)
      || toolName(tool) === toolName(drawerItem.value)
    ));
    if (next) drawerItem.value = next;
  }
}
async function setMcpEnabled(value) {
  const previous = Boolean(status.value?.enabled);
  mcpToggling.value = true;
  status.value = { ...status.value, enabled: value, summary: { ...(status.value.summary || {}), enabled: value } };
  try {
    const data = okOrThrow(await Api.setMcpEnabled(value));
    applyStatusPayload(data);
    ElMessage.success(`全局 MCP 已${value ? "启用" : "禁用"}`);
    await load({ silent: true });
  } catch (error) {
    status.value = { ...status.value, enabled: previous, summary: { ...(status.value.summary || {}), enabled: previous } };
    ElMessage.error(apiError(error));
    await load({ silent: true });
  } finally {
    mcpToggling.value = false;
  }
}
async function setServerEnabled(row, value) {
  const key = serverKey(row);
  if (!key) return;
  const previous = Boolean(row?.enabled);
  setServerToggling(key, true);
  setServerEnabledLocal(key, value);
  try {
    const data = okOrThrow(await Api.setMcpServerEnabled(key, value));
    applyStatusPayload(data);
    ElMessage.success(`MCP「${serverName(row)}」已${value ? "启用" : "禁用"}`);
    await load({ silent: true });
  } catch (error) {
    setServerEnabledLocal(key, previous);
    ElMessage.error(apiError(error));
    await load({ silent: true });
  } finally {
    setServerToggling(key, false);
  }
}
async function uninstallServer(row) {
  const key = serverKey(row);
  if (!key) return;
  try {
    await ElMessageBox.prompt(
      `这会删除 OpenBear 中的 MCP 注册并关闭当前连接，但不会卸载外部软件或关闭远程服务。请输入完整内部标识「${key}」确认。`,
      `卸载 MCP · ${serverName(row)}`,
      {
        type: "error",
        confirmButtonText: "确认卸载",
        cancelButtonText: "取消",
        inputPlaceholder: key,
        inputValidator: (value) => value === key || "内部标识不一致",
        closeOnClickModal: false,
      },
    );
  } catch {
    return;
  }
  setServerToggling(key, true);
  try {
    okOrThrow(await Api.uninstallMcpServer(key));
    if (drawerKind.value === "server" && serverKey(drawerItem.value) === key) {
      drawerOpen.value = false;
      drawerItem.value = null;
    }
    ElMessage.success(`MCP「${serverName(row)}」已从 OpenBear 移除`);
    await load({ silent: true });
  } catch (error) {
    ElMessage.error(apiError(error));
    await load({ silent: true });
  } finally {
    setServerToggling(key, false);
  }
}

async function setServerApproval(row, value) {
  const key = serverKey(row);
  const approval = String(value || "").trim().toLowerCase();
  if (!key || !["allow", "ask", "deny"].includes(approval) || approval === row?.approval) return;
  if (approval === "allow") {
    try {
      await ElMessageBox.confirm(
        `信任「${serverName(row)}」后，它的全部可用接口将在主会话、子 Agent 和后台任务中直接执行。风险标签仍会展示并记录审计。`,
        "始终信任此 MCP",
        { type: "warning", confirmButtonText: "始终信任", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
  }
  const previous = String(row?.approval || "ask");
  setServerToggling(key, true);
  setServerApprovalLocal(key, approval);
  try {
    const data = okOrThrow(await Api.setMcpServerApproval(key, approval));
    applyStatusPayload(data);
    ElMessage.success(`MCP「${serverName(row)}」审批策略已设为“${approvalText(approval)}”`);
    await load({ silent: true });
  } catch (error) {
    setServerApprovalLocal(key, previous);
    ElMessage.error(apiError(error));
    await load({ silent: true });
  } finally {
    setServerToggling(key, false);
  }
}
function openServer(row) {
  drawerKind.value = "server";
  drawerItem.value = row;
  drawerOpen.value = true;
}
function openTool(row) {
  drawerKind.value = "tool";
  drawerItem.value = row;
  drawerOpen.value = true;
}
async function loadSettingsSpecHint() {
  if (settingsSpecLoaded.value) return;
  settingsSpecLoaded.value = true;
  try {
    const data = okOrThrow(await Api.settingsSpecs());
    const groups = asArray(data.groups);
    const paths = [];
    for (const group of groups) {
      for (const path of asArray(group?.paths)) {
        if (String(path || "").startsWith("mcp.")) paths.push(path);
      }
    }
    mcpSettingPaths.value = paths;
    mcpSettingsAvailable.value = paths.length > 0;
  } catch {
    mcpSettingsAvailable.value = false;
  }
}
async function load(options = {}) {
  const silent = Boolean(options.silent);
  if (!silent) loading.value = true;
  try {
    status.value = okOrThrow(await Api.mcpStatus());
    syncDrawerItem();
    void loadSettingsSpecHint();
  } catch (error) {
    if (!silent) ElMessage.error(apiError(error));
  } finally {
    if (!silent) loading.value = false;
  }
}
async function reloadMcp() {
  reloading.value = true;
  try {
    const result = okOrThrow(await Api.reloadMcp());
    const s = result.summary || {};
    ElMessage.success(`MCP 配置已重新加载：${s.serverCount || result.servers || 0} 个 MCP，${s.visibleTools || result.tools || 0} 个可用接口`);
    await load({ silent: true });
  } catch (error) {
    ElMessage.error(apiError(error));
    await load({ silent: true });
  } finally {
    reloading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="h-full flex flex-col bg-macbg" v-loading="loading || reloading">
    <header class="shrink-0 border-b border-macborder bg-white/70 px-6 py-4 backdrop-blur">
      <div class="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div class="min-w-0">
          <h1 class="text-base font-semibold">MCP 管理</h1>
          <p class="mt-1 text-sm leading-6 text-macsub">
            MCP 是 OpenBear 接入外部工具服务的方式；本页每张卡就是一个 MCP，卡内「接口」就是 OpenBear 可以调用的外部能力。这里展示连接状态、接口说明、参数摘要，以及风险与审批策略。
          </p>
        </div>
        <div class="flex shrink-0 flex-wrap items-center gap-2">
          <a v-if="settingsEntryAvailable" href="/settings" class="settings-link" :title="mcpSettingPaths.length ? `已发现 ${mcpSettingPaths.length} 个 MCP 设置项` : '打开设置页'">打开 MCP 设置</a>
          <el-button round type="primary" :loading="reloading" @click="reloadMcp">重新加载配置</el-button>
          <el-button round :icon="'Refresh'" :loading="loading" @click="load">刷新状态</el-button>
        </div>
      </div>
    </header>

    <section class="grid grid-cols-1 gap-3 px-6 pt-5 shrink-0 md:grid-cols-6">
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">全局 MCP 开关</div>
        <div class="mt-2 flex items-center gap-3">
          <el-switch
            :model-value="summary.enabled"
            :loading="mcpToggling"
            active-text="启用"
            inactive-text="禁用"
            inline-prompt
            style="--el-switch-on-color: #10b981; --el-switch-off-color: #94a3b8"
            @change="setMcpEnabled"
          />
          <el-tag :type="summary.enabled ? 'success' : 'info'" round>{{ enabledText(summary.enabled) }}</el-tag>
        </div>
      </div>
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">已配置 MCP 数</div>
        <div class="mt-1 text-lg font-semibold">{{ summary.serverCount }}</div>
      </div>
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">已连接数</div>
        <div class="mt-1 text-lg font-semibold text-emerald-700">{{ summary.connectedCount }}</div>
      </div>
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">可用接口数</div>
        <div class="mt-1 text-lg font-semibold text-macblue">{{ summary.visibleTools }}</div>
      </div>
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">过滤接口数</div>
        <div class="mt-1 text-lg font-semibold" :class="summary.filteredTools ? 'text-amber-700' : ''">{{ summary.filteredTools }}</div>
      </div>
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">提示词数</div>
        <div class="mt-1 text-lg font-semibold text-indigo-700">{{ summary.promptCount }}</div>
      </div>
    </section>

    <section class="mx-6 mt-4 shrink-0 rounded-2xl border border-macborder bg-white/75 p-3 backdrop-blur">
      <div class="flex flex-col gap-3 lg:flex-row lg:items-center">
        <el-input v-model="query" clearable :prefix-icon="'Search'" placeholder="搜索 MCP 名称、接口名、说明或参数" class="lg:max-w-lg" />
        <div class="ml-auto text-xs text-macsub">当前显示 {{ filteredServerCards.length }} / {{ serverCards.length }} 个 MCP</div>
      </div>
      <div class="mt-3 rounded-xl border border-amber-200 bg-amber-50/70 px-3 py-2 text-xs leading-5 text-amber-900">
        敏感连接配置已隐藏：启动命令、环境变量、请求头、令牌和接口密钥不会从本页返回；卸载仅移除 OpenBear 注册，不会卸载外部软件或关闭远程服务。
      </div>
      <div v-if="!settingsEntryAvailable" class="mt-2 text-xs text-macsub">
        当前未发现 Web 设置页里的 MCP 详细配置入口；可手动编辑配置文件后点击「重新加载配置」热应用。
      </div>
    </section>

    <main class="min-h-0 flex-1 overflow-y-auto p-6">
      <el-empty v-if="!serverCards.length" description="尚未配置 MCP" class="mac-panel py-12">
        <p class="mx-auto max-w-xl text-sm leading-6 text-macsub">
          配置 MCP 后，OpenBear 才能接入外部工具服务。本页会按「一个 MCP 一张卡」展示连接状态、接口数量、审批策略和可用接口列表。
        </p>
        <template #extra>
          <a v-if="settingsEntryAvailable" href="/settings" class="settings-link">去设置页配置 MCP</a>
          <span v-else class="text-xs text-macsub">请在配置文件中添加 MCP 配置，然后回到本页重新加载。</span>
        </template>
      </el-empty>

      <el-empty v-else-if="!filteredServerCards.length" description="没有匹配的 MCP 或接口" class="mac-panel py-12">
        <template #extra>
          <el-button type="primary" plain round @click="query = ''">清空搜索</el-button>
        </template>
      </el-empty>

      <div v-else class="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <article v-for="card in filteredServerCards" :key="card.key" class="mcp-card mac-panel mac-shadow p-4">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex min-w-0 flex-wrap items-center gap-2">
                <span class="mcp-mark">MCP</span>
                <h2 class="truncate text-[15px] font-semibold" :title="serverName(card)">{{ serverName(card) }}</h2>
                <el-tag :type="statusType(card.status)" size="small" round>{{ statusText(card.status) }}</el-tag>
              </div>
            </div>
            <div class="mcp-card-controls" @click.stop>
              <el-select
                :model-value="card.approval"
                size="small"
                :loading="isServerToggling(card)"
                :disabled="!serverKey(card) || !status.settingsAvailable"
                aria-label="MCP 审批策略"
                title="审批策略"
                @change="(value) => setServerApproval(card, value)"
              >
                <el-option label="始终允许" value="allow" />
                <el-option label="敏感操作询问" value="ask" />
                <el-option label="禁止调用" value="deny" />
              </el-select>
              <el-switch
                :model-value="Boolean(card.enabled)"
                :loading="isServerToggling(card)"
                :disabled="!serverKey(card)"
                inline-prompt
                active-text="开"
                inactive-text="关"
                aria-label="启用此 MCP"
                title="启用此 MCP"
                style="--el-switch-on-color: #10b981; --el-switch-off-color: #94a3b8"
                @change="(value) => setServerEnabled(card, value)"
              />
            </div>
          </div>

          <div class="mcp-facts">
            <span>{{ transportText(card.transport) }}</span>
            <span><strong>{{ card.visibleTools }}</strong>/{{ card.totalTools }} 个接口</span>
            <span v-if="card.promptCount"><strong>{{ card.promptCount }}</strong> 个提示词</span>
            <span v-if="card.filteredTools" class="is-warning"><strong>{{ card.filteredTools }}</strong> 个已过滤</span>
            <span v-if="card.required">必需连接</span>
          </div>

          <div class="mcp-health" :class="card.lastFailedAt ? 'has-failure' : ''">
            <span class="status-dot" :class="card.status === 'connected' ? 'is-online' : 'is-muted'" />
            <span>最近连接 {{ formatTime(card.lastConnectedAt) }}</span>
            <span v-if="card.lastFailedAt" class="failure-text">最近失败 {{ formatTime(card.lastFailedAt) }}</span>
          </div>

          <div v-if="card.errorPresent" class="mcp-error-line">
            最近存在连接错误，错误明文已隐藏
          </div>

          <p class="mcp-intro" :title="compactServerIntro(card)">{{ compactServerIntro(card) }}</p>

          <div class="mcp-tool-preview">
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs font-semibold text-zinc-700">接口预览</span>
              <span class="text-[11px] text-macsub">{{ card.visibleTools }} 个可用</span>
            </div>
            <div v-if="card.tools.length" class="mt-2 flex flex-wrap gap-2">
              <button
                v-for="tool in toolsForCard(card)"
                :key="tool.publicName || tool.originalToolName"
                type="button"
                class="tool-chip"
                :class="`risk-${tool.risk || 'unknown'}`"
                :title="`${toolName(tool)} · ${riskText(tool.risk)} · ${tool.description || '暂无说明'}`"
                @click="openTool(tool)"
              >
                {{ compactToolName(tool) }}
              </button>
              <button v-if="toolHiddenCount(card)" type="button" class="tool-chip more-tools" @click="openServer(card)">
                +{{ toolHiddenCount(card) }}
              </button>
            </div>
            <div v-else class="mt-2 text-xs text-macsub">此 MCP 暂无可用接口</div>
          </div>

          <div class="mcp-card-footer">
            <span class="truncate text-[11px] text-macsub" :title="card.key">{{ card.key }}</span>
            <div class="flex shrink-0 items-center gap-1">
              <el-button size="small" text type="danger" :loading="isServerToggling(card)" @click="uninstallServer(card)">卸载</el-button>
              <el-button size="small" text type="primary" @click="openServer(card)">查看详情 →</el-button>
            </div>
          </div>
        </article>
      </div>
    </main>

    <el-drawer v-model="drawerOpen" size="44%" :title="drawerTitle" direction="rtl">
      <div v-if="drawerItem" class="space-y-4 text-sm">
        <section class="rounded-2xl border border-macborder bg-zinc-50/80 p-4 text-xs leading-6 text-zinc-700">
          这是只读安全摘要视图。敏感连接配置与错误明文不会在详情中展示；如需修改连接参数，请到配置文件或设置入口处理后重新加载。
        </section>

        <template v-if="drawerKind === 'server'">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="MCP 名称">{{ serverName(drawerItem) }}</el-descriptions-item>
            <el-descriptions-item label="内部标识"><code>{{ drawerItem.key || '—' }}</code></el-descriptions-item>
            <el-descriptions-item label="传输方式">{{ transportText(drawerItem.transport) }}</el-descriptions-item>
            <el-descriptions-item label="连接状态"><el-tag :type="statusType(drawerItem.status)" round>{{ statusText(drawerItem.status) }}</el-tag></el-descriptions-item>
            <el-descriptions-item label="启用状态">
              <div class="flex items-center gap-2">
                <el-switch
                  :model-value="Boolean(drawerItem.enabled)"
                  :loading="isServerToggling(drawerItem)"
                  :disabled="!serverKey(drawerItem)"
                  inline-prompt
                  active-text="启用"
                  inactive-text="禁用"
                  style="--el-switch-on-color: #10b981; --el-switch-off-color: #94a3b8"
                  @change="(value) => setServerEnabled(drawerItem, value)"
                />
                <el-tag :type="drawerItem.enabled ? 'success' : 'info'" effect="plain" round>{{ enabledText(drawerItem.enabled) }}</el-tag>
              </div>
            </el-descriptions-item>
            <el-descriptions-item label="接口数量">共 {{ drawerItem.totalTools || 0 }} 个，可用 {{ drawerItem.visibleTools || 0 }} 个，已过滤 {{ drawerItem.filteredTools || 0 }} 个</el-descriptions-item>
            <el-descriptions-item label="审批策略"><el-tag :type="approvalType(drawerItem.approval)" effect="plain" round>{{ approvalText(drawerItem.approval) }}</el-tag></el-descriptions-item>
            <el-descriptions-item label="是否必需">{{ boolText(drawerItem.required) }}</el-descriptions-item>
            <el-descriptions-item label="最近连接">{{ formatTime(drawerItem.lastConnectedAt) }}</el-descriptions-item>
            <el-descriptions-item label="最近失败">{{ formatTime(drawerItem.lastFailedAt) }}</el-descriptions-item>
            <el-descriptions-item label="错误明文">{{ drawerItem.errorPresent ? '已隐藏' : '无' }}</el-descriptions-item>
          </el-descriptions>

          <div class="rounded-2xl border border-macborder bg-white p-4">
            <h3 class="mb-2 text-sm font-semibold">简介</h3>
            <p class="text-sm leading-6 text-zinc-700">{{ serverIntro(drawerItem) }}</p>
          </div>

          <div class="rounded-2xl border border-red-200 bg-red-50/50 p-4">
            <div class="flex items-center justify-between gap-4">
              <div>
                <h3 class="text-sm font-semibold text-red-800">卸载此 MCP</h3>
                <p class="mt-1 text-xs leading-5 text-red-700">移除 OpenBear 注册和当前连接，不删除外部软件或远程服务。</p>
              </div>
              <el-button type="danger" plain round :loading="isServerToggling(drawerItem)" @click="uninstallServer(drawerItem)">卸载</el-button>
            </div>
          </div>

          <div class="rounded-2xl border border-macborder bg-white p-4">
            <h3 class="mb-2 text-sm font-semibold">风险与审批分布</h3>
            <div class="flex flex-wrap gap-2">
              <el-tag v-for="item in riskCountItems(drawerItem)" :key="item.key" :type="riskType(item.key)" effect="plain">{{ item.label }}：{{ item.count }}</el-tag>
              <span v-if="!riskCountItems(drawerItem).length" class="text-xs text-macsub">暂无接口风险数据</span>
            </div>
            <div class="mt-3 flex flex-wrap gap-2">
              <el-tag v-for="item in approvalCountItems(drawerItem)" :key="item.key" :type="approvalType(item.key)" effect="plain">{{ item.label }}：{{ item.count }}</el-tag>
              <span v-if="!approvalCountItems(drawerItem).length" class="text-xs text-macsub">暂无审批分布数据</span>
            </div>
          </div>

          <div class="rounded-2xl border border-macborder bg-white p-4">
            <h3 class="mb-3 text-sm font-semibold">接口清单</h3>
            <el-empty v-if="!drawerItem.tools?.length" description="此 MCP 暂无接口" :image-size="72" />
            <div v-else class="space-y-2">
              <button v-for="tool in drawerItem.tools" :key="tool.publicName || tool.originalToolName" type="button" class="tool-row" @click="openTool(tool)">
                <div class="min-w-0 flex-1 text-left">
                  <div class="flex min-w-0 flex-wrap items-center gap-2">
                    <code class="break-all text-xs">{{ toolName(tool) }}</code>
                    <el-tag :type="riskType(tool.risk)" effect="plain" round>{{ riskText(tool.risk) }}</el-tag>
                    <el-tag :type="approvalType(tool.approval)" effect="plain" round>{{ approvalText(tool.approval) }}</el-tag>
                    <el-tag :type="toolAvailabilityType(tool)" effect="plain" round>{{ toolAvailabilityText(tool) }}</el-tag>
                  </div>
                  <p class="mt-2 text-xs leading-5 text-zinc-700">{{ tool.description || '暂无说明' }}</p>
                  <p class="mt-1 text-xs leading-5 text-macsub">参数：{{ toolParameterSummary(tool) }}</p>
                </div>
              </button>
            </div>
          </div>

          <div v-if="drawerItem.prompts?.length" class="rounded-2xl border border-macborder bg-white p-4">
            <h3 class="mb-3 text-sm font-semibold">提示词清单</h3>
            <div class="space-y-2">
              <div v-for="prompt in drawerItem.prompts" :key="prompt.name" class="rounded-xl border border-indigo-100 bg-indigo-50/40 px-3 py-2">
                <div class="flex flex-wrap items-center gap-2">
                  <code class="break-all text-xs">{{ prompt.name }}</code>
                  <el-tag v-if="prompt.title" size="small" effect="plain">{{ prompt.title }}</el-tag>
                </div>
                <p class="mt-2 text-xs leading-5 text-zinc-700">{{ prompt.description || '暂无说明' }}</p>
                <p class="mt-1 text-xs text-macsub">参数：{{ promptArgumentSummary(prompt) }}</p>
              </div>
            </div>
          </div>
        </template>

        <template v-else-if="drawerKind === 'tool'">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="接口名称"><code class="break-all">{{ toolName(drawerItem) }}</code></el-descriptions-item>
            <el-descriptions-item label="所属 MCP"><code>{{ drawerItem.serverKey || drawerItem.serverName || '—' }}</code></el-descriptions-item>
            <el-descriptions-item label="原始名称"><code class="break-all">{{ drawerItem.originalToolName || '—' }}</code></el-descriptions-item>
            <el-descriptions-item label="标准化名称"><code class="break-all">{{ drawerItem.normalizedToolName || '—' }}</code></el-descriptions-item>
            <el-descriptions-item label="风险等级"><el-tag :type="riskType(drawerItem.risk)" round>{{ riskText(drawerItem.risk) }}</el-tag></el-descriptions-item>
            <el-descriptions-item label="审批策略"><el-tag :type="approvalType(drawerItem.approval)" effect="plain" round>{{ approvalText(drawerItem.approval) }}</el-tag></el-descriptions-item>
            <el-descriptions-item label="可用状态"><el-tag :type="toolAvailabilityType(drawerItem)" effect="plain" round>{{ toolAvailabilityText(drawerItem) }}</el-tag></el-descriptions-item>
            <el-descriptions-item label="过滤原因">{{ drawerItem.filtered ? filterReasonText(drawerItem.filterReason) : '—' }}</el-descriptions-item>
          </el-descriptions>
          <div class="rounded-2xl border border-macborder bg-white p-4">
            <h3 class="mb-2 text-sm font-semibold">接口说明</h3>
            <p class="whitespace-pre-wrap text-sm leading-6 text-zinc-700">{{ drawerItem.description || '暂无说明' }}</p>
          </div>
          <div class="rounded-2xl border border-macborder bg-white p-4">
            <h3 class="mb-3 text-sm font-semibold">参数</h3>
            <div v-if="toolParameters(drawerItem).length" class="space-y-2">
              <div v-for="param in toolParameters(drawerItem)" :key="param.name" class="parameter-row">
                <div class="flex min-w-0 flex-wrap items-center gap-2">
                  <code class="break-all text-xs">{{ param.name }}</code>
                  <el-tag size="small" effect="plain">类型：{{ schemaTypeText(param.rawType) }}</el-tag>
                  <el-tag size="small" :type="param.required ? 'danger' : 'info'" effect="plain">{{ param.required ? '必填' : '可选' }}</el-tag>
                </div>
                <p class="mt-1 text-xs leading-5 text-zinc-700">{{ param.description || '暂无说明' }}</p>
                <p v-if="param.enum?.length" class="mt-1 text-xs text-macsub">可选值：{{ param.enum.join('、') }}</p>
              </div>
            </div>
            <p v-else class="text-sm text-macsub">{{ noParameterText(drawerItem) }}</p>
          </div>
          <div v-if="annotationItems(drawerItem).length" class="rounded-2xl border border-macborder bg-white p-4">
            <h3 class="mb-2 text-sm font-semibold">接口只读信息</h3>
            <div class="flex flex-wrap gap-2">
              <el-tag v-for="item in annotationItems(drawerItem)" :key="item.key" effect="plain">{{ item.label }}：{{ item.value }}</el-tag>
            </div>
          </div>
          <el-collapse>
            <el-collapse-item title="JSON Schema 摘要" name="schema">
              <pre class="schema-preview">{{ toolSchemaPreview(drawerItem) }}</pre>
            </el-collapse-item>
          </el-collapse>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.settings-link {
  display: inline-flex;
  align-items: center;
  height: 32px;
  padding: 0 12px;
  border: 1px solid rgba(0, 122, 255, 0.28);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.88);
  color: #007aff;
  font-size: 12px;
  font-weight: 500;
}
.settings-link:hover {
  background: rgba(0, 122, 255, 0.08);
}
.mcp-card {
  transition: border-color 0.18s ease, transform 0.18s ease, box-shadow 0.18s ease;
}
.mcp-card:hover {
  border-color: rgba(0, 122, 255, 0.18);
  transform: translateY(-1px);
}
.mcp-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  min-width: 42px;
  border-radius: 999px;
  background: rgba(0, 122, 255, 0.1);
  color: #007aff;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.mcp-card-controls {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
}
.mcp-card-controls :deep(.el-select) {
  width: 118px;
}
.mcp-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}
.mcp-facts > span {
  border-radius: 999px;
  background: rgba(24, 24, 27, 0.045);
  padding: 4px 9px;
  color: #52525b;
  font-size: 11px;
  line-height: 1.35;
}
.mcp-facts > span.is-warning {
  background: rgba(245, 158, 11, 0.11);
  color: #a16207;
}
.mcp-facts strong {
  color: #18181b;
  font-weight: 700;
}
.mcp-health {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 7px;
  margin-top: 11px;
  color: #71717a;
  font-size: 11px;
}
.status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: #a1a1aa;
}
.status-dot.is-online {
  background: #10b981;
  box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.11);
}
.failure-text {
  margin-left: auto;
  color: #b91c1c;
}
.mcp-error-line {
  margin-top: 9px;
  border-radius: 9px;
  background: rgba(254, 226, 226, 0.72);
  padding: 6px 9px;
  color: #991b1b;
  font-size: 11px;
}
.mcp-intro {
  display: -webkit-box;
  overflow: hidden;
  margin-top: 12px;
  color: #52525b;
  font-size: 12px;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 1;
}
.mcp-tool-preview {
  margin-top: 12px;
  border-top: 1px solid rgba(24, 24, 27, 0.07);
  padding-top: 11px;
}
.tool-chip {
  max-width: min(100%, 220px);
  overflow: hidden;
  border: 1px solid rgba(24, 24, 27, 0.09);
  border-radius: 9px;
  background: rgba(255, 255, 255, 0.78);
  padding: 5px 9px;
  color: #3f3f46;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 11px;
  line-height: 1.25;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: border-color 0.16s ease, background 0.16s ease, color 0.16s ease;
}
.tool-chip:hover {
  border-color: rgba(0, 122, 255, 0.25);
  background: rgba(0, 122, 255, 0.055);
  color: #0066d6;
}
.tool-chip.risk-destructive,
.tool-chip.risk-secret {
  border-color: rgba(239, 68, 68, 0.16);
}
.tool-chip.more-tools {
  border-style: dashed;
  color: #007aff;
  font-family: inherit;
  font-weight: 650;
}
.mcp-card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
  border-top: 1px solid rgba(24, 24, 27, 0.06);
  padding-top: 5px;
}
.tool-row {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 12px;
  border-radius: 14px;
  border: 1px solid rgba(24, 24, 27, 0.08);
  background: rgba(255, 255, 255, 0.82);
  padding: 10px 12px;
  text-align: left;
  transition: background 0.16s ease, border-color 0.16s ease;
}
.tool-row:hover {
  border-color: rgba(0, 122, 255, 0.2);
  background: rgba(0, 122, 255, 0.045);
}
.parameter-row {
  border-radius: 14px;
  border: 1px solid rgba(24, 24, 27, 0.08);
  background: rgba(24, 24, 27, 0.025);
  padding: 10px 12px;
}
.schema-preview {
  max-height: 320px;
  overflow: auto;
  border-radius: 12px;
  background: rgba(24, 24, 27, 0.04);
  padding: 12px;
  color: #3f3f46;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}
code {
  border-radius: 6px;
  background: rgba(24, 24, 27, 0.05);
  padding: 1px 5px;
  color: #27272a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}
</style>
