function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function text(value, fallback = "") {
  const normalized = String(value ?? "").trim();
  return normalized || fallback;
}

function positiveInteger(value) {
  const number = Number(value || 0);
  return Number.isInteger(number) && number > 0 ? number : 0;
}

function boolean(value) {
  return value === true;
}

function taskUuidOf(value = {}) {
  const task = object(value);
  return text(task.taskUuid || task.task_uuid || object(task.task).taskUuid || object(task.task).task_uuid);
}

function taskAgentId(value = {}) {
  const task = object(value);
  return text(task.agentId || task.agent_id || task.agentSessionUuid || task.agent_session_uuid);
}

function sessionAgentId(value = {}) {
  const session = object(value);
  return text(session.agentId || session.agent_id || session.sessionUuid || session.session_uuid);
}

function sessionField(session, fallback, keys) {
  for (const source of [object(session), object(fallback)]) {
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(source, key) && source[key] !== null && source[key] !== undefined) {
        return source[key];
      }
    }
  }
  return undefined;
}

function normalizeSessionKind(value) {
  return text(value).toLowerCase() === "independent" ? "independent" : "legacy";
}

function uniqueTextList(value) {
  const seen = new Set();
  const result = [];
  for (const item of array(value)) {
    const normalized = text(item);
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    result.push(normalized);
  }
  return result;
}

const CONTEXT_STATE = {
  fresh: "新建上下文",
  restored: "已恢复上下文",
  partial: "部分恢复",
  unavailable: "上下文不可用",
};

const TASK_STATUS = {
  queued: {label: "等待调度", tone: "running"},
  running: {label: "本轮执行中", tone: "running"},
  resuming: {label: "本轮恢复中", tone: "running"},
  pausing: {label: "本轮暂停中", tone: "pending"},
  paused: {label: "本轮已暂停", tone: "pending"},
  stopping: {label: "本轮停止中", tone: "pending"},
  needs_openbear_control: {label: "等待裁决", tone: "pending"},
  completed: {label: "本轮已结束", tone: "ok"},
  partial: {label: "本轮部分完成", tone: "partial"},
  failed: {label: "本轮失败", tone: "error"},
  cancelled: {label: "本轮已取消", tone: "stopped"},
  interrupted: {label: "本轮已中断", tone: "partial"},
};

export function shortAgentId(value, length = 8) {
  const id = text(value);
  if (!id) return "未记录";
  return id.length > length ? id.slice(0, length) : id;
}

export function contextSourceView(value = {}) {
  const source = object(value);
  const state = text(source.state).toLowerCase();
  const taskUuid = text(source.taskUuid || source.task_uuid);
  const revision = positiveInteger(source.revision);
  const stateLabel = CONTEXT_STATE[state] || (state ? state : "来源未记录");
  const facts = [stateLabel];
  if (taskUuid) facts.push(`任务 ${shortAgentId(taskUuid)}`);
  if (revision) facts.push(`r${revision}`);
  return {taskUuid, revision, state, stateLabel, label: facts.join(" · ")};
}

export function assignmentStatusView(status, {canContinue = false} = {}) {
  const normalized = text(status).toLowerCase();
  const base = TASK_STATUS[normalized] || {label: normalized || "状态未知", tone: "partial"};
  if (normalized === "completed" && canContinue === true) {
    return {...base, label: "本轮已结束，可继续指派"};
  }
  return {...base};
}

