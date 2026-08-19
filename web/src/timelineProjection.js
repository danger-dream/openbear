import {contextCompactionView} from "./views/consoleView/agentPlanPresentation.js";
import {isUserInteractionOperation} from "./views/consoleView/userInteractionPresentation.js";

function defaultAnswerContent(text) {
  return String(text || "");
}

function defaultDedupeDisplayEvents(events) {
  return events;
}

function defaultAgentSummary() {
  return { preview: "" };
}

function optionHelpers(options = {}) {
  return {
    answerContent: options.answerContent || defaultAnswerContent,
    dedupeDisplayEvents: options.dedupeDisplayEvents || defaultDedupeDisplayEvents,
    agentSummary: options.agentSummary || defaultAgentSummary,
  };
}

function answerDisplayEventKey(event) {
  if (event?.kind !== "answer") return "";
  return String(event.eventKey || event.message?.eventKey || "").trim();
}

function answerTurnKey(event) {
  if (event?.kind !== "answer") return "";
  return String(event.turnUuid || event.turn_uuid || event.message?.turnUuid || event.message?.turn_uuid || "").trim();
}

function answerDedupeKey(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function textSnapshotsCompatible(a, b) {
  const left = answerDedupeKey(a);
  const right = answerDedupeKey(b);
  return Boolean(left && right && (left === right || left.startsWith(right) || right.startsWith(left)));
}

function answerEventsCompatible(prev, next) {
  if (!prev || !next || prev.kind !== "answer" || next.kind !== "answer") return false;
  const prevTurn = answerTurnKey(prev);
  const nextTurn = answerTurnKey(next);
  if (prevTurn && nextTurn && prevTurn !== nextTurn) return false;
  const prevKey = answerDisplayEventKey(prev);
  const nextKey = answerDisplayEventKey(next);
  const prevText = prev.message?.content || "";
  const nextText = next.message?.content || "";
  const prevDedupe = answerDedupeKey(prevText);
  const nextDedupe = answerDedupeKey(nextText);
  const sameContent = Boolean(prevDedupe && nextDedupe && prevDedupe === nextDedupe && textSnapshotsCompatible(prevText, nextText));
  if (prevKey || nextKey) {
    if (prevKey && nextKey) return prevKey === nextKey;
    return sameContent;
  }
  const prevReasoning = String(prev.message?.reasoning || "").trim();
  const nextReasoning = String(next.message?.reasoning || "").trim();
  const sameReasoningOnly = !prevDedupe && !nextDedupe && prevReasoning && prevReasoning === nextReasoning;
  return Boolean(sameContent || sameReasoningOnly);
}

function mergeAnswerDisplayEvent(prev, next) {
  const prevText = prev?.message?.content || "";
  const nextText = next?.message?.content || "";
  const content = answerDedupeKey(nextText).length >= answerDedupeKey(prevText).length ? nextText : prevText;
  const reasoning = next?.message?.reasoning || prev?.message?.reasoning || "";
  return {
    ...prev,
    ...next,
    eventKey: answerDisplayEventKey(next) || answerDisplayEventKey(prev),
    turnUuid: answerTurnKey(next) || answerTurnKey(prev),
    reasoningActive: Boolean(next?.reasoningActive),
    message: {
      ...(prev?.message || {}),
      ...(next?.message || {}),
      eventKey: answerDisplayEventKey(next) || answerDisplayEventKey(prev),
      turnUuid: answerTurnKey(next) || answerTurnKey(prev),
      content,
      reasoning,
      live: Boolean(next?.message?.live),
    },
    ts: next?.ts || prev?.ts,
  };
}

function mergeAnswerEvent(events, next) {
  const idx = events.findIndex((item) => answerEventsCompatible(item, next));
  if (idx >= 0) events[idx] = mergeAnswerDisplayEvent(events[idx], next);
  else events.push(next);
}

function finishActiveReasoning(turn) {
  if (!turn || !Array.isArray(turn.events)) return;
  turn.events = turn.events.map((item) => {
    if (!item || item.kind !== "answer" || !item.reasoningActive) return item;
    return {
      ...item,
      reasoningActive: false,
      message: { ...(item.message || {}), live: false },
    };
  });
}

const OP_ACTIVE_LIFECYCLES = new Set(["active", "paused"]);
const OP_ACTIVE_STATUSES = new Set(["queued", "running", "pausing", "paused", "resuming", "stopping"]);
const OP_TERMINAL_STATUSES = new Set(["completed", "partial", "failed", "cancelled", "interrupted", "needs_openbear_control"]);

export function normalizeOperations(operations = []) {
  return [...(operations || [])]
    .filter((op) => op && typeof op === "object" && op.opId)
    .sort((a, b) => (Number(a.displaySeq || 0) || 0) - (Number(b.displaySeq || 0) || 0));
}

function isPlainObject(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

export function isContextCompactionOperation(operation = {}) {
  if (!isPlainObject(operation)) return false;
  if (String(operation.opType || operation.op_type || "").trim() === "context_compaction") return true;
  if (String(operation.source || "").trim() === "context_compaction") return true;
  if (!String(operation.opId || operation.op_id || "").trim().startsWith("tool:context-compaction:")) return false;
  const payload = isPlainObject(operation.payload) ? operation.payload : {};
  const toolName = String(payload.toolName || payload.name || payload.rootToolName || "").trim();
  const compactionId = String(payload.compactionId || "").trim();
  const summaryId = Number(payload.summaryId || 0);
  return toolName === "ContextCompaction"
    && (compactionId.startsWith("context-compaction:") || (Number.isInteger(summaryId) && summaryId > 0));
}

function mergeOperationPayload(oldPayload = {}, patchPayload = {}) {
  const out = { ...(isPlainObject(oldPayload) ? oldPayload : {}) };
  for (const [key, value] of Object.entries(isPlainObject(patchPayload) ? patchPayload : {})) {
    if (value === null || value === undefined) continue;
    if (isPlainObject(value) && isPlainObject(out[key])) out[key] = mergeOperationPayload(out[key], value);
    else out[key] = value;
  }
  return out;
}

function operationValueText(value) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); }
  catch { return String(value); }
}

function compactTextChunks(chunks = []) {
  if (chunks.length <= 128) return chunks;
  return [chunks.join("")];
}

function appendPayloadDelta(oldPayload = {}, delta = "") {
  const oldChunks = Array.isArray(oldPayload.textChunks)
    ? oldPayload.textChunks
    : (oldPayload.text ? [String(oldPayload.text)] : []);
  const chunks = compactTextChunks(oldChunks.slice());
  if (delta) chunks.push(String(delta));
  return chunks;
}

