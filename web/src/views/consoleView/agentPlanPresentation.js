const PHASE_META = {
  drafting: { label: "正在制定计划", tone: "active", description: "Agent 正在把任务拆成可检查的执行步骤。" },
  awaiting_plan_decision: { label: "等待计划确认", tone: "waiting", description: "执行计划已提交，正在等待主控确认。" },
  revising: { label: "正在调整计划", tone: "active", description: "Agent 正在根据审查意见修改执行计划。" },
  executing: { label: "正在执行", tone: "active", description: "Agent 正按已确认的计划执行并收集验收依据。" },
  replan_required: { label: "需要调整执行计划", tone: "warning", description: "任务发生实质变化，需要先更新计划再继续。" },
  awaiting_replan_decision: { label: "等待调整确认", tone: "waiting", description: "更新后的计划正在等待主控确认。" },
  needs_user_decision: { label: "等待你的决定", tone: "warning", description: "自动调整已到上限，需要用户明确下一步。" },
  blocked_control: { label: "等待主控处理", tone: "warning", description: "当前步骤遇到阻塞，正在等待主控处理。" },
  failed: { label: "任务执行失败", tone: "danger", description: "Agent 任务已经失败；已完成的步骤、条件和验收依据仍然保留。" },
  cancelled: { label: "任务已取消", tone: "warning", description: "Agent 任务已经取消；已完成的步骤、条件和验收依据仍然保留。" },
  interrupted: { label: "任务已中断", tone: "warning", description: "Agent 任务已经中断；已完成的步骤、条件和验收依据仍然保留。" },
  finalizing: { label: "任务已验收", tone: "success", description: "必做步骤、完成条件、验收依据和最终交付均已通过完成检查。" },
};

const STEP_STATUS = {
  pending: { label: "待执行", tone: "muted" },
  running: { label: "执行中", tone: "active" },
  completed: { label: "已完成", tone: "success" },
  blocked: { label: "受阻", tone: "danger" },
  failed: { label: "执行失败", tone: "danger" },
  cancelled: { label: "已取消", tone: "warning" },
  interrupted: { label: "已中断", tone: "warning" },
  superseded: { label: "已被新计划替代", tone: "muted" },
  skipped: { label: "已跳过", tone: "muted" },
};

const CRITERION_STATUS = {
  satisfied: { label: "已满足", tone: "success" },
  failed: { label: "未通过", tone: "danger" },
  blocked: { label: "受阻", tone: "danger" },
  pending: { label: "待验证", tone: "muted" },
};

const VERSION_STATUS = {
  pending: "等待确认",
  approved: "已确认",
  revise_requested: "需要修改",
  superseded: "已被新版本替代",
  cancelled: "已取消",
};

const EVIDENCE_TYPE_LABELS = {
  source: "源码依据",
  read_result: "文件核验",
  command: "命令结果",
  command_output: "命令结果",
  test: "测试结果",
  test_result: "测试结果",
  log: "日志依据",
  url: "网页依据",
  artifact: "交付产物",
};

const AGENT_MONITOR_EVENT_KINDS = new Set([
  "task_created", "task_started", "task_completed", "task_failed", "task_cancelled", "task_interrupted",
  "agent_control", "agent_supervision", "control_requested", "control_response", "steer_applied",
  "pause_applied", "resume_applied", "cancel_requested", "needs_openbear_control",
  "agent_plan_protocol_corrected", "agent_control_continuation_saved",
  "model_context_pre_compacted", "model_context_overflow_compacted", "model_context_compaction_failed",
]);
const ACTIVE_AGENT_STATUSES = new Set([
  "queued", "running", "resuming", "pausing", "stopping", "needs_openbear_control",
]);

const FINAL_CONTEXT_COMPACTION_KINDS = new Set([
  "model_context_pre_compacted",
  "model_context_overflow_compacted",
  "model_context_compaction_failed",
  "model_context_compacted",
  "model_context_compaction_completed",
]);

const CONTEXT_COMPACTION_SOURCE_LABELS = {
  agent_result_preflight: "Agent 结果回灌前",
  emergency: "上下文溢出应急压缩",
  pre_model_request: "模型请求前",
  pre_model: "模型请求前",
  preflight: "本轮执行前",
  tool_batch: "工具批次结束后",
  turn_epilogue: "本轮结束后",
  manual: "手动触发",
  pre_compacted: "达到阈值",
  overflow_compacted: "上下文溢出恢复",
  llm_summary: "模型摘要压缩",
  deterministic_fallback: "规则降级压缩",
  compression: "专用压缩模型",
  "primary-fallback": "主模型降级压缩",
  agent_context: "Agent 上下文压缩",
  legacy_summary: "历史摘要",
  context_compaction: "上下文压缩",
};

export function contextCompactionSourceLabel(value) {
  const source = text(value);
  return CONTEXT_COMPACTION_SOURCE_LABELS[source] || source || "上下文压缩";
}

const ACTIVITY_EXCLUDED_EVENT_KINDS = new Set([
  "agent_control", "agent_supervision", "control_requested", "control_response", "steer_applied",
  "pause_applied", "resume_applied", "cancel_requested", "needs_openbear_control",
  "plan_submitted", "plan_decision", "plan_decision_approve", "plan_decision_revise", "plan_decision_cancel",
  "plan_replan_requested", "agent_plan_protocol_corrected", "agent_control_continuation_saved",
]);

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function text(value, fallback = "") {
  const result = String(value ?? "").trim();
  return result || fallback;
}

function number(value) {
  const result = Number(value || 0);
  return Number.isFinite(result) ? result : 0;
}

function compactLabel(value, limit = 48) {
  const result = text(value);
  return result.length > limit ? `${result.slice(0, limit)}…` : result;
}

function evidenceTypeLabel(value) {
  const kind = text(value);
  return EVIDENCE_TYPE_LABELS[kind] || "验收依据";
}

export function planPhaseMeta(value) {
  const phase = text(value);
  return PHASE_META[phase] || { label: phase || "等待执行计划", tone: "muted", description: "当前任务尚未生成可展示的执行计划。" };
}

export function stepStatusMeta(value) {
  return STEP_STATUS[text(value, "pending")] || { label: text(value, "待执行"), tone: "muted" };
}

export function criterionStatusMeta(value) {
  return CRITERION_STATUS[text(value, "pending")] || { label: text(value, "待验证"), tone: "muted" };
}

export function versionStatusLabel(value) {
  return VERSION_STATUS[text(value)] || text(value, "未知状态");
}

