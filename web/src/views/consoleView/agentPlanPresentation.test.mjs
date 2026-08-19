import test from "node:test";
import assert from "node:assert/strict";

import {
  agentStepActivityLines,
  agentTaskPushSnapshot,
  buildAgentMonitorTimeline,
  buildAgentPlanView,
  compactAgentStepActivityLines,
  agentCompactionActivityView,
  initialAgentLaunch,
  isActiveAgentEvent,
  isActivityExcludedEventKind,
  mergeAgentEventLines,
  isAgentMonitorEventKind,
  planPhaseMeta,
} from "./agentPlanPresentation.js";

test("Agent WS push cursor ignores supervision displaySeq from the projected payload", () => {
  const event = {
    operation: {
      revision: 182,
      payload: {
        taskUuid: "task-1",
        recentEvents: [
          {taskUuid: "task-1", seq: 185, kind: "model_call_started"},
          {taskUuid: "task-1", seq: 186, kind: "model_stream_progress"},
        ],
      },
    },
    livePayload: {
      recentEvents: [
        {taskUuid: "task-1", seq: 620, kind: "agent_supervision"},
      ],
    },
  };
  const pushed = agentTaskPushSnapshot(event, "task-1");
  assert.equal(pushed.operationRevision, 182);
  assert.equal(pushed.latestSeq, 186);
  assert.deepEqual(pushed.events.map((item) => item.seq), [185, 186]);
});

test("Agent WS event merge detects a real task-sequence gap without using a timer", () => {
  const current = [{key: "task-1|185|model_call_started", seq: 185, kind: "model_call_started"}];
  const contiguous = mergeAgentEventLines(current, [
    {key: "task-1|186|model_stream_progress", seq: 186, kind: "model_stream_progress"},
  ]);
  assert.equal(contiguous.gap, false);
  assert.deepEqual(contiguous.lines.map((item) => item.seq), [185, 186]);

  const gap = mergeAgentEventLines(current, [
    {key: "task-1|190|model_call_started", seq: 190, kind: "model_call_started"},
  ]);
  assert.equal(gap.gap, true);
  assert.deepEqual(gap.lines.map((item) => item.seq), [185]);
});

test("step activity projection follows plan start through complete or block and isolates plan versions", () => {
  const events = [
    {seq: 1, kind: "model_call_finished", message: "计划前模型调用"},
    {seq: 2, kind: "plan_progress_start", summary: "Plan start: step-1", detail: {planVersion: 1}},
    {seq: 3, kind: "model_call_started", message: "步骤一模型调用"},
    {seq: 4, kind: "tool_call_started", message: "调用工具 Read"},
    {seq: 5, kind: "plan_progress_complete", summary: "Plan complete: step-1"},
    {seq: 6, kind: "model_call_started", message: "步骤间模型调用"},
    {seq: 7, kind: "plan_progress_start", summary: "Plan start: step-2", detail: {planVersion: 1}},
    {seq: 8, kind: "tool_call_started", message: "调用工具 Bash"},
    {seq: 9, kind: "plan_progress_block", summary: "Plan block: step-2"},
    {seq: 10, kind: "plan_progress_start", summary: "Plan start: step-1", detail: {planVersion: 2}},
    {seq: 11, kind: "model_call_started", message: "新版步骤一模型调用"},
  ];

  assert.deepEqual(agentStepActivityLines(events, 1, "step-1").map((item) => item.seq), [2, 3, 4, 5]);
  assert.deepEqual(agentStepActivityLines(events, 1, "step-2").map((item) => item.seq), [7, 8, 9]);
  assert.deepEqual(agentStepActivityLines(events, 2, "step-1").map((item) => item.seq), [10, 11]);
});

test("step activity falls back to persisted step times when the recent page starts after the step boundary", () => {
  const recentPage = [
    {seq: 201, ts: 1_720_000_020, kind: "tool_call_started"},
    {seq: 202, ts: 1_720_000_021, kind: "tool_call_finished"},
    {seq: 203, ts: 1_720_000_022, kind: "model_call_started"},
    {seq: 204, ts: 1_720_000_040, kind: "plan_progress_complete", summary: "Plan complete: step-3"},
    {seq: 205, ts: 1_720_000_041, kind: "model_call_started"},
  ];
  const lines = agentStepActivityLines(recentPage, 1, "step-3", {
    startedAt: 1_720_000_000,
    completedAt: 1_720_000_040,
  });
  assert.deepEqual(lines.map((item) => item.seq), [201, 202, 203, 204]);
});

