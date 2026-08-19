const AGENT_PANEL_PREFIX = "agent-panel:";
const AGENT_PANEL_INTENTS = new Set(["auto", "open", "closed"]);

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value) {
  return String(value ?? "").trim();
}

function taskUuidFrom(value = {}) {
  const item = object(value);
  const task = object(item.task);
  return text(item.taskUuid || item.task_uuid || task.taskUuid || task.task_uuid);
}

export function agentOperationIdentity(operation = {}) {
  const op = object(operation);
  const payload = object(op.payload);
  return taskUuidFrom(op) || taskUuidFrom(payload) || text(op.opId);
}

export function agentEventIdentity(event = {}) {
  const item = object(event);
  const operationIdentity = agentOperationIdentity(item.operation);
  if (operationIdentity) return operationIdentity;
  const payloads = [
    item,
    item.livePayload,
    item.result,
    ...(Array.isArray(item.results) ? item.results : []),
  ];
  for (const payload of payloads) {
    const identity = taskUuidFrom(payload);
    if (identity) return identity;
  }
  return text(item.id || item.eventKey);
}

export function agentPanelDetailKey(conversationUuid, identity) {
  const conversation = text(conversationUuid);
  const stableIdentity = text(identity);
  if (!conversation || !stableIdentity) return "";
  return `${AGENT_PANEL_PREFIX}${encodeURIComponent(conversation)}:${encodeURIComponent(stableIdentity)}`;
}

export function isAgentPanelDetailKey(value) {
  return text(value).startsWith(AGENT_PANEL_PREFIX);
}

export function normalizeAgentPanelIntents(value = {}) {
  const out = {};
  for (const [key, intent] of Object.entries(object(value))) {
    if (!isAgentPanelDetailKey(key) || !AGENT_PANEL_INTENTS.has(text(intent))) continue;
    out[key] = text(intent);
  }
  return out;
}

function operationTime(value) {
  const numeric = Number(value || 0);
  if (Number.isFinite(numeric) && numeric > 0) return numeric;
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

export function selectLatestActiveAgentOperation(operations = [], runState = {}) {
  const activeIds = new Set((Array.isArray(runState?.activeAgentOperationIds) ? runState.activeAgentOperationIds : []).map(text).filter(Boolean));
  if (!activeIds.size) return null;
  const active = (Array.isArray(operations) ? operations : [])
    .filter((operation) => (
      text(operation?.opType) === "agent"
      && object(operation?.payload).merged !== true
      && activeIds.has(text(operation?.opId))
    ));
  active.sort((left, right) => {
    const displaySeq = Number(left?.displaySeq || 0) - Number(right?.displaySeq || 0);
    if (displaySeq) return displaySeq;
    const createdAt = operationTime(left?.createdAtMs || left?.createdAt) - operationTime(right?.createdAtMs || right?.createdAt);
    if (createdAt) return createdAt;
    return text(left?.opId).localeCompare(text(right?.opId));
  });
  return active.at(-1) || null;
}

export function decideAgentAutoOpen({conversationUuid = "", operations = [], runState = {}, intents = {}} = {}) {
  const operation = selectLatestActiveAgentOperation(operations, runState);
  if (!operation) return {action: "pending", key: "", intent: "", operation: null, fallbackOperation: null};
  const key = agentPanelDetailKey(conversationUuid, agentOperationIdentity(operation));
  if (!key) return {action: "pending", key: "", intent: "", operation: null, fallbackOperation: null};
  const currentIntent = text(object(intents)[key]);
  if (currentIntent === "closed") {
    return {action: "respect_closed", key, intent: "closed", operation, fallbackOperation: null};
  }
  if (currentIntent === "open" || currentIntent === "auto") {
    return {action: "unchanged", key, intent: currentIntent, operation, fallbackOperation: null};
  }
  return {action: "open", key, intent: "auto", operation, fallbackOperation: null};
}
