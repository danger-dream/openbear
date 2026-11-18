import MarkdownIt from 'markdown-it';
import cjkFriendly from 'markdown-it-cjk-friendly';
import texmath from 'markdown-it-texmath';
import katex from 'katex';
import { referenceFromUrl, referenceUrl } from '../references/codec.js';

const mdParser = new MarkdownIt('default', {
  html: false,
  linkify: true,
  breaks: false,
  typographer: false,
});
mdParser.enable(['table', 'strikethrough']);
// Same emphasis and math recognition as the chat renderer. texmath keeps a
// process-wide engine, so it must receive the real KaTeX here as well.
mdParser.use(cjkFriendly);
mdParser.use(texmath, { engine: katex, delimiters: ['dollars'] });

function unescapeMemoLabel(raw) {
  return String(raw || '').replace(/\\([[\]\\|])/g, '$1');
}

function unescapeMemoBody(raw) {
  let out = '';
  for (let i = 0; i < raw.length; i += 1) {
    if (raw[i] === '\\' && i + 1 < raw.length) {
      const next = raw[i + 1];
      if (next === 'n') out += '\n';
      else out += next;
      i += 1;
    } else {
      out += raw[i];
    }
  }
  return out;
}

export function escapeMemoLabel(text = '') {
  return String(text || '')
    .replace(/\\/g, '\\\\')
    .replace(/\[/g, '\\[')
    .replace(/\]/g, '\\]')
    .replace(/\|/g, '\\|')
    .replace(/\r?\n/g, ' ');
}

export function escapeMemoBody(text = '') {
  return String(text || '')
    .replace(/\\/g, '\\\\')
    .replace(/[()]/g, '\\$&')
    .replace(/\|/g, '\\|')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/\n/g, '\\n');
}

// Inline markup content is parsed like any other inline text, so nesting and
// backslash escapes behave the same inside <u>, ==, ^, ~ and <span>.
function pushWrapped(state, name, tag, from, to, attrs = []) {
  const open = state.push(`${name}_open`, tag, 1);
  for (const [key, value] of attrs) open.attrSet(key, value);
  const oldPos = state.pos;
  const oldMax = state.posMax;
  state.pos = from;
  state.posMax = to;
  state.md.inline.tokenize(state);
  state.pos = oldPos;
  state.posMax = oldMax;
  state.push(`${name}_close`, tag, -1);
}

function findClosing(src, from, max, marker, accept = () => true) {
  for (let p = from; p < max; p += 1) {
    const ch = src.charCodeAt(p);
    if (ch === 0x5c /* \ */) {
      p += 1;
      continue;
    }
    if (ch === 0x0a) return -1;
    if (src.startsWith(marker, p) && accept(p)) return p;
  }
  return -1;
}

// 1. [label]^(memo) rule (before link!)
mdParser.inline.ruler.before('link', 'sy_inline_memo', (state, silent) => {
  const src = state.src;
  const max = state.posMax;
  const start = state.pos;
  if (src.charCodeAt(start) !== 0x5b /* [ */) return false;

  let p = start + 1;
  let foundBracket = -1;
  while (p < max) {
    const ch = src.charCodeAt(p);
    if (ch === 0x5c /* \ */) {
      p += 2;
      continue;
    }
    if (ch === 0x0a /* \n */) return false;
    if (ch === 0x5d /* ] */) {
      if (p + 2 < max && src.charCodeAt(p + 1) === 0x5e /* ^ */ && src.charCodeAt(p + 2) === 0x28 /* ( */) {
        foundBracket = p;
        break;
      }
    }
    p += 1;
  }
  if (foundBracket <= start + 0) return false;

  const memoStart = foundBracket + 3;
  let q = memoStart;
  let parenDepth = 0;
  let memoEnd = -1;
  while (q < max) {
    const ch = src.charCodeAt(q);
    if (ch === 0x5c /* \ */) {
      q += 2;
      continue;
    }
    if (ch === 0x28 /* ( */) {
      parenDepth += 1;
    } else if (ch === 0x29 /* ) */) {
      if (parenDepth === 0) {
        memoEnd = q;
        break;
      }
      parenDepth -= 1;
    }
    q += 1;
  }
  if (memoEnd < 0) return false;

  if (!silent) {
    const labelText = unescapeMemoLabel(src.slice(start + 1, foundBracket));
    const memoText = unescapeMemoBody(src.slice(memoStart, memoEnd));
    const openTok = state.push('memo_open', 'span', 1);
    openTok.attrSet('data-memo', memoText);
    const txtTok = state.push('text', '', 0);
    txtTok.content = labelText;
    state.push('memo_close', 'span', -1);
  }
  state.pos = memoEnd + 1;
  return true;
});