test("step activity compacts model streaming and tool lifecycle into logical rows", () => {
  const events = [
    {key: "m1", seq: 1, kind: "model_call_started", timeLabel: "11:37:13", detail: {modelLabel: "OpenAI/gpt-5.6-sol", thinkLevel: "xhigh"}},
    {key: "m2", seq: 2, kind: "model_stream_progress", timeLabel: "11:37:18", detail: {textChars: 352}},
    {key: "m3", seq: 3, kind: "model_stream_progress", timeLabel: "11:37:23", detail: {textChars: 1872}},
    {key: "m4", seq: 4, kind: "model_call_finished", timeLabel: "11:37:25", detail: {modelLabel: "OpenAI/gpt-5.6-sol", thinkLevel: "xhigh", durationMs: 11763, tps: 59.763}},
    {key: "t1", seq: 5, kind: "tool_call_started", timeLabel: "11:37:25", toolName: "Bash", rawArguments: "{\"command\":\"pwd\",\"description\":\"确认当前目录\"}", toolDescription: "确认当前目录", detail: {name: "Bash", round: 7}},
    {key: "t2", seq: 6, kind: "tool_call_finished", timeLabel: "11:37:26", detail: {name: "Bash", round: 7, resultPreview: "status: ok"}},
  ];
  const compacted = compactAgentStepActivityLines(events, {thinkLevel: "high", fastMode: true});
  assert.equal(compacted.length, 2);
  assert.equal(compacted[0].message, "模型调用：OpenAI/gpt-5.6-sol · 12s · 59.8 t/s √");
  assert.equal(compacted[0].timeLabel, "11:37:25");
  assert.equal(compacted[0].tone, "success");
  assert.equal(compacted[0].modelThinkLevel, "xhigh");
  assert.equal(compacted[0].modelFastMode, true);
  assert.equal(compacted[1].message, "调用工具 Bash √");
  assert.equal(compacted[1].kind, "tool_call_started");
  assert.equal(compacted[1].rawArguments, "{\"command\":\"pwd\",\"description\":\"确认当前目录\"}");
  assert.equal(compacted[1].toolDescription, "确认当前目录");
  assert.equal(compacted[1].processStatus, "success");
});

test("tool process lifecycle keeps generic summary fields and exposes running, success, failed and denied states", () => {
  const started = {
    key: "tool-start",
    seq: 1,
    kind: "tool_call_started",
    timeLabel: "10:00:01",
    toolName: "WebSearch",
    rawArguments: "{\"query\":\"OpenBear\"}",
    toolDescription: "搜索 OpenBear",
    detail: {name: "WebSearch", round: 3},
  };
  const running = compactAgentStepActivityLines([started])[0];
  assert.equal(running.processStatus, "running");
  assert.equal(running.toolDescription, "搜索 OpenBear");

  const success = compactAgentStepActivityLines([
    started,
    {key: "tool-finish", seq: 2, kind: "tool_call_finished", detail: {name: "WebSearch", round: 3, resultPreview: "status: ok"}},
  ])[0];
  assert.equal(success.processStatus, "success");
  assert.equal(success.toolName, "WebSearch");
  assert.equal(success.rawArguments, started.rawArguments);
  assert.equal(success.toolDescription, "搜索 OpenBear");

  const failed = compactAgentStepActivityLines([
    started,
    {key: "tool-finish", seq: 2, kind: "tool_call_finished", detail: {name: "WebSearch", round: 3, resultPreview: "{\"ok\":false}"}},
  ])[0];
  assert.equal(failed.processStatus, "failed");
  assert.equal(failed.toolDescription, "搜索 OpenBear");

  const denied = compactAgentStepActivityLines([
    started,
    {key: "tool-denied", seq: 2, kind: "tool_call_denied", detail: {name: "WebSearch", round: 3}},
  ])[0];
  assert.equal(denied.processStatus, "denied");
  assert.equal(denied.toolDescription, "搜索 OpenBear");

  const failedEvent = compactAgentStepActivityLines([
    started,
    {key: "tool-failed", seq: 2, kind: "tool_call_failed", detail: {name: "WebSearch", round: 3}},
  ])[0];
  assert.equal(failedEvent.processStatus, "failed");
  assert.equal(failedEvent.toolDescription, "搜索 OpenBear");
});