export function versionTypeLabel(value) {
  return text(value) === "replan" ? "调整计划" : "初始计划";
}

export function initialAgentLaunch(snapshot = {}, fallbackArguments = "") {
  const task = object(snapshot?.task);
  const input = object(task.input);
  const agent = object(input.agentSnapshot || input.agent_snapshot);
  return {
    prompt: text(input.instruction || input.raw, text(fallbackArguments)),
    agentName: text(agent.name || agent.agentKey || agent.agent_key || task.current_agent_name || task.current_agent_key, "Agent"),
    model: text(agent.model || task.model, "—"),
    thinkLevel: text(agent.thinkLevel || agent.think_level, "—"),
    fastMode: Boolean(agent.fastMode ?? agent.fast_mode),
    tools: array(agent.toolAllowlist || agent.tool_allowlist).map((item) => text(item)).filter(Boolean),
    source: text(input.source),
  };
}

export function isAgentMonitorEventKind(kind) {
  const value = text(kind);
  return AGENT_MONITOR_EVENT_KINDS.has(value)
    || value.startsWith("plan_")
    || value.startsWith("control_")
    || value.startsWith("agent_supervision")
    || value.startsWith("model_context_compaction");
}

export function isActivityExcludedEventKind(kind) {
  const value = text(kind);
  if (FINAL_CONTEXT_COMPACTION_KINDS.has(value)) return false;
  return ACTIVITY_EXCLUDED_EVENT_KINDS.has(value)
    || value.startsWith("control_")
    || (value.startsWith("plan_") && !value.startsWith("plan_progress_"))
    || value.startsWith("agent_supervision")
    || value.startsWith("model_context_compaction")
    || value.startsWith("model_context_pre_compaction")
    || value.startsWith("model_context_overflow_compaction");
}

function firstCompactionField(records, keys) {
  for (const record of records) {
    for (const key of keys) {
      const value = object(record)[key];
      if (value !== undefined && value !== null && String(value).trim() !== "") return value;
    }
  }
  return "";
}

function compactionFactNumber(value) {
  const numeric = Number(value || 0);
  return Number.isFinite(numeric) && numeric > 0 ? numeric.toLocaleString() : "";
}

export function contextCompactionView(value = {}) {
  const event = object(value);
  const detail = object(event.detail);
  const payload = object(event.payload);
  const nested = object(detail.compaction || payload.compaction);
  const argumentsValue = firstCompactionField([event, detail, payload], ["arguments", "args"]);
  const args = parseJsonObject(argumentsValue);
  const records = [event, detail, payload, nested, args];
  const kind = text(event.kind || event.type);
  const name = text(event.name || event.toolName || detail.name || payload.name || payload.toolName);
  const compactionId = text(firstCompactionField(records, ["compactionId", "compaction_id"]));
  const summaryId = text(firstCompactionField(records, ["summaryId", "summary_id"]));
  const scope = text(firstCompactionField(records, ["scope"]));
  const source = text(firstCompactionField(records, ["source"]), scope === "agent" ? "agent_context" : "context_compaction");
  const sourceLabel = contextCompactionSourceLabel(source);
  const status = text(firstCompactionField(records, ["status"]));
  const beforeTokens = number(firstCompactionField(records, ["beforeTokens", "before_tokens", "estimatedTokensBefore", "estimated_tokens_before", "triggerTokens", "trigger_tokens"]));
  const afterTokens = number(firstCompactionField(records, ["afterTokens", "after_tokens", "estimatedTokensAfter", "estimated_tokens_after"]));
  const output = text(firstCompactionField(records, ["compactedOutput", "compacted_output"]));
  const outputPreview = text(firstCompactionField(records, ["outputPreview", "output_preview"]));
  const summaryChars = number(firstCompactionField(records, ["summaryChars", "summary_chars"])) || output.length;
  const summaryRef = text(firstCompactionField(records, ["summaryRef", "summary_ref"]));
  const reasonValue = firstCompactionField(records, ["reason", "error", "outputUnavailable", "output_unavailable"]);
  const reason = typeof reasonValue === "object" ? text(reasonValue?.message || reasonValue?.error) : text(reasonValue);
  const failed = kind === "model_context_compaction_failed" || status === "failed";
  const isCompaction = FINAL_CONTEXT_COMPACTION_KINDS.has(kind)
    || name === "ContextCompaction"
    || Boolean(compactionId && ["root", "agent"].includes(scope));
  const facts = [];
  if (sourceLabel) facts.push(sourceLabel);
  if (beforeTokens || afterTokens) facts.push(`${compactionFactNumber(beforeTokens) || "?"} → ${compactionFactNumber(afterTokens) || "?"} token`);
  if (summaryChars) facts.push(`摘要 ${compactionFactNumber(summaryChars)} 字`);
  const message = failed
    ? `上下文压缩失败${reason ? ` · ${reason}` : ""}`
    : `上下文压缩完成${facts.length ? ` · ${facts.join(" · ")}` : ""}`;
  const active = ["queued", "running", "pausing", "paused", "resuming"].includes(status);
  const cardFacts = [
    `来源：${sourceLabel}`,
    `压缩前上下文：${compactionFactNumber(beforeTokens) || "未记录"}${beforeTokens ? " token" : ""}`,
    `压缩后上下文：${compactionFactNumber(afterTokens) || (active ? "处理中" : "未记录")}${afterTokens ? " token" : ""}`,
  ];
  if (summaryChars) cardFacts.push(`摘要：${compactionFactNumber(summaryChars)} 字`);
  if (failed) cardFacts.push(`状态：压缩失败${reason ? `（${reason}）` : ""}`);
  else if (status === "unavailable") cardFacts.push("状态：没有可压缩内容");
  return {
    isCompaction,
    cardTitle: "上下文压缩",
    cardPreview: cardFacts.join(" · "),
    kind,
    compactionId,
    summaryId,
    scope,
    source,
    sourceLabel,
    status,
    beforeTokens,
    afterTokens,
    summaryChars,
    summaryRef,
    output,
    outputPreview,
    outputAvailable: Boolean(output || firstCompactionField(records, ["outputAvailable", "output_available"])),
    failed,
    reason,
    message,
    emptyOutputText: "旧记录未持久化压缩摘要",
    previewLabel: "摘要预览（非完整输出）",
  };
}

