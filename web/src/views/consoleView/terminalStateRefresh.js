export function createTerminalStateRefreshScheduler(options = {}) {
  const delayMs = Number(options.delayMs ?? 650);
  const scheduleTimeout = options.scheduleTimeout || ((callback, delay) => setTimeout(callback, delay));
  const clearScheduledTimeout = options.clearScheduledTimeout || ((handle) => clearTimeout(handle));
  let timer = null;
  let generation = 0;
  let disposed = false;

  const clearTimer = () => {
    if (timer === null) return;
    clearScheduledTimeout(timer);
    timer = null;
  };

  const isCurrent = (token) => {
    if (disposed || !token || token.generation !== generation || !options.isComponentActive?.()) return false;
    const conversationUuid = String(options.getConversationUuid?.() || "").trim();
    const socketConversationUuid = String(options.getSocketConversationUuid?.() || "").trim();
    const socket = options.getSocket?.();
    return Boolean(
      token.conversationUuid
      && token.conversationUuid === conversationUuid
      && token.conversationUuid === socketConversationUuid
      && token.socket
      && token.socket === socket
      && (options.isSocketActive?.(socket) ?? true)
    );
  };

  const invalidate = () => {
    generation += 1;
    clearTimer();
  };

  const schedule = (reason = {}) => {
    if (disposed) return false;
    invalidate();
    const token = {
      generation,
      conversationUuid: String(options.getConversationUuid?.() || "").trim(),
      socket: options.getSocket?.(),
    };
    if (!isCurrent(token)) return false;
    const timeoutHandle = scheduleTimeout(async () => {
      if (timer === timeoutHandle) timer = null;
      if (!isCurrent(token)) return;
      try {
        await options.refresh?.({
          conversationUuid: token.conversationUuid,
          socket: token.socket,
          reason,
          isCurrent: () => isCurrent(token),
        });
      } catch (error) {
        options.onError?.(error, reason);
      }
    }, delayMs);
    timer = timeoutHandle;
    return true;
  };

  const dispose = () => {
    if (disposed) return;
    disposed = true;
    invalidate();
  };

  return {schedule, invalidate, dispose};
}

export async function runGuardedConversationStateRefresh(options = {}) {
  const conversationUuid = String(options.conversationUuid || "").trim();
  const isCurrent = typeof options.isCurrent === "function" ? options.isCurrent : () => false;
  if (!conversationUuid || !isCurrent()) return {stage: "before-request", applied: false, connected: false};

  const state = await options.requestState(conversationUuid);
  if (!isCurrent()) return {stage: "after-request", applied: false, connected: false};

  options.applyState(state, conversationUuid);
  if (!isCurrent()) return {stage: "before-connect", applied: true, connected: false};

  await options.connectState(conversationUuid);
  if (!isCurrent()) return {stage: "after-connect", applied: true, connected: true};
  return {stage: "complete", applied: true, connected: true};
}