test("step activity absorbs the complete model retry lifecycle and updates one row", () => {
  const retrying = [
    {key: "m1", seq: 1, kind: "model_call_started", detail: {modelLabel: "OpenAI/gpt-5.6-sol"}},
    {key: "m2", seq: 2, kind: "model_call_retry", summary: "模型调用失败，准备重试 1/10", detail: {durationMs: 90052}},
    {key: "m3", seq: 3, kind: "model_call_retry_wait", summary: "模型调用失败，0.6 秒后重试 1/10", detail: {retry: {attempt: 1, maxRetries: 10, summary: "当前账户请求过于频繁，请稍后再试", error: 'HTTP 503: {"error":{"message":"HTTP 429"}}'}}},
    {key: "m4", seq: 4, kind: "model_call_retry_resumed", summary: "模型重试等待结束 1/10", detail: {retry: {attempt: 1, maxRetries: 10}}},
    {key: "m5", seq: 5, kind: "model_call_started", detail: {modelLabel: "OpenAI/gpt-5.6-sol", attempt: 1, retryMax: 10}},
  ];
  const pending = compactAgentStepActivityLines(retrying);
  assert.equal(pending.length, 1);
  assert.equal(pending[0].message, "模型调用：OpenAI/gpt-5.6-sol · 正在重试 1/10 · 当前账户请求过于频繁，请稍后再试 ×");
  assert.ok(!pending[0].message.includes("HTTP 503"));
  assert.ok(!pending[0].message.includes("{"));
  assert.equal(pending[0].tone, "danger");

  const finished = compactAgentStepActivityLines([
    ...retrying,
    {key: "m6", seq: 6, kind: "model_call_finished", detail: {modelLabel: "OpenAI/gpt-5.6-sol", durationMs: 5388, tps: 33.5}},
  ]);
  assert.equal(finished.length, 1);
  assert.equal(finished[0].message, "模型调用：OpenAI/gpt-5.6-sol · 5.4s · 33.5 t/s √");
  assert.equal(finished[0].tone, "success");
});

test("step activity merges AgentPlanProgress tool triples and preserves explicit failures", () => {
  const successful = compactAgentStepActivityLines([
    {key: "p1", seq: 1, kind: "tool_call_started", toolName: "AgentPlanProgress", rawArguments: "{\"action\":\"start\",\"stepId\":\"step-5\"}", detail: {name: "AgentPlanProgress", round: 8}},
    {key: "p2", seq: 2, kind: "plan_progress_start", summary: "Plan start: step-5", detail: {planVersion: 1}},
    {key: "p3", seq: 3, kind: "tool_call_finished", detail: {name: "AgentPlanProgress", round: 8, resultPreview: "{\"ok\":true}"}},
  ]);
  assert.equal(successful.length, 1);
  assert.equal(successful[0].message, "开始步骤 · step-5 √");
  assert.equal(successful[0].kind, "tool_call_started");

  const boundaryPage = compactAgentStepActivityLines([
    {key: "p2", seq: 2, kind: "plan_progress_start", summary: "Plan start: step-5", detail: {planVersion: 1}},
    {key: "p3", seq: 3, kind: "tool_call_finished", detail: {name: "AgentPlanProgress", round: 8, resultPreview: "{\"ok\":true}"}},
  ]);
  assert.equal(boundaryPage.length, 1);
  assert.equal(boundaryPage[0].message, "开始步骤 · step-5 √");

  const failed = compactAgentStepActivityLines([
    {key: "p4", seq: 4, kind: "tool_call_started", toolName: "AgentPlanProgress", rawArguments: "{\"action\":\"complete\",\"stepId\":\"step-1\"}", detail: {name: "AgentPlanProgress", round: 9}},
    {key: "p5", seq: 5, kind: "tool_call_finished", detail: {name: "AgentPlanProgress", round: 9, resultPreview: "{\"ok\":false,\"error\":\"evidence_not_found\"}"}},
  ]);
  assert.equal(failed.length, 1);
  assert.equal(failed[0].message, "完成步骤 · step-1 · 失败 ×");
  assert.equal(failed[0].tone, "danger");
});