// 2. Safe inline tags: <br>, <u>...</u>, <kbd>...</kbd>, <mark>, <sup>, <sub>, <span data-color/data-bg>
mdParser.inline.ruler.before('escape', 'sy_safe_inline_tags', (state, silent) => {
  const src = state.src;
  const start = state.pos;
  const max = state.posMax;
  if (src.charCodeAt(start) !== 0x3c /* < */) return false;
  const tail = src.slice(start, max);

  const brMatch = tail.match(/^<br\s*\/?>/i);
  if (brMatch) {
    if (!silent) state.push('hardbreak', 'br', 0);
    state.pos += brMatch[0].length;
    return true;
  }

  const spanMatch = tail.match(/^<span(?:\s+data-color="([#a-zA-Z0-9(),.\s%-]{1,48})")?(?:\s+data-bg="([#a-zA-Z0-9(),.\s%-]{1,48})")?\s*>/i);
  if (spanMatch && (spanMatch[1] || spanMatch[2])) {
    const innerStart = start + spanMatch[0].length;
    const close = src.toLowerCase().indexOf('</span>', innerStart);
    if (close < 0 || close <= innerStart || close + 7 > max) return false;
    if (!silent) {
      const attrs = [];
      if (spanMatch[1]) attrs.push(['data-color', spanMatch[1]]);
      if (spanMatch[2]) attrs.push(['data-bg', spanMatch[2]]);
      pushWrapped(state, 'textstyle', 'span', innerStart, close, attrs);
    }
    state.pos = close + 7;
    return true;
  }

  const tagMatch = tail.match(/^<(u|kbd|mark|sup|sub)>/i);
  if (tagMatch) {
    const tag = tagMatch[1].toLowerCase();
    const innerStart = start + tagMatch[0].length;
    const closeTag = `</${tag}>`;
    const close = src.toLowerCase().indexOf(closeTag, innerStart);
    if (close < 0 || close <= innerStart || close + closeTag.length > max) return false;
    if (!silent) pushWrapped(state, tag, tag, innerStart, close);
    state.pos = close + closeTag.length;
    return true;
  }
  return false;
});

// 3. ==highlight==, ^sup^, ~sub~
mdParser.inline.ruler.after('strikethrough', 'sy_extra_marks', (state, silent) => {
  const src = state.src;
  const start = state.pos;
  const max = state.posMax;
  const ch = src.charCodeAt(start);
  const isSpace = (pos) => /\s/.test(src[pos] || '');

  // ==highlight==
  if (ch === 0x3d /* = */ && src.charCodeAt(start + 1) === 0x3d && start + 2 < max && !isSpace(start + 2) && src.charCodeAt(start + 2) !== 0x3d) {
    const close = findClosing(src, start + 2, max, '==', (p) => !isSpace(p - 1));
    if (close > start + 2) {
      if (!silent) pushWrapped(state, 'mark', 'mark', start + 2, close);
      state.pos = close + 2;
      return true;
    }
  }

  // ^superscript^ (not followed by `(`)
  if (ch === 0x5e /* ^ */ && start + 2 < max && src.charCodeAt(start + 1) !== 0x28 && !isSpace(start + 1)) {
    const close = findClosing(src, start + 1, max, '^', (p) => !isSpace(p - 1));
    if (close > start + 1) {
      if (!silent) pushWrapped(state, 'sup', 'sup', start + 1, close);
      state.pos = close + 1;
      return true;
    }
  }

  // ~subscript~ (single tilde, not ~~strikethrough~~)
  if (ch === 0x7e /* ~ */ && start + 2 < max && src.charCodeAt(start + 1) !== 0x7e && !isSpace(start + 1)) {
    const close = findClosing(src, start + 1, max, '~', (p) => !isSpace(p - 1) && src.charCodeAt(p + 1) !== 0x7e);
    if (close > start + 1) {
      if (!silent) pushWrapped(state, 'sub', 'sub', start + 1, close);
      state.pos = close + 1;
      return true;
    }
  }

  return false;
});

const TASK_ITEM_RE = /^\[([ xX])\]\s+([\s\S]*)$/;
const TASK_SOURCE_RE = /^\[[ xX]\](?:\s|$)/;
const INLINE_MATH_CLOSE = { $: '$', $$: '$$' };
// Raw inline source of a paragraph, used where unescaped text is ambiguous.
const paragraphSource = new WeakMap();

function parseAlignment(token) {
  const style = token?.attrGet?.('style') || '';
  const m = style.match(/text-align:\s*(left|center|right)/i);
  return m ? m[1].toLowerCase() : null;
}