function operationPayloadText(payload = {}) {
  if (typeof payload.text === "string") return payload.text;
  const chunks = Array.isArray(payload.textChunks) ? payload.textChunks : null;
  if (!chunks || !chunks.length) return "";
  const cached = payload.__openbearJoinedText;
  if (cached?.chunks === chunks && cached?.count === chunks.length) return cached.value;
  const value = chunks.join("");
  try {
    Object.defineProperty(payload, "__openbearJoinedText", {
      value: { chunks, count: chunks.length, value },
      configurable: true,
    });
  } catch { /* payload may be sealed; joining is still correct */ }
  return value;
}

export function reduceOperationFrame(oldOperation, frame) {
  if (!frame || typeof frame !== "object" || !frame.opId) return oldOperation || null;
  const oldRevision = Number(oldOperation?.revision || 0) || 0;
  const revision = Number(frame.revision || 0) || oldRevision + 1;
  if (oldOperation && revision <= oldRevision) return oldOperation;
  const payload = frame.payload && typeof frame.payload === "object" ? frame.payload : {};
  const oldPayload = oldOperation?.payload && typeof oldOperation.payload === "object" ? oldOperation.payload : {};
  let nextPayload = mergeOperationPayload(oldPayload, payload);
  if (["assistant_message", "reasoning"].includes(String(frame.opType || oldOperation?.opType || ""))) {
    if (Object.prototype.hasOwnProperty.call(payload, "delta") && !payload.snapshot) {
      nextPayload.textChunks = appendPayloadDelta(oldPayload, payload.delta || "");
      delete nextPayload.text;
    } else if (Object.prototype.hasOwnProperty.call(payload, "text")) {
      nextPayload.text = String(payload.text || "");
      delete nextPayload.textChunks;
      delete nextPayload.__openbearJoinedText;
    }
    if (Object.prototype.hasOwnProperty.call(payload, "complete")) nextPayload.complete = Boolean(payload.complete);
  }
  const action = String(frame.action || "");
  const inferredTerminal = ["end", "error", "cancel", "stop"].includes(action) ? (action === "error" ? "failed" : action === "cancel" || action === "stop" ? "cancelled" : "completed") : "";
  const hasPayloadStatus = Object.prototype.hasOwnProperty.call(payload, "status") && payload.status !== null && payload.status !== undefined && payload.status !== "";
  const explicitStatus = hasPayloadStatus ? String(payload.status || "") : "";
  const status = explicitStatus || inferredTerminal || String(frame.status || nextPayload.status || oldOperation?.status || "");
  if (inferredTerminal && !explicitStatus) nextPayload.status = inferredTerminal;
  const lifecycle = lifecycleFromOperation(String(frame.opType || oldOperation?.opType || ""), status, nextPayload, frame.action);
  const terminalAtMs = Number(
    oldOperation?.terminalAtMs
    || (["terminal", "waiting_control"].includes(lifecycle)
      ? (frame.updatedAtMs || frame.createdAtMs || Date.now())
      : 0)
  ) || 0;
  return {
    ...(oldOperation || {}),
    conversationId: frame.conversationId || frame.conversationUuid || oldOperation?.conversationId || "",
    conversationUuid: frame.conversationUuid || frame.conversationId || oldOperation?.conversationUuid || "",
    internalChatId: Number(frame.internalChatId || oldOperation?.internalChatId || 0) || 0,
    opId: frame.opId,
    opType: frame.opType || oldOperation?.opType || "",
    turnId: frame.turnId || frame.turnUuid || oldOperation?.turnId || "",
    turnUuid: frame.turnUuid || frame.turnId || oldOperation?.turnUuid || "",
    parentTurnId: frame.parentTurnId || oldOperation?.parentTurnId || "",
    runRootTurnId: frame.runRootTurnId || oldOperation?.runRootTurnId || "",
    // displaySeq is the operation's immutable first-placement key. Later patches
    // update state in place and must never move a visible timeline row.
    displaySeq: Number(oldOperation?.displaySeq || frame.displaySeq || 0) || 0,
    createdAtMs: Number(oldOperation?.createdAtMs || frame.createdAtMs || Date.now()) || Date.now(),
    updatedAtMs: Number(frame.updatedAtMs || Date.now()) || Date.now(),
    terminalAtMs,
    revision,
    status,
    lifecycle,
    internal: Boolean(oldOperation?.internal || nextPayload.internal),
    source: oldOperation?.source || frame.source || "",
    targetType: frame.targetType || oldOperation?.targetType || "",
    targetId: frame.targetId || oldOperation?.targetId || "",
    taskUuid: frame.taskUuid || oldOperation?.taskUuid || nextPayload.taskUuid || "",
    runId: frame.runId || oldOperation?.runId || nextPayload.runId || frame.turnId || frame.turnUuid || "",
    transcriptMessageIds: oldOperation?.transcriptMessageIds || [],
    payload: nextPayload,
  };
}

function lifecycleFromOperation(opType, status, payload = {}, action = "") {
  if (opType === "notice") return "informational";
  if (action === "end" && status === "needs_openbear_control") return "waiting_control";
  if (["end", "error", "cancel", "stop"].includes(action)) return "terminal";
  if (["queued", "running", "pausing", "resuming", "stopping"].includes(status)) return "active";
  if (status === "paused") return "paused";
  if (status === "needs_openbear_control") return "waiting_control";
  if (OP_TERMINAL_STATUSES.has(status)) return "terminal";
  if (["assistant_message", "reasoning"].includes(opType) && payload.complete) return "terminal";
  if (["notice", "stats", "user_message", "run_control", "agent_supervision"].includes(opType)) return "informational";
  return "";
}