test("step activity synthesizes truncated lifecycle rows and preserves unknown events", () => {
  const compacted = compactAgentStepActivityLines([
    {key: "s1", seq: 10, kind: "model_stream_progress", detail: {round: 4, textChars: 2806}},
    {key: "t1", seq: 11, kind: "tool_call_finished", detail: {name: "Read", round: 4, resultPreview: "status: ok"}},
    {key: "x1", seq: 12, kind: "artifact_created", message: "已保存产物"},
  ], {modelLabel: "OpenAI/gpt-5.6-sol"});
  assert.equal(compacted.length, 3);
  assert.equal(compacted[0].message, "模型调用：OpenAI/gpt-5.6-sol · 流式输出中 · 2806 字");
  assert.equal(compacted[1].message, "调用工具 Read √");
  assert.equal(compacted[2].message, "已保存产物");
});

test("persisted tool-kind Agent cards remain active for WS-driven updates", () => {
  const event = {kind: "tool", live: true};
  const state = {
    summary: {cls: "running"},
    rows: [{status: "running"}],
  };
  assert.equal(isActiveAgentEvent(event, state), true);
  assert.equal(isActiveAgentEvent({...event, live: false}, {...state, rows: [{status: "completed"}], summary: {cls: "success"}}), false);
});

function completedSnapshot() {
  return {
    task: {
      model: "OpenAI/gpt-5.6-sol",
      input: {
        instruction: "最初的 Agent prompt",
        source: "openbear_agent_tool",
        agentSnapshot: {
          name: "general-purpose",
          model: "OpenAI/gpt-5.6-sol",
          thinkLevel: "xhigh",
          fastMode: true,
          toolAllowlist: ["Read"],
        },
      },
    },
    state: {
      phase: "finalizing",
      active_plan_version: 1,
      pending_plan_version: 0,
      current_step_id: "",
      final_outputs_state: {
        "output-1": {
          summary: "已交付版本号核验结果",
          sources: ["evidence:evidence-1", "step:step-1"],
        },
      },
    },
    current: {
      version: 1,
      status: "approved",
      plan_type: "initial",
      plan: {
        title: "只读核验",
        objective: "读取版本号",
        steps: [{
          id: "step-1",
          title: "读取文件",
          objective: "取得版本号",
          method: "Read 前 5 行",
          required: true,
          criteria: [{ id: "criterion-1", description: "取得版本号及原文", required: true }],
        }],
        finalOutputs: [{
          id: "output-1",
          title: "版本核验结果",
          description: "报告版本号",
          supportedBy: ["criterion-1"],
        }],
      },
    },
    versions: [{ version: 1, status: "approved", plan_type: "initial" }],
    decisions: [{ id: 1, expected_version: 1, action: "approve", reason: "计划可执行" }],
    steps: [{
      plan_version: 1,
      step_id: "step-1",
      status: "completed",
      result: "版本号为 0.1.0",
      completed_at: 100,
      criteria_state: {
        "criterion-1": {
          status: "satisfied",
          note: "已取得第 3 行原文",
          evidence: ["evidence-1"],
        },
      },
      blocker: {},
    }],
    evidence: [{
      evidence_uuid: "evidence-1",
      plan_version: 1,
      step_id: "step-1",
      criterion_id: "criterion-1",
      evidence_type: "read_result",
      reference: "/app/__init__.py:3",
      summary: "第 3 行为 __version__ = 0.1.0",
      metadata: { path: "/app/__init__.py", line: 3, excerpt: "__version__ = 0.1.0" },
    }],
  };
}