export function agentCompactionActivityView(event = {}) {
  const view = contextCompactionView(event);
  const kind = text(event?.kind || event?.type);
  if (!FINAL_CONTEXT_COMPACTION_KINDS.has(kind)) return {...view, isCompaction: false};
  return view;
}

export function isActiveAgentEvent(event = {}, state = {}) {
  const rows = array(state?.rows);
  if (rows.some((row) => ACTIVE_AGENT_STATUSES.has(text(row?.status)))) return true;
  return Boolean(event?.live && text(state?.summary?.cls) === "running");
}

function taskUuidFrom(value = {}) {
  const item = object(value);
  const task = object(item.task);
  return text(item.taskUuid || item.task_uuid || task.taskUuid || task.task_uuid);
}

export function agentTaskPushSnapshot(event = {}, expectedTaskUuid = "") {
  const operation = object(event?.operation);
  const payload = object(operation.payload);
  const expected = text(expectedTaskUuid || taskUuidFrom(payload));
  const collected = [];
  const append = (items, fallbackTaskUuid = "") => {
    for (const raw of array(items)) {
      const item = object(raw);
      const itemTaskUuid = taskUuidFrom(item) || text(fallbackTaskUuid);
      if (expected && itemTaskUuid && itemTaskUuid !== expected) continue;
      const seq = number(item.seq || item.sequence || item.eventSeq);
      if (!seq) continue;
      collected.push({...item, taskUuid: itemTaskUuid || expected, seq});
    }
  };
  const payloadTaskUuid = taskUuidFrom(payload) || expected;
  append(payload.recentEvents, payloadTaskUuid);
  append(object(payload.task).recentEvents, payloadTaskUuid);
  for (const result of array(payload.results)) {
    const resultTaskUuid = taskUuidFrom(result) || payloadTaskUuid;
    append(object(result).recentEvents, resultTaskUuid);
    append(object(result.result).recentEvents, resultTaskUuid);
    append(object(result.task).recentEvents, resultTaskUuid);
  }
  const bySeq = new Map();
  for (const item of collected) bySeq.set(number(item.seq), item);
  const events = [...bySeq.values()].sort((left, right) => number(left.seq) - number(right.seq));
  return {
    operationRevision: number(operation.revision),
    latestSeq: events.length ? number(events[events.length - 1].seq) : 0,
    events,
  };
}

export function mergeAgentEventLines(current = [], incoming = []) {
  const existing = array(current);
  const lastSeq = existing.reduce((latest, item) => Math.max(latest, number(item?.seq)), 0);
  const known = new Set(existing.map((item) => text(item?.key, `${number(item?.seq)}|${text(item?.kind)}`)));
  const novel = array(incoming).filter((item) => !known.has(text(item?.key, `${number(item?.seq)}|${text(item?.kind)}`)));
  const newer = novel.filter((item) => number(item?.seq) > lastSeq).sort((left, right) => number(left?.seq) - number(right?.seq));
  const gap = Boolean(lastSeq && newer.length && number(newer[0]?.seq) > lastSeq + 1);
  return {
    gap,
    novel,
    lines: gap ? existing : [...existing, ...novel].sort((left, right) => number(left?.seq) - number(right?.seq)),
    lastSeq,
    latestSeq: newer.length ? number(newer[newer.length - 1]?.seq) : lastSeq,
  };
}

function monitorStepId(item = {}) {
  const detail = object(item.detail);
  const direct = text(detail.stepId || detail.step_id);
  if (direct) return direct;
  const match = text(item.summary || item.message).match(/(?:start|update|complete)\s*:\s*([^\s]+)/i);
  return text(match?.[1]);
}

function monitorStepContext(item, planView) {
  const id = monitorStepId(item);
  const step = array(planView?.steps).find((candidate) => text(candidate.id) === id);
  return {
    id,
    step,
    label: step ? `步骤 ${step.index}` : (id ? `步骤 ${id}` : "执行步骤"),
    title: text(step?.title, id || "未命名步骤"),
  };
}

