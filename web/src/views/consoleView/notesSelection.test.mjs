import assert from 'node:assert/strict';
import test from 'node:test';
import { clampOverlayPosition, extractTranscriptSelection } from './notesSelection.js';

test('clampOverlayPosition keeps menus and selection bars inside viewport margins', () => {
  assert.deepEqual(clampOverlayPosition(-20, -10, 180, 150, 1280, 800, 10), { x: 10, y: 10 });
  assert.deepEqual(clampOverlayPosition(1250, 760, 180, 150, 1280, 800, 10), { x: 1090, y: 640 });
});

test('extractTranscriptSelection resolves turnNumber and opId from transcript DOM', () => {
  const rowEl = {
    nodeType: 1,
    getAttribute: (k) => (k === 'data-search-op-id' ? 'op-answer-6' : null),
  };
  const turnEl = {
    nodeType: 1,
    getAttribute: (k) => (k === 'data-turn-index' ? '5' : null),
    querySelectorAll: () => [rowEl],
    querySelector: () => rowEl,
  };
  const leafEl = {
    nodeType: 1,
    closest(sel) {
      if (sel.includes('input')) return null;
      if (sel === '.turn-block') return turnEl;
      if (sel === '[data-search-op-id]') return rowEl;
      return null;
    },
  };
  const scroller = { contains: (el) => el === leafEl };
  const selection = {
    isCollapsed: false,
    rangeCount: 1,
    toString: () => '  proxy_protocol on;  ',
    getRangeAt: () => ({
      startContainer: { nodeType: 3, parentElement: leafEl },
      endContainer: { nodeType: 3, parentElement: leafEl },
      getBoundingClientRect: () => ({ left: 100, top: 200, right: 260, bottom: 222, width: 160, height: 22 }),
    }),
  };

  const result = extractTranscriptSelection(selection, scroller);
  assert.deepEqual(result, {
    quote: 'proxy_protocol on;',
    opId: 'op-answer-6',
    turnNumber: 6,
    rect: { left: 100, top: 200, right: 260, bottom: 222, width: 160, height: 22 },
  });
});
