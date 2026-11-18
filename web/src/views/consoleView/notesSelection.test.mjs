import assert from 'node:assert/strict';
import test from 'node:test';
import { buildWebSearchUrl, clampOverlayPosition, extractTranscriptSelection } from './notesSelection.js';

test('clampOverlayPosition keeps menus and selection bars inside viewport margins', () => {
  assert.deepEqual(clampOverlayPosition(-20, -10, 180, 150, 1280, 800, 10), { x: 10, y: 10 });
  assert.deepEqual(clampOverlayPosition(1250, 760, 180, 150, 1280, 800, 10), { x: 1090, y: 640 });
});

test('buildWebSearchUrl normalizes whitespace, encodes special characters and rejects blank input', () => {
  assert.equal(buildWebSearchUrl(''), '');
  assert.equal(buildWebSearchUrl('   \n\t  '), '');
  assert.equal(
    buildWebSearchUrl('  proxy_protocol on;\n  C++20 & 路由  '),
    'https://www.google.com/search?q=proxy_protocol%20on%3B%20C%2B%2B20%20%26%20%E8%B7%AF%E7%94%B1',
  );
  const huge = 'a'.repeat(1500);
  assert.equal(buildWebSearchUrl(huge), `https://www.google.com/search?q=${'a'.repeat(1000)}`);
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

test('search truncates by Unicode code point and safely repairs malformed surrogates', () => {
  const query = 'a'.repeat(999) + '😀' + 'tail';
  assert.equal(new URL(buildWebSearchUrl(query)).searchParams.get('q'), 'a'.repeat(999) + '😀');
  assert.equal(new URL(buildWebSearchUrl('𠀀'.repeat(1001))).searchParams.get('q'), '𠀀'.repeat(1000));
  assert.equal(new URL(buildWebSearchUrl('x\uD800y\uDC00z')).searchParams.get('q'), 'x�y�z');
});