function monitorEventView(item = {}, planView = {}) {
  const kind = text(item.kind || item.type);
  const detail = object(item.detail);
  const summary = text(item.summary || item.message || item.currentStatus, "Agent 状态已更新");
  const base = {
    ...item,
    kind,
    category: "运行状态",
    title: summary,
    description: "",
    bullets: [],
    badges: [],
    tone: "neutral",
  };

  if (kind === "task_created") {
    return {...base, category: "任务", title: "任务已创建", description: summary, tone: "active"};
  }
  if (kind === "task_started") {
    const agent = object(detail.agent);
    return {
      ...base,
      category: "启动",
      title: `${text(agent.name || agent.agentKey, "Agent")} 开始执行`,
      description: "Agent 已接手任务，开始制定并执行结构化计划。",
      badges: [text(detail.modelLabel), text(detail.thinkLevel), ...array(agent.toolAllowlist)].filter(Boolean),
      tone: "active",
    };
  }
  if (kind === "plan_submitted") {
    const version = number(detail.planVersion);
    const planType = text(detail.planType) === "replan" ? "调整计划" : "初始计划";
    return {
      ...base,
      category: "计划",
      title: `${planType}${version ? ` v${version}` : ""} 已提交`,
      description: text(planView?.plan?.objective || planView?.plan?.title, "Agent 已提交结构化执行计划，等待主控审查。"),
      bullets: array(planView?.steps).map((step) => `步骤 ${step.index} · ${step.title}`),
      badges: [`${array(planView?.steps).length} 个步骤`],
      tone: "waiting",
    };
  }
  const planDecisionAction = text(
    detail.action
    || (kind.includes("approve") ? "approve" : "")
    || (kind.includes("revise") ? "revise" : "")
    || (kind.includes("cancel") ? "cancel" : ""),
  );
  if (kind.startsWith("plan_") && ["approve", "revise", "cancel"].includes(planDecisionAction)) {
    const action = planDecisionAction;
    const actionMeta = {
      approve: {title: "执行计划已批准", tone: "success"},
      revise: {title: "执行计划需要修改", tone: "warning"},
      cancel: {title: "执行计划已取消", tone: "danger"},
    }[action];
    return {
      ...base,
      category: "计划审查",
      title: actionMeta.title,
      description: text(detail.reason, summary),
      bullets: array(detail.requiredChanges || detail.issues).map((value) => text(value)).filter(Boolean),
      badges: [number(detail.planVersion) ? `计划 v${number(detail.planVersion)}` : "", text(detail.requestedBy)].filter(Boolean),
      tone: actionMeta.tone,
    };
  }
  if (kind === "plan_replan_requested") {
    return {...base, category: "计划调整", title: "Agent 请求调整执行计划", description: text(detail.reason, summary), tone: "warning"};
  }
  if (kind === "plan_progress_start") {
    const context = monitorStepContext(item, planView);
    return {
      ...base,
      category: context.label,
      title: `开始：${context.title}`,
      description: text(context.step?.objective || context.step?.method, "Agent 已开始执行该步骤。"),
      badges: [text(context.step?.statusMeta?.label, "执行中")],
      tone: "active",
    };
  }
  if (kind === "plan_progress_update") {
    const context = monitorStepContext(item, planView);
    const evidence = array(detail.evidence);
    return {
      ...base,
      category: context.label,
      title: `${context.title} · 更新进展`,
      description: evidence.length ? `本次记录了 ${evidence.length} 条新的验收依据。` : "Agent 更新了该步骤的执行状态。",
      bullets: evidence.map((value) => text(value?.summary)).filter(Boolean),
      badges: evidence.length ? [`新增依据 ${evidence.length}`] : [],
      tone: "active",
    };
  }
  if (kind === "plan_progress_complete") {
    const context = monitorStepContext(item, planView);
    return {
      ...base,
      category: context.label,
      title: `完成：${context.title}`,
      description: text(context.step?.result || context.step?.objective, "该步骤已完成并通过完成条件检查。"),
      badges: context.step ? [`${context.step.criteria.filter((criterion) => criterion.satisfied).length}/${context.step.criteria.length} 条件`] : [],
      tone: "success",
    };
  }
  if (kind === "plan_progress_finalize") {
    return {
      ...base,
      category: "计划验收",
      title: "执行计划已通过最终验收",
      description: text(planView?.phaseMeta?.description, "所有必做步骤、完成条件和验收依据均已检查。"),
      badges: [number(detail.planVersion) ? `计划 v${number(detail.planVersion)}` : ""].filter(Boolean),
      tone: "success",
    };
  }
  if (kind === "control_requested" || kind === "agent_control") {
    const action = text(detail.action || item.controlAction, "message");
    const actionTitle = {
      steer: "主控发出追加指导",
      stop: "主控请求停止任务",
      pause: "主控请求暂停任务",
      resume: "主控请求恢复任务",
      status: "主控查询任务状态",
    }[action] || "主控发出控制请求";
    return {
      ...base,
      category: "主控干预",
      title: actionTitle,
      description: text(detail.message || detail.text || item.message, summary),
      badges: [text(detail.requestedBy), action].filter(Boolean),
      tone: action === "stop" ? "danger" : "warning",
    };
  }
  if (kind === "steer_applied") {
    const stageLabels = {before_tool_call: "工具调用前生效", before_model_call: "模型调用前生效"};
    return {
      ...base,
      category: "指导生效",
      title: "Agent 已接收追加指导",
      description: text(detail.message, summary),
      badges: [stageLabels[text(detail.stage)] || text(detail.stage)].filter(Boolean),
      tone: "success",
    };
  }
  if (kind === "task_completed") {
    return {
      ...base,
      category: "任务结束",
      title: "Agent 任务已完成",
      description: "计划执行、验收和最终交付均已完成。",
      bullets: array(planView?.finalOutputs).map((output) => text(output.summary || output.title)).filter(Boolean),
      tone: "success",
    };
  }
  if (["task_failed", "task_cancelled", "task_interrupted"].includes(kind)) {
    const title = {task_failed: "Agent 任务执行失败", task_cancelled: "Agent 任务已取消", task_interrupted: "Agent 任务已中断"}[kind];
    return {...base, category: "任务结束", title, description: summary, tone: kind === "task_failed" ? "danger" : "warning"};
  }
  if (kind.startsWith("model_context_")) {
    return {...base, category: "上下文维护", title: summary, description: text(detail.reason || detail.message), tone: kind.endsWith("failed") ? "danger" : "warning"};
  }
  return {...base, description: text(detail.message || detail.reason)};
}

export function buildAgentMonitorTimeline(events = [], snapshot = {}) {
  const planView = buildAgentPlanView(snapshot);
  return array(events)
    .filter((item) => isAgentMonitorEventKind(item?.kind || item?.type))
    .map((item) => monitorEventView(item, planView));
}

function progressEventStepId(item = {}) {
  const detail = object(item.detail);
  const explicit = text(detail.stepId || detail.step_id);
  if (explicit) return explicit;
  const match = text(item.summary).match(/^Plan\s+(?:start|update|complete|block):\s*(.+)$/i);
  return text(match?.[1]);
}

function eventTimeMs(value) {
  const timestamp = number(value);
  if (!timestamp) return 0;
  return timestamp * (timestamp < 10_000_000_000 ? 1000 : 1);
}

export function agentStepActivityLines(events = [], planVersion = 0, stepId = "", interval = {}) {
  const targetVersion = number(planVersion);
  const targetStepId = text(stepId);
  if (!targetStepId) return [];
  const selected = [];
  let activeStep = null;
  let targetBoundarySeen = false;
  const ordered = [...array(events)].sort((left, right) => number(left?.seq) - number(right?.seq));
  for (const item of ordered) {
    const kind = text(item?.kind || item?.type);
    if (kind === "plan_progress_start") {
      const startedStepId = progressEventStepId(item);
      activeStep = startedStepId ? {
        stepId: startedStepId,
        planVersion: number(item?.detail?.planVersion || item?.detail?.plan_version) || targetVersion,
      } : null;
      if (activeStep?.stepId === targetStepId && (!targetVersion || activeStep.planVersion === targetVersion)) {
        targetBoundarySeen = true;
      }
    }
    if (activeStep && activeStep.stepId === targetStepId && (!targetVersion || activeStep.planVersion === targetVersion)) {
      selected.push(item);
    }
    if (["plan_progress_complete", "plan_progress_block"].includes(kind)) {
      const finishedStepId = progressEventStepId(item);
      if (activeStep && (!finishedStepId || finishedStepId === activeStep.stepId)) activeStep = null;
    }
  }
  if (targetBoundarySeen) return selected;
  const startedAt = eventTimeMs(interval?.startedAt);
  const completedAt = eventTimeMs(interval?.completedAt);
  if (!startedAt) return [];
  return ordered.filter((item) => {
    const timestamp = eventTimeMs(item?.ts);
    return timestamp >= startedAt && (!completedAt || timestamp <= completedAt);
  });
}

