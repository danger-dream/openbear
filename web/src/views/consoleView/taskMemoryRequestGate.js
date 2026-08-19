function normalizeIdentity(identity = {}) {
  return {
    conversationUuid: String(identity.conversationUuid || ""),
    scopeType: String(identity.scopeType || "conversation"),
    taskUuid: String(identity.taskUuid || ""),
  };
}

export function sameTaskMemoryRequestIdentity(left, right) {
  const a = normalizeIdentity(left);
  const b = normalizeIdentity(right);
  return a.conversationUuid === b.conversationUuid
    && a.scopeType === b.scopeType
    && a.taskUuid === b.taskUuid;
}

export function createTaskMemoryRequestGate() {
  let active = true;
  let generation = 0;
  const channelSequences = new Map();

  function invalidate() {
    generation += 1;
    channelSequences.clear();
    return generation;
  }

  function capture(identity, channel = "default") {
    const normalizedChannel = String(channel || "default");
    const sequence = Number(channelSequences.get(normalizedChannel) || 0) + 1;
    channelSequences.set(normalizedChannel, sequence);
    return Object.freeze({
      ...normalizeIdentity(identity),
      channel: normalizedChannel,
      sequence,
      generation,
    });
  }

  function isCurrent(token, currentIdentity) {
    if (!active || !token || Number(token.generation) !== generation) return false;
    if (Number(channelSequences.get(String(token.channel || "default")) || 0) !== Number(token.sequence)) {
      return false;
    }
    return sameTaskMemoryRequestIdentity(token, currentIdentity);
  }

  function dispose() {
    active = false;
    invalidate();
  }

  return Object.freeze({capture, dispose, invalidate, isCurrent});
}