export function applyOperationFrame(store, frame) {
  if (!store || !frame || typeof frame !== "object" || !frame.opId) return false;
  const byId = store.operationsById instanceof Map ? store.operationsById : new Map();
  const revisionByOpId = store.revisionByOpId instanceof Map ? store.revisionByOpId : new Map();
  const oldRevision = Number(revisionByOpId.get(frame.opId) || byId.get(frame.opId)?.revision || 0) || 0;
  const incomingRevision = Number(frame.revision || 0) || oldRevision + 1;
  const action = String(frame.action || "");
  const missingBaseOperation = !byId.has(frame.opId)
    && incomingRevision > 1
    && action !== "snapshot";
  const revisionGap = oldRevision > 0
    && incomingRevision > oldRevision + 1
    && action !== "snapshot";
  if (missingBaseOperation || revisionGap) {
    const previousLastFrameSeq = Number(store.lastFrameSeq || 0) || 0;
    const incomingFrameSeq = Number(frame.frameSeq || 0) || 0;
    const requiresFullState = Boolean(incomingFrameSeq && incomingFrameSeq <= previousLastFrameSeq);
    store.needsResync = true;
    store.revisionGap = {
      opId: frame.opId,
      expectedRevision: oldRevision + 1,
      incomingRevision,
      frameSeq: incomingFrameSeq,
      resyncMode: requiresFullState ? "full_state" : "frames",
      requiresFullState,
    };
    store.lastFrameSeq = Math.max(previousLastFrameSeq, incomingFrameSeq);
    return false;
  }
  if (incomingRevision <= oldRevision) {
    store.lastFrameSeq = Math.max(Number(store.lastFrameSeq || 0), Number(frame.frameSeq || 0) || 0);
    return false;
  }
  const next = reduceOperationFrame(byId.get(frame.opId), frame);
  if (!next) return false;
  byId.set(frame.opId, next);
  revisionByOpId.set(frame.opId, next.revision);
  const ids = Array.isArray(store.orderedOpIds) ? store.orderedOpIds : [];
  if (!ids.includes(frame.opId)) {
    ids.push(frame.opId);
    ids.sort((a, b) => (Number(byId.get(a)?.displaySeq || 0) || 0) - (Number(byId.get(b)?.displaySeq || 0) || 0));
  }
  store.operationsById = byId;
  store.revisionByOpId = revisionByOpId;
  store.orderedOpIds = ids;
  store.lastFrameSeq = Math.max(Number(store.lastFrameSeq || 0), Number(frame.frameSeq || 0) || 0);
  return true;
}

export function isTerminalOperationFrame(frame = {}) {
  const action = String(frame?.action || "");
  const payload = frame?.payload && typeof frame.payload === "object" ? frame.payload : {};
  if (action === "cancel" && payload.merged === true && payload.mergedTo) return false;
  const status = String(payload.status || frame?.status || "");
  return ["end", "error", "cancel", "stop"].includes(action)
    || ["completed", "failed", "cancelled", "stopped"].includes(status);
}

export function shouldApplyOperationFrame(frame, store = {}) {
  if (!frame || typeof frame !== "object" || !frame.opId) return false;
  const frameSeq = Number(frame.frameSeq || 0) || 0;
  const lastFrameSeq = Number(store.lastFrameSeq || 0) || 0;
  if (!frameSeq || frameSeq > lastFrameSeq) return true;
  const byId = store.operationsById instanceof Map ? store.operationsById : new Map();
  const revisions = store.revisionByOpId instanceof Map ? store.revisionByOpId : new Map();
  const currentRevision = Number(revisions.get(frame.opId) || byId.get(frame.opId)?.revision || 0) || 0;
  const incomingRevision = Number(frame.revision || 0) || 0;
  return incomingRevision > currentRevision;
}

export function convergeStoppedAcknowledgement(options = {}) {
  const message = options.message && typeof options.message === "object" ? options.message : {};
  const sourceConversationUuid = String(options.sourceConversationUuid || "").trim();
  const socketConversationUuid = String(options.socketConversationUuid || "").trim();
  const activeConversationUuid = String(options.activeConversationUuid || "").trim();
  if (
    String(message.type || "") !== "stopped"
    || !options.sourceSocket
    || options.sourceSocket !== options.activeSocket
    || !sourceConversationUuid
    || sourceConversationUuid !== socketConversationUuid
    || sourceConversationUuid !== activeConversationUuid
  ) return false;
  const reason = String(message.reason || "").trim();
  options.setStatus?.(reason || "停止已确认，正在同步状态");
  options.refreshCurrentState?.({
    source: "stopped_ack",
    stopAtMs: Number(message.stopAtMs || 0) || 0,
  });
  options.refreshConversationList?.();
  return true;
}

function operationTaskUuid(op = {}) {
  const payload = isPlainObject(op?.payload) ? op.payload : {};
  const task = isPlainObject(payload.task) ? payload.task : {};
  return String(op?.taskUuid || payload.taskUuid || payload.task_uuid || task.taskUuid || task.task_uuid || "").trim();
}

export function deriveOperationRunState(operations = []) {
  const ops = normalizeOperations(operations);
  const active = ops.filter((op) => {
    const lifecycle = String(op.lifecycle || "");
    if (["terminal", "informational", "paused", "waiting_control"].includes(lifecycle)) return false;
    return OP_ACTIVE_LIFECYCLES.has(lifecycle) || OP_ACTIVE_STATUSES.has(String(op.status || op.payload?.status || ""));
  });
  const waitingControl = ops.filter((op) => op.opType !== "notice" && (String(op.lifecycle || "") === "waiting_control" || String(op.status || op.payload?.status || "") === "needs_openbear_control"));
  const activeRootRuns = active.filter((op) => op.opType === "run" && !operationTaskUuid(op));
  const activeAgents = active.filter((op) => op.opType === "agent");
  const activeTools = active.filter((op) => op.opType === "tool");
  const activeSupervision = active.filter((op) => op.opType === "agent_supervision");
  const supervisionWaiting = activeSupervision.length > 0;
  const rootTurnRunning = activeRootRuns.length > 0;
  const foregroundRunning = !supervisionWaiting && (rootTurnRunning || (activeTools.length > 0 && activeAgents.length === 0));
  const backgroundRunning = supervisionWaiting || (!foregroundRunning && activeAgents.length > 0);
  const activeRootRun = activeRootRuns.at(-1) || null;
  const activeTurn = activeRootRun || activeTools.at(-1) || null;
  const activeAgent = activeAgents.at(-1) || null;
  const activeSupervisor = activeSupervision.at(-1) || null;
  const startedAt = Number(activeTurn?.createdAtMs || activeAgent?.createdAtMs || activeSupervisor?.createdAtMs || 0) || 0;
  let statusLabel = "就绪";
  if (foregroundRunning) statusLabel = activeTools.length ? "工具执行中" : "运行中";
  else if (backgroundRunning) statusLabel = String(activeSupervisor?.payload?.statusText || "") || (activeAgents.length > 1 ? "Agent 并行执行中" : "Agent 后台执行中");
  else if (waitingControl.some((op) => op.opType === "agent")) statusLabel = "Agent 等待裁决";
  return {
    rootTurnRunning,
    activeRootTurnId: activeRootRun?.runRootTurnId || activeRootRun?.run_root_turn_uuid || activeRootRun?.turnId || activeRootRun?.turnUuid || "",
    foregroundRunning,
    backgroundRunning,
    running: foregroundRunning || backgroundRunning,
    statusLabel,
    activeTurnId: activeTurn?.turnId || activeTurn?.turnUuid || "",
    activeStartedAtMs: startedAt,
    activeOperationIds: active.map((op) => op.opId).filter(Boolean),
    activeAgentOperationIds: activeAgents.map((op) => op.opId).filter(Boolean),
  };
}

