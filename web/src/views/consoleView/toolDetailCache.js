import {inject} from "vue";

export const TOOL_DETAIL_CACHE_KEY = Symbol("openbear.console.toolDetailCache");

function cacheKey(conversationUuid, operationId, revision) {
  return `${String(conversationUuid || "")}\u0000${String(operationId || "")}\u0000${Number(revision || 0) || 0}`;
}

/**
 * Volatile, conversation-scoped detail cache. Nothing is written to browser
 * storage. Exact conversation/op/revision keys prevent stale sensitive payloads
 * from crossing either a revision or conversation boundary.
 */
export function createToolDetailCache() {
  const values = new Map();
  const inFlight = new Map();
  let activeConversationUuid = "";

  function reset(conversationUuid = "") {
    activeConversationUuid = String(conversationUuid || "");
    values.clear();
    inFlight.clear();
  }

  function ensureConversation(conversationUuid) {
    const next = String(conversationUuid || "");
    if (next !== activeConversationUuid) reset(next);
  }

  function pruneOperationRevisions(conversationUuid, operationId, keepKey) {
    const prefix = `${String(conversationUuid || "")}\u0000${String(operationId || "")}\u0000`;
    for (const key of values.keys()) {
      if (key.startsWith(prefix) && key !== keepKey) values.delete(key);
    }
    for (const key of inFlight.keys()) {
      if (key.startsWith(prefix) && key !== keepKey) inFlight.delete(key);
    }
  }

  async function load({conversationUuid, operationId, revision, loader}) {
    ensureConversation(conversationUuid);
    const key = cacheKey(conversationUuid, operationId, revision);
    pruneOperationRevisions(conversationUuid, operationId, key);
    if (values.has(key)) return values.get(key);
    if (inFlight.has(key)) return inFlight.get(key);
    const promise = Promise.resolve().then(loader).then((detail) => {
      // A conversation/revision switch invalidates this request even if the
      // transport cannot be cancelled at the browser level.
      if (
        String(conversationUuid || "") === activeConversationUuid
        && inFlight.get(key) === promise
        && Number(detail?.revision || 0) >= (Number(revision || 0) || 0)
      ) {
        values.set(key, detail);
        const actualKey = cacheKey(conversationUuid, operationId, detail?.revision);
        values.set(actualKey, detail);
      }
      return detail;
    }).finally(() => {
      if (inFlight.get(key) === promise) inFlight.delete(key);
    });
    inFlight.set(key, promise);
    return promise;
  }

  return {
    load,
    reset,
    get size() { return values.size; },
    get inFlightSize() { return inFlight.size; },
  };
}

export function useToolDetailCache() {
  return inject(TOOL_DETAIL_CACHE_KEY, null);
}