export function normalizeAgentSession(value = {}, fallback = {}) {
  const session = object(value);
  const fallbackSession = object(fallback);
  const kind = normalizeSessionKind(sessionField(session, fallbackSession, ["sessionKind", "session_kind"]));
  return {
    raw: session,
    agentId: sessionAgentId(session) || sessionAgentId(fallbackSession),
    sessionKind: kind,
    status: text(sessionField(session, fallbackSession, ["status"])),
    title: text(sessionField(session, fallbackSession, ["title"])),
    summary: text(sessionField(session, fallbackSession, ["summary"])),
    activeTaskUuid: text(sessionField(session, fallbackSession, ["activeTaskUuid", "active_task_uuid"])),
    lastTaskUuid: text(sessionField(session, fallbackSession, ["lastTaskUuid", "last_task_uuid"])),
    contextTaskUuid: text(sessionField(session, fallbackSession, ["contextTaskUuid", "context_task_uuid"])),
    contextRevision: positiveInteger(sessionField(session, fallbackSession, ["contextRevision", "context_revision"])),
    revision: positiveInteger(sessionField(session, fallbackSession, ["revision"])),
    turnCount: positiveInteger(sessionField(session, fallbackSession, ["turnCount", "turn_count"])),
    canContinue: boolean(sessionField(session, fallbackSession, ["canContinue", "can_continue"])),
    continuationBlocker: text(sessionField(session, fallbackSession, ["continuationBlocker", "continuation_blocker"])),
  };
}

export function normalizeAgentInstanceTask(value = {}, {session = {}, currentTaskUuid = "", canContinue = false} = {}) {
  const task = object(value);
  const normalizedSession = object(session);
  const taskUuid = taskUuidOf(task);
  const contextSource = contextSourceView(task.contextSource || task.context_source);
  const grantedTools = uniqueTextList(task.grantedTools || task.granted_tools);
  const sessionTurn = positiveInteger(task.sessionTurn || task.session_turn);
  const status = text(task.status).toLowerCase();
  const isCurrent = Boolean(currentTaskUuid && taskUuid === currentTaskUuid);
  const taskCanContinue = status === "completed" && canContinue === true;
  return {
    raw: task,
    taskUuid,
    agentId: taskAgentId(task) || text(normalizedSession.agentId),
    sessionKind: normalizeSessionKind(task.sessionKind || task.session_kind || normalizedSession.sessionKind),
    sessionTurn,
    continuedFromTaskUuid: text(task.continuedFromTaskUuid || task.continued_from_task_uuid),
    grantedTools,
    contextSource,
    status,
    statusView: assignmentStatusView(status, {canContinue: taskCanContinue}),
    title: text(task.title || task.description || task.displayName || task.display_name, "未命名指派"),
    currentStatus: text(task.currentStatus || task.current_status),
    planMode: text(task.planMode || task.plan_mode || object(task.input).planMode || object(task.input).plan_mode),
    output: task.output,
    isCurrent,
  };
}

function canContinueFromTask(session, taskUuid) {
  if (!session?.canContinue || session?.activeTaskUuid) return false;
  return Boolean(taskUuid && session.lastTaskUuid && taskUuid === session.lastTaskUuid);
}

function sortTasks(left, right) {
  const leftTurn = positiveInteger(left.sessionTurn) || Number.MAX_SAFE_INTEGER;
  const rightTurn = positiveInteger(right.sessionTurn) || Number.MAX_SAFE_INTEGER;
  return leftTurn - rightTurn || left.taskUuid.localeCompare(right.taskUuid);
}

