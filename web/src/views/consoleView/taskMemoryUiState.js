export const TASK_MEMORY_CHANGED_EVENT_KEY = Symbol("openbear.task-memory.changed");

const TASK_MEMORY_ACTIONS = new Set(["create", "update", "delete", "restore"]);

function normalizedIdentity(value = {}) {
  return {
    conversationUuid: String(value?.conversationUuid || ""),
    scopeType: String(value?.scopeType || "conversation"),
    taskUuid: String(value?.taskUuid || ""),
  };
}

export function normalizeTaskMemoryChangedEvent(value = {}) {
  if (String(value?.type || "") !== "task_memory.changed") return null;
  const event = {
    type: "task_memory.changed",
    conversationUuid: String(value?.conversationUuid || ""),
    scopeType: String(value?.scopeType || ""),
    taskUuid: String(value?.taskUuid || ""),
    memoryUuid: String(value?.memoryUuid || ""),
    action: String(value?.action || ""),
    revision: Number(value?.revision || 0),
  };
  if (!event.conversationUuid || !["conversation", "agent_task"].includes(event.scopeType)) return null;
  if (!event.memoryUuid || !TASK_MEMORY_ACTIONS.has(event.action) || event.revision <= 0) return null;
  if (event.scopeType === "conversation" && event.taskUuid) return null;
  if (event.scopeType === "agent_task" && !event.taskUuid) return null;
  return Object.freeze(event);
}

export function taskMemoryChangedTransportEvent(value, transport = {}) {
  if (transport.sourceIsActive === false) return null;
  const event = normalizeTaskMemoryChangedEvent(value);
  if (!event) return null;
  const activeConversationUuid = String(transport.activeConversationUuid || "");
  const sourceConversationUuid = String(transport.sourceConversationUuid || "");
  const socketConversationUuid = String(transport.socketConversationUuid || "");
  if (!activeConversationUuid || event.conversationUuid !== activeConversationUuid) return null;
  if (sourceConversationUuid && sourceConversationUuid !== activeConversationUuid) return null;
  if (socketConversationUuid && socketConversationUuid !== activeConversationUuid) return null;
  return event;
}

export function taskMemoryChangedMatchesIdentity(eventValue, identityValue) {
  const event = normalizeTaskMemoryChangedEvent(eventValue);
  if (!event) return false;
  const identity = normalizedIdentity(identityValue);
  return event.conversationUuid === identity.conversationUuid
    && event.scopeType === identity.scopeType
    && event.taskUuid === (identity.scopeType === "agent_task" ? identity.taskUuid : "");
}

export function createTaskMemoryBadgeState(initialConversationUuid = "") {
  let conversationUuid = String(initialConversationUuid || "");
  let identity = normalizedIdentity({conversationUuid});
  let count = 0;

  function snapshot() {
    return Object.freeze({...identity, count});
  }

  function switchConversation(nextConversationUuid = "") {
    const next = String(nextConversationUuid || "");
    if (next !== conversationUuid) {
      conversationUuid = next;
      identity = normalizedIdentity({conversationUuid});
      count = 0;
    }
    return snapshot();
  }

  function set(nextIdentity, nextCount) {
    const next = normalizedIdentity(nextIdentity);
    if (!conversationUuid) conversationUuid = next.conversationUuid;
    if (!next.conversationUuid || next.conversationUuid !== conversationUuid) return snapshot();
    identity = next;
    count = Math.max(0, Number(nextCount || 0));
    return snapshot();
  }

  return Object.freeze({set, snapshot, switchConversation});
}

export function taskMemoryMutationRecovery(error) {
  const status = Number(error?.response?.status || 0);
  if (status === 409) {
    return Object.freeze({
      kind: "conflict",
      resetEditor: true,
      refresh: true,
      message: "任务记忆已被其他操作更新，已刷新最新版本，请重新编辑。",
    });
  }
  if (status === 404) {
    return Object.freeze({
      kind: "not_found",
      resetEditor: true,
      refresh: true,
      message: "该任务记忆已不存在或当前不可访问，已清理陈旧状态。",
    });
  }
  return Object.freeze({kind: "generic", resetEditor: false, refresh: false, message: ""});
}

function shortSourceId(value) {
  return String(value || "").slice(0, 8) || "—";
}

export function taskMemorySourceLabel(item = {}) {
  const createdBy = String(item?.createdBy || "");
  let actor = "系统";
  if (createdBy.startsWith("web:")) actor = "用户";
  else if (createdBy === "main-controller" || createdBy.startsWith("agent:")) actor = "AI";
  return `${actor} · turn ${shortSourceId(item?.sourceTurnUuid)} · run ${shortSourceId(item?.sourceRunUuid)}`;
}

export function createTaskMemoryChangedEventGate(initialIdentity = {}) {
  let identity = normalizedIdentity(initialIdentity);
  let revisions = new Map();

  function reset(nextIdentity = {}) {
    identity = normalizedIdentity(nextIdentity);
    revisions = new Map();
  }

  function accept(eventValue, currentIdentity = identity) {
    const current = normalizedIdentity(currentIdentity);
    if (
      current.conversationUuid !== identity.conversationUuid
      || current.scopeType !== identity.scopeType
      || current.taskUuid !== identity.taskUuid
    ) reset(current);
    const event = normalizeTaskMemoryChangedEvent(eventValue);
    if (!event || !taskMemoryChangedMatchesIdentity(event, current)) return false;
    const key = `${event.scopeType}\u0000${event.taskUuid}\u0000${event.memoryUuid}`;
    const previous = Number(revisions.get(key) || 0);
    if (event.revision <= previous) return false;
    revisions.set(key, event.revision);
    return true;
  }

  return Object.freeze({accept, reset});
}
