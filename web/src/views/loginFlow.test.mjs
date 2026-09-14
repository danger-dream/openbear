import test from "node:test";
import assert from "node:assert/strict";
import { createLoginFlow } from "./loginFlow.js";
import { createLoginLifecycle } from "./loginLifecycle.js";

const networkError = () => Object.assign(new Error("Network Error"), {code: "ERR_NETWORK", isAxiosError: true});
const httpError = (status, data = {}, headers = {}) => ({response: {status, data, headers}});
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
class LifecycleTarget extends EventTarget {
  listeners = new Map();
  addEventListener(event, listener) {
    this.listeners.set(event, listener);
    super.addEventListener(event, listener);
  }
  removeEventListener(event, listener) {
    assert.equal(this.listeners.get(event), listener);
    this.listeners.delete(event);
    super.removeEventListener(event, listener);
  }
}

function harness(overrides = {}) {
  const state = {secret: "test-only", loading: false, status: "", requestUuid: "", retryAfter: 0, notice: ""};
  const page = new LifecycleTarget();
  page.visibilityState = "visible";
  const browser = new LifecycleTarget();
  const network = {onLine: true};
  const lifecycle = createLoginLifecycle({page, browser, network});
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
  const flow = createLoginFlow({state, api, lifecycle, success: () => {}, error: (x) => messages.push(x),
    authenticated: () => calls.authenticated++, now: () => clock,
    schedule: (fn, ms) => { timers.set(++timerId, {fn, at: clock + ms, delay: ms}); return timerId; },
    unschedule: (id) => timers.delete(id),
  });
  return {state, calls, timers, messages, flow, api, page, browser, network,
    advance: (ms) => { clock += ms; },
    async visibility(hidden) {
      page.visibilityState = hidden ? "hidden" : "visible";
      page.dispatchEvent(new Event("visibilitychange"));
      await flush();
    },
    async online(value) {
      network.onLine = value;
      browser.dispatchEvent(new Event(value ? "online" : "offline"));
      await flush();
    },
    async event(name) { browser.dispatchEvent(new Event(name)); await flush(); },
    async tick() {
      const [id, timer] = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      timers.delete(id);
      clock = Math.max(clock, timer.at);
      await timer.fn();
    },
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

test("Telegram approval while hidden resumes immediately, without another start", async () => {
  const h = harness();
  await h.flow.submit();
  await h.visibility(true);
  assert.equal(h.timers.size, 0);
  h.advance(10_000);
  h.api.loginStatus = async () => { h.calls.status++; return {status: "approved"}; };
  assert.equal(h.calls.status, 0);
  await h.visibility(false);
  assert.deepEqual(h.calls, {start: 1, status: 1, consume: 1, session: 1, authenticated: 1});
  assert.equal(h.timers.size, 0);
  assert.equal(h.messages.length, 0);
});

test("pagehide/pageshow supports a living BFCache page and event bursts keep one read chain", async () => {
  const h = harness();
  await h.flow.submit();
  await h.event("pagehide");
  assert.equal(h.timers.size, 0);
  h.advance(5000);
  await h.event("pageshow");
  assert.equal(h.calls.status, 1);
  for (let i = 0; i < 5; i++) {
    await h.event("pageshow");
    await h.online(true);
    await h.visibility(true);
    await h.visibility(false);
  }
  assert.equal(h.calls.status, 1);
  assert.equal(h.timers.size, 1);
  await h.tick();
  assert.equal(h.calls.status, 2);
  assert.equal(h.timers.size, 1);
});

test("a late background approval read does not consume until foreground", async () => {
  const read = deferred();
  let reads = 0;
  const h = harness({loginStatus: () => ++reads === 1 ? read.promise : Promise.resolve({status: "approved"})});
  await h.flow.submit();
  const polling = h.tick();
  await h.visibility(true);
  read.resolve({status: "approved"});
  await polling;
  assert.equal(h.calls.consume, 0);
  assert.equal(h.timers.size, 0);
  h.advance(3000);
  await h.visibility(false);
  assert.equal(reads, 2);
  assert.equal(h.calls.consume, 1);
  assert.equal(h.calls.authenticated, 1);
});

test("foreground events during a pending read never overlap requests", async () => {
  const read = deferred();
  let reads = 0;
  const h = harness({loginStatus: () => { reads++; return read.promise; }});
  await h.flow.submit();
  const polling = h.tick();
  await h.visibility(true);
  h.advance(4000);
  await h.visibility(false);
  await h.online(true);
  await h.event("pageshow");
  assert.equal(reads, 1);
  assert.equal(h.timers.size, 0);
  read.resolve({status: "pending"});
  await polling;
  assert.equal(h.timers.size, 1);
});

test("backgrounding during start retains the sole result but schedules no background poll", async () => {
  const start = deferred();
  let starts = 0;
  const h = harness({loginStart: () => { starts++; return start.promise; }});
  const submission = h.flow.submit();
  await h.visibility(true);
  start.resolve({requestUuid: "one", expiresIn: 60});
  await submission;
  assert.equal(h.timers.size, 0);
  await h.flow.submit();
  h.advance(4000);
  await h.visibility(false);
  assert.equal(h.calls.status, 1);
  assert.equal(starts, 1);
});

test("short network failure recovers on online without toast repetition or POST replay", async () => {
  const h = harness({loginStatus: async () => { throw networkError(); }});
  await h.flow.submit();
  await h.tick();
  assert.equal(h.state.loading, true);
  assert.match(h.state.notice, /网络/);
  assert.equal(h.messages.length, 0);
  await h.online(false);
  assert.equal(h.timers.size, 0);
  h.advance(5000);
  h.api.loginStatus = async () => ({status: "approved"});
  await h.online(true);
  assert.equal(h.calls.authenticated, 1);
  assert.equal(h.calls.start, 1);
  assert.equal(h.calls.consume, 1);
  assert.equal(h.messages.length, 0);
});

test("timeout retries without depending on an online event", async () => {
  let reads = 0;
  const h = harness({loginStatus: async () => {
    reads++;
    if (reads === 1) throw Object.assign(new Error("timeout"), {code: "ECONNABORTED"});
    return {status: "approved"};
  }});
  await h.flow.submit();
  await h.tick();
  await h.tick();
  assert.equal(reads, 2);
  assert.equal(h.calls.authenticated, 1);
  assert.equal(h.calls.start, 1);
});

test("transient service errors have bounded backoff that lifecycle bursts cannot bypass", async () => {
  let reads = 0;
  const h = harness({loginStatus: async () => { reads++; throw httpError(503); }});
  await h.flow.submit();
  await h.tick();
  await h.tick();
  assert.equal([...h.timers.values()][0].delay, 3600);
  for (let i = 0; i < 5; i++) { await h.online(false); await h.online(true); }
  assert.equal(reads, 2);
  assert.equal([...h.timers.values()][0].delay, 3600);
  while (h.timers.size) await h.tick();
  assert.equal(reads, 6);
  assert.equal(h.state.loading, false);
  assert.equal(h.messages.length, 1);
  await h.event("pageshow");
  assert.equal(reads, 6);
});

for (const code of [400, 401, 403, 404, 501]) {
  test(`definitive status HTTP ${code} is not retried`, async () => {
    let reads = 0;
    const h = harness({loginStatus: async () => { reads++; throw httpError(code); }});
    await h.flow.submit();
    await h.tick();
    await h.visibility(true);
    await h.visibility(false);
    assert.equal(reads, 1);
    assert.equal(h.state.loading, false);
    assert.equal(h.timers.size, 0);
    assert.equal(h.calls.consume, 0);
  });
}

for (const status of ["expired", "rejected", "denied"]) {
  test(`foreground ${status} terminates recovery`, async () => {
    const h = harness({loginStatus: async () => ({status})});
    await h.flow.submit();
    await h.visibility(true);
    h.advance(3000);
    await h.visibility(false);
    assert.equal(h.state.status, status);
    assert.equal(h.state.loading, false);
    assert.equal(h.timers.size, 0);
    assert.equal(h.calls.consume, 0);
    assert.equal(h.messages.length, 1);
  });
}

test("expired offline request stops on return without another server query", async () => {
  const h = harness();
  await h.flow.submit();
  await h.online(false);
  h.advance(60_001);
  await h.online(true);
  assert.equal(h.state.status, "expired");
  assert.equal(h.calls.status, 0);
  assert.equal(h.timers.size, 0);
});

test("server TTL is not extended by a slow start or a short configured lifetime", async () => {
  const start = deferred();
  const h = harness({loginStart: () => start.promise});
  const submitting = h.flow.submit();
  h.advance(12_000);
  start.resolve({requestUuid: "one", expiresIn: 10});
  await submitting;
  assert.equal(h.state.status, "expired");
  assert.equal(h.calls.status, 0);
  assert.equal(h.timers.size, 0);
});

test("uncertain start is never automatically resent by lifecycle or network recovery", async () => {
  let starts = 0;
  const h = harness({loginStart: async () => { starts++; throw networkError(); }});
  await h.flow.submit();
  await h.online(false);
  h.advance(5000);
  await h.online(true);
  await h.event("pageshow");
  assert.equal(starts, 1);
  assert.equal(h.state.loading, false);
  assert.equal(h.timers.size, 0);
  assert.match(h.messages[0], /未自动重试/);
});

test("uncertain consume checks session first and never replays consume", async () => {
  const order = [];
  const h = harness({
    loginStatus: async () => { order.push("status"); return {status: "approved"}; },
    consumeLogin: async () => { order.push("consume"); throw networkError(); },
    authSession: async () => { order.push("session"); return {ok: true}; },
  });
  await h.flow.submit();
  await h.tick();
  assert.deepEqual(order, ["status", "consume", "session"]);
  assert.equal(h.calls.authenticated, 1);
  assert.equal(h.timers.size, 0);
  assert.equal(h.messages.length, 0);
});

test("uncertain consume with weak session network retries only the session GET", async () => {
  let reads = 0, consumes = 0, sessions = 0;
  const h = harness({
    loginStatus: async () => { reads++; return {status: "approved"}; },
    consumeLogin: async () => { consumes++; throw httpError(504); },
    authSession: async () => { if (++sessions < 3) throw networkError(); return {ok: true}; },
  });
  await h.flow.submit();
  await h.tick();
  await h.visibility(true);
  h.advance(5000);
  await h.visibility(false);
  await h.tick();
  assert.deepEqual({reads, consumes, sessions}, {reads: 1, consumes: 1, sessions: 3});
  assert.equal(h.calls.authenticated, 1);
  assert.equal(h.calls.start, 1);
  assert.equal(h.messages.length, 0);
});

test("uncertain consume with missing session stops and cannot replay the nonce", async () => {
  let consumes = 0;
  const h = harness({
    loginStatus: async () => ({status: "approved"}),
    consumeLogin: async () => { consumes++; throw networkError(); },
    authSession: async () => { throw httpError(401, {error: "unauthorized"}); },
  });
  await h.flow.submit();
  await h.tick();
  await h.visibility(true);
  await h.visibility(false);
  await h.event("pageshow");
  assert.equal(consumes, 1);
  assert.equal(h.state.loading, false);
  assert.equal(h.calls.authenticated, 0);
  assert.equal(h.timers.size, 0);
  assert.match(h.messages[0], /避免重复消费/);
});

test("consume settling in background defers session confirmation until return", async () => {
  const consume = deferred();
  let consumes = 0;
  const h = harness({
    loginStatus: async () => ({status: "approved"}),
    consumeLogin: () => { consumes++; return consume.promise; },
  });
  await h.flow.submit();
  const polling = h.tick();
  await flush();
  await h.visibility(true);
  consume.resolve();
  await polling;
  assert.equal(h.calls.session, 0);
  assert.equal(h.timers.size, 0);
  h.advance(3000);
  await h.visibility(false);
  assert.equal(consumes, 1);
  assert.equal(h.calls.session, 1);
  assert.equal(h.calls.authenticated, 1);
});

test("session recovery has its own short deadline and does not consume again after expiry", async () => {
  const h = harness({loginStatus: async () => ({status: "approved"}), authSession: async () => { throw networkError(); }});
  await h.flow.submit();
  await h.tick();
  await h.visibility(true);
  h.advance(60_001);
  await h.visibility(false);
  assert.equal(h.state.status, "expired");
  assert.equal(h.calls.consume, 1);
  assert.equal(h.timers.size, 0);
});

test("a malformed session response never counts as authenticated", async () => {
  const h = harness({loginStatus: async () => ({status: "approved"}), authSession: async () => ({ok: false})});
  await h.flow.submit();
  await h.tick();
  assert.equal(h.calls.authenticated, 0);
  assert.equal(h.state.loading, false);
  assert.equal(h.timers.size, 0);
});

for (const operation of ["loginStart", "loginStatus", "consumeLogin", "authSession"]) {
  test(`${operation} 429 terminates the chain and explicit submit respects cooldown`, async () => {
    const h = harness({loginStatus: async () => ({status: "approved"})});
    let attempts = 0;
    h.api[operation] = async () => { attempts++; throw httpError(429, {}, {"retry-after": "20"}); };
    await h.flow.submit();
    if (operation !== "loginStart") await h.tick();
    assert.equal(h.state.retryAfter, 20);
    assert.equal(h.state.loading, false);
    assert.equal(h.timers.size, 0);
    await h.flow.submit();
    await h.online(false);
    await h.online(true);
    assert.equal(attempts, 1);
    h.advance(20_001);
    let starts = 0;
    h.api.loginStart = async () => { starts++; return {requestUuid: "new", expiresIn: 60}; };
    await h.flow.submit();
    assert.equal(starts, 1);
    assert.equal(h.state.retryAfter, 0);
  });
}

for (const operation of ["loginStart", "loginStatus", "consumeLogin", "authSession"]) {
  for (const rejected of [false, true]) {
    test(`dispose cleans listeners and ignores late ${operation} ${rejected ? "error" : "success"}`, async () => {
      const response = deferred();
      const h = harness({loginStatus: async () => ({status: "approved"}), [operation]: () => response.promise});
      let pending = h.flow.submit();
      if (operation !== "loginStart") { await pending; pending = h.tick(); }
      await flush();
      const snapshot = structuredClone(h.state);
      const calls = structuredClone(h.calls);
      h.flow.dispose();
      assert.equal(h.page.listeners.size, 0);
      assert.equal(h.browser.listeners.size, 0);
      if (rejected) response.reject(networkError());
      else response.resolve({ok: true, status: "approved", requestUuid: "late"});
      await pending;
      await h.visibility(true);
      await h.visibility(false);
      await h.online(true);
      await h.event("pageshow");
      await h.flow.submit();
      assert.deepEqual(h.state, snapshot);
      assert.deepEqual(h.calls, calls);
      assert.equal(h.timers.size, 0);
      assert.equal(h.messages.length, 0);
    });
  }
}

test("a cleared callback cannot steal the foreground timer or create another read", async () => {
  const h = harness();
  await h.flow.submit();
  const stale = [...h.timers.values()][0].fn;
  await h.visibility(true);
  h.advance(5000);
  await h.visibility(false);
  assert.equal(h.calls.status, 1);
  assert.equal(h.timers.size, 1);
  await stale();
  assert.equal(h.calls.status, 1);
  assert.equal(h.timers.size, 1);
  h.flow.dispose();
  assert.equal(h.timers.size, 0);
});

test("nonce rejection is final and cannot fall back to another consume or auth bypass", async () => {
  let consumes = 0;
  const h = harness({
    loginStatus: async () => ({status: "approved"}),
    consumeLogin: async () => { consumes++; throw httpError(403); },
  });
  await h.flow.submit();
  await h.tick();
  await h.visibility(true);
  await h.visibility(false);
  assert.equal(consumes, 1);
  assert.equal(h.calls.session, 0);
  assert.equal(h.calls.authenticated, 0);
  assert.equal(h.state.loading, false);
});

test("HTTPS gate retains its explanation and never follows a server-provided URL", async () => {
  let starts = 0;
  const h = harness({loginStart: async (secret) => {
    starts++;
    assert.equal(secret, "test-only");
    throw httpError(400, {error: "https_required", loginUrl: "https://unfollowed.invalid/login"});
  }});
  await h.flow.submit();
  await h.online(false);
  await h.online(true);
  assert.equal(starts, 1);
  assert.match(h.messages[0], /HTTPS/);
  assert.equal(h.calls.status, 0);
  assert.equal(h.calls.authenticated, 0);
});

test("unrecognized server errors do not echo input credentials, URLs or raw error bodies", async () => {
  const h = harness({loginStart: async () => {
    throw httpError(400, {error: "test-only", message: "/login?nonce=test-only"});
  }});
  await h.flow.submit();
  assert.equal(h.messages.length, 1);
  assert.doesNotMatch(h.messages[0], /test-only|nonce|\/login/);
});

test("a fresh login page does not query status or authenticate without explicit submission", async () => {
  const h = harness();
  await h.event("pageshow");
  await h.online(false);
  await h.online(true);
  assert.deepEqual(h.calls, {start: 0, status: 0, consume: 0, session: 0, authenticated: 0});
  assert.equal(h.timers.size, 0);
});

test("disposing a queued retry clears its timer and stale callbacks are inert", async () => {
  const h = harness({loginStatus: async () => { throw networkError(); }});
  await h.flow.submit();
  await h.tick();
  const callback = [...h.timers.values()][0].fn;
  const snapshot = structuredClone(h.state);
  h.flow.dispose();
  await callback();
  assert.deepEqual(h.state, snapshot);
  assert.equal(h.timers.size, 0);
  assert.equal(h.messages.length, 0);
});
