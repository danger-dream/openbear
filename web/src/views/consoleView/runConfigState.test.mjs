import test from "node:test";
import assert from "node:assert/strict";
import {
  createRunConfigSaveQueue,
  mayRetireRunConfigOverride,
  runConfigForDisplay,
  runConfigFromResponse,
} from "./runConfigState.js";

function config(conversationUuid, model, overrides = {}) {
  return {
    conversationUuid,
    model,
    thinkingLevel: "medium",
    effectiveThinkingLevel: "medium",
    thinkingLevels: ["low", "medium", "high"],
    defaultThinkingLevel: "medium",
    supportsThinking: true,
    fastMode: false,
    fastRequested: false,
    fastSupported: true,
    effectiveFastMode: false,
    agentRunConfig: {
      model: "",
      thinkLevel: "",
      fastMode: null,
      effective: {model, thinkLevel: "medium", fastMode: false},
    },
    contextWindow: 128000,
    compactTriggerTokens: 89600,
    compactRatio: 0.7,
    ...overrides,
  };
}

function response(conversationUuid, model, overrides = {}) {
  return {ok: true, runConfig: config(conversationUuid, model, overrides)};
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return {promise, resolve, reject};
}

test("save responses must contain a complete config for the requested conversation", () => {
  const valid = response("conv-a", "openai/gpt");
  assert.equal(runConfigFromResponse(valid, "conv-a"), valid.runConfig);
  assert.equal(runConfigFromResponse(valid, "conv-b"), null);
  assert.equal(runConfigFromResponse({ok: true}, "conv-a"), null);
  const incomplete = response("conv-a", "openai/gpt");
  delete incomplete.runConfig.compactRatio;
  assert.equal(runConfigFromResponse(incomplete, "conv-a"), null);
});

test("rapid saves are serialized per conversation and applied in server order", async () => {
  const firstGate = deferred();
  const started = [];
  const applied = [];
  const queue = createRunConfigSaveQueue({
    isCurrent: (uuid) => uuid === "conv-a",
    apply: (value) => applied.push(value.model),
  });

  const first = queue.enqueue("conv-a", async () => {
    started.push("first");
    await firstGate.promise;
    return response("conv-a", "openai/first");
  });
  const second = queue.enqueue("conv-a", async () => {
    started.push("second");
    return response("conv-a", "openai/second");
  });

  await Promise.resolve();
  assert.deepEqual(started, ["first"]);
  firstGate.resolve();
  assert.equal((await first).applied, true);
  assert.equal((await second).applied, true);
  assert.deepEqual(started, ["first", "second"]);
  assert.deepEqual(applied, ["openai/first", "openai/second"]);
  assert.equal(queue.appliedVersion, 2);
});

test("one failed save does not block the next save", async () => {
  const applied = [];
  const queue = createRunConfigSaveQueue({
    isCurrent: () => true,
    apply: (value) => applied.push(value.model),
  });
  const failed = queue.enqueue("conv-a", async () => { throw new Error("save_failed"); });
  const next = queue.enqueue("conv-a", async () => response("conv-a", "openai/recovered"));

  await assert.rejects(failed, /save_failed/);
  assert.equal((await next).applied, true);
  assert.deepEqual(applied, ["openai/recovered"]);
  assert.equal(queue.appliedVersion, 1);
});

test("a late response is ignored while another conversation is active", async () => {
  let active = "conv-a";
  const gate = deferred();
  const applied = [];
  const queue = createRunConfigSaveQueue({
    isCurrent: (uuid) => uuid === active,
    apply: (value) => applied.push([value.conversationUuid, value.model]),
  });
  const oldSave = queue.enqueue("conv-a", async () => {
    await gate.promise;
    return response("conv-a", "openai/late");
  });

  await Promise.resolve();
  active = "conv-b";
  gate.resolve();
  assert.equal((await oldSave).applied, false);
  assert.deepEqual(applied, []);
  assert.equal(queue.appliedVersion, 0);

  const currentSave = await queue.enqueue("conv-b", async () => response("conv-b", "openai/current"));
  assert.equal(currentSave.applied, true);
  assert.deepEqual(applied, [["conv-b", "openai/current"]]);
});

test("leaving and returning to the same conversation rejects the previous visit's late save", async () => {
  let active = "conv-a";
  let generation = 0;
  const gate = deferred();
  const applied = [];
  const queue = createRunConfigSaveQueue({
    captureScope: () => generation,
    isCurrent: (uuid, captured) => uuid === active && captured === generation,
    apply: (value) => applied.push(value.model),
  });
  const oldSave = queue.enqueue("conv-a", async () => {
    await gate.promise;
    return response("conv-a", "openai/old-visit");
  });
  await Promise.resolve();
  active = "conv-b";
  generation += 1;
  active = "conv-a";
  generation += 1;
  gate.resolve();
  assert.equal((await oldSave).applied, false);
  assert.deepEqual(applied, []);
  const fresh = await queue.enqueue("conv-a", async () => response("conv-a", "openai/new-visit"));
  assert.equal(fresh.applied, true);
  assert.deepEqual(applied, ["openai/new-visit"]);
});

test("a pre-save state cannot mask the saved config, while a post-save state retires it", async () => {
  let state = config("conv-a", "openai/original");
  let override = null;
  const queue = createRunConfigSaveQueue({
    isCurrent: () => true,
    apply: (value) => { override = value; },
  });
  const staleStateRequestVersion = queue.appliedVersion;

  await queue.enqueue("conv-a", async () => response("conv-a", "openai/saved"));
  assert.equal(runConfigForDisplay(state, override, "conv-a").model, "openai/saved");

  state = config("conv-a", "openai/stale");
  if (mayRetireRunConfigOverride(staleStateRequestVersion, queue.appliedVersion)) override = null;
  assert.equal(runConfigForDisplay(state, override, "conv-a").model, "openai/saved");

  const authoritativeStateRequestVersion = queue.appliedVersion;
  state = config("conv-a", "openai/external-newer");
  if (mayRetireRunConfigOverride(authoritativeStateRequestVersion, queue.appliedVersion)) override = null;
  assert.equal(override, null);
  assert.equal(runConfigForDisplay(state, override, "conv-a").model, "openai/external-newer");
});

test("an override never leaks across a conversation boundary and can be explicitly cleared", () => {
  const stateB = config("conv-b", "openai/b");
  let override = config("conv-a", "openai/a-saved");
  assert.equal(runConfigForDisplay(stateB, override, "conv-b").model, "openai/b");
  override = null;
  assert.equal(runConfigForDisplay(stateB, override, "conv-b"), stateB);
});