test("completed Plan explains every completion check with linked evidence", () => {
  const view = buildAgentPlanView(completedSnapshot());
  assert.equal(view.phaseMeta.label, "任务已验收");
  assert.equal(view.phaseMeta.label.includes("门禁"), false);
  assert.equal(view.defaultStepId, "step-1");
  assert.equal(view.steps[0].criteria[0].satisfied, true);
  assert.equal(view.steps[0].criteria[0].statusMeta.label, "已满足");
  assert.equal(view.steps[0].criteria[0].evidence[0].reference, "/app/__init__.py:3");
  assert.equal(view.steps[0].criteria[0].evidence[0].typeLabel, "文件核验");
  assert.equal(view.finalOutputs[0].completed, true);
  assert.equal(view.finalOutputs[0].sources[0].label, "文件核验：第 3 行为 __version__ = 0.1.0");
  assert.equal(view.finalOutputs[0].sources[0].summary, "第 3 行为 __version__ = 0.1.0");
  assert.equal(view.finalOutputs[0].sources[1].label, "执行步骤：读取文件");
  assert.deepEqual(view.completionChecks.map((item) => item.done), [true, true, true, true, true]);
});

test("monitor timeline turns structured task events into readable supervision cards", () => {
  const events = [
    {key: "1", kind: "task_started", timeLabel: "10:00:00", summary: "Agent started", detail: {agent: {name: "general-purpose", description: "定点检查代码", toolAllowlist: ["Read"]}, modelLabel: "GPT", thinkLevel: "xhigh"}},
    {key: "2", kind: "plan_submitted", timeLabel: "10:00:01", summary: "submitted", detail: {planVersion: 1, planType: "initial"}},
    {key: "3", kind: "plan_approve", timeLabel: "10:00:02", summary: "approved", detail: {action: "approve", planVersion: 1, reason: "范围明确，可以执行"}},
    {key: "4", kind: "plan_progress_start", timeLabel: "10:00:03", summary: "Plan start: step-1", detail: {planVersion: 1}},
    {key: "5", kind: "plan_progress_update", timeLabel: "10:00:04", summary: "Plan update: step-1", detail: {stepId: "step-1", evidence: [{summary: "版本号原文已核验"}]}},
    {key: "6", kind: "control_requested", timeLabel: "10:00:05", summary: "steer", detail: {action: "steer", message: "证据已经足够，请立即收口"}},
    {key: "7", kind: "plan_progress_complete", timeLabel: "10:00:06", summary: "Plan complete: step-1", detail: {stepId: "step-1"}},
    {key: "8", kind: "task_completed", timeLabel: "10:00:07", summary: "done", detail: {}},
    {key: "9", kind: "tool_call_started", timeLabel: "10:00:08", summary: "Read", detail: {}},
  ];
  const timeline = buildAgentMonitorTimeline(events, completedSnapshot());
  assert.equal(timeline.length, 8);
  assert.equal(timeline[1].title, "初始计划 v1 已提交");
  assert.deepEqual(timeline[1].bullets, ["步骤 1 · 读取文件"]);
  assert.equal(timeline[2].description, "范围明确，可以执行");
  assert.equal(timeline[3].title, "开始：读取文件");
  assert.deepEqual(timeline[4].bullets, ["版本号原文已核验"]);
  assert.equal(timeline[5].description, "证据已经足够，请立即收口");
  assert.equal(timeline[6].description, "版本号为 0.1.0");
  assert.deepEqual(timeline[7].bullets, ["已交付版本号核验结果"]);
  assert.equal(isAgentMonitorEventKind("plan_progress_update"), true);
  assert.equal(isAgentMonitorEventKind("tool_call_started"), false);
  assert.equal(isAgentMonitorEventKind("model_call_finished"), false);
});

test("terminal Plan defaults to the last completed required step", () => {
  const snapshot = completedSnapshot();
  snapshot.current.plan.steps.push({
    id: "step-2",
    title: "验证结果",
    required: true,
    criteria: [{ id: "criterion-2", description: "完成验证", required: true }],
  });
  snapshot.steps.push({
    plan_version: 1,
    step_id: "step-2",
    status: "completed",
    criteria_state: { "criterion-2": { status: "satisfied", evidence: ["evidence-2"] } },
    blocker: {},
  });
  snapshot.evidence.push({
    evidence_uuid: "evidence-2",
    plan_version: 1,
    step_id: "step-2",
    criterion_id: "criterion-2",
    summary: "验证通过",
    metadata: {},
  });
  assert.equal(buildAgentPlanView(snapshot).defaultStepId, "step-2");
});

