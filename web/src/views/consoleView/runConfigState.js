const REQUIRED_RUN_CONFIG_FIELDS = [
  "conversationUuid",
  "model",
  "thinkingLevel",
  "effectiveThinkingLevel",
  "thinkingLevels",
  "defaultThinkingLevel",
  "supportsThinking",
  "fastMode",
  "fastRequested",
  "fastSupported",
  "effectiveFastMode",
  "agentRunConfig",
  "contextWindow",
  "rolloverTriggerTokens",
  "windowTriggerRatio",
];

/** Validate the complete server snapshot before it becomes the visible override. */
export function runConfigFromResponse(response, expectedConversationUuid = "") {
  const value = response?.runConfig;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (REQUIRED_RUN_CONFIG_FIELDS.some((field) => !Object.prototype.hasOwnProperty.call(value, field))) return null;
  if (!value.agentRunConfig || typeof value.agentRunConfig !== "object" || Array.isArray(value.agentRunConfig)) return null;
  const uuid = String(value.conversationUuid || "");
  if (!uuid || (expectedConversationUuid && uuid !== String(expectedConversationUuid))) return null;
  return value;
}

/** An override is visible only for the conversation whose save produced it. */
export function runConfigForDisplay(state, override, activeConversationUuid = "") {
  return override && String(override.conversationUuid || "") === String(activeConversationUuid || "")
    ? override
    : (state || {});
}

/**
 * Serialize mutations per conversation so rapid clicks reach the server in user
 * order. Different conversations remain independent, and late responses are
 * applied only while their source conversation is still active.
 */
export function createRunConfigSaveQueue(options = {}) {
  const tails = new Map();
  let appliedVersion = 0;

  function enqueue(conversationUuid, request) {
    const uuid = String(conversationUuid || "");
    if (!uuid || typeof request !== "function") return Promise.reject(new Error("conversation_required"));
    const scope = options.captureScope?.();
    const previous = tails.get(uuid) || Promise.resolve();
    const task = previous.then(async () => {
      const response = await request();
      const runConfig = runConfigFromResponse(response, uuid);
      if (!runConfig) throw new Error("run_config_missing");
      const applied = Boolean(options.isCurrent?.(uuid, scope));
      if (applied) {
        options.apply?.(runConfig, response);
        appliedVersion += 1;
      }
      return {response, runConfig, applied};
    });
    const tail = task.catch(() => {});
    tails.set(uuid, tail);
    void tail.finally(() => {
      if (tails.get(uuid) === tail) tails.delete(uuid);
    });
    return task;
  }

  return {
    enqueue,
    get appliedVersion() { return appliedVersion; },
  };
}

/** Only a state request started after the latest applied mutation may retire it. */
export function mayRetireRunConfigOverride(requestAppliedVersion, currentAppliedVersion) {
  return Number(requestAppliedVersion) === Number(currentAppliedVersion);
}