export function withTransientIdleThinking(turnList = [], runState = {}, options = {}) {
  if (!runState?.rootTurnRunning || !runState?.activeRootTurnId || !turnList.length) return turnList;
  const activeRootTurnId = String(runState.activeRootTurnId).trim();
  const targetIndex = turnList.findIndex((turn) => {
    const turnUuid = String(turn?.turnUuid || turn?.user?.turnUuid || turn?.user?.turn_uuid || "").trim();
    return turnUuid === activeRootTurnId;
  });
  if (targetIndex < 0) return turnList;
  return turnList.map((turn, index) => {
    if (index !== targetIndex) return turn;
    const events = Array.isArray(turn?.events)
      ? turn.events.filter((event) => event?.id !== "transient-run-thinking")
      : [];
    return {
      ...turn,
      events: [...events, {
        kind: "live_status",
        id: "transient-run-thinking",
        status: "正在思考 …",
        startedAt: Number(options.startedAtMs || options.lastVisibleOutputAtMs || Date.now()) || Date.now(),
        active: true,
        transient: true,
        persistentRunIndicator: true,
      }],
    };
  });
}

function opTsSec(op) {
  const ts = Number(op?.createdAtMs || op?.updatedAtMs || 0) || Date.now();
  return Math.floor(ts / 1000);
}
function opTsMs(op) {
  return Number(op?.updatedAtMs || op?.createdAtMs || 0) || Date.now();
}
function opStartedAtMs(op) {
  return Number(op?.createdAtMs || op?.payload?.startedAtMs || op?.updatedAtMs || 0) || Date.now();
}

function firstOperationValue(objects, keys) {
  for (const object of objects) {
    if (!isPlainObject(object)) continue;
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(object, key) && object[key] !== null && object[key] !== undefined && object[key] !== "") {
        return object[key];
      }
    }
  }
  return undefined;
}

export function normalizeModelRetryOperation(op = {}) {
  const payload = isPlainObject(op?.payload) ? op.payload : {};
  const state = isPlainObject(payload.state) ? payload.state : {};
  const nestedRetry = isPlainObject(payload.retry) ? payload.retry : {};
  const sources = [payload, state, nestedRetry];
  const status = String(firstOperationValue(sources, ["status", "retryStatus", "retry_status"]) || op.status || "").trim();
  const activeValue = firstOperationValue(sources, ["active", "isActive", "is_active"]);
  const active = activeValue === undefined
    ? OP_ACTIVE_STATUSES.has(status) || OP_ACTIVE_LIFECYCLES.has(String(op.lifecycle || ""))
    : Boolean(activeValue);
  const waitMsValue = firstOperationValue(sources, ["waitMs", "wait_ms", "delayMs", "delay_ms", "backoffMs", "backoff_ms"]);
  const waitSecondsValue = firstOperationValue(sources, ["waitSeconds", "wait_seconds", "delaySeconds", "delay_seconds"]);
  const rootCauseValue = firstOperationValue(sources, ["rootCause", "root_cause"]);
  const attemptsValue = firstOperationValue(sources, ["attempts"]);
  const detailsValue = firstOperationValue(sources, ["details"]);
  return {
    attempt: Number(firstOperationValue(sources, ["attempt", "retryAttempt", "retry_attempt"]) || 0) || 0,
    maxAttempts: Number(firstOperationValue(sources, ["maxAttempts", "max_attempts", "maxRetries", "max_retries", "retryMax", "retry_max"]) || 0) || 0,
    waitMs: Number(waitMsValue || 0) || (Number(waitSecondsValue || 0) * 1000) || 0,
    retryAtMs: Number(firstOperationValue(sources, ["retryAtMs", "retry_at_ms", "resumeAtMs", "resume_at_ms"]) || 0) || 0,
    reason: String(firstOperationValue(sources, ["reason", "retryReason", "retry_reason"]) || "").trim(),
    summary: String(firstOperationValue(sources, ["summary", "errorSummary", "error_summary"]) || "").trim(),
    error: String(firstOperationValue(sources, ["error", "errorMessage", "error_message", "message"]) || "").trim(),
    transportStatus: Number(firstOperationValue(sources, ["transportStatus", "transport_status"]) || 0) || 0,
    upstreamStatus: Number(firstOperationValue(sources, ["upstreamStatus", "upstream_status"]) || 0) || 0,
    rootCause: isPlainObject(rootCauseValue) ? {...rootCauseValue} : {},
    attempts: Array.isArray(attemptsValue) ? attemptsValue.map((item) => isPlainObject(item) ? {...item} : item) : [],
    details: isPlainObject(detailsValue) ? {...detailsValue} : {},
    active,
    status: status || (active ? "running" : "resumed"),
    cancellable: Boolean(firstOperationValue(sources, ["cancellable", "cancelSupported", "cancel_supported", "canCancel", "can_cancel"])),
    taskUuid: operationTaskUuid(op),
  };
}

function secondsOrMs(value) {
  const number = Number(value || 0);
  if (!number) return 0;
  return number < 100_000_000_000 ? number * 1000 : number;
}

export function eventStartedAtMs(event) {
  return Number(
    event?.startedAt
    || event?.startedAtMs
    || event?.operation?.createdAtMs
    || event?.message?.createdAtMs
    || 0
  ) || 0;
}

export function eventUpdatedAtMs(event) {
  return Number(
    event?.operation?.terminalAtMs
    || event?.terminalAtMs
    || event?.ts
    || event?.updatedAtMs
    || event?.operation?.updatedAtMs
    || eventStartedAtMs(event)
    || 0
  ) || secondsOrMs(event?.message?.createdAt || event?.result?.createdAt || event?.createdAt);
}