test("completion check stays incomplete when a satisfied condition has no evidence", () => {
  const snapshot = completedSnapshot();
  snapshot.steps[0].criteria_state["criterion-1"].evidence = [];
  const view = buildAgentPlanView(snapshot);
  assert.equal(view.steps[0].criteria[0].satisfied, true);
  assert.equal(view.completionChecks.find((item) => item.key === "evidence").done, false);
});

test("final delivery resolves historical raw evidence UUIDs to readable summaries", () => {
  const snapshot = completedSnapshot();
  snapshot.state.final_outputs_state["output-1"].sources = ["evidence-1", "step:step-1"];
  const view = buildAgentPlanView(snapshot);
  assert.equal(view.finalOutputs[0].completed, true);
  assert.equal(view.finalOutputs[0].sources[0].label, "文件核验：第 3 行为 __version__ = 0.1.0");
  assert.equal(view.finalOutputs[0].sources[0].uuid, "evidence-1");
  assert.equal(view.finalOutputs[0].sources[0].label.includes("evidence-1"), false);
});

test("blocked and waiting-user phases use user-facing language", () => {
  assert.equal(planPhaseMeta("needs_user_decision").label, "等待你的决定");
  assert.equal(planPhaseMeta("blocked_control").label, "等待主控处理");
  assert.equal(planPhaseMeta("finalizing").description.includes("门禁"), false);

  const snapshot = completedSnapshot();
  snapshot.state.phase = "blocked_control";
  snapshot.state.current_step_id = "step-1";
  snapshot.steps[0].status = "blocked";
  snapshot.steps[0].blocker = { reason: "依赖服务不可用" };
  const blocked = buildAgentPlanView(snapshot);
  assert.equal(blocked.defaultStepId, "step-1");
  assert.equal(blocked.steps[0].statusMeta.label, "受阻");
  assert.equal(blocked.steps[0].blocker.reason, "依赖服务不可用");
  assert.equal(blocked.completionChecks.find((item) => item.key === "steps").done, false);
});

test("pending Replan is presented instead of stale approved Plan", () => {
  const snapshot = completedSnapshot();
  const approved = structuredClone(snapshot.current);
  approved.version = 1;
  snapshot.state.phase = "awaiting_replan_decision";
  snapshot.state.active_plan_version = 1;
  snapshot.state.pending_plan_version = 2;
  snapshot.current = {
    ...structuredClone(snapshot.current),
    version: 2,
    status: "pending",
    plan_type: "replan",
    plan: {
      ...structuredClone(snapshot.current.plan),
      title: "等待确认的调整计划",
      steps: [{
        id: "step-2",
        title: "调整后的步骤",
        required: true,
        criteria: [{ id: "criterion-2", description: "调整条件", required: true }],
      }],
    },
  };
  snapshot.versions = [approved, snapshot.current];
  snapshot.steps.push({
    plan_version: 2,
    step_id: "step-2",
    status: "pending",
    criteria_state: {},
    blocker: {},
  });
  const view = buildAgentPlanView(snapshot);
  assert.equal(view.currentVersion, 2);
  assert.equal(view.phaseMeta.label, "等待调整确认");
  assert.equal(view.steps[0].id, "step-2");
  assert.equal(view.steps[0].status, "pending");
  assert.equal(view.completionChecks[0].done, false);
});

