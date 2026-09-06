import test from "node:test";
import assert from "node:assert/strict";
import { createLoginFlow } from "./loginFlow.js";

function harness(overrides = {}) {
  const state = {secret: "test-only", loading: false, status: "", requestUuid: "", retryAfter: 0};
  const timers = new Map();
  const messages = [];
  const calls = {start: 0, status: 0, consume: 0, session: 0, authenticated: 0};
  let clock = 0;
  let timerId = 0;
  const api = {
    async loginStart() { calls.start++; return {requestUuid: "one", expiresIn: 60}; },
    async loginStatus() { calls.status++; return {status: "pending"}; },
    async consumeLogin() { calls.consume++; },
    async authSession() { calls.session++; return {ok: true}; },
    ...overrides,
  };
  const flow = createLoginFlow({state, api, success: () => {}, error: (x) => messages.push(x),
    authenticated: () => calls.authenticated++, now: () => clock,
    schedule: (fn) => { timers.set(++timerId, fn); return timerId; },
    unschedule: (id) => timers.delete(id),
  });
  return {state, calls, timers, messages, flow, api,
    advance: (ms) => { clock += ms; },
    async tick() { const [id, fn] = timers.entries().next().value; timers.delete(id); await fn(); },
  };
}

test("concurrent submits including Enter/click own exactly one request", async () => {
  let resolve;
  let starts = 0;
  const h = harness({loginStart: () => { starts++; return new Promise((r) => { resolve = r; }); }});
  const first = h.flow.submit();
  await h.flow.submit();
  assert.equal(starts, 1);
  resolve({requestUuid: "one"});
  await first;
  await h.flow.submit();
  assert.equal(starts, 1);
  assert.equal(h.timers.size, 1);
});

for (const status of ["missing", "consumed", "expired", "rejected", "denied", "future-state"]) {
  test(`${status} is terminal and allows a new submission`, async () => {
    const h = harness({loginStatus: async () => ({status}), authSession: async () => { throw new Error("401"); }});
    await h.flow.submit();
    await h.tick();
    assert.equal(h.state.loading, false);
    assert.equal(h.state.requestUuid, "");
    assert.equal(h.timers.size, 0);
    assert.equal(h.calls.authenticated, 0);
    assert.equal(h.messages.length, 1);
    await h.flow.submit();
    assert.equal(h.calls.start, 2);
    assert.equal(h.state.loading, true);
  });
}

test("approved verifies a stored session after consuming, consumed resumes an existing session", async () => {
  for (const status of ["approved", "consumed"]) {
    const h = harness({loginStatus: async () => ({status})});
    await h.flow.submit();
    await h.tick();
    assert.equal(h.calls.consume, status === "approved" ? 1 : 0);
    assert.equal(h.calls.session, 1);
    assert.equal(h.calls.authenticated, 1);
    assert.equal(h.timers.size, 0);
  }
});

test("approved but missing Cookie does not navigate and reports recovery", async () => {
  const h = harness({loginStatus: async () => ({status: "approved"}), authSession: async () => { throw new Error("401"); }});
  await h.flow.submit();
  await h.tick();
  assert.equal(h.state.loading, false);
  assert.equal(h.calls.authenticated, 0);
  assert.match(h.messages[0], /Cookie/);
});

test("pending polling is bounded even if the server never expires it", async () => {
  const h = harness();
  await h.flow.submit();
  await h.tick();
  assert.equal(h.timers.size, 1);
  h.advance(60_001);
  await h.tick();
  assert.equal(h.state.loading, false);
  assert.equal(h.timers.size, 0);
  assert.equal(h.calls.status, 1);
});

test("unmount invalidates a late login-start response", async () => {
  let resolve;
  const h = harness({loginStart: () => new Promise((r) => { resolve = r; })});
  const pending = h.flow.submit();
  h.flow.dispose();
  resolve({requestUuid: "late"});
  await pending;
  assert.equal(h.state.requestUuid, "");
  assert.equal(h.timers.size, 0);
});

test("unmount invalidates a late approved poll response", async () => {
  let resolve;
  const h = harness({loginStatus: () => new Promise((r) => { resolve = r; })});
  await h.flow.submit();
  const polling = h.tick();
  h.flow.dispose();
  resolve({status: "approved"});
  await polling;
  assert.equal(h.calls.consume, 0);
  assert.equal(h.calls.authenticated, 0);
  assert.equal(h.timers.size, 0);
});

test("request errors clear loading and retain rate-limit recovery information", async () => {
  const h = harness({loginStart: async () => { throw {response: {data: {error: "rate_limited", retryAfter: 60}}}; }});
  await h.flow.submit();
  assert.equal(h.state.loading, false);
  assert.equal(h.state.retryAfter, 60);
  assert.equal(h.timers.size, 0);
});