export function eventDisplayTimeMs(event) {
  return eventStartedAtMs(event) || eventUpdatedAtMs(event);
}
function opTurnId(op) {
  // execution run IDs are lifecycle identities, not visible turn identities.
  // Same-root continuations must project into the original user turn.
  const rootTurnId = String(op?.runRootTurnId || op?.run_root_turn_uuid || "").trim();
  if (rootTurnId) return rootTurnId;
  const targetType = String(op?.targetType || "");
  if (targetType === "run") {
    const runId = String(op?.runId || op?.targetId || op?.payload?.runId || "").trim();
    if (runId) return runId;
  }
  return String(op?.turnId || op?.turnUuid || op?.runId || op?.targetId || op?.payload?.runId || "").trim();
}
function ensureOperationTurn(turns, byTurn, op, currentRef) {
  const key = opTurnId(op) || currentRef.current || `op-turn-${turns.length}`;
  if (byTurn.has(key)) {
    currentRef.current = key;
    return byTurn.get(key);
  }
  const turn = { turnUuid: key, user: null, events: [], localStats: null, queued: null, parentTurnUuid: op.parentTurnId || "", internal: Boolean(op.internal || op.payload?.internal), startedAt: opTsSec(op), lastAt: opTsSec(op) };
  byTurn.set(key, turn);
  turns.push(turn);
  currentRef.current = key;
  return turn;
}
function contextCompactionProjectionOperation(op, payload = {}) {
  const compactionId = String(payload.compactionId || payload.toolCallId || op.opId || "");
  const existingArguments = String(payload.arguments || payload.args || "");
  let argumentsValue = existingArguments;
  if (!argumentsValue) {
    try { argumentsValue = JSON.stringify(payload); }
    catch { argumentsValue = ""; }
  }
  const compaction = contextCompactionView({...payload, name: "ContextCompaction"});
  const preview = compaction.cardPreview;
  const normalizedPayload = {
    ...payload,
    toolCallId: compactionId,
    name: "ContextCompaction",
    toolName: "ContextCompaction",
    arguments: argumentsValue,
    args: argumentsValue,
    preview,
  };
  if (
    !Object.prototype.hasOwnProperty.call(normalizedPayload, "resultText")
    && !Object.prototype.hasOwnProperty.call(normalizedPayload, "result")
    && payload.outputPreview
  ) normalizedPayload.result = payload.outputPreview;
  return {...op, payload: normalizedPayload};
}

function opToolCall(op, mergedRootCall = null) {
  const payload = op.payload || {};
  const isAgentOp = String(op?.opType || "") === "agent";
  const root = mergedRootCall && typeof mergedRootCall === "object" ? mergedRootCall : {};
  const name = isAgentOp
    ? String(payload.rootToolName || root.name || payload.toolName || "Agent")
    : String(payload.name || payload.toolName || "Tool");
  const id = isAgentOp
    ? String(payload.rootToolCallId || payload.agentToolCallId || root.id || op.opId || payload.toolCallId || "")
    : String(payload.toolCallId || op.opId || "");
  const fullArguments = isAgentOp
    ? String(payload.rootArguments || payload.agentArguments || root.arguments || payload.arguments || payload.args || "")
    : String(payload.arguments || payload.args || "");
  // Historical summary operations deliberately omit full arguments.  Put their
  // bounded preview source into the established `arguments` slot so every
  // existing collapsed-card renderer receives the same input it used before
  // lazy loading, rather than depending on a new optional display field.
  const previewArguments = String(payload.previewArguments || "");
  const argumentsValue = fullArguments || previewArguments;
  return {
    id,
    name,
    arguments: argumentsValue,
    previewArguments,
    preview: String(payload.preview || ""),
  };
}
function opToolResult(op, mergedRootCall = null) {
  const payload = op.payload || {};
  const hasResult = Object.prototype.hasOwnProperty.call(payload, "resultText")
    || Object.prototype.hasOwnProperty.call(payload, "result");
  // State snapshots intentionally omit persisted tool result bodies. Do not
  // invent an empty result card: the expanded card will lazy-fetch it instead.
  if (!hasResult) return null;
  const call = opToolCall(op, mergedRootCall);
  const content = operationValueText(payload.resultText || payload.result || "");
  return {
    id: `op-result-${op.opId}`,
    role: "tool",
    toolCallId: call.id,
    tool_call_id: call.id,
    name: call.name,
    content,
    durationMs: Number(payload.durationMs || 0),
    createdAt: opTsSec(op),
  };
}

function collectTaskUuids(value, out = new Set()) {
  if (!value || typeof value !== "object") return out;
  const add = (raw) => {
    const text = String(raw || "").trim();
    if (text) out.add(text);
  };
  add(value.taskUuid || value.task_uuid);
  if (Array.isArray(value.taskUuids)) for (const item of value.taskUuids) add(item);
  if (value.task && typeof value.task === "object") collectTaskUuids(value.task, out);
  if (value.result && typeof value.result === "object") collectTaskUuids(value.result, out);
  if (value.agentSession && typeof value.agentSession === "object") add(value.agentSession.lastTaskUuid || value.agentSession.last_task_uuid);
  if (Array.isArray(value.results)) for (const item of value.results) collectTaskUuids(item, out);
  return out;
}

function agentToolTaskUuids(event) {
  if (!event || event.kind !== "tool") return new Set();
  return collectTaskUuids(event.livePayload || event.result || event.results || {});
}

