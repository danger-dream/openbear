export function isLocalConversationRow(rowOrUuid) {
  const uuid = typeof rowOrUuid === "string" ? rowOrUuid : rowOrUuid?.conversationUuid;
  return Boolean(rowOrUuid?.local || String(uuid || "").startsWith("local:"));
}

export function isPinnedConversationRow(row) {
  return !isLocalConversationRow(row) && Boolean(row?.pinned);
}

function createdAtValue(row) {
  const value = Number(row?.createdAt ?? 0);
  return Number.isFinite(value) ? value : 0;
}

export function compareConversationCreatedAtDesc(left, right) {
  const leftCreatedAt = createdAtValue(left);
  const rightCreatedAt = createdAtValue(right);
  if (leftCreatedAt === rightCreatedAt) return 0;
  return rightCreatedAt > leftCreatedAt ? 1 : -1;
}

function displayOrderValue(row) {
  const raw = row?.displayOrder;
  if (raw === null || raw === undefined || raw === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

export function compareConversationDisplayOrder(left, right) {
  const leftOrder = displayOrderValue(left);
  const rightOrder = displayOrderValue(right);
  if (leftOrder !== null && rightOrder !== null && leftOrder !== rightOrder) return leftOrder - rightOrder;
  if (leftOrder !== null && rightOrder === null) return -1;
  if (leftOrder === null && rightOrder !== null) return 1;
  return compareConversationCreatedAtDesc(left, right);
}

export function normalizeConversationRows(rows = []) {
  const drafts = [];
  const pinned = [];
  const unpinned = [];
  for (const row of Array.isArray(rows) ? rows : []) {
    if (isLocalConversationRow(row)) drafts.push(row);
    else if (isPinnedConversationRow(row)) pinned.push(row);
    else unpinned.push(row);
  }
  // A rank is authoritative only inside its own pin group. Legacy rows without
  // one retain the existing createdAt/id order until they are first reindexed.
  pinned.sort(compareConversationDisplayOrder);
  unpinned.sort(compareConversationDisplayOrder);
  // A persisted pinned conversation is always at the visible top. The optional
  // local draft remains between the pinned and ordinary manually ordered groups.
  return [...pinned, ...drafts, ...unpinned];
}

export function mergeConversationRows(currentRows = [], apiRows = [], options = {}) {
  const drafts = (Array.isArray(currentRows) ? currentRows : []).filter(isLocalConversationRow);
  if (options.ensureLocal && drafts.length === 0 && typeof options.createLocalRow === "function") {
    drafts.push(options.createLocalRow());
  }

  const seen = new Set();
  const persisted = [];
  for (const row of Array.isArray(apiRows) ? apiRows : []) {
    if (isLocalConversationRow(row)) continue;
    const uuid = String(row?.conversationUuid || "");
    if (uuid && seen.has(uuid)) continue;
    if (uuid) seen.add(uuid);
    persisted.push(row);
  }
  return normalizeConversationRows([...drafts, ...persisted]);
}
