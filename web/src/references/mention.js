// ProseMirror hardBreak is a leaf; capsules remain one atomic placeholder.
function literalAtCursor(before) {
  let fence = '', fenceLength = 0, code = 0, quote = '';
  for (const line of before.split('\n')) {
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence) {
      if (marker && marker[1][0] === fence && marker[1].length >= fenceLength && !marker[2].trim()) fence = '';
      continue;
    }
    if (marker) { fence = marker[1][0]; fenceLength = marker[1].length; code = 0; quote = ''; continue; }
    quote = '';
    for (let i = 0; i < line.length; i++) {
      const char = line[i];
      if (char === '\\') { i++; continue; }
      if (char === '`') {
        let end = i + 1;
        while (line[end] === '`') end++;
        const length = end - i;
        if (!code) code = length;
        else if (code === length) code = 0;
        i = end - 1;
        continue;
      }
      if (code) continue;
      if (quote) { if (char === quote) quote = ''; continue; }
      if (char === '“' || char === '‘') quote = char === '“' ? '”' : '’';
      else if ((char === '"' || char === "'") && !/[\p{L}\p{N}]/u.test(line[i - 1] || '')) quote = char;
    }
  }
  return Boolean(fence || code || quote);
}

export function mentionAtPosition(position) {
  const before = position.doc.textBetween(0, position.pos, '\n', node =>
    node.type.name === 'hardBreak' ? '\n' : '\uFFFC');
  if (literalAtCursor(before)) return null;
  // A mention is one search token, not the remainder of the sentence. Preserve
  // namespace '/', pinyin, IDs and '*' wildcards; prose separators end it.
  const match = /(?:^|[^\w.+%/@\\-])@([^\s@\uFFFC"'`“”‘’，。！？；：、,.!?;:()[\]{}<>《》【】\\]*)$/u.exec(before);
  if (!match) return null;
  const raw = match[1], prefix = /^(mem|doc|secret|chat)\//.exec(raw);
  return {from: position.pos - raw.length - 1, to: position.pos,
    query: prefix ? raw.slice(prefix[0].length) : raw, kind: prefix?.[1] || ''};
}
