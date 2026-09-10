export const SEND_PREPARE_TIMEOUT_MS = 30000;
export const SEND_ACK_TIMEOUT_MS = 15000;
export const SOCKET_OPEN_TIMEOUT_MS = 8000;
export const SOCKET_PROBE_TIMEOUT_MS = 4000;

// A browser send is not an acceptance receipt. Never retry message content here:
// the server may have accepted it even when the ACK was lost.
export function createOutboundSendTracker(options = {}) {
  const now = options.now || Date.now;
  const schedule = options.scheduleTimeout || ((fn, delay) => setTimeout(fn, delay));
  const cancel = options.clearScheduledTimeout || ((timer) => clearTimeout(timer));
  let current = null;
  let timer = null;

  function clearTimer() {
    if (timer !== null) cancel(timer);
    timer = null;
  }

  function take(requestId) {
    if (!current || current.requestId !== requestId) return null;
    const pending = current;
    current = null;
    clearTimer();
    return pending;
  }

  function checkDeadline() {
    if (!current || now() < current.deadlineAt) return false;
    const pending = take(current.requestId);
    options.onTimeout?.(pending);
    return true;
  }

  function arm(pending, timeoutMs) {
    clearTimer();
    pending.deadlineAt = now() + timeoutMs;
    timer = schedule(() => {
      timer = null;
      if (current === pending) checkDeadline();
    }, timeoutMs);
  }

  function begin(data) {
    if (current) return null;
    current = {...data, phase: "preparing"};
    arm(current, options.prepareTimeoutMs ?? SEND_PREPARE_TIMEOUT_MS);
    return current;
  }

  function markUploading(pending) {
    if (current !== pending) return false;
    clearTimer();
    pending.phase = "uploading";
    pending.deadlineAt = Infinity;
    return true;
  }

  function markPrepared(pending) {
    if (current !== pending) return false;
    pending.phase = "preparing";
    arm(pending, options.prepareTimeoutMs ?? SEND_PREPARE_TIMEOUT_MS);
    return true;
  }

  function markSent(pending) {
    if (current !== pending) return false;
    pending.phase = "sent";
    arm(pending, options.ackTimeoutMs ?? SEND_ACK_TIMEOUT_MS);
    return true;
  }

  return {
    begin,
    markUploading,
    markPrepared,
    markSent,
    take,
    checkDeadline,
    isCurrent: (pending) => Boolean(pending && current === pending),
    get current() { return current; },
  };
}

export function restoreOutboundDraft(sentDraft, currentDraft) {
  const sent = String(sentDraft || "");
  const current = String(currentDraft || "");
  if (!current.trim()) return sent;
  if (!sent.trim() || current === sent) return current;
  // Keep text typed while awaiting the receipt; neither draft may be discarded.
  // This is an editable recovery draft, never an automatically resent message.
  return `${sent}\n\n${current}`;
}

function socketWait(socket, {timeoutMs, start, accept, errorPrefix, scheduleTimeout, clearScheduledTimeout}) {
  const schedule = scheduleTimeout || ((fn, delay) => setTimeout(fn, delay));
  const cancel = clearScheduledTimeout || ((timer) => clearTimeout(timer));
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer = null;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      if (timer !== null) cancel(timer);
      socket.removeEventListener("open", onEvent);
      socket.removeEventListener("message", onEvent);
      socket.removeEventListener("error", onError);
      socket.removeEventListener("close", onClose);
      if (error) reject(error);
      else resolve(socket);
    };
    const onEvent = (event) => { if (accept(event)) finish(); };
    const onError = () => finish(new Error(`${errorPrefix}_failed`));
    const onClose = () => finish(new Error("ws_closed"));
    socket.addEventListener("open", onEvent);
    socket.addEventListener("message", onEvent);
    socket.addEventListener("error", onError);
    socket.addEventListener("close", onClose);
    timer = schedule(() => finish(new Error(`${errorPrefix}_timeout`)), timeoutMs);
    try { start?.(); } catch (error) { finish(error); }
  });
}

export function waitForSocketOpen(socket, options = {}) {
  if (!socket) return Promise.reject(new Error("ws_not_ready"));
  if (socket.readyState === 1) return Promise.resolve(socket);
  if (socket.readyState !== 0) return Promise.reject(new Error("ws_closed"));
  return socketWait(socket, {
    ...options,
    timeoutMs: options.timeoutMs ?? SOCKET_OPEN_TIMEOUT_MS,
    errorPrefix: "ws_connect",
    accept: (event) => event.type === "open",
  });
}

const probes = new WeakMap();
export function probeSocket(socket, options = {}) {
  if (!socket || socket.readyState !== 1) return Promise.reject(new Error("ws_not_open"));
  if (probes.has(socket)) return probes.get(socket);
  const promise = socketWait(socket, {
    ...options,
    timeoutMs: options.timeoutMs ?? SOCKET_PROBE_TIMEOUT_MS,
    errorPrefix: "ws_probe",
    // The existing server already supports application ping/pong. A native
    // WebSocket OPEN flag alone cannot detect a half-open idle connection.
    start: () => socket.send(JSON.stringify({type: "ping"})),
    accept: (event) => {
      if (event.type !== "message") return false;
      try { return JSON.parse(event.data || "{}").type === "pong"; }
      catch { return false; }
    },
  }).finally(() => probes.delete(socket));
  probes.set(socket, promise);
  return promise;
}
