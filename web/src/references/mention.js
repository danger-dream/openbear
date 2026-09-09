// ProseMirror hardBreak is a leaf, not a block separator. Preserve its newline
// while keeping reference capsules atomic (one position, one placeholder).
// Include earlier paragraphs too: native paste/input can create paragraph nodes,
// and an opening code fence in one paragraph still applies to following ones.
export function mentionAtPosition(position) {
  const before = position.doc.textBetween(0, position.pos, '\n', node =>
    node.type.name === 'hardBreak' ? '\n' : '\uFFFC');
  if ((before.match(/^```/gm) || []).length % 2 === 1) return null;
  // Chinese text/punctuation and capsules may precede @ without a space.
  // ASCII email local parts and URL/path segments must not open the picker.
  const match = /(?:^|[^\w.+%/@\\-])@([^\n@\uFFFC]*)$/u.exec(before);
  if (!match) return null;
  const raw = match[1], prefix = /^(mem|doc|secret|chat)\//.exec(raw);
  return {from:position.pos - raw.length - 1,to:position.pos,
    query:prefix ? raw.slice(prefix[0].length) : raw,kind:prefix?.[1] || ''};
}