function parseJsonObject(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) return value;
  try {
    const parsed = JSON.parse(String(value || ""));
    return object(parsed);
  } catch {
    return {};
  }
}

function compactDuration(value) {
  const ms = number(value);
  if (!ms) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 10) return `${seconds.toFixed(1).replace(/\.0$/, "")}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return remainder ? `${minutes}m${remainder}s` : `${minutes}m`;
}

function compactTps(value) {
  const tps = number(value);
  return tps ? `${tps.toFixed(1)} t/s` : "";
}

function retryPosition(item = {}) {
  const detail = object(item.detail);
  const retry = object(detail.retry);
  let attempt = number(retry.attempt || detail.attempt);
  let maximum = number(retry.maxRetries || retry.retryMax || detail.retryMax);
  if (!attempt || !maximum) {
    const match = text(item.summary || item.message).match(/(\d+)\s*\/\s*(\d+)/);
    attempt ||= number(match?.[1]);
    maximum ||= number(match?.[2]);
  }
  return {attempt, maximum};
}

function planProgressInfo(item = {}, argumentsValue = "") {
  const detail = object(item.detail);
  const args = parseJsonObject(argumentsValue || detail.arguments);
  const kind = text(item.kind || item.type);
  const action = text(args.action || (kind.startsWith("plan_progress_") ? kind.slice(14) : ""));
  const summaryMatch = text(item.summary).match(/^Plan\s+(?:start|update|complete|block):\s*(.+)$/i);
  const stepId = text(args.stepId || args.step_id || detail.stepId || detail.step_id || summaryMatch?.[1]);
  const labels = {
    start: "开始步骤",
    update: "更新步骤进展",
    complete: "完成步骤",
    block: "标记步骤受阻",
    finalize: "提交最终交付",
  };
  return {action, stepId, label: labels[action] || "更新执行计划"};
}

function explicitToolFailure(item = {}) {
  const detail = object(item.detail);
  const preview = text(detail.resultPreview || detail.result_preview);
  const parsed = parseJsonObject(preview);
  if (Object.prototype.hasOwnProperty.call(parsed, "ok")) return parsed.ok === false;
  return /"ok"\s*:\s*false/i.test(preview) || /^status:\s*(?:error|failed)\b/im.test(preview);
}

function compactLine(base, message, tone, kind = "activity_compact") {
  return {...base, kind, message, tone};
}

function toolProcessLine(item, name, processStatus) {
  const detail = object(item?.detail);
  return {
    ...item,
    toolName: text(item?.toolName || detail.name, name || "Tool"),
    rawArguments: item?.rawArguments ?? detail.arguments ?? "",
    toolDescription: text(item?.toolDescription || item?.description || detail.description),
    processStatus,
  };
}

function modelProcessLine(item, label, description, modelStatus, modelStatusText, options = {}) {
  const detail = object(item?.detail);
  const modelThinkLevel = text(
    detail.thinkLevel || detail.think_level,
    text(item?.modelThinkLevel, text(options.thinkLevel)),
  );
  let modelFastMode = Boolean(options.fastMode);
  if (Object.prototype.hasOwnProperty.call(item || {}, "modelFastMode")) {
    modelFastMode = Boolean(item.modelFastMode);
  }
  if (Object.prototype.hasOwnProperty.call(detail, "fastMode")) {
    modelFastMode = Boolean(detail.fastMode);
  } else if (Object.prototype.hasOwnProperty.call(detail, "fast_mode")) {
    modelFastMode = Boolean(detail.fast_mode);
  }
  return {
    ...item,
    processType: "model",
    modelLabel: text(label, "模型"),
    modelThinkLevel,
    modelFastMode,
    modelDescription: text(description),
    modelStatus,
    modelStatusText: text(modelStatusText),
  };
}

export function compactAgentStepActivityLines(events = [], options = {}) {
  const output = [];
  const pendingTools = [];
  let modelState = null;
  let recentPlanIndex = -1;
  let recentPlanInfo = null;
  const fallbackModelLabel = text(options.modelLabel, "模型");

  const updateModel = (item, presentation, tone, {finish = false} = {}) => {
    const detail = object(item.detail);
    const label = modelState?.label || text(detail.modelLabel || detail.model, fallbackModelLabel);
    const view = presentation(label);
    const presentedLabel = text(view.label, label);
    const base = modelState
      ? {...output[modelState.index], ts: item.ts, timeLabel: item.timeLabel, detail: item.detail}
      : item;
    const line = compactLine(
      modelProcessLine(base, presentedLabel, view.description, view.status, view.statusText, options),
      view.message,
      tone,
      "model_call_compact",
    );
    if (!modelState) {
      const index = output.length;
      output.push(line);
      modelState = {index, label: presentedLabel, attempt: 0, maximum: 0, streamChars: 0};
    } else {
      output[modelState.index] = line;
    }
    if (finish) modelState = null;
  };

  for (const item of [...array(events)].sort((left, right) => number(left?.seq) - number(right?.seq))) {
    const kind = text(item?.kind || item?.type);
    const detail = object(item?.detail);
    const compaction = agentCompactionActivityView(item);

    if (compaction.isCompaction) {
      output.push(compactLine(
        {...item, compaction, compactedOutput: compaction.output, emptyOutputText: compaction.emptyOutputText},
        compaction.message,
        compaction.failed ? "danger" : "success",
        "context_compaction_compact",
      ));
      continue;
    }

    if (kind === "model_call_started") {
      const retry = retryPosition(item);
      if (!modelState || !retry.attempt) {
        const label = text(detail.modelLabel || detail.model, fallbackModelLabel);
        const index = output.length;
        output.push(compactLine(
          modelProcessLine(item, label, "", "running", "调用中", options),
          `模型调用：${label} · 调用中`,
          "active",
          "model_call_compact",
        ));
        modelState = {index, label, attempt: retry.attempt, maximum: retry.maximum, streamChars: 0, retrySummary: ""};
      } else {
        modelState.attempt = retry.attempt;
        modelState.maximum = retry.maximum;
        const description = `重试 ${retry.attempt}/${retry.maximum}${modelState.retrySummary ? ` · ${modelState.retrySummary}` : ""}`;
        const statusText = "正在重试 ×";
        output[modelState.index] = compactLine(
          modelProcessLine(
            {...output[modelState.index], ts: item.ts, timeLabel: item.timeLabel, detail},
            modelState.label,
            description,
            "failed",
            statusText,
            options,
          ),
          `模型调用：${modelState.label} · 正在重试 ${retry.attempt}/${retry.maximum}${modelState.retrySummary ? ` · ${modelState.retrySummary}` : ""} ×`,
          "danger",
          "model_call_compact",
        );
      }
      continue;
    }

    if (["model_call_retry", "model_call_retry_wait", "model_call_retry_resumed"].includes(kind)) {
      const retry = retryPosition(item);
      const retrySummary = text(detail.retry?.summary || detail.summary, "");
      updateModel(
        item,
        (label) => {
          const description = `重试 ${retry.attempt || 1}/${retry.maximum || "?"}${retrySummary ? ` · ${retrySummary}` : ""}`;
          return {
            message: `模型调用：${label} · 正在重试 ${retry.attempt || 1}/${retry.maximum || "?"}${retrySummary ? ` · ${retrySummary}` : ""} ×`,
            description,
            status: "failed",
            statusText: "正在重试 ×",
          };
        },
        "danger",
      );
      if (modelState) {
        modelState.attempt = retry.attempt || modelState.attempt;
        modelState.maximum = retry.maximum || modelState.maximum;
        if (retrySummary) modelState.retrySummary = retrySummary;
      }
      continue;
    }

    if (kind === "model_stream_progress") {
      const chars = number(detail.textChars || detail.text_chars);
      updateModel(item, (label) => {
        const retry = modelState?.attempt && modelState?.maximum ? `重试 ${modelState.attempt}/${modelState.maximum}` : "";
        const statusText = `流式输出中${chars ? ` · ${chars} 字` : ""}`;
        return {
          message: `模型调用：${label}${retry ? ` · ${retry}` : ""} · ${statusText}`,
          description: retry,
          status: "running",
          statusText,
        };
      }, "active");
      if (modelState) modelState.streamChars = chars;
      continue;
    }

    if (kind === "model_call_finished") {
      const duration = compactDuration(detail.durationMs || detail.duration_ms);
      const tps = compactTps(detail.tps);
      updateModel(
        item,
        (label) => {
          const presentedLabel = text(detail.modelLabel || detail.model, label);
          const description = [duration, tps].filter(Boolean).join(" · ");
          return {
            label: presentedLabel,
            message: `模型调用：${presentedLabel}${description ? ` · ${description}` : ""} √`,
            description,
            status: "success",
            statusText: "执行完成 √",
          };
        },
        "success",
        {finish: true},
      );
      continue;
    }

    if (["model_call_retry_cancelled", "model_call_failed"].includes(kind)) {
      const retry = retryPosition(item);
      updateModel(
        item,
        (label) => {
          const description = retry.attempt && retry.maximum ? `重试 ${retry.attempt}/${retry.maximum}` : "";
          const statusText = `${kind === "model_call_retry_cancelled" ? "重试已取消" : "调用失败"} ×`;
          return {
            message: `模型调用：${label}${description ? ` · ${description}` : ""} · ${statusText}`,
            description,
            status: "failed",
            statusText,
          };
        },
        "danger",
        {finish: true},
      );
      continue;
    }

    if (kind === "tool_call_started") {
      const name = text(item.toolName || detail.name, "Tool");
      const round = number(detail.round);
      const isPlanProgress = name === "AgentPlanProgress";
      const info = isPlanProgress ? planProgressInfo(item, item.rawArguments || detail.arguments) : null;
      const message = isPlanProgress
        ? `${info.label}${info.stepId ? ` · ${info.stepId}` : ""} · 执行中`
        : `调用工具 ${name} · 执行中`;
      const index = output.length;
      output.push(compactLine(toolProcessLine(item, name, "running"), message, "active", "tool_call_started"));
      pendingTools.push({index, name, round, isPlanProgress, info, accepted: false});
      if (isPlanProgress) recentPlanIndex = index;
      continue;
    }

    if (kind.startsWith("plan_progress_")) {
      const info = planProgressInfo(item);
      const pending = [...pendingTools].reverse().find((entry) => entry.isPlanProgress);
      const index = pending?.index ?? output.length;
      const base = pending
        ? {...output[index], ts: item.ts, timeLabel: item.timeLabel, detail: item.detail, processStatus: "success"}
        : item;
      output[index] = compactLine(base, `${info.label}${info.stepId ? ` · ${info.stepId}` : ""} √`, "success", pending ? "tool_call_started" : "plan_progress_compact");
      if (pending) {
        pending.info = info;
        pending.accepted = true;
      }
      recentPlanIndex = index;
      recentPlanInfo = info;
      continue;
    }

    if (kind === "tool_call_finished") {
      const name = text(item.toolName || detail.name, "Tool");
      const round = number(detail.round);
      let pendingIndex = pendingTools.findIndex((entry) => entry.name === name && (!round || !entry.round || entry.round === round));
      const pending = pendingIndex >= 0 ? pendingTools[pendingIndex] : null;
      if (pendingIndex >= 0) pendingTools.splice(pendingIndex, 1);
      const failed = explicitToolFailure(item);
      if (pending?.isPlanProgress || (!pending && name === "AgentPlanProgress" && recentPlanIndex >= 0)) {
        const index = pending?.index ?? recentPlanIndex;
        const info = pending?.info || recentPlanInfo || planProgressInfo(item, output[index]?.rawArguments || output[index]?.detail?.arguments);
        const accepted = pending?.accepted || output[index]?.kind === "plan_progress_compact";
        const success = accepted || !failed;
        output[index] = compactLine(
          {
            ...output[index],
            ts: item.ts,
            timeLabel: item.timeLabel,
            detail: {...object(output[index]?.detail), ...detail},
            processStatus: success ? "success" : "failed",
          },
          `${info.label}${info.stepId ? ` · ${info.stepId}` : ""}${success ? " √" : " · 失败 ×"}`,
          success ? "success" : "danger",
          output[index]?.kind || "plan_progress_compact",
        );
        continue;
      }
      const index = pending?.index ?? output.length;
      const base = pending
        ? {...output[index], ts: item.ts, timeLabel: item.timeLabel, detail: {...object(output[index]?.detail), ...detail}}
        : item;
      output[index] = compactLine(
        toolProcessLine(base, name, failed ? "failed" : "success"),
        `调用工具 ${name}${failed ? " · 返回失败 ×" : " √"}`,
        failed ? "danger" : "success",
        pending ? "tool_call_started" : "tool_call_compact",
      );
      continue;
    }

    if (["tool_call_failed", "tool_call_denied"].includes(kind)) {
      const name = text(item.toolName || detail.name, "Tool");
      const round = number(detail.round);
      const pendingIndex = pendingTools.findIndex((entry) => entry.name === name && (!round || !entry.round || entry.round === round));
      const pending = pendingIndex >= 0 ? pendingTools.splice(pendingIndex, 1)[0] : null;
      const index = pending?.index ?? output.length;
      const denied = kind === "tool_call_denied";
      const base = pending
        ? {...output[index], ts: item.ts, timeLabel: item.timeLabel, detail: {...object(output[index]?.detail), ...detail}}
        : item;
      output[index] = compactLine(
        toolProcessLine(base, name, denied ? "denied" : "failed"),
        `调用工具 ${name} · ${denied ? "已拒绝" : "执行失败"} ×`,
        "danger",
        pending ? "tool_call_started" : "tool_call_compact",
      );
      continue;
    }

    output.push(item);
  }

  return output;
}

function evidenceView(item = {}) {
  const metadata = object(item.metadata);
  const type = text(item.evidence_type || item.evidenceType);
  return {
    ...item,
    uuid: text(item.evidence_uuid || item.evidenceUuid),
    type,
    typeLabel: evidenceTypeLabel(type),
    reference: text(item.reference),
    summary: text(item.summary, "已记录验收依据"),
    excerpt: text(metadata.excerpt),
    path: text(metadata.path),
    line: number(metadata.line),
    url: text(metadata.url),
    metadata,
  };
}

function criterionView(criterion = {}, state = {}, evidenceByUuid = new Map()) {
  const status = text(state.status, "pending");
  const evidenceIds = array(state.evidence).map((item) => text(item)).filter(Boolean);
  return {
    ...criterion,
    id: text(criterion.id),
    description: text(criterion.description, "未命名完成条件"),
    required: criterion.required !== false,
    status,
    statusMeta: criterionStatusMeta(status),
    satisfied: status === "satisfied",
    note: text(state.note),
    evidenceIds,
    evidence: evidenceIds.map((id) => evidenceByUuid.get(id)).filter(Boolean),
  };
}

function sourceView(source, evidenceByUuid = new Map(), stepById = new Map()) {
  const raw = text(source);
  const evidenceUuid = raw.startsWith("evidence:") ? raw.slice(9) : (evidenceByUuid.has(raw) ? raw : "");
  if (evidenceUuid) {
    const evidence = evidenceByUuid.get(evidenceUuid);
    if (evidence) {
      return {
        raw,
        kind: "evidence",
        uuid: evidence.uuid,
        typeLabel: evidence.typeLabel,
        summary: evidence.summary,
        label: `${evidence.typeLabel}：${compactLabel(evidence.summary)}`,
      };
    }
    return {
      raw,
      kind: "missing-evidence",
      uuid: evidenceUuid,
      typeLabel: "验收依据",
      summary: "对应的验收依据记录暂时不可用",
      label: "验收依据暂不可用",
    };
  }
  if (raw.startsWith("step:")) {
    const stepId = raw.slice(5);
    const step = stepById.get(stepId);
    const title = text(step?.title, stepId);
    return {
      raw,
      kind: "step",
      uuid: "",
      typeLabel: "执行步骤",
      summary: text(step?.result, `由步骤“${title}”提供支持`),
      label: `执行步骤：${compactLabel(title)}`,
    };
  }
  return { raw, kind: "other", uuid: "", typeLabel: "来源", summary: raw, label: compactLabel(raw) };
}

export function preferredAgentPlanVersion(snapshot = {}) {
  const state = object(snapshot.state);
  const versions = array(snapshot.versions);
  const knownVersions = new Set(versions.map((item) => number(item?.version)).filter(Boolean));
  const candidates = [
    number(state.pending_plan_version),
    number(state.active_plan_version),
    number(snapshot?.current?.version),
    ...versions.map((item) => number(item?.version)).filter(Boolean).reverse(),
  ];
  return candidates.find((version) => version && (!knownVersions.size || knownVersions.has(version))) || 0;
}

const TERMINAL_TASK_STATUSES = new Set(["failed", "cancelled", "interrupted"]);

function taskStopReason(snapshot = {}) {
  const task = object(snapshot.task);
  const state = object(snapshot.state);
  return text(task.stopReason || task.stop_reason || task.cancelReason || task.cancel_reason || state.stopReason || state.stop_reason);
}

function terminalTaskPhaseMeta(status, stopReason = "") {
  if (status === "cancelled" && stopReason) {
    return {
      label: "任务已停止",
      tone: "warning",
      description: "停止请求已持久化为 cancelled；已完成的步骤、条件和验收依据仍然保留。",
    };
  }
  return planPhaseMeta(status);
}

export function buildAgentPlanView(snapshot = {}, options = {}) {
  const state = object(snapshot.state);
  const versions = array(snapshot.versions);
  const apiCurrent = object(snapshot.current);
  const preferredVersion = preferredAgentPlanVersion(snapshot);
  const requestedVersion = number(options.planVersion);
  const versionByNumber = new Map(versions.map((item) => [number(item?.version), object(item)]));
  const requestedExists = requestedVersion > 0
    && (versionByNumber.has(requestedVersion) || number(apiCurrent.version) === requestedVersion);
  const currentVersion = requestedExists ? requestedVersion : preferredVersion;
  const versionRecord = object(versionByNumber.get(currentVersion));
  const current = number(apiCurrent.version) === currentVersion
    ? {...versionRecord, ...apiCurrent}
    : versionRecord;
  const plan = object(current.plan);
  const taskStatus = text(snapshot?.task?.status);
  const stopReason = taskStopReason(snapshot);
  const taskTerminal = TERMINAL_TASK_STATUSES.has(taskStatus);
  const hasPlan = Boolean(Object.keys(plan).length) || versions.some((item) => Boolean(item?.plan));
  const activeVersion = number(state.active_plan_version);
  const pendingVersion = number(state.pending_plan_version);
  const isActiveVersion = currentVersion > 0 && currentVersion === activeVersion;
  const isPendingVersion = currentVersion > 0 && currentVersion === pendingVersion;
  const isHistoricalVersion = currentVersion > 0 && !isActiveVersion && !isPendingVersion;
  let phaseMeta = planPhaseMeta(state.phase);
  if (isHistoricalVersion) {
    phaseMeta = {
      label: `历史计划 v${currentVersion}`,
      tone: "muted",
      description: "正在查看历史版本；执行状态仍以标记为“当前执行”的已批准计划为准。",
    };
  } else if (!hasPlan && ["completed", "failed", "cancelled", "interrupted"].includes(taskStatus)) {
    phaseMeta = {
      label: "历史任务无结构化计划",
      tone: "muted",
      description: "该 Agent 任务已经结束，但创建时没有提交可展示的 Plan；启动信息、过程记录和结果仍然保留。",
    };
  } else if (taskTerminal) {
    phaseMeta = terminalTaskPhaseMeta(taskStatus, stopReason);
  }
  const runByStep = new Map(
    array(snapshot.steps)
      .filter((item) => number(item.plan_version) === currentVersion)
      .map((item) => [text(item.step_id), object(item)]),
  );
  const evidence = array(snapshot.evidence)
    .filter((item) => {
      const evidenceVersion = number(item?.plan_version || item?.planVersion);
      return !evidenceVersion || evidenceVersion === currentVersion;
    })
    .map(evidenceView);
  const evidenceByUuid = new Map(evidence.map((item) => [item.uuid, item]));
  const currentStepId = text(state.current_step_id);
  const steps = array(plan.steps).map((step, index) => {
    const run = runByStep.get(text(step.id)) || {};
    const criteriaState = object(run.criteria_state);
    const criteria = array(step.criteria).map((criterion) => criterionView(criterion, object(criteriaState[criterion.id]), evidenceByUuid));
    const persistedStatus = text(run.status, "pending");
    const status = taskTerminal && persistedStatus === "running" ? taskStatus : persistedStatus;
    return {
      ...step,
      id: text(step.id, `step-${index + 1}`),
      index: index + 1,
      title: text(step.title, `步骤 ${index + 1}`),
      objective: text(step.objective),
      method: text(step.method),
      required: step.required !== false,
      status,
      statusMeta: stepStatusMeta(status),
      current: !taskTerminal && isActiveVersion && text(step.id) === currentStepId,
      result: text(run.result),
      blocker: object(run.blocker),
      startedAt: number(run.started_at),
      completedAt: number(run.completed_at),
      criteria,
    };
  });

  const requiredSteps = steps.filter((step) => step.required);
  const requiredCriteria = requiredSteps.flatMap((step) => step.criteria.filter((criterion) => criterion.required));
  const stepById = new Map(steps.map((step) => [step.id, step]));
  const finalState = isActiveVersion ? object(state.final_outputs_state) : {};
  const finalOutputs = array(plan.finalOutputs).map((item, index) => {
    const id = text(item.id, `output-${index + 1}`);
    const runtime = object(finalState[id]);
    const sources = array(runtime.sources).map((source) => sourceView(source, evidenceByUuid, stepById));
    return {
      ...item,
      id,
      title: text(item.title, `最终交付 ${index + 1}`),
      description: text(item.description),
      supportedBy: array(item.supportedBy).map((source) => text(source)).filter(Boolean),
      summary: text(runtime.summary),
      sources,
      completed: Boolean(runtime.summary && sources.length),
    };
  });

  const approved = currentVersion > 0 && (["approved", "superseded"].includes(text(current.status)) || isActiveVersion);
  const allStepsDone = requiredSteps.length > 0 && requiredSteps.every((step) => step.status === "completed");
  const allCriteriaSatisfied = requiredCriteria.length > 0 && requiredCriteria.every((criterion) => criterion.satisfied);
  const allCriteriaHaveEvidence = requiredCriteria.length > 0 && requiredCriteria.every((criterion) => criterion.evidence.length > 0);
  const allOutputsSupported = finalOutputs.length > 0 && finalOutputs.every((item) => item.completed);
  const completionChecks = [
    { key: "approved", label: "执行计划已经确认", detail: approved ? `计划 v${currentVersion} 已获确认` : "计划仍在等待确认", done: approved },
    { key: "steps", label: "所有必做步骤已经完成", detail: `${requiredSteps.filter((step) => step.status === "completed").length} / ${requiredSteps.length} 个必做步骤`, done: allStepsDone },
    { key: "criteria", label: "所有完成条件已经满足", detail: `${requiredCriteria.filter((criterion) => criterion.satisfied).length} / ${requiredCriteria.length} 个完成条件`, done: allCriteriaSatisfied },
    { key: "evidence", label: "完成条件都有验收依据", detail: `${requiredCriteria.filter((criterion) => criterion.evidence.length > 0).length} / ${requiredCriteria.length} 个条件已绑定依据`, done: allCriteriaHaveEvidence },
    { key: "outputs", label: "最终交付已有来源支持", detail: `${finalOutputs.filter((item) => item.completed).length} / ${finalOutputs.length} 项最终交付`, done: allOutputsSupported },
  ];

  const defaultStep = steps.find((step) => step.current)
    || steps.find((step) => step.status === "running")
    || steps.find((step) => step.status === "blocked")
    || [...requiredSteps].reverse().find((step) => step.status === "completed")
    || steps[0]
    || null;

  const decisions = array(snapshot.decisions);
  const versionDecisions = decisions.filter((decision) => number(decision?.expected_version || decision?.expectedVersion) === currentVersion);

  return {
    phase: text(state.phase),
    phaseMeta,
    taskStatus,
    taskTerminal,
    stopReason,
    hasPlan,
    state,
    current,
    plan,
    currentVersion,
    preferredVersion,
    activeVersion,
    pendingVersion,
    isActiveVersion,
    isPendingVersion,
    isHistoricalVersion,
    versions: versions.map((version) => ({
      ...version,
      statusLabel: versionStatusLabel(version.status),
      typeLabel: versionTypeLabel(version.plan_type),
      selected: number(version.version) === currentVersion,
      active: number(version.version) === activeVersion,
      pending: number(version.version) === pendingVersion,
    })),
    decisions,
    versionDecisions,
    evidence,
    evidenceByUuid,
    steps,
    defaultStepId: text(defaultStep?.id),
    finalOutputs,
    completionChecks,
    counts: {
      requiredSteps: requiredSteps.length,
      completedSteps: requiredSteps.filter((step) => step.status === "completed").length,
      requiredCriteria: requiredCriteria.length,
      satisfiedCriteria: requiredCriteria.filter((criterion) => criterion.satisfied).length,
      evidence: evidence.length,
      finalOutputs: finalOutputs.length,
      completedOutputs: finalOutputs.filter((item) => item.completed).length,
    },
  };
}