function inlineTokensToNodes(tokens = []) {
  const result = [];
  const markStack = [];

  const currentMarks = () => markStack.map((m) => ({ ...m }));
  const pushText = (text) => {
    if (!text) return;
    const marks = currentMarks();
    const prev = result[result.length - 1];
    if (prev && prev.type === 'text' && JSON.stringify(prev.marks || []) === JSON.stringify(marks)) {
      prev.text += text;
    } else {
      result.push(marks.length ? { type: 'text', text, marks } : { type: 'text', text });
    }
  };
  const pushAtom = (node) => {
    const marks = currentMarks();
    result.push(marks.length ? { ...node, marks } : node);
  };
  const closeMark = (type) => {
    for (let k = markStack.length - 1; k >= 0; k -= 1) {
      if (markStack[k].type === type) { markStack.splice(k, 1); break; }
    }
  };
  const SIMPLE_MARKS = {
    strong: 'bold',
    em: 'italic',
    s: 'strike',
    u: 'underline',
    mark: 'highlight',
    sup: 'superscript',
    sub: 'subscript',
    kbd: 'kbd',
  };

  for (let i = 0; i < tokens.length; i += 1) {
    const tok = tokens[i];
    const simple = /^(.*)_(open|close)$/.exec(tok.type);
    if (simple && SIMPLE_MARKS[simple[1]]) {
      if (simple[2] === 'open') markStack.push({ type: SIMPLE_MARKS[simple[1]] });
      else closeMark(SIMPLE_MARKS[simple[1]]);
      continue;
    }
    switch (tok.type) {
      case 'text':
        pushText(tok.content);
        break;
      case 'code_inline': {
        const marks = [...currentMarks(), { type: 'code' }];
        if (tok.content) {
          result.push({ type: 'text', text: tok.content, marks });
        }
        break;
      }
      case 'softbreak':
        result.push({ type: 'hardBreak', attrs: { soft: true } });
        break;
      case 'hardbreak':
        result.push({ type: 'hardBreak' });
        break;
      case 'math_inline':
      case 'math_inline_double': {
        const open = tok.markup || '$';
        pushAtom({ type: 'rawInline', attrs: { source: `${open}${tok.content}${INLINE_MATH_CLOSE[open] || open}` } });
        break;
      }
      case 'memo_open':
        markStack.push({ type: 'inlineMemo', attrs: { memo: tok.attrGet('data-memo') || '' } });
        break;
      case 'memo_close':
        closeMark('inlineMemo');
        break;
      case 'textstyle_open':
        markStack.push({
          type: 'textStyle',
          attrs: {
            color: tok.attrGet('data-color') || '',
            bg: tok.attrGet('data-bg') || '',
          },
        });
        break;
      case 'textstyle_close':
        closeMark('textStyle');
        break;
      case 'link_open': {
        const href = tok.attrGet('href') || '';
        const title = tok.attrGet('title') || '';
        // Check if this is a single openbear://ref/... link
        let closeIdx = -1;
        let plainLabel = '';
        for (let j = i + 1; j < tokens.length; j += 1) {
          if (tokens[j].type === 'link_close') { closeIdx = j; break; }
          if (tokens[j].type === 'text' || tokens[j].type === 'code_inline') plainLabel += tokens[j].content;
        }
        const refObj = href.startsWith('openbear://ref/') ? referenceFromUrl(href, plainLabel) : null;
        if (refObj && closeIdx > i) {
          result.push({ type: 'reference', attrs: refObj });
          i = closeIdx;
        } else {
          markStack.push({ type: 'link', attrs: { href, title: title || null } });
        }
        break;
      }
      case 'link_close':
        closeMark('link');
        break;
      case 'image': {
        const src = tok.attrGet('src') || '';
        const alt = tok.content || (tok.children || []).map((c) => c.content || '').join('') || '';
        const title = tok.attrGet('title') || '';
        pushAtom({ type: 'inlineImage', attrs: { src, alt, title } });
        break;
      }
      default:
        if (tok.content) pushText(tok.content);
        break;
    }
  }
  return result;
}

function isStandaloneImageInline(inlineToken) {
  const children = (inlineToken?.children || []).filter((t) => !(t.type === 'text' && !t.content.trim()));
  return children.length === 1 && children[0].type === 'image' ? children[0] : null;
}

function stripTaskPrefixFromInline(paragraph) {
  // `\[ \] x` is literal text, not a task; only the unescaped source decides.
  if (!TASK_SOURCE_RE.test(paragraphSource.get(paragraph) || '')) return null;
  const inlineNodes = paragraph.content || [];
  if (!inlineNodes.length || inlineNodes[0].type !== 'text') return null;
  const firstText = inlineNodes[0].text || '';
  const match = firstText.match(TASK_ITEM_RE) || firstText.match(/^\[([ xX])\]()$/);
  if (!match) return null;
  const checked = match[1].toLowerCase() === 'x';
  const restText = match[2];
  const nextNodes = [...inlineNodes];
  if (restText) {
    nextNodes[0] = { ...nextNodes[0], text: restText };
  } else {
    nextNodes.shift();
  }
  return { checked, content: nextNodes };
}

function rawMathBlock(tok, sourceLines) {
  const open = tok.tag || '$$';
  const eqno = tok.type === 'math_block_eqno' && tok.info ? ` (${tok.info})` : '';
  const raw = `${open}${tok.content}${open}${eqno}`;
  // Inside lists the captured content keeps the container's indentation;
  // the serializer adds it back, so store the block relative to its column.
  const firstLine = sourceLines[tok.map?.[0] ?? -1] || '';
  const indent = (firstLine.match(/^[ \t]*/) || [''])[0].length;
  if (!indent) return raw;
  const pad = new RegExp(`^[ \\t]{0,${indent}}`);
  return raw.split('\n').map((line, idx) => (idx === 0 ? line : line.replace(pad, ''))).join('\n');
}