export function normalizeAgentInstanceEnvelope(data = {}, {currentTask = {}, currentSession = {}} = {}) {
  const envelope = object(data);
  const taskUuid = taskUuidOf(currentTask);
  const session = normalizeAgentSession(envelope.agentSession, currentSession);
  const legacy = envelope.legacy === true || session.sessionKind !== "independent";
  const expectedAgentId = session.agentId || taskAgentId(currentTask);
  let sourceTasks = array(envelope.tasks);

  if (legacy) {
    const selected = sourceTasks.find((task) => taskUuidOf(task) === taskUuid);
    sourceTasks = selected ? [selected] : (taskUuid ? [currentTask] : sourceTasks.slice(0, 1));
  } else {
    sourceTasks = sourceTasks.filter((task) => {
      const candidateAgentId = taskAgentId(task);
      return !expectedAgentId || !candidateAgentId || candidateAgentId === expectedAgentId;
    });
    if (taskUuid && !sourceTasks.some((task) => taskUuidOf(task) === taskUuid)) sourceTasks.push(currentTask);
  }

  const unique = new Map();
  for (const task of sourceTasks) {
    const id = taskUuidOf(task);
    if (!id || unique.has(id)) continue;
    unique.set(id, task);
  }
  const tasks = [...unique.values()].map((task) => {
    const id = taskUuidOf(task);
    const isHead = Boolean(id && session.lastTaskUuid && id === session.lastTaskUuid);
    return normalizeAgentInstanceTask(task, {
      session,
      currentTaskUuid: taskUuid,
      canContinue: isHead && canContinueFromTask(session, id),
    });
  }).sort(sortTasks);

  const declaredTotal = Number(envelope.total || 0);
  return {
    ok: envelope.ok !== false,
    legacy,
    session,
    tasks,
    total: Number.isFinite(declaredTotal) && declaredTotal > 0 ? Math.max(declaredTotal, tasks.length) : tasks.length,
    limited: Number.isFinite(declaredTotal) && declaredTotal > tasks.length,
  };
}

export function localAgentInstanceEnvelope({currentTask = {}, currentSession = {}} = {}) {
  const session = normalizeAgentSession(currentSession);
  return normalizeAgentInstanceEnvelope({
    ok: true,
    legacy: session.sessionKind !== "independent",
    agentSession: currentSession,
    tasks: taskUuidOf(currentTask) ? [currentTask] : [],
  }, {currentTask, currentSession});
}

export function currentAgentInstanceView({currentTask = {}, currentSession = {}} = {}) {
  const session = normalizeAgentSession(currentSession);
  const taskUuid = taskUuidOf(currentTask);
  const task = normalizeAgentInstanceTask(currentTask, {
    session,
    currentTaskUuid: taskUuid,
    canContinue: canContinueFromTask(session, taskUuid),
  });
  return {
    session,
    task,
    legacy: task.sessionKind !== "independent",
    instanceLabel: task.sessionKind === "independent" ? shortAgentId(task.agentId || session.agentId) : "旧任务",
    turnLabel: task.sessionTurn ? `第 ${task.sessionTurn} 次指派` : "单次记录",
    previousLabel: task.continuedFromTaskUuid ? shortAgentId(task.continuedFromTaskUuid) : "无",
    statusView: task.statusView,
  };
}

function parseJsonObject(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) return value;
  const raw = text(value);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return object(parsed);
  } catch {
    const start = raw.indexOf("{");
    if (start < 0) return {};
    try { return object(JSON.parse(raw.slice(start))); }
    catch { return {}; }
  }
}

export function agentInfoResultView(toolName, value) {
  if (text(toolName) !== "AgentInfo") return {isAgentInfo: false, hasData: false, readOnly: false};
  const data = parseJsonObject(value);
  const currentTask = object(data.task);
  const envelope = normalizeAgentInstanceEnvelope({
    ...data,
    tasks: array(data.tasks).length ? data.tasks : (taskUuidOf(currentTask) ? [currentTask] : []),
  }, {currentTask, currentSession: data.agentSession});
  const session = envelope.session;
  const hasData = Boolean(session.agentId || envelope.tasks.length);
  return {
    isAgentInfo: true,
    hasData,
    readOnly: true,
    session,
    tasks: envelope.tasks,
    legacy: envelope.legacy,
    instanceLabel: session.sessionKind === "independent" ? shortAgentId(session.agentId) : "旧任务",
    availabilityLabel: session.activeTaskUuid
      ? `当前任务 ${shortAgentId(session.activeTaskUuid)}`
      : (session.canContinue ? "空闲，可继续指派" : (session.status === "closed" ? "已关闭" : "空闲")),
  };
}
