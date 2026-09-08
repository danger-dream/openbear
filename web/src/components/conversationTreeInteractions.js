// Drag targets describe a destination, never mutate pin state or creation time.
export function treeItemId(row) {
  return String(row?.kind === 'folder' ? row.folderId : row?.kind === 'conversation' ? row.conversationUuid : row?.id || '');
}
export function treeItemParent(row) {
  return String(row?.kind === 'folder' ? row.parentId || '' : row?.folderId || '');
}
export function sameTreeItem(a, b) {
  return Boolean(a && b && a.kind === b.kind && treeItemId(a) && treeItemId(a) === treeItemId(b));
}
export function compareTreeItems(a, b) {
  const kind = (a.kind === 'folder' ? 0 : 1) - (b.kind === 'folder' ? 0 : 1);
  if (kind) return kind;
  const pin = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
  if (pin) return pin;
  const am = a.displayOrder == null, bm = b.displayOrder == null;
  if (am !== bm) return am ? 1 : -1;
  return Number(a.displayOrder || 0) - Number(b.displayOrder || 0)
    || Number(b.createdAt || 0) - Number(a.createdAt || 0)
    || treeItemId(a).localeCompare(treeItemId(b));
}
function wouldCycle(source, targetFolderId, knownRows) {
  if (source.kind !== 'folder') return false;
  const parents = new Map(knownRows.filter(row => row.kind === 'folder').map(row => [treeItemId(row), treeItemParent(row)]));
  const seen = new Set();
  let current = String(targetFolderId || '');
  while (current && !seen.has(current)) {
    if (current === treeItemId(source)) return true;
    seen.add(current); current = parents.get(current) || '';
  }
  return false;
}
export function resolveTreeDrop(source, target, ratio, knownRows = []) {
  if (!source || !target || source.local || source.archived || target.local || target.archived || target.search
      || !['folder', 'conversation'].includes(source.kind) || sameTreeItem(source, target)) return null;
  let zone, parent;
  if (target.kind === 'root' && source.kind === 'folder') { zone = 'inside'; parent = ''; }
  else if (target.kind === 'system' && target.systemNode === 'temporary' && source.kind === 'conversation') { zone = 'inside'; parent = ''; }
  else if (target.kind === 'empty' && target.parentId) { zone = 'inside'; parent = String(target.parentId); }
  else if (target.kind === 'empty' && target.systemNode === 'temporary' && source.kind === 'conversation') { zone = 'inside'; parent = ''; }
  else if (target.kind === 'folder' && ratio > .28 && ratio < .72) { zone = 'inside'; parent = treeItemId(target); }
  else if (target.kind === source.kind && Boolean(target.pinned) === Boolean(source.pinned)) {
    zone = ratio <= .5 ? 'before' : 'after'; parent = treeItemParent(target);
  } else return null;
  if (wouldCycle(source, parent, knownRows)) return null;
  const sameParent = treeItemParent(source) === parent;
  if (zone === 'inside' && sameParent) return null;
  const beforeId = zone === 'after' ? treeItemId(target) : '';
  const afterId = zone === 'before' ? treeItemId(target) : '';
  if (sameParent && zone !== 'inside') {
    const unique = new Map(knownRows.filter(row => row.kind === source.kind && !row.archived && !row.local
      && treeItemParent(row) === parent && Boolean(row.pinned) === Boolean(source.pinned)).map(row => [treeItemId(row), row]));
    const ids = [...unique.values()].sort(compareTreeItems).map(treeItemId);
    const sourceIndex = ids.indexOf(treeItemId(source)), targetIndex = ids.indexOf(treeItemId(target));
    if (sourceIndex >= 0 && targetIndex >= 0) {
      const next = ids.filter(id => id !== treeItemId(source));
      next.splice(next.indexOf(treeItemId(target)) + (zone === 'after' ? 1 : 0), 0, treeItemId(source));
      if (next.every((id, index) => id === ids[index])) return null;
    }
  }
  return {zone, targetFolderId: parent, beforeId, afterId};
}
