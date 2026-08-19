import test from "node:test";
import assert from "node:assert/strict";

import {createTaskMemoryRequestGate} from "./taskMemoryRequestGate.js";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

async function guardedApply({gate, identity, channel, request, apply}) {
  const token = gate.capture(identity.current(), channel);
  const value = await request;
  if (gate.isCurrent(token, identity.current())) apply(value);
}

test("late list, task list, count, and detail responses cannot cross conversation identity", async () => {
  const gate = createTaskMemoryRequestGate();
  let current = {conversationUuid: "conv-old", scopeType: "conversation", taskUuid: ""};
  const identity = {current: () => current};
  const state = {items: [], tasks: [], count: 0, detail: ""};
  const old = {
    items: deferred(), tasks: deferred(), count: deferred(), detail: deferred(),
  };
  const pending = [
    guardedApply({gate, identity, channel: "items", request: old.items.promise, apply: (value) => { state.items = value; }}),
    guardedApply({gate, identity, channel: "tasks", request: old.tasks.promise, apply: (value) => { state.tasks = value; }}),
    guardedApply({gate, identity, channel: "count", request: old.count.promise, apply: (value) => { state.count = value; }}),
    guardedApply({gate, identity, channel: "detail", request: old.detail.promise, apply: (value) => { state.detail = value; }}),
  ];

  gate.invalidate();
  current = {conversationUuid: "conv-new", scopeType: "agent_task", taskUuid: "task-new"};
  const freshItems = deferred();
  const fresh = guardedApply({
    gate, identity, channel: "items", request: freshItems.promise,
    apply: (value) => { state.items = value; },
  });
  freshItems.resolve(["new-item"]);
  await fresh;

  old.items.resolve(["old-item"]);
  old.tasks.resolve(["old-task"]);
  old.count.resolve(99);
  old.detail.resolve("old-body");
  await Promise.all(pending);

  assert.deepEqual(state, {items: ["new-item"], tasks: [], count: 0, detail: ""});
});

test("scope/task changes and dispose invalidate pending responses", async () => {
  const gate = createTaskMemoryRequestGate();
  let current = {conversationUuid: "conv", scopeType: "agent_task", taskUuid: "task-a"};
  const identity = {current: () => current};
  const state = {preview: "", detail: ""};

  const oldTaskPreview = deferred();
  const previewPending = guardedApply({
    gate, identity, channel: "preview", request: oldTaskPreview.promise,
    apply: (value) => { state.preview = value; },
  });
  current = {...current, taskUuid: "task-b"};
  gate.invalidate();
  oldTaskPreview.resolve("task-a-preview");
  await previewPending;
  assert.equal(state.preview, "");

  const detail = deferred();
  const detailPending = guardedApply({
    gate, identity, channel: "detail", request: detail.promise,
    apply: (value) => { state.detail = value; },
  });
  gate.dispose();
  detail.resolve("late-after-unmount");
  await detailPending;
  assert.equal(state.detail, "");
});
