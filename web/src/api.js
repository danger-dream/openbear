import axios from "axios";

const api = axios.create({ baseURL: "/api", timeout: 30000 });

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      window.location.href = "/login";
      return Promise.reject(error);
    }
    return Promise.reject(error);
  },
);

function unwrap(response) {
  return response.data;
}

export const Api = {
  loginStart: (secret) => api.post("/auth/login/start", { secret }).then(unwrap),
  loginStatus: (requestUuid) => api.get(`/auth/login/status/${requestUuid}`).then(unwrap),
  consumeLogin: (requestUuid) => api.post(`/auth/login/consume/${requestUuid}`).then(unwrap),


  conversations: (params = {}) => api.get("/conversations", { params }).then(unwrap),
  conversationDefaults: () => api.get("/conversations/defaults").then(unwrap),
  updateConversationDefaults: (data = {}) => api.patch("/conversations/defaults", data).then(unwrap),
  createConversation: (data = {}) => api.post("/conversations", data).then(unwrap),
  updateConversation: (uuid, data = {}) => api.patch(`/conversations/${encodeURIComponent(uuid)}`, data).then(unwrap),
  setConversationArchived: (uuid, archived) => api.patch(`/conversations/${encodeURIComponent(uuid)}`, { archived: Boolean(archived) }).then(unwrap),
  reorderConversation: (uuid, data = {}) => api.post(`/conversations/${encodeURIComponent(uuid)}/reorder`, data).then(unwrap),
  duplicateConversation: (uuid, data = {}) => api.post(`/conversations/${encodeURIComponent(uuid)}/duplicate`, data, { timeout: 120000 }).then(unwrap),
  pinConversation: (uuid) => api.post(`/conversations/${encodeURIComponent(uuid)}/pin`).then(unwrap),
  unpinConversation: (uuid) => api.post(`/conversations/${encodeURIComponent(uuid)}/unpin`).then(unwrap),
  deleteConversation: (uuid) => api.delete(`/conversations/${encodeURIComponent(uuid)}`).then(unwrap),
  deleteConversationTurnSuffix: (uuid, turnUuid) => api.delete(`/conversations/${encodeURIComponent(uuid)}/turns/${encodeURIComponent(turnUuid)}/suffix`).then(unwrap),
  conversationState: (uuid, params = {}) => api.get(`/conversations/${encodeURIComponent(uuid)}/state`, { params }).then(unwrap),
  conversationOperations: (uuid, params = {}) => api.get(`/conversations/${encodeURIComponent(uuid)}/operations`, { params }).then(unwrap),
  conversationOperationDetail: (uuid, operationId) => api.get(`/conversations/${encodeURIComponent(uuid)}/operations/${encodeURIComponent(operationId)}/detail`).then(unwrap),
  conversationCompaction: (uuid, summaryId) => api.get(`/conversations/${encodeURIComponent(uuid)}/compactions/${encodeURIComponent(summaryId)}`).then(unwrap),
  conversationFrames: (uuid, afterFrameSeq = 0, limit = 1000) => api.get(`/conversations/${encodeURIComponent(uuid)}/frames`, { params: { afterFrameSeq, limit } }).then(unwrap),
  conversationCompact: (uuid) => api.post(`/conversations/${encodeURIComponent(uuid)}/compact`, {}, { timeout: 1800000 }).then(unwrap),
  conversationStop: (uuid) => api.post(`/conversations/${encodeURIComponent(uuid)}/stop`).then(unwrap),
  conversationCancelRetry: (uuid, taskUuid = "") => api.post(`/conversations/${encodeURIComponent(uuid)}/retry/cancel`, taskUuid ? { taskUuid } : {}).then(unwrap),
  answerConversationConfirmation: (uuid, confirmationId, answer = {}) => api.post(`/conversations/${encodeURIComponent(uuid)}/confirmations/${encodeURIComponent(confirmationId)}/answer`, answer).then(unwrap),
  conversationSetModel: (uuid, model) => api.post(`/conversations/${encodeURIComponent(uuid)}/model`, { model }).then(unwrap),
  conversationSetThinking: (uuid, level) => api.post(`/conversations/${encodeURIComponent(uuid)}/thinking`, { level }).then(unwrap),
  conversationSetFast: (uuid, enabled) => api.post(`/conversations/${encodeURIComponent(uuid)}/fast`, { enabled }).then(unwrap),
  conversationSetAgentRunConfig: (uuid, data = {}) => api.post(`/conversations/${encodeURIComponent(uuid)}/agent-run-config`, data).then(unwrap),
  taskMemories: (uuid, params = {}) => api.get(`/conversations/${encodeURIComponent(uuid)}/task-memories`, { params }).then(unwrap),
  taskMemoryTasks: (uuid) => api.get(`/conversations/${encodeURIComponent(uuid)}/task-memories/tasks`).then(unwrap),
  taskMemoryPreview: (uuid, params = {}) => api.get(`/conversations/${encodeURIComponent(uuid)}/task-memories/preview`, { params }).then(unwrap),
  taskMemory: (uuid, memoryUuid, params = {}) => api.get(`/conversations/${encodeURIComponent(uuid)}/task-memories/${encodeURIComponent(memoryUuid)}`, { params }).then(unwrap),
  createTaskMemory: (uuid, data = {}) => api.post(`/conversations/${encodeURIComponent(uuid)}/task-memories`, data).then(unwrap),
  updateTaskMemory: (uuid, memoryUuid, data = {}) => api.patch(`/conversations/${encodeURIComponent(uuid)}/task-memories/${encodeURIComponent(memoryUuid)}`, data).then(unwrap),
  deleteTaskMemory: (uuid, memoryUuid, data = {}) => api.delete(`/conversations/${encodeURIComponent(uuid)}/task-memories/${encodeURIComponent(memoryUuid)}`, { data }).then(unwrap),
  restoreTaskMemory: (uuid, memoryUuid, data = {}) => api.post(`/conversations/${encodeURIComponent(uuid)}/task-memories/${encodeURIComponent(memoryUuid)}/restore`, data).then(unwrap),

  entries: (category = "", includeArchived = false, scope = "") => api.get("/memory/entries", { params: { ...(category ? { category } : {}), ...(scope ? { scope } : {}), ...(includeArchived ? { archived: 1 } : {}) } }).then(unwrap),
  entry: (id) => api.get(`/memory/entries/${id}`).then(unwrap),
  createEntry: (data) => api.post("/memory/entries", data).then(unwrap),
  updateEntry: (id, data) => api.put(`/memory/entries/${id}`, data).then(unwrap),
  deleteEntry: (id) => api.delete(`/memory/entries/${id}`).then(unwrap),

  secrets: (full = true, includeArchived = false) => api.get("/memory/secrets", { params: { ...(full ? { full: 1 } : {}), ...(includeArchived ? { archived: 1 } : {}) } }).then(unwrap),
  secret: (id) => api.get(`/memory/secrets/${id}`).then(unwrap),
  createSecret: (data) => api.post("/memory/secrets", data).then(unwrap),
  updateSecret: (id, data) => api.put(`/memory/secrets/${id}`, data).then(unwrap),
  deleteSecret: (id) => api.delete(`/memory/secrets/${id}`).then(unwrap),

  docs: (includeArchived = false) => api.get("/memory/docs", { params: includeArchived ? { archived: 1 } : {} }).then(unwrap),
  doc: (id) => api.get(`/memory/docs/${id}`).then(unwrap),
  createDoc: (data) => api.post("/memory/docs", data).then(unwrap),
  updateDoc: (id, data) => api.put(`/memory/docs/${id}`, data).then(unwrap),
  deleteDoc: (id) => api.delete(`/memory/docs/${id}`).then(unwrap),

  templates: () => api.get("/memory/templates").then(unwrap),
  createTemplate: (data) => api.post("/memory/templates", data).then(unwrap),
  updateTemplate: (id, data) => api.put(`/memory/templates/${id}`, data).then(unwrap),
  deleteTemplate: (id) => api.delete(`/memory/templates/${id}`).then(unwrap),
  reorder: (kind, items) => api.post("/memory/reorder", { kind, items }).then(unwrap),
  previewTemplate: (params, templateContent, templateName = "web-preview") => api.post("/memory/preview", { params, templateContent, templateName }, { timeout: 60000 }).then(unwrap),
  renderLogs: (params = {}) => api.get("/memory/render-logs", { params }).then(unwrap),
  renderLog: (id) => api.get(`/memory/render-logs/${id}`).then(unwrap),
  auditLogs: (params = {}) => api.get("/audit-logs", { params }).then(unwrap),

  systemRestart: (data = {}) => api.post("/system/restart", data).then(unwrap),
  systemVersion: () => api.get("/system/version").then(unwrap),
  systemUpdate: (data = {}) => api.post("/system/update", data, { timeout: 60000 }).then(unwrap),
  ackSystemUpdate: (data = {}) => api.post("/system/update/ack", data).then(unwrap),

  mcpStatus: () => api.get("/mcp/status").then(unwrap),
  setMcpEnabled: (enabled) => api.patch("/mcp/enabled", { enabled }).then(unwrap),
  setMcpServerEnabled: (serverKey, enabled) => api.patch(`/mcp/servers/${encodeURIComponent(serverKey)}/enabled`, { enabled }).then(unwrap),
  setMcpServerApproval: (serverKey, approval) => api.patch(`/mcp/servers/${encodeURIComponent(serverKey)}/approval`, { approval }).then(unwrap),
  uninstallMcpServer: (serverKey) => api.post(`/mcp/servers/${encodeURIComponent(serverKey)}/uninstall`, { confirm: true, name: serverKey }, { timeout: 120000 }).then(unwrap),
  reloadMcp: () => api.post("/mcp/reload").then(unwrap),

  skills: () => api.get("/skills").then(unwrap),
  skillDetail: (name) => api.get(`/skills/${encodeURIComponent(name)}`).then(unwrap),
  skillToggle: (name, enabled) => api.patch(`/skills/${encodeURIComponent(name)}/enabled`, { enabled }).then(unwrap),
  uninstallSkill: (name) => api.post(`/skills/${encodeURIComponent(name)}/uninstall`, { confirm: true, name }, { timeout: 120000 }).then(unwrap),
  skillsReload: () => api.post("/skills/reload").then(unwrap),

  settingsSpecs: () => api.get("/settings/specs").then(unwrap),
  settings: () => api.get("/settings").then(unwrap),
  updateSetting: (path, value) => api.patch(`/settings/${encodeURIComponent(path)}`, { value }).then(unwrap),
  previewSettingPrompt: (path, value, variables = {}) => api.post("/settings/prompt-preview", { path, value, variables }).then(unwrap),
  testWebTaskNotification: () => api.post("/settings/web-task-notifications/test").then(unwrap),

  channels: () => api.get("/channels").then(unwrap),
  channel: (name) => api.get(`/channels/${encodeURIComponent(name)}`).then(unwrap),
  createChannel: (data) => api.post("/channels", data).then(unwrap),
  updateChannel: (name, data) => api.patch(`/channels/${encodeURIComponent(name)}`, data).then(unwrap),
  deleteChannel: (name) => api.delete(`/channels/${encodeURIComponent(name)}`).then(unwrap),
  reorderChannels: (order) => api.post("/channels/reorder", { order }).then(unwrap),
  setPrimaryModel: (model) => api.post("/channels/primary", { model }).then(unwrap),
  setCompressionModel: (modelOrModels) => api.post("/channels/compression", Array.isArray(modelOrModels) ? { models: modelOrModels } : { model: modelOrModels }).then(unwrap),
  fetchChannelModels: (data) => api.post("/channels/models/fetch", data, { timeout: 30000 }).then(unwrap),
  modelsDevStatus: () => api.get("/models-dev/status").then(unwrap),
  refreshModelsDev: () => api.post("/models-dev/refresh", {}, { timeout: 45000 }).then(unwrap),
  modelsDevProviders: (params = {}) => api.get("/models-dev/providers", { params }).then(unwrap),
  modelsDevProviderModels: (providerId, params = {}) => api.get("/models-dev/provider-models", { params: { providerId, ...params } }).then(unwrap),
  previewChannelModelModelsDev: (provider, modelId, data = {}) => api.post(`/channels/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelId)}/models-dev/preview`, data).then(unwrap),
  syncChannelModelModelsDev: (provider, modelId, data = {}) => api.post(`/channels/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelId)}/models-dev/sync`, data).then(unwrap),
  channelModelsDevMatches: (provider) => api.get(`/channels/${encodeURIComponent(provider)}/models-dev/matches`).then(unwrap),
  previewChannelModelsDevBatch: (provider, data = {}) => api.post(`/channels/${encodeURIComponent(provider)}/models-dev/preview`, data).then(unwrap),
  syncChannelModelsDevBatch: (provider, data = {}) => api.post(`/channels/${encodeURIComponent(provider)}/models-dev/sync`, data).then(unwrap),
  createChannelModel: (provider, data) => api.post(`/channels/${encodeURIComponent(provider)}/models`, data).then(unwrap),
  updateChannelModel: (provider, modelId, data) => api.patch(`/channels/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelId)}`, data).then(unwrap),
  deleteChannelModel: (provider, modelId) => api.delete(`/channels/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelId)}`).then(unwrap),
  reorderChannelModels: (provider, order) => api.post(`/channels/${encodeURIComponent(provider)}/models/reorder`, { order }).then(unwrap),
  testChannelModel: (provider, modelId) => api.post(`/channels/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelId)}/test`, {}, { timeout: 120000 }).then(unwrap),
  testChannel: (provider) => api.post(`/channels/${encodeURIComponent(provider)}/test`, {}, { timeout: 30000 }).then(unwrap),
  channelTestStatus: (provider, jobUuid) => api.get(`/channels/${encodeURIComponent(provider)}/test/${encodeURIComponent(jobUuid)}`).then(unwrap),


  rathOptions: () => api.get("/rath/options").then(unwrap),
  rathAgents: (params = {}) => api.get("/rath/agents", { params }).then(unwrap),
  createRathAgent: (data) => api.post("/rath/agents", data).then(unwrap),
  updateRathAgent: (id, data) => api.put(`/rath/agents/${id}`, data).then(unwrap),
  trialRathAgent: (id, instruction) => api.post(`/rath/agents/${id}/trial`, { instruction }, { timeout: 60000 }).then(unwrap),
  deleteRathAgent: (id) => api.delete(`/rath/agents/${id}`).then(unwrap),
  rathTaskPlan: (conversationUuid, taskUuid) => api.get(`/conversations/${encodeURIComponent(conversationUuid)}/agents/${encodeURIComponent(taskUuid)}/plan`).then(unwrap),
  rathTaskEvents: (conversationUuid, taskUuid, { beforeSeq = 0, afterSeq = 0, limit = 20 } = {}) => api.get(
    `/conversations/${encodeURIComponent(conversationUuid)}/agents/${encodeURIComponent(taskUuid)}/events`,
    { params: { beforeSeq, afterSeq, limit } },
  ).then(unwrap),
};

export function conversationWsUrl(uuid, afterFrameSeq = 0, options = {}) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  if (afterFrameSeq) params.set("afterFrameSeq", String(afterFrameSeq));
  if (options?.bootstrap) params.set("bootstrap", String(options.bootstrap));
  const qs = params.toString() ? `?${params.toString()}` : "";
  return `${proto}//${window.location.host}/api/conversations/${encodeURIComponent(uuid)}/ws${qs}`;
}

export async function filesToWsPayload(files = []) {
  const rows = [];
  for (const file of Array.from(files || [])) {
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error || new Error("file_read_failed"));
      reader.onload = () => resolve(String(reader.result || ""));
      reader.readAsDataURL(file);
    });
    rows.push({ name: file.name || "upload.bin", type: file.type || "application/octet-stream", size: file.size || 0, data });
  }
  return rows;
}

export function apiError(error) {
  return error?.response?.data?.error || error?.response?.data?.message || error?.message || String(error);
}