function parseBlocks(tokens, startIdx = 0, stopType = null, sourceLines = []) {
  const blocks = [];
  let i = startIdx;

  while (i < tokens.length) {
    const tok = tokens[i];
    if (stopType && tok.type === stopType) {
      return { blocks, nextIndex: i + 1 };
    }

    if (tok.type === 'heading_open') {
      const level = Math.max(1, Math.min(6, Number(tok.tag.slice(1)) || 1));
      const inlineTok = tokens[i + 1];
      const content = inlineTok?.type === 'inline' ? inlineTokensToNodes(inlineTok.children || []) : [];
      blocks.push({
        type: 'heading',
        attrs: { level },
        ...(content.length ? { content } : {}),
      });
      i += 3;
      continue;
    }

    if (tok.type === 'paragraph_open') {
      const inlineTok = tokens[i + 1];
      const soloImg = inlineTok?.type === 'inline' ? isStandaloneImageInline(inlineTok) : null;
      if (soloImg) {
        const src = soloImg.attrGet('src') || '';
        const alt = soloImg.content || (soloImg.children || []).map((c) => c.content || '').join('') || '';
        const title = soloImg.attrGet('title') || '';
        blocks.push({
          type: 'imageBlock',
          attrs: { src, alt, title },
        });
      } else {
        const content = inlineTok?.type === 'inline' ? inlineTokensToNodes(inlineTok.children || []) : [];
        const paragraph = {
          type: 'paragraph',
          ...(content.length ? { content } : {}),
        };
        paragraphSource.set(paragraph, inlineTok?.content || '');
        blocks.push(paragraph);
      }
      i += 3;
      continue;
    }

    if (tok.type === 'fence' || tok.type === 'code_block') {
      const info = String(tok.info || '').trim().split(/\s+/)[0] || '';
      const rawCode = String(tok.content || '').replace(/\n$/, '');
      blocks.push({
        type: 'codeBlock',
        attrs: { language: info.toLowerCase() },
        ...(rawCode ? { content: [{ type: 'text', text: rawCode }] } : {}),
      });
      i += 1;
      continue;
    }

    if (tok.type === 'math_block' || tok.type === 'math_block_eqno') {
      blocks.push({ type: 'rawBlock', attrs: { source: rawMathBlock(tok, sourceLines), kind: 'math' } });
      i += 1;
      continue;
    }

    if (tok.type === 'hr') {
      blocks.push({ type: 'horizontalRule' });
      i += 1;
      continue;
    }

    if (tok.type === 'blockquote_open') {
      const inner = parseBlocks(tokens, i + 1, 'blockquote_close', sourceLines);
      blocks.push({
        type: 'blockquote',
        content: inner.blocks.length ? inner.blocks : [{ type: 'paragraph' }],
      });
      i = inner.nextIndex;
      continue;
    }

    if (tok.type === 'bullet_list_open' || tok.type === 'ordered_list_open') {
      const isOrdered = tok.type === 'ordered_list_open';
      const closeType = isOrdered ? 'ordered_list_close' : 'bullet_list_close';
      const startAttr = Number(tok.attrGet('start') || 1);
      const rawItems = [];
      i += 1;
      while (i < tokens.length && tokens[i].type !== closeType) {
        if (tokens[i].type === 'list_item_open') {
          const itemRes = parseBlocks(tokens, i + 1, 'list_item_close', sourceLines);
          const itemBlocks = itemRes.blocks.length ? itemRes.blocks : [{ type: 'paragraph' }];
          if (itemBlocks[0]?.type === 'imageBlock') {
            itemBlocks[0] = {
              type: 'paragraph',
              content: [{ type: 'inlineImage', attrs: itemBlocks[0].attrs }],
            };
          }
          rawItems.push(itemBlocks);
          i = itemRes.nextIndex;
        } else {
          i += 1;
        }
      }
      if (i < tokens.length && tokens[i].type === closeType) i += 1;

      if (!isOrdered && rawItems.length > 0) {
        const parsedTasks = rawItems.map((itemBlocks) => {
          const first = itemBlocks[0];
          if (first?.type !== 'paragraph') return null;
          const stripped = stripTaskPrefixFromInline(first);
          if (!stripped) return null;
          const updatedFirst = {
            type: 'paragraph',
            ...(stripped.content.length ? { content: stripped.content } : {}),
          };
          return {
            type: 'taskItem',
            attrs: { checked: stripped.checked },
            content: [updatedFirst, ...itemBlocks.slice(1)],
          };
        });
        if (parsedTasks.every(Boolean)) {
          blocks.push({ type: 'taskList', content: parsedTasks });
          continue;
        }
      }

      const listNode = {
        type: isOrdered ? 'orderedList' : 'bulletList',
        ...(isOrdered && startAttr !== 1 ? { attrs: { start: startAttr } } : {}),
        content: rawItems.map((itemBlocks) => ({
          type: 'listItem',
          content: itemBlocks,
        })),
      };
      blocks.push(listNode);
      continue;
    }

    if (tok.type === 'table_open') {
      const rows = [];
      i += 1;
      while (i < tokens.length && tokens[i].type !== 'table_close') {
        if (tokens[i].type === 'tr_open') {
          const cells = [];
          i += 1;
          while (i < tokens.length && tokens[i].type !== 'tr_close') {
            if (tokens[i].type === 'th_open' || tokens[i].type === 'td_open') {
              const isHeader = tokens[i].type === 'th_open';
              const closeCellType = isHeader ? 'th_close' : 'td_close';
              const textAlign = parseAlignment(tokens[i]);
              const inlineTok = tokens[i + 1];
              const inlineContent = inlineTok?.type === 'inline'
                ? inlineTokensToNodes(inlineTok.children || [])
                : [];
              cells.push({
                type: isHeader ? 'tableHeader' : 'tableCell',
                attrs: { colspan: 1, rowspan: 1, colwidth: null, textAlign },
                content: [
                  {
                    type: 'paragraph',
                    ...(inlineContent.length ? { content: inlineContent } : {}),
                  },
                ],
              });
              while (i < tokens.length && tokens[i].type !== closeCellType) i += 1;
              if (i < tokens.length) i += 1;
            } else {
              i += 1;
            }
          }
          if (i < tokens.length && tokens[i].type === 'tr_close') i += 1;
          if (cells.length) rows.push({ type: 'tableRow', content: cells });
        } else {
          i += 1;
        }
      }
      if (i < tokens.length && tokens[i].type === 'table_close') i += 1;
      if (rows.length) {
        blocks.push({ type: 'table', content: rows });
      }
      continue;
    }

    i += 1;
  }

  return { blocks, nextIndex: i };
}

