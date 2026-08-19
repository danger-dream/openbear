import test from "node:test";
import assert from "node:assert/strict";

import {createToolDetailCache} from "./toolDetailCache.js";

test("tool detail cache deduplicates in-flight requests and keys values by revision", async () => {
  const cache = createToolDetailCache();
  cache.reset("conversation-a");
  let calls = 0;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const loader = async () => {
    calls += 1;
    await gate;
    return {opId: "tool:1", revision: 1, payload: {result: "secret"}};
  };

  const first = cache.load({conversationUuid: "conversation-a", operationId: "tool:1", revision: 1, loader});
  const duplicate = cache.load({conversationUuid: "conversation-a", operationId: "tool:1", revision: 1, loader});
  await Promise.resolve();
  assert.equal(calls, 1);
  release();
  assert.deepEqual(await Promise.all([first, duplicate]), [
    {opId: "tool:1", revision: 1, payload: {result: "secret"}},
    {opId: "tool:1", revision: 1, payload: {result: "secret"}},
  ]);

  await cache.load({conversationUuid: "conversation-a", operationId: "tool:1", revision: 1, loader});
  assert.equal(calls, 1);

  await cache.load({
    conversationUuid: "conversation-a",
    operationId: "tool:1",
    revision: 2,
    loader: async () => {
      calls += 1;
      return {opId: "tool:1", revision: 2, payload: {result: "new"}};
    },
  });
  assert.equal(calls, 2);
  assert.equal(cache.size, 1);
});

test("conversation reset discards cached and still-resolving sensitive details", async () => {
  const cache = createToolDetailCache();
  cache.reset("conversation-a");
  let release;
  const pending = cache.load({
    conversationUuid: "conversation-a",
    operationId: "tool:old",
    revision: 1,
    loader: () => new Promise((resolve) => { release = resolve; }),
  });
  await Promise.resolve();

  cache.reset("conversation-b");
  release({opId: "tool:old", revision: 1, payload: {result: "old secret"}});
  await pending;
  assert.equal(cache.size, 0);

  const current = await cache.load({
    conversationUuid: "conversation-b",
    operationId: "tool:new",
    revision: 1,
    loader: async () => ({opId: "tool:new", revision: 1, payload: {result: "current"}}),
  });
  assert.equal(current.payload.result, "current");
  assert.equal(cache.size, 1);
});