test("approved active Plan wins a stale API current after refresh", () => {
  const snapshot = completedSnapshot();
  const stale = structuredClone(snapshot.current);
  stale.status = "superseded";
  const latest = structuredClone(snapshot.current);
  latest.version = 2;
  latest.status = "approved";
  latest.plan.title = "最新批准的调整计划";
  latest.plan.steps = [{
    id: "step-2",
    title: "调整后的执行步骤",
    required: true,
    criteria: [{ id: "criterion-2", description: "调整后的完成条件", required: true }],
  }];
  snapshot.state.active_plan_version = 2;
  snapshot.current = stale;
  snapshot.versions = [stale, latest];
  snapshot.steps.push({
    plan_version: 2,
    step_id: "step-2",
    status: "running",
    criteria_state: {"criterion-2": {status: "pending", evidence: []}},
    blocker: {},
  });

  const view = buildAgentPlanView(snapshot);
  assert.equal(view.currentVersion, 2);
  assert.equal(view.current.plan.title, "最新批准的调整计划");
  assert.equal(view.steps[0].id, "step-2");
  assert.equal(view.isActiveVersion, true);
  assert.equal(view.versions.find((item) => item.version === 2).active, true);
});

test("Plan version selection switches steps, evidence and decision context together", () => {
  const snapshot = completedSnapshot();
  const historical = structuredClone(snapshot.current);
  historical.status = "superseded";
  const latest = structuredClone(snapshot.current);
  latest.version = 2;
  latest.status = "approved";
  latest.plan.title = "v2 plan";
  latest.plan.steps = [{
    id: "step-2",
    title: "v2 step",
    required: true,
    criteria: [{ id: "criterion-2", description: "v2 criterion", required: true }],
  }];
  snapshot.state.active_plan_version = 2;
  snapshot.current = latest;
  snapshot.versions = [historical, latest];
  snapshot.decisions.push({id: 2, expected_version: 2, action: "approve", reason: "v2 approved"});
  snapshot.steps.push({
    plan_version: 2,
    step_id: "step-2",
    status: "completed",
    result: "v2 complete",
    criteria_state: {"criterion-2": {status: "satisfied", evidence: ["evidence-2"]}},
    blocker: {},
  });
  snapshot.evidence.push({
    evidence_uuid: "evidence-2",
    plan_version: 2,
    step_id: "step-2",
    criterion_id: "criterion-2",
    summary: "v2 evidence",
    metadata: {},
  });

  const historicalView = buildAgentPlanView(snapshot, {planVersion: 1});
  assert.equal(historicalView.currentVersion, 1);
  assert.equal(historicalView.isHistoricalVersion, true);
  assert.equal(historicalView.steps[0].id, "step-1");
  assert.deepEqual(historicalView.evidence.map((item) => item.uuid), ["evidence-1"]);
  assert.deepEqual(historicalView.versionDecisions.map((item) => item.expected_version), [1]);

  const activeView = buildAgentPlanView(snapshot, {planVersion: 2});
  assert.equal(activeView.currentVersion, 2);
  assert.equal(activeView.isActiveVersion, true);
  assert.equal(activeView.steps[0].id, "step-2");
  assert.deepEqual(activeView.evidence.map((item) => item.uuid), ["evidence-2"]);
  assert.deepEqual(activeView.versionDecisions.map((item) => item.expected_version), [2]);
});

test("initial launch prefers durable task input over overwritten operation arguments", () => {
  const launch = initialAgentLaunch(completedSnapshot(), '{"mode":"event_only","reason":"等待 Agent"}');
  assert.equal(launch.prompt, "最初的 Agent prompt");
  assert.equal(launch.agentName, "general-purpose");
  assert.equal(launch.model, "OpenAI/gpt-5.6-sol");
  assert.equal(launch.thinkLevel, "xhigh");
  assert.equal(launch.fastMode, true);
  assert.deepEqual(launch.tools, ["Read"]);
});

test("missing Plan data produces stable empty presentation", () => {
  const view = buildAgentPlanView({ state: { phase: "drafting" } });
  assert.equal(view.hasPlan, false);
  assert.equal(view.phaseMeta.label, "正在制定计划");
  assert.equal(view.steps.length, 0);
  assert.equal(view.defaultStepId, "");
  assert.equal(view.completionChecks.every((item) => item.done === false), true);
});

test("terminal legacy task explains why no Plan is shown", () => {
  const view = buildAgentPlanView({ task: { status: "completed" }, state: { phase: "drafting" } });
  assert.equal(view.hasPlan, false);
  assert.equal(view.phaseMeta.label, "历史任务无结构化计划");
  assert.match(view.phaseMeta.description, /启动信息、过程记录和结果/);
});

