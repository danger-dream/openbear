import test from "node:test";
import assert from "node:assert/strict";
import {createOutboundSendTracker, probeSocket, restoreOutboundDraft, waitForSocketOpen} from "./outboundSend.js";

function clock() {
  let time = 100;
  let seq = 0;
  const timers = new Map();
  return {
    timers,
    now: () => time,
    scheduleTimeout: (fn, delay) => { timers.set(++seq, {fn, due: time + delay}); return seq; },
    clearScheduledTimeout: (id) => timers.delete(id),
    advance: (ms, {run = true} = {}) => {
      time += ms;
      if (run) for (const [id, timer] of [...timers]) if (timer.due <= time) { timers.delete(id); timer.fn(); }
    },
  };
}

class Socket {
  constructor(readyState = 1) { this.readyState = readyState; this.listeners = new Map(); this.sent = []; }
  addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(fn); }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
  send(body) { this.sent.push(JSON.parse(body)); }
  emit(type, data) { for (const fn of [...(this.listeners.get(type) || [])]) fn({type, data}); }
  listenerCount() { return [...this.listeners.values()].reduce((n, items) => n + items.size, 0); }
}

function trackerHarness() {
  const timer = clock();
  const expired = [];
  const tracker = createOutboundSendTracker({...timer, prepareTimeoutMs: 100, ackTimeoutMs: 50, onTimeout: (item) => expired.push(item)});
  return {timer, expired, tracker};
}

test("lost ACK expires once and allows a new send; no content is retransmitted", () => {
  const {timer, expired, tracker} = trackerHarness();
  const pending = tracker.begin({requestId: "a", conversationUuid: "conv", draftText: "hello"});
  assert.equal(tracker.begin({requestId: "second"}), null);
  tracker.markSent(pending);
  timer.advance(50);
  assert.equal(tracker.current, null);
  assert.deepEqual(expired, [pending]);
  assert.equal(expired[0].phase, "sent");
  timer.advance(500);
  assert.equal(expired.length, 1);
  assert.ok(tracker.begin({requestId: "b"}));
});

test("successful ACK or explicit rejection clears timers; late ACK cannot release a newer request", () => {
  const {timer, tracker, expired} = trackerHarness();
  const first = tracker.begin({requestId: "first"});
  tracker.markSent(first);
  assert.equal(tracker.take("wrong"), null);
  assert.equal(tracker.take("first"), first);
  assert.equal(timer.timers.size, 0);
  const second = tracker.begin({requestId: "second"});
  assert.equal(tracker.take("first"), null);
  assert.equal(tracker.current, second);
  assert.equal(tracker.markSent(first), false);
  tracker.take("second");
  timer.advance(1000);
  assert.deepEqual(expired, []);
});

test("prepare timeout cancels delayed file/conversation work before it can send", () => {
  const {timer, tracker, expired} = trackerHarness();
  const pending = tracker.begin({requestId: "prepare", attachments: [{id: "file"}]});
  timer.advance(100);
  assert.equal(tracker.isCurrent(pending), false);
  assert.equal(tracker.markSent(pending), false);
  assert.equal(expired[0].phase, "preparing");
  assert.deepEqual(expired[0].attachments, [{id: "file"}]);
});

test("page resume checks elapsed deadline even when background timers were suspended", () => {
  const {timer, tracker, expired} = trackerHarness();
  const pending = tracker.begin({requestId: "hidden"});
  tracker.markSent(pending);
  timer.advance(1000, {run: false});
  assert.equal(tracker.current, pending);
  assert.equal(tracker.checkDeadline(), true);
  assert.equal(tracker.current, null);
  assert.equal(timer.timers.size, 0);
  assert.equal(expired.length, 1);
});

test("leaving a conversation takes pending work and invalidates all its asynchronous continuations", () => {
  const {timer, tracker, expired} = trackerHarness();
  const first = tracker.begin({requestId: "old", conversationUuid: "old-conv"});
  assert.equal(tracker.take(first.requestId), first);
  const next = tracker.begin({requestId: "new", conversationUuid: "new-conv"});
  assert.equal(tracker.isCurrent(first), false);
  assert.equal(tracker.isCurrent(next), true);
  tracker.take(next.requestId);
  timer.advance(1000);
  assert.deepEqual(expired, []);
});

test("recovering a draft preserves both original text and text typed while waiting", () => {
  assert.equal(restoreOutboundDraft("original", ""), "original");
  assert.equal(restoreOutboundDraft("original", "next"), "original\n\nnext");
  assert.equal(restoreOutboundDraft("original", "original"), "original");
  assert.equal(restoreOutboundDraft("", "next"), "next");
  assert.equal(restoreOutboundDraft("  original\n", ""), "  original\n");
});

test("OPEN sockets must answer application ping; half-open socket times out with clean listeners", async () => {
  const timer = clock();
  const socket = new Socket();
  const promise = probeSocket(socket, {...timer, timeoutMs: 50});
  const rejected = assert.rejects(promise, /ws_probe_timeout/);
  assert.deepEqual(socket.sent, [{type: "ping"}]);
  socket.emit("message", JSON.stringify({type: "frame"}));
  timer.advance(50);
  await rejected;
  assert.equal(socket.listenerCount(), 0);
  assert.equal(timer.timers.size, 0);
});

test("concurrent resume/send probes share one ping and leave the normal message listener intact", async () => {
  const timer = clock();
  const socket = new Socket();
  let messages = 0;
  socket.addEventListener("message", () => messages++);
  const first = probeSocket(socket, timer);
  const second = probeSocket(socket, timer);
  assert.equal(first, second);
  assert.equal(socket.sent.length, 1);
  socket.emit("message", "not json");
  socket.emit("message", JSON.stringify({type: "pong", ts: 1}));
  assert.equal(await first, socket);
  assert.equal(await second, socket);
  assert.equal(messages, 2);
  assert.equal(socket.listenerCount(), 1);
  assert.equal(timer.timers.size, 0);
});

test("connection closes before OPEN or while probing reject promptly without overriding socket handlers", async () => {
  const timer = clock();
  const connecting = new Socket(0);
  const originalOpen = () => {};
  connecting.onopen = originalOpen;
  const opened = waitForSocketOpen(connecting, timer);
  const openRejected = assert.rejects(opened, /ws_closed/);
  connecting.emit("close");
  await openRejected;
  assert.equal(connecting.onopen, originalOpen);
  assert.equal(connecting.listenerCount(), 0);
  assert.equal(timer.timers.size, 0);
  const socket = new Socket();
  const probing = probeSocket(socket, timer);
  const probeRejected = assert.rejects(probing, /ws_closed/);
  socket.emit("close");
  await probeRejected;
  assert.equal(socket.listenerCount(), 0);
});

test("fresh connection opening and its timeout both clean up listeners", async () => {
  const timer = clock();
  const socket = new Socket(0);
  const promise = waitForSocketOpen(socket, timer);
  socket.readyState = 1;
  socket.emit("open");
  assert.equal(await promise, socket);
  assert.equal(socket.listenerCount(), 0);
  assert.equal(timer.timers.size, 0);
  const stale = new Socket(0);
  const timeout = waitForSocketOpen(stale, {...timer, timeoutMs: 20});
  const rejected = assert.rejects(timeout, /ws_connect_timeout/);
  timer.advance(20);
  await rejected;
  assert.equal(stale.listenerCount(), 0);
  await assert.rejects(waitForSocketOpen(new Socket(3)), /ws_closed/);
});
