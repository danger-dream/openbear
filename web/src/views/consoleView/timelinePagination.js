function operationRevision(operation) {
  return Number(operation?.revision || 0) || 0;
}

function displaySeq(operation) {
  return Number(operation?.displaySeq || 0) || 0;
}

function operationOrder(operation) {
  return Number(operation?.operationOrder || 0) || 0;
}

function compareOperationPlacement(left, right) {
  const displayDifference = displaySeq(left) - displaySeq(right);
  if (displayDifference) return displayDifference;
  const leftOrder = operationOrder(left);
  const rightOrder = operationOrder(right);
  return leftOrder > 0 && rightOrder > 0 ? leftOrder - rightOrder : 0;
}

/**
 * Merge durable snapshot pages without letting a slower page request overwrite a
 * newer live-frame revision. Incoming server order is authoritative for every
 * operation it contains; updating an existing Map value keeps that position, so
 * an older row tied with an active higher-id extra cannot be reversed by the
 * current-first merge. The server's durable operationOrder preserves its
 * display_seq/id tie order; missing legacy metadata falls back to stable input order.
 */
export function mergeOperationSnapshots(current = [], incoming = []) {
  const byId = new Map();
  for (const operation of incoming || []) {
    if (!operation?.opId) continue;
    byId.set(operation.opId, operation);
  }
  for (const operation of current || []) {
    if (!operation?.opId) continue;
    const existing = byId.get(operation.opId);
    if (!existing || operationRevision(operation) > operationRevision(existing)) {
      byId.set(operation.opId, operation);
    }
  }
  return Array.from(byId.values()).sort(compareOperationPlacement);
}

/**
 * Decide whether one user input may request an older page. Programmatic scroll
 * always wins the guard. A recent input marker alone is insufficient: either a
 * wheel/touch handler must provide an explicit upward direction, or scrollTop
 * must have actually decreased (the scrollbar path).
 */
export function shouldRequestEarlierPage({
  programmaticScroll = false,
  userIntent = false,
  explicitUpward = false,
  previousScrollTop = null,
  scrollTop = 0,
  threshold = 0,
} = {}) {
  if (programmaticScroll || !userIntent) return false;
  const current = Number(scrollTop || 0);
  const previous = Number(previousScrollTop);
  const movedUp = previousScrollTop !== null
    && previousScrollTop !== undefined
    && Number.isFinite(previous)
    && current < previous;
  return Boolean((explicitUpward || movedUp) && current <= Number(threshold || 0));
}

/** A downward finger movement scrolls timeline content toward older rows. */
export function touchMovesTimelineUp(previousClientY, currentClientY) {
  if (previousClientY === null || previousClientY === undefined || currentClientY === null || currentClientY === undefined) return false;
  const previous = Number(previousClientY);
  const current = Number(currentClientY);
  return Number.isFinite(previous) && Number.isFinite(current) && current > previous;
}

export function sameTimelinePageRequest(current, candidate) {
  if (!current || !candidate) return false;
  return Number(current.generation) === Number(candidate.generation)
    && Number(current.token) === Number(candidate.token)
    && String(current.conversationUuid || "") === String(candidate.conversationUuid || "");
}

/** Only the matching request may clear the observable in-flight state. */
export function settleTimelinePageRequest(current, finished) {
  return sameTimelinePageRequest(current, finished) ? null : current;
}

/** Prefer the durable turn UUID, with the existing turn id as its legacy fallback. */
export function stableTurnIdentity(turn) {
  const turnUuid = String(turn?.turnUuid || "").trim();
  if (turnUuid) return {field: "turnUuid", value: turnUuid};
  const id = String(turn?.id || "").trim();
  return id ? {field: "id", value: id} : null;
}

export function findTurnIndexByIdentity(turns = [], identity = null) {
  if (!identity?.field || !identity?.value) return -1;
  return turns.findIndex((turn) => String(turn?.[identity.field] || "").trim() === identity.value);
}

export function capturePrependAnchor(scroller) {
  if (!scroller) return null;
  return {
    scrollTop: Number(scroller.scrollTop || 0),
    scrollHeight: Number(scroller.scrollHeight || 0),
  };
}

/** Keep the same content pixel anchored after older rows are prepended. */
export function prependAnchoredScrollTop(anchor, nextScrollHeight) {
  if (!anchor) return 0;
  const addedHeight = Math.max(0, Number(nextScrollHeight || 0) - Number(anchor.scrollHeight || 0));
  return Math.max(0, Number(anchor.scrollTop || 0) + addedHeight);
}
