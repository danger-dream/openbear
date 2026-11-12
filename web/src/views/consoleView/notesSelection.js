// Pure DOM-geometry & selection helpers for user sticky notes.

export function clampOverlayPosition(x, y, boxWidth, boxHeight, viewportWidth, viewportHeight, margin = 10) {
  const maxX = Math.max(margin, viewportWidth - boxWidth - margin);
  const maxY = Math.max(margin, viewportHeight - boxHeight - margin);
  return {
    x: Math.round(Math.min(Math.max(margin, x), maxX)),
    y: Math.round(Math.min(Math.max(margin, y), maxY)),
  };
}

function elementOf(node) {
  if (!node) return null;
  return node.nodeType === 1 ? node : node.parentElement || null;
}

export function extractTranscriptSelection(selection, scrollerEl) {
  if (!selection || selection.isCollapsed || !selection.rangeCount || !scrollerEl) return null;
  const rawText = String(selection.toString() || '').replace(/\r\n/g, '\n').trim();
  if (!rawText) return null;

  const range = selection.getRangeAt(0);
  const startEl = elementOf(range.startContainer);
  const endEl = elementOf(range.endContainer);
  if (!startEl || !endEl) return null;
  if (!scrollerEl.contains(startEl) || !scrollerEl.contains(endEl)) return null;

  // Ignore selections inside inputs, textareas or editable regions.
  if (startEl.closest('input, textarea, [contenteditable="true"]') || endEl.closest('input, textarea, [contenteditable="true"]')) {
    return null;
  }

  const turnEl = startEl.closest('.turn-block') || endEl.closest('.turn-block');
  if (!turnEl) return null;

  const turnIndexRaw = Number(turnEl.getAttribute('data-turn-index'));
  const turnNumber = Number.isFinite(turnIndexRaw) && turnIndexRaw >= 0 ? turnIndexRaw + 1 : 0;

  // Prefer the exact timed-row carrying data-search-op-id; otherwise fall back to the
  // nearest visible searchable row inside the same turn so jump-back always has an anchor.
  const directOpEl = startEl.closest('[data-search-op-id]') || endEl.closest('[data-search-op-id]');
  let opId = directOpEl?.getAttribute('data-search-op-id') || '';
  if (!opId) {
    const assistantRows = [...turnEl.querySelectorAll('.timed-row-assistant[data-search-op-id]')];
    const fallbackRow = assistantRows.at(-1) || turnEl.querySelector('[data-search-op-id]');
    opId = fallbackRow?.getAttribute('data-search-op-id') || '';
  }

  const rect = range.getBoundingClientRect();
  return {
    quote: rawText.slice(0, 8000),
    opId: String(opId || ''),
    turnNumber,
    rect: rect ? {
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      width: rect.width,
      height: rect.height,
    } : null,
  };
}