test("parent terminal task overrides a stale running step without mutating evidence or criteria", () => {
  for (const status of ["failed", "cancelled", "interrupted"]) {
    const snapshot = completedSnapshot();
    snapshot.task.status = status;
    snapshot.state.phase = "executing";
    snapshot.state.current_step_id = "step-1";
    snapshot.steps[0].status = "running";
    snapshot.steps[0].completed_at = 0;
    const originalCriteria = structuredClone(snapshot.steps[0].criteria_state);
    const view = buildAgentPlanView(snapshot);
    assert.equal(view.steps[0].status, status);
    assert.equal(view.steps[0].current, false);
    assert.equal(view.steps[0].statusMeta.label, {failed: "执行失败", cancelled: "已取消", interrupted: "已中断"}[status]);
    assert.equal(view.phaseMeta.label, {failed: "任务执行失败", cancelled: "任务已取消", interrupted: "任务已中断"}[status]);
    assert.deepEqual(snapshot.steps[0].criteria_state, originalCriteria);
    assert.equal(view.steps[0].criteria[0].satisfied, true);
    assert.equal(view.steps[0].criteria[0].evidence.length, 1);
  }
});

test("durable cancelled task may be presented as stopped without inventing a new status", () => {
  const snapshot = completedSnapshot();
  snapshot.task.status = "cancelled";
  snapshot.task.stopReason = "web_stop_requested";
  snapshot.state.phase = "executing";
  snapshot.steps[0].status = "running";
  const view = buildAgentPlanView(snapshot);
  assert.equal(view.taskStatus, "cancelled");
  assert.equal(view.phaseMeta.label, "任务已停止");
  assert.equal(view.steps[0].status, "cancelled");
});

test("final context compaction kinds stay in Agent activity while low-level candidates are excluded", () => {
  for (const kind of [
    "model_context_pre_compacted",
    "model_context_overflow_compacted",
    "model_context_compaction_failed",
    "model_context_compacted",
    "model_context_compaction_completed",
  ]) assert.equal(isActivityExcludedEventKind(kind), false, kind);
  assert.equal(isActivityExcludedEventKind("model_context_compaction_candidate"), true);
  assert.equal(isActivityExcludedEventKind("model_context_compaction_fallback_started"), true);
});

test("Agent context compaction activity preserves output, failure reason, and honest legacy fallback", () => {
  const successful = agentCompactionActivityView({
    kind: "model_context_pre_compacted",
    summary: "压缩完成",
    detail: {
      compaction_id: "compact-1",
      summary_id: "summary-1",
      source: "pre_model",
      before_tokens: 120000,
      afterTokens: 42000,
      summary_chars: 8600,
      compacted_output: "## 持久化摘要\n\n正文",
    },
  });
  assert.equal(successful.isCompaction, true);
  assert.equal(successful.failed, false);
  assert.equal(successful.output, "## 持久化摘要\n\n正文");
  assert.match(successful.message, /120,000 → 42,000 token/);
  assert.equal(successful.cardTitle, "上下文压缩");
  assert.equal(successful.cardPreview, "来源：模型请求前 · 压缩前上下文：120,000 token · 压缩后上下文：42,000 token · 摘要：8,600 字");
  assert.equal(successful.sourceLabel, "模型请求前");
  assert.equal(successful.summaryId, "summary-1");

  const failed = agentCompactionActivityView({kind: "model_context_compaction_failed", detail: {reason: "overflow retry exhausted"}});
  assert.equal(failed.failed, true);
  assert.equal(failed.reason, "overflow retry exhausted");

  const legacy = agentCompactionActivityView({kind: "model_context_overflow_compacted", detail: {summaryId: "old-summary", source: "legacy_summary"}});
  assert.equal(legacy.sourceLabel, "历史摘要");
  assert.match(legacy.cardPreview, /压缩前上下文：未记录 · 压缩后上下文：未记录/);
  assert.equal(legacy.output, "");
  assert.equal(legacy.emptyOutputText, "旧记录未持久化压缩摘要");
});