function parseOperationArguments(value) {
  if (value && typeof value === "object") return value;
  try {
    const parsed = JSON.parse(String(value || "{}"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function agentControlRecentEvent(op, payload) {
  const action = String(payload.controlAction || "message");
  const taskUuid = String(payload.taskUuid || "").trim();
  return {
    taskUuid,
    taskUuids: Array.isArray(payload.taskUuids) ? payload.taskUuids : (taskUuid ? [taskUuid] : []),
    seq: Number(op.displaySeq || op.revision || 0) || 0,
    ts: opTsMs(op),
    kind: "agent_control",
    type: "agent_control",
    summary: payload.statusText || payload.summary || "Agent 控制事件",
    message: payload.text || "",
    controlAction: action,
    controlUuid: payload.controlUuid || "",
    detail: {
      action,
      text: payload.text || "",
      controlUuid: payload.controlUuid || "",
    },
  };
}

function agentSupervisionRecentEvent(op, payload) {
  return {
    seq: Number(op.displaySeq || op.revision || 0) || 0,
    ts: opTsMs(op),
    kind: "agent_supervision",
    type: "agent_supervision",
    summary: payload.statusText || payload.summary || "Agent 监督事件",
    detail: {
      mode: payload.mode || "",
      wakeReason: payload.wakeReason || "",
      active: Boolean(payload.active),
    },
  };
}

function agentPlanDecisionRecentEvent(op, payload, call) {
  const args = parseOperationArguments(call.arguments);
  const result = parseOperationArguments(payload.result || payload.resultText);
  const taskUuid = String(args.taskUuid || args.task_uuid || result.taskUuid || payload.taskUuid || "").trim();
  const action = String(args.action || result.action || "decision");
  return {
    taskUuid,
    taskUuids: taskUuid ? [taskUuid] : [],
    seq: Number(op.displaySeq || op.revision || 0) || 0,
    ts: opTsMs(op),
    kind: "plan_decision",
    type: "plan_decision",
    summary: `Agent Plan 决策：${action}`,
    detail: {
      action,
      expectedPlanVersion: Number(args.expectedPlanVersion || result.planVersion || 0),
      reason: String(args.reason || result.reason || ""),
      grantedTools: Array.isArray(args.grantedTools) ? args.grantedTools : [],
    },
  };
}

function mergeAgentRecentEvent(payload, recent) {
  const base = payload && typeof payload === "object" ? { ...payload } : {};
  const current = Array.isArray(base.recentEvents) ? base.recentEvents : [];
  const key = [recent.kind, recent.controlUuid, recent.taskUuid, recent.seq, recent.summary].join("|");
  const exists = current.some((item) => [item?.kind || item?.type, item?.controlUuid, item?.taskUuid || item?.task_uuid, item?.seq || item?.eventSeq, item?.summary || item?.message].join("|") === key);
  base.recentEvents = exists ? current : [...current, recent];
  const targetTaskUuid = String(recent.taskUuid || "").trim();
  if (targetTaskUuid && Array.isArray(base.results)) {
    base.results = base.results.map((item) => {
      const itemTasks = collectTaskUuids(item);
      if (!itemTasks.has(targetTaskUuid)) return item;
      const itemRecent = Array.isArray(item?.recentEvents) ? item.recentEvents : [];
      return { ...item, recentEvents: [...itemRecent, recent] };
    });
  }
  return base;
}

function resolveAgentTaskReferences(payload, taskInfoByUuid = new Map()) {
  const raw = collectTaskUuids(payload);
  for (const value of [payload?.to, payload?.target, payload?.task, payload?.title]) {
    if (typeof value === "string" && value.trim()) raw.add(value.trim());
  }
  const resolved = new Set();
  for (const reference of raw) {
    if (taskInfoByUuid.has(reference)) {
      resolved.add(reference);
      continue;
    }
    const normalized = String(reference || "").trim().toLowerCase();
    if (!normalized) continue;
    const matches = [...taskInfoByUuid.entries()].filter(([taskUuid, info]) => (
      taskUuid.toLowerCase().startsWith(normalized)
      || String(info?.title || "").trim().toLowerCase() === normalized
    ));
    if (matches.length === 1) resolved.add(matches[0][0]);
  }
  return resolved;
}

function attachAgentRecentToTask(turns, recent, wanted) {
  if (!wanted.size) return false;
  let attached = false;
  for (let ti = turns.length - 1; ti >= 0; ti -= 1) {
    const turn = turns[ti];
    const events = Array.isArray(turn?.events) ? turn.events : [];
    for (let ei = events.length - 1; ei >= 0; ei -= 1) {
      const event = events[ei];
      if (!event || event.kind !== "tool") continue;
      const toolTasks = agentToolTaskUuids(event);
      const targetTaskUuid = [...wanted].find((taskUuid) => toolTasks.has(taskUuid));
      if (!targetTaskUuid) continue;
      const taskRecent = {...recent, taskUuid: targetTaskUuid, taskUuids: [targetTaskUuid]};
      const nextPayload = mergeAgentRecentEvent(event.livePayload || {}, taskRecent);
      events[ei] = {...event, livePayload: nextPayload};
      turn.lastAt = Math.max(turn.lastAt || 0, Math.floor(Number(taskRecent.ts || 0) / 1000));
      attached = true;
    }
  }
  return attached;
}

function attachAgentControlToTask(turns, op, payload, taskInfoByUuid) {
  const wanted = resolveAgentTaskReferences(payload, taskInfoByUuid);
  if (!wanted.size) return false;
  const recent = agentControlRecentEvent(op, payload);
  const attached = attachAgentRecentToTask(turns, recent, wanted);
  if (attached) {
    for (const turn of turns) {
      for (let index = 0; index < (turn.events || []).length; index += 1) {
        const event = turn.events[index];
        if (!event || event.kind !== "tool") continue;
        if (![...wanted].some((taskUuid) => agentToolTaskUuids(event).has(taskUuid))) continue;
        turn.events[index] = {
          ...event,
          livePreview: payload.statusText || payload.summary || event.livePreview || "",
        };
      }
    }
  }
  return attached;
}

function agentTaskInfoMap(ops = []) {
  const out = new Map();
  for (const op of ops) {
    if (!op || op.opType !== "agent") continue;
    const payload = op.payload && typeof op.payload === "object" ? op.payload : {};
    const task = payload.task && typeof payload.task === "object" ? payload.task : {};
    const taskUuid = String(op.taskUuid || payload.taskUuid || task.taskUuid || task.task_uuid || "").trim();
    if (!taskUuid) continue;
    out.set(taskUuid, {
      title: String(task.title || task.displayName || payload.title || payload.displayName || "").trim(),
      status: String(task.status || payload.status || op.status || "").trim(),
      currentStatus: String(task.currentStatus || payload.currentStatus || "").trim(),
      runRootTurnId: String(op.runRootTurnId || op.run_root_turn_uuid || op.turnId || op.turnUuid || "").trim(),
    });
  }
  return out;
}

function attachAgentSupervisionToTasks(turns, op, payload, taskInfoByUuid) {
  let wanted = resolveAgentTaskReferences(payload, taskInfoByUuid);
  if (!wanted.size) {
    const root = String(op.runRootTurnId || op.run_root_turn_uuid || op.turnId || op.turnUuid || "").trim();
    wanted = new Set(
      [...taskInfoByUuid.entries()]
        .filter(([, info]) => !root || String(info?.runRootTurnId || "") === root)
        .map(([taskUuid]) => taskUuid),
    );
  }
  return attachAgentRecentToTask(turns, agentSupervisionRecentEvent(op, payload), wanted);
}

function formatTaskNotice(payload = {}, taskInfo = null) {
  const taskUuid = String(payload.taskUuid || payload.task_uuid || "").trim();
  const rawText = String(payload.text || payload.summary || payload.statusText || "").trim();
  const status = String(payload.status || taskInfo?.status || "").trim();
  const done = ["completed", "done", "success"].includes(status);
  const result = rawText && rawText !== taskUuid ? rawText : "";
  const looksLikeStatusText = /^Agent\s/.test(rawText) || /已回传|已通知|任务完成/.test(rawText);

  // Only Rath/Agent task notices have a matching agent operation in taskInfoByUuid.
  // Generic background process notices (for example Bash background jobs) also carry
  // taskUuid, but must not be rendered as “Agent 后台 Agent 完成”.
  if (!taskInfo) {
    return {
      status: rawText || (done ? "后台任务完成" : "后台任务有新进展"),
      preview: "",
      taskUuid,
      notification: done,
      agentNotice: false,
    };
  }

  const title = String(payload.title || taskInfo.title || "").trim();
  const cleanTitle = title && !/^general-purpose-[0-9a-f]{8}$/i.test(title) ? title : "后台 Agent";
  if (!done && looksLikeStatusText) {
    return {
      status: rawText,
      preview: taskInfo.currentStatus || "",
      taskUuid,
      notification: false,
      agentNotice: true,
    };
  }
  return {
    status: done ? `Agent ${cleanTitle} 完成` : `Agent ${cleanTitle} 有新进展`,
    preview: result ? `结果：${result}` : (taskInfo.currentStatus || "已通知主会话"),
    taskUuid,
    notification: done,
    agentNotice: true,
  };
}

function agentMergedRootCallMap(operations = []) {
  const roots = new Map();
  for (const op of operations) {
    if (String(op?.opType || "") !== "agent") continue;
    const payload = op?.payload && typeof op.payload === "object" ? op.payload : {};
    if (payload.merged !== true) continue;
    const target = String(payload.mergedTo || "").trim();
    if (!target || roots.has(target)) continue;
    roots.set(target, {
      id: String(payload.rootToolCallId || payload.agentToolCallId || payload.toolCallId || op.opId || ""),
      name: String(payload.rootToolName || payload.toolName || payload.name || "Agent"),
      arguments: String(payload.rootArguments || payload.agentArguments || payload.arguments || payload.args || ""),
    });
  }
  return roots;
}

export function projectOperationMessages(operations = [], options = {}) {
  const helpers = optionHelpers(options);
  const ops = normalizeOperations(operations);
  const taskInfoByUuid = agentTaskInfoMap(ops);
  const agentRootCallsByTarget = agentMergedRootCallMap(ops);
  const turns = [];
  const byTurn = new Map();
  const currentRef = { current: "" };
  let lastVisibleTurn = null;

  for (const op of ops) {
    const opType = String(op.opType || "");
    const payload = op.payload && typeof op.payload === "object" ? op.payload : {};
    const isVisibleContextCompaction = isContextCompactionOperation(op);
    const isVisibleUserInteraction = isUserInteractionOperation(op);
    // Root transcript retry rows are sourced only from the dedicated root
    // operation. Agent/task retry payloads remain inside their own panel.
    if (opType === "model_retry" && operationTaskUuid(op)) continue;
    const turn = ensureOperationTurn(turns, byTurn, op, currentRef);
    if (isVisibleContextCompaction || isVisibleUserInteraction) turn.internal = false;
    else if (op.internal || payload.internal) turn.internal = true;
    if (payload.hidden) turn.hidden = true;
    turn.lastAt = Math.max(turn.lastAt || 0, opTsSec(op));
    if (opType === "run") {
      turn.internal = Boolean(turn.internal || payload.internal || payload.source === "task_notification");
      turn.status = payload.status || op.status || turn.status || "";
      continue;
    }
    if (opType === "user_message") {
      if (payload.internal || op.internal) {
        turn.internal = true;
        continue;
      }
      const isQueued = Boolean(payload.queued);
      const isInterruption = Boolean(payload.interruption);
      const statusEvent = {
        kind: "live_status",
        id: op.opId,
        ts: opTsMs(op),
        startedAt: opStartedAtMs(op),
        status: payload.status || (isQueued ? "已追加到当前运行" : "插话已交给主会话"),
        preview: String(payload.text || ""),
        queued: isQueued,
        interruption: isInterruption,
        active: false,
      };
      // An interruption is part of the existing root turn.  Its durable
      // user_message operation changes queued=true -> false when the main loop
      // consumes it, but it must remain at the same timeline position instead
      // of becoming/replacing the root user bubble.
      if (isInterruption) {
        turn.events.push(statusEvent);
        turn.queuedSteering = Boolean(turn.queuedSteering || isQueued);
        continue;
      }
      if (isQueued) {
        const parent = lastVisibleTurn || turns.find((item) => item !== turn && !item.internal) || turn;
        parent.events.push(statusEvent);
        parent.queuedSteering = true;
        if (turn.steeringParentTurnUuid && byTurn.has(turn.steeringParentTurnUuid)) {
          byTurn.get(turn.steeringParentTurnUuid).continuedBySteering = true;
        }
        turn.internal = true;
        continue;
      }
      turn.steeringParentTurnUuid = lastVisibleTurn?.turnUuid || "";
      turn.user = {
        id: op.opId,
        opId: op.opId,
        turnUuid: String(turn.turnUuid || op.runRootTurnId || op.turnUuid || ""),
        deleteTraceable: Array.isArray(op.transcriptMessageIds) && op.transcriptMessageIds.some((id) => Number(id) > 0),
        role: "user",
        content: String(payload.text || payload.content || ""),
        attachments: Array.isArray(payload.attachments) ? payload.attachments : [],
        createdAt: opTsSec(op),
        queuedSteering: Boolean(turn.queuedSteering),
      };
      turn.startedAt = opTsSec(op);
      lastVisibleTurn = turn;
      continue;
    }
    if (opType === "model_retry") {
      const retry = normalizeModelRetryOperation(op);
      finishActiveReasoning(turn);
      turn.events.push({
        kind: "model_retry",
        id: op.opId,
        eventKey: op.opId,
        turnUuid: turn.turnUuid,
        ts: opTsMs(op),
        startedAt: opStartedAtMs(op),
        source: String(op.source || payload.source || "model_retry"),
        active: retry.active,
        retry,
        operation: op,
      });
      continue;
    }
    if (opType === "reasoning") {
      const text = operationPayloadText(payload);
      if (text) {
        mergeAnswerEvent(turn.events, {
          kind: "answer",
          id: op.opId,
          eventKey: op.opId,
          turnUuid: turn.turnUuid,
          ts: opTsMs(op),
          startedAt: opStartedAtMs(op),
          reasoningActive: !payload.complete,
          operation: op,
          message: { id: `${op.opId}:message`, eventKey: op.opId, turnUuid: turn.turnUuid, role: "assistant", content: "", reasoning: text, live: !payload.complete },
        });
      }
      continue;
    }
    if (opType === "assistant_message") {
      const text = operationPayloadText(payload);
      const isError = Boolean(payload.error || op.status === "failed");
      if (text || isError) {
        finishActiveReasoning(turn);
        mergeAnswerEvent(turn.events, {
          kind: "answer",
          id: op.opId,
          eventKey: op.opId,
          turnUuid: turn.turnUuid,
          ts: opTsMs(op),
          startedAt: opStartedAtMs(op),
          error: isError,
          reasoningActive: false,
          operation: op,
          message: { id: `${op.opId}:message`, eventKey: op.opId, turnUuid: turn.turnUuid, role: "assistant", content: helpers.answerContent(text), reasoning: "", error: isError, live: !payload.complete },
        });
      }
      continue;
    }
    if (opType === "tool" || opType === "agent" || isVisibleContextCompaction || isVisibleUserInteraction) {
      // Merged Agent tool-call placeholders may be finalized after the real task
      // completion card. They are not a new Agent generation and must not unlock
      // historical terminal-status folding.
      if (opType === "agent" && payload.merged === true) continue;
      if (opType === "agent") {
        // A later real Agent operation starts a new supervision generation. This
        // lets genuinely new/resumed work produce its own terminal status.
        turn.agentTerminalSupervisionSeen = false;
      }
      finishActiveReasoning(turn);
      const mergedRootCall = opType === "agent" ? agentRootCallsByTarget.get(String(op.opId || "")) : null;
      const projectedOp = isVisibleContextCompaction
        ? contextCompactionProjectionOperation(op, payload)
        : op;
      const call = opToolCall(projectedOp, mergedRootCall);
      const result = opToolResult(projectedOp, mergedRootCall);
      if (opType === "tool" && call.name === "AgentPlanDecision") {
        const recent = agentPlanDecisionRecentEvent(op, payload, call);
        const wanted = resolveAgentTaskReferences(recent, taskInfoByUuid);
        attachAgentRecentToTask(turns, recent, wanted);
        turn.internal = Boolean(turn.internal || !turn.user);
        continue;
      }
      const live = (OP_ACTIVE_LIFECYCLES.has(String(op.lifecycle || "")) || OP_ACTIVE_STATUSES.has(String(op.status || payload.status || "")))
        && !OP_TERMINAL_STATUSES.has(String(op.status || payload.status || ""))
        && String(op.lifecycle || "") !== "terminal"
        && String(op.lifecycle || "") !== "waiting_control";
      turn.events.push({
        kind: isVisibleUserInteraction ? "user_interaction" : "tool",
        id: op.opId,
        ts: opTsMs(op),
        startedAt: opStartedAtMs(op),
        calls: [call],
        toolName: call.name,
        message: null,
        result,
        results: result ? [result] : [],
        live,
        livePayload: opType === "agent" ? payload : (payload.progress || null),
        livePreview: payload.preview || helpers.agentSummary({ livePayload: payload }).preview || "",
        operation: projectedOp,
      });
      continue;
    }
    if (opType === "agent_supervision") {
      const terminalAll = !Boolean(payload.active)
        && String(payload.statusText || "") === "全部 Agent 已完成";
      if (terminalAll && turn.agentTerminalSupervisionSeen) continue;
      if (terminalAll) turn.agentTerminalSupervisionSeen = true;
      attachAgentSupervisionToTasks(turns, op, payload, taskInfoByUuid);
      turn.internal = Boolean(turn.internal || !turn.user);
      continue;
    }
    if (opType === "agent_control") {
      // AgentMessage/continue can resume work after a previous terminal boundary.
      turn.agentTerminalSupervisionSeen = false;
      attachAgentControlToTask(turns, op, payload, taskInfoByUuid);
      turn.internal = Boolean(turn.internal || !turn.user);
      continue;
    }
    if (opType === "notice" || opType === "status" || opType === "run_control") {
      // `status` operations drive the header/run-state facts, but they are not
      // transcript events. The visible “正在思考…” row is a transient frontend
      // tail idle-indicator so it can appear only after no visible output for a
      // short delay, and disappear immediately once any tool/reasoning/answer
      // output arrives.
      if (opType === "status") continue;
      const taskUuid = String(payload.taskUuid || payload.task_uuid || "").trim();
      const taskNotice = opType === "notice" && taskUuid ? formatTaskNotice(payload, taskInfoByUuid.get(taskUuid)) : null;
      turn.events.push({
        kind: "live_status",
        id: op.opId,
        ts: opTsMs(op),
        startedAt: opStartedAtMs(op),
        status: taskNotice?.status || payload.statusText || payload.text || payload.reason || "状态更新",
        preview: taskNotice?.preview || payload.preview || "",
        active: false,
        notification: Boolean(taskNotice?.notification || payload.notification),
        agentNotice: Boolean(taskNotice?.agentNotice),
        taskUuid: taskNotice?.taskUuid || taskUuid,
      });
      continue;
    }
    if (opType === "stats") {
      turn.localStats = payload;
    }
  }

  const out = [];
  for (const turn of turns) {
    if (turn.hidden && !turn.user) continue;
    if (turn.internal && !turn.events.length && !turn.localStats) continue;
    if (turn.user) out.push({ ...turn.user, queuedSteering: Boolean(turn.queuedSteering || turn.user.queuedSteering) });
    else if (!turn.internal && (turn.events.length || turn.localStats)) out.push({ id: `op-user-${turn.turnUuid}`, role: "user", content: "", syntheticPlaceholder: true, createdAt: turn.startedAt || turn.lastAt || Math.floor(Date.now() / 1000) });
    if (turn.events.length || turn.localStats) {
      out.push({
        id: `op-assistant-${turn.turnUuid}`,
        role: "assistant",
        content: "",
        reasoning: "",
        createdAt: turn.lastAt || turn.startedAt || Math.floor(Date.now() / 1000),
        localTimeline: helpers.dedupeDisplayEvents(turn.events),
        localStats: turn.localStats,
        continuedBySteering: Boolean(turn.continuedBySteering),
        queuedSteering: Boolean(turn.queuedSteering),
      });
    }
  }
  return out;
}