// YAML/TOML-style metadata at the very top is kept verbatim as a raw block.
const FRONT_MATTER_RE = /^\ufeff?---\n(?=[A-Za-z0-9_-]+[ \t]*:)(?:[^\n]*\n)*?(?:---|\.\.\.)[ \t]*(?=\n|$)/;

export function markdownToDoc(markdown = '') {
  let source = String(markdown || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const blocks = [];
  const frontMatter = FRONT_MATTER_RE.exec(source);
  if (frontMatter) {
    blocks.push({ type: 'rawBlock', attrs: { source: frontMatter[0].replace(/^\ufeff/, ''), kind: 'frontmatter' } });
    source = source.slice(frontMatter[0].length);
  }
  if (source.trim()) {
    blocks.push(...parseBlocks(mdParser.parse(source, {}), 0, null, source.split('\n')).blocks);
  }
  return {
    type: 'doc',
    content: blocks.length ? blocks : [{ type: 'paragraph' }],
  };
}

const ASCII_PUNCT_RE = /[!-/:-@[-`{-~]/;
const WORD_CHAR_RE = /[\p{L}\p{N}]/u;
// Only `<` that our parser would read as a tag or an autolink needs escaping.
const TAG_OR_AUTOLINK_RE = /^(?:\/?(?:u|kbd|mark|sup|sub|span|br)\b|[A-Za-z][A-Za-z0-9+.-]{1,31}:|[^\s@<>]+@[^\s@<>]+>)/i;

// `a == b` cannot open ==highlight==; only a run touching following text can.
function equalsRunMayOpen(text, index) {
  let end = index;
  while (text[end] === '=') end += 1;
  return end >= text.length || !/\s/.test(text[end]);
}

// Escape only what our parser could otherwise read as markup, so prose stays readable.
function escapeText(text, { inTable = false } = {}) {
  const dollarPair = (text.match(/\$/g) || []).length >= 2;
  let out = '';
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const prev = text[i - 1] || '';
    const next = text[i + 1] || '';
    if (ch === '\\') out += !next || ASCII_PUNCT_RE.test(next) ? '\\\\' : '\\';
    else if ('`*[]~^'.includes(ch)) out += `\\${ch}`;
    else if (ch === '_') out += WORD_CHAR_RE.test(prev) && WORD_CHAR_RE.test(next) ? '_' : '\\_';
    else if (ch === '=' && (prev === '=' || next === '=') && equalsRunMayOpen(text, i)) out += '\\=';
    else if (ch === '<' && TAG_OR_AUTOLINK_RE.test(text.slice(i + 1))) out += '\\<';
    else if (ch === '&' && /^#?[A-Za-z0-9]+;/.test(text.slice(i + 1))) out += '\\&';
    else if (ch === '$' && dollarPair) out += '\\$';
    else if (ch === '|' && inTable) out += '\\|';
    else if (ch === '\n' && inTable) out += '<br>';
    else out += ch;
  }
  return out;
}

const TABLE_DELIMITER_ROW_RE = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

// Text at the start of a line must not turn into a heading, quote, list,
// thematic break, setext underline or table delimiter row.
function escapeLineStart(line) {
  if (line.includes('|') && TABLE_DELIMITER_ROW_RE.test(line)) line = line.replace(/\|/g, '\\|');
  return line
    .replace(/^(\s*)(#{1,6})(?=\s|$)/, '$1\\$2')
    .replace(/^(\s*)>/, '$1\\>')
    .replace(/^(\s*)([-+])(?=\s|$)/, '$1\\$2')
    .replace(/^(\s*)(-{2,}|={1,})(\s*)$/, '$1\\$2$3')
    .replace(/^(\s*)(\d{1,9})([.)])(?=\s|$)/, '$1$2\\$3');
}

const SAFE_LINK_SCHEMES = new Set(['http', 'https', 'mailto', 'tel', 'openbear']);

// Pasted HTML and typed URLs bypass markdown-it's link validation, so a
// script-capable scheme must be refused in the editor. Relative links are allowed.
export function safeLinkHref(href) {
  const value = String(href || '').trim();
  if (!value) return null;
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(value.replace(/[\u0000-\u0020\u007f]/g, ''));
  return !scheme || SAFE_LINK_SCHEMES.has(scheme[1].toLowerCase()) ? value : null;
}

function escapeLinkLabel(text) {
  return String(text || '').replace(/([[\]\\])/g, '\\$1');
}

// markdown-it percent-encodes destinations; write non-ASCII text back readable
// (it re-encodes on parse). ASCII escapes such as %25 or %20 stay as they are.
function readableUrl(url) {
  return url.replace(/(?:%[89a-f][0-9a-f])+/gi, (seq) => {
    try {
      return decodeURIComponent(seq);
    } catch {
      return seq;
    }
  });
}

function linkDestination(url) {
  const raw = readableUrl(String(url || ''));
  if (/[\s()<>]/.test(raw)) return `<${raw.replace(/[<>]/g, (c) => encodeURIComponent(c))}>`;
  return raw;
}

function linkTitle(title) {
  return title ? ` "${String(title).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"` : '';
}

const MARK_ORDER = ['link', 'bold', 'italic', 'strike', 'highlight', 'underline', 'superscript', 'subscript', 'kbd', 'textStyle'];

function sameMark(a, b) {
  return a.type === b.type && JSON.stringify(a.attrs || {}) === JSON.stringify(b.attrs || {});
}

function openMarkup(mark) {
  switch (mark.type) {
    case 'link': return '[';
    case 'bold': return '**';
    case 'italic': return '*';
    case 'strike': return '~~';
    case 'highlight': return '==';
    case 'underline': return '<u>';
    case 'superscript': return '^';
    case 'subscript': return '~';
    case 'kbd': return '<kbd>';
    case 'textStyle': {
      const color = mark.attrs?.color ? ` data-color="${mark.attrs.color}"` : '';
      const bg = mark.attrs?.bg ? ` data-bg="${mark.attrs.bg}"` : '';
      return `<span${color}${bg}>`;
    }
    default: return '';
  }
}

function closeMarkup(mark) {
  switch (mark.type) {
    case 'link': return `](${linkDestination(mark.attrs?.href)}${linkTitle(mark.attrs?.title)})`;
    case 'underline': return '</u>';
    case 'kbd': return '</kbd>';
    case 'textStyle': return '</span>';
    default: return openMarkup(mark);
  }
}

function stackMarks(node) {
  return (node.marks || [])
    .filter((m) => MARK_ORDER.includes(m.type) && !(m.type === 'textStyle' && !m.attrs?.color && !m.attrs?.bg))
    .sort((a, b) => MARK_ORDER.indexOf(a.type) - MARK_ORDER.indexOf(b.type));
}

function isBareAutolink(node, nodes, index) {
  const link = (node.marks || []).find((m) => m.type === 'link');
  const href = link?.attrs?.href || '';
  if (!link || link.attrs?.title || node.type !== 'text') return false;
  if (!(node.text === href || `mailto:${node.text}` === href) || !/^(https?:|mailto:)/i.test(href)) return false;
  const shares = (other) => other?.marks?.some((m) => m.type === 'link' && m.attrs?.href === href);
  return !shares(nodes[index - 1]) && !shares(nodes[index + 1]);
}

function inlineChunk(node, nodes, index, inTable) {
  if (node.type === 'hardBreak') {
    if (inTable) return '<br>';
    return node.attrs?.soft ? '\n' : '  \n';
  }
  if (node.type === 'reference') {
    const attrs = node.attrs || {};
    return `[${escapeLinkLabel(attrs.label || attrs.id || '引用')}](${referenceUrl(attrs)})`;
  }
  if (node.type === 'rawInline') return String(node.attrs?.source || '');
  if (node.type === 'inlineImage' || node.type === 'imageBlock') {
    const { src = '', alt = '', title = '' } = node.attrs || {};
    return `![${escapeLinkLabel(alt)}](${linkDestination(src)}${linkTitle(title)})`;
  }
  if (node.type !== 'text') return '';
  const text = String(node.text || '');
  const marks = node.marks || [];
  const memoMark = marks.find((m) => m.type === 'inlineMemo');
  if (memoMark) return `[${escapeMemoLabel(text)}]^(${escapeMemoBody(memoMark.attrs?.memo || '')})`;
  if (marks.some((m) => m.type === 'code')) {
    const chunk = inTable ? text.replace(/\|/g, '\\|') : text;
    const tickMatch = chunk.match(/`+/g);
    const fence = '`'.repeat((tickMatch ? Math.max(...tickMatch.map((s) => s.length)) : 0) + 1);
    const pad = /^`|`$/.test(chunk) || (/^ .* $/.test(chunk) && chunk.trim()) ? ' ' : '';
    return `${fence}${pad}${chunk}${pad}${fence}`;
  }
  if (isBareAutolink(node, nodes, index)) return text;
  return escapeText(text, { inTable });
}

function serializeInline(nodes = [], { inTable = false } = {}) {
  let out = '';
  let active = [];
  let heldSpace = '';

  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index];
    if (!node) continue;
    let chunk = inlineChunk(node, nodes, index, inTable);
    if (!chunk) continue;
    const isText = node.type === 'text' && !node.marks?.some((m) => m.type === 'code' || m.type === 'inlineMemo');
    let target = stackMarks(node);
    if (isBareAutolink(node, nodes, index)) target = target.filter((m) => m.type !== 'link');

    let keep = 0;
    while (keep < active.length && keep < target.length && sameMark(active[keep], target[keep])) keep += 1;
    // Whitespace-only text never opens emphasis of its own.
    if (isText && !chunk.trim()) target = active.slice(0, keep);

    if (keep < active.length) {
      for (let k = active.length - 1; k >= keep; k -= 1) out += closeMarkup(active[k]);
    }
    out += heldSpace;
    heldSpace = '';
    if (keep < target.length) {
      // Delimiters must touch non-space text, so move edge spaces outside them.
      if (isText) {
        const lead = chunk.match(/^\s*/)[0];
        out += lead;
        chunk = chunk.slice(lead.length);
      }
      for (let k = keep; k < target.length; k += 1) out += openMarkup(target[k]);
    }
    active = target;
    if (isText && active.length) {
      const trail = chunk.match(/\s*$/)[0];
      heldSpace = trail;
      chunk = chunk.slice(0, chunk.length - trail.length);
    }
    out += chunk;
  }
  for (let k = active.length - 1; k >= 0; k -= 1) out += closeMarkup(active[k]);
  return out + heldSpace;
}

function serializeTable(tableNode) {
  const rows = (tableNode.content || []).filter((r) => r.type === 'tableRow');
  if (!rows.length) return '';
  const matrix = rows.map((row) =>
    (row.content || []).map((cell) => {
      const align = cell.attrs?.textAlign || null;
      const text = (cell.content || [])
        .map((block) => serializeInline(block.content || [], { inTable: true }).trim())
        .filter(Boolean)
        .join('<br>');
      return { text, align };
    }),
  );
  const colCount = Math.max(1, ...matrix.map((r) => r.length));
  const alignments = Array.from({ length: colCount }, (_, colIdx) => matrix[0]?.[colIdx]?.align || null);

  const formatRow = (rowCells) => {
    const parts = Array.from({ length: colCount }, (_, colIdx) => rowCells[colIdx]?.text || '');
    return `| ${parts.join(' | ')} |`;
  };
  const divider = `| ${alignments
    .map((align) => {
      if (align === 'center') return ':---:';
      if (align === 'right') return '---:';
      if (align === 'left') return ':---';
      return '---';
    })
    .join(' | ')} |`;

  const lines = [formatRow(matrix[0] || []), divider];
  for (let r = 1; r < matrix.length; r += 1) {
    lines.push(formatRow(matrix[r]));
  }
  return lines.join('\n');
}

function serializeBlock(node, indent = '') {
  if (!node) return '';
  switch (node.type) {
    case 'heading': {
      const level = Math.max(1, Math.min(6, Number(node.attrs?.level || 1)));
      // A trailing ` #` run would be read as the optional closing sequence.
      const text = serializeInline(node.content || []).replace(/\n+/g, ' ').trim().replace(/(\s)(#+)$/, '$1\\$2');
      return `${indent}${'#'.repeat(level)} ${text}`.trimEnd();
    }
    case 'paragraph': {
      const text = serializeInline(node.content || []);
      if (!text) return '';
      return text
        .split('\n')
        .map((line) => `${indent}${escapeLineStart(line)}`)
        .join('\n');
    }
    case 'imageBlock':
      return `${indent}${inlineChunk(node, [node], 0, false)}`;
    case 'rawBlock':
      return String(node.attrs?.source || '')
        .split('\n')
        .map((line) => (line ? `${indent}${line}` : line))
        .join('\n');
    case 'codeBlock': {
      const lang = String(node.attrs?.language || '').trim();
      const code = (node.content || []).map((c) => c.text || '').join('');
      const backtickMatches = code.match(/`{3,}/g);
      const fenceLen = backtickMatches ? Math.max(3, ...backtickMatches.map((m) => m.length + 1)) : 3;
      const fence = '`'.repeat(fenceLen);
      const bodyLines = code.split('\n').map((l) => (l ? `${indent}${l}` : l)).join('\n');
      return `${indent}${fence}${lang}\n${bodyLines}\n${indent}${fence}`;
    }
    case 'blockquote': {
      const inner = serializeBlocks(node.content || [], '');
      if (!inner) return `${indent}>`;
      return inner
        .split('\n')
        .map((line) => (line.trim() ? `${indent}> ${line}` : `${indent}>`))
        .join('\n');
    }
    case 'horizontalRule':
      return `${indent}---`;
    case 'bulletList': {
      return (node.content || [])
        .map((item) => serializeListItem(item, `${indent}- `, `${indent}  `))
        .join('\n');
    }
    case 'orderedList': {
      const start = Number(node.attrs?.start || 1);
      return (node.content || [])
        .map((item, idx) => {
          const marker = `${start + idx}. `;
          return serializeListItem(item, `${indent}${marker}`, `${indent}${' '.repeat(marker.length)}`);
        })
        .join('\n');
    }
    case 'taskList': {
      return (node.content || [])
        .map((item) => {
          const box = item.attrs?.checked ? '[x] ' : '[ ] ';
          return serializeListItem(item, `${indent}- ${box}`, `${indent}  `);
        })
        .join('\n');
    }
    case 'table':
      return serializeTable(node)
        .split('\n')
        .map((line) => `${indent}${line}`)
        .join('\n');
    default:
      return '';
  }
}

const LIST_TYPES = new Set(['bulletList', 'orderedList', 'taskList']);

function serializeListItem(itemNode, firstPrefix, restIndent) {
  const children = itemNode.content || [];
  if (!children.length) return firstPrefix.trimEnd();
  let out = '';
  children.forEach((child, idx) => {
    const rendered = idx === 0 ? serializeBlock(child, '') : serializeBlock(child, restIndent);
    if (idx === 0) {
      const lines = rendered.split('\n');
      // Only an empty item drops the prefix's space; a hard break's trailing spaces must stay.
      out = [
        lines[0] ? `${firstPrefix}${lines[0]}` : firstPrefix.trimEnd(),
        ...lines.slice(1).map((l) => (l ? `${restIndent}${l}` : '')),
      ].join('\n');
      return;
    }
    if (!rendered) return;
    // A nested list may follow directly; any other block needs a blank line
    // or it would merge into the previous paragraph as a lazy continuation.
    out += LIST_TYPES.has(child.type) ? `\n${rendered}` : `\n\n${rendered}`;
  });
  return out;
}

// Empty blocks are skipped here rather than collapsing blank lines afterwards,
// which would also squeeze blank lines inside code blocks.
function serializeBlocks(blocks = [], indent = '') {
  return blocks
    .map((block) => serializeBlock(block, indent))
    .filter(Boolean)
    .join('\n\n');
}

export function docToMarkdown(docJson) {
  const blocks = docJson?.content || [];
  if (!blocks.length) return '';
  if (blocks.length === 1 && blocks[0].type === 'paragraph' && !(blocks[0].content || []).length) {
    return '';
  }
  return serializeBlocks(blocks, '').trimEnd();
}

export function looksLikeStructuredMarkdown(text = '') {
  const raw = String(text || '').trim();
  if (!raw) return false;
  if (/^(?:#{1,6}\s|```|~~~|>\s|[-*+]\s+\[[ xX]\]\s|---\s*$)/m.test(raw)) return true;
  if (/^\|.+\|\s*\n\|[-:\s|]+\|/m.test(raw)) return true;
  const listLines = raw.match(/^(?:\s*[-*+]\s+\S|\s*\d+\.\s+\S)/gm) || [];
  return listLines.length >= 2;
}

export function extractOutlineFromMarkdown(markdown = '') {
  const doc = markdownToDoc(markdown);
  const headings = [];
  let idx = 0;
  for (const block of doc.content || []) {
    if (block.type === 'heading') {
      const text = (block.content || [])
        .map((c) => c.text || c.attrs?.label || '')
        .join('')
        .trim();
      if (text) {
        headings.push({
          index: idx,
          level: Number(block.attrs?.level || 1),
          text,
        });
      }
      idx += 1;
    }
  }
  return headings;
}

export function computeDocumentStats(markdown = '') {
  const raw = String(markdown || '');
  const chars = raw.replace(/\r/g, '').length;
  const cjk = (raw.match(/[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]/g) || []).length;
  const words = (raw.replace(/[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]/g, ' ').match(/[A-Za-z0-9_]+/g) || []).length;
  return {
    chars,
    words: cjk + words,
  };
}

const HTML_ENTITY_MAP = {
  '&amp;': '&',
  '&lt;': '<',
  '&gt;': '>',
  '&quot;': '"',
  '&#39;': "'",
  '&#x27;': "'",
};

export function parseHljsHtmlToRanges(html, originalLength) {
  const ranges = [];
  const stack = [];
  let offset = 0;
  const tagOrEntityRe = /<span class="([^"]+)">|<\/span>|&(amp|lt|gt|quot|#39|#x27);/g;
  let lastIdx = 0;
  let match;
  while ((match = tagOrEntityRe.exec(html)) !== null) {
    if (match.index > lastIdx) {
      offset += match.index - lastIdx;
    }
    if (match[1]) {
      stack.push({ className: match[1], from: offset });
    } else if (match[0] === '</span>') {
      const top = stack.pop();
      if (top && offset > top.from) {
        ranges.push({ from: top.from, to: offset, className: top.className });
      }
    } else {
      const decoded = HTML_ENTITY_MAP[match[0]] || ' ';
      offset += decoded.length;
    }
    lastIdx = tagOrEntityRe.lastIndex;
  }
  if (lastIdx < html.length) {
    offset += html.length - lastIdx;
  }
  return offset === originalLength ? ranges : [];
}
