import MarkdownIt from 'markdown-it';

export const REFERENCE_MIME = 'application/x-openbear-reference+json';
export const REFERENCE_KINDS = ['mem', 'doc', 'secret', 'chat', 'turn', 'message'];
export const KIND_LABELS = {mem: '记忆', doc: '文档', secret: '凭证', chat: '会话', turn: '本轮问答', message: '消息'};
const referencePattern = /\[((?:\\.|[^\[\]\\\n])*)\]\((openbear:\/\/ref\/[^\s)]+)\)/g;
// Keep this syntax-only preset aligned with app/references.py.
const referenceMarkdown = new MarkdownIt('default', {html:false,linkify:false});

export function normalizeReference(input) {
  if (!input || !REFERENCE_KINDS.includes(input.kind)) return null;
  if (input.mode != null && !['', 'content', 'mention'].includes(input.mode)) return null;
  const id = String(input.id || input.conversationUuid || '');
  if (!id || id.length > 160 || /[\r\n\0]/.test(id)) return null;
  if (['mem','doc','secret'].includes(input.kind) && (!/^[1-9]\d*$/.test(id) || BigInt(id) > 9223372036854775807n)) return null;
  const ref = {kind: input.kind, id, label: String(input.label || input.title || input.name || id).slice(0,300), scope: input.scope === 'recent' ? 'recent' : 'full'};
  if (input.mode === 'mention') {
    ref.mode = 'mention';
    if (['chat','turn','message'].includes(ref.kind) && ['conversation_too_long','conversation_total_limit'].includes(input.modeReason)) ref.modeReason=input.modeReason;
  }
  if (Number.isFinite(input.bodyChars) && input.bodyChars>=0) ref.bodyChars=input.bodyChars;
  if (Number.isFinite(input.contentLimit) && input.contentLimit>0) ref.contentLimit=input.contentLimit;
  if (['turn','message'].includes(ref.kind)) {
    ref.itemId = String(input.itemId || '').slice(0,200);
    if (!ref.itemId) return null;
  }
  if (ref.scope === 'recent') {
    const count = input.turns == null || input.turns === '' ? 20 : Number(input.turns);
    ref.turns = Math.max(1, Math.min(50, Number.isFinite(count) ? Math.trunc(count) : 20));
  }
  return ref;
}
export function referenceKey(ref) { return [ref.kind,ref.id,ref.itemId || '',ref.scope || 'full',ref.turns || ''].join(':') + (ref.mode === 'mention' ? ':mention' : ''); }
export function boundReference(ref,binding) {
  if (!binding || referenceKey({...binding,mode:''})!==referenceKey({...ref,mode:''})) return ref;
  return normalizeReference({...ref,mode:binding.mode==='mention'?'mention':'',modeReason:binding.modeReason||'',bodyChars:binding.bodyChars,contentLimit:binding.contentLimit});
}
export function catalogKey(ref) { return ['turn','message'].includes(ref.kind) ? `${ref.kind}:${ref.id}:${ref.itemId}` : `${ref.kind}:${ref.id}`; }
export function referenceUrl(input) {
  const ref = normalizeReference(input); if (!ref) return '';
  const path = [ref.kind,ref.id,...(ref.itemId ? [ref.itemId] : [])].map(encodeURIComponent).join('/');
  const params = new URLSearchParams();
  if (ref.scope === 'recent') { params.set('scope', 'recent'); params.set('turns', ref.turns); }
  if (ref.mode === 'mention') params.set('mode', 'mention');
  if (ref.modeReason) params.set('reason',ref.modeReason);
  return `openbear://ref/${path}${params.size ? '?' + params : ''}`;
}
export function referenceFromUrl(href, label = '') {
  try {
    const url = new URL(href);
    if (url.protocol !== 'openbear:' || url.host !== 'ref' || url.username || url.password) return null;
    const parts = url.pathname.slice(1).split('/').map(decodeURIComponent);
    if (parts.length !== (['turn','message'].includes(parts[0]) ? 3 : 2)) return null;
    const modes=url.searchParams.getAll('mode');
    if(modes.length>1 || (modes.length===1&&!['content','mention'].includes(modes[0])))return null;
    const scope=url.searchParams.get('scope') || 'full',turns=url.searchParams.get('turns');
    if(!['full','recent'].includes(scope) || (turns!==null&&!/^[+-]?\d+$/.test(turns)))return null;
    return normalizeReference({kind:parts[0],id:parts[1],itemId:parts[2],label,scope,turns,mode:url.searchParams.get('mode'),modeReason:url.searchParams.get('reason')});
  } catch { return null; }
}
export function referenceToken(input) {
  const ref = normalizeReference(input); if (!ref) return '';
  const label = ref.label.replace(/([\\\[\]`*_])/g,'\\$1').replace(/\n/g,' ');
  return `[${label}](${referenceUrl(ref)})`;
}
function referenceMatches(text) {
  const matches = [...text.matchAll(referencePattern)];
  if (!matches.length) return [];
  // Tag only the candidate URL host in a temporary parser input. Preserve all
  // Markdown delimiters/escapes, then use the tag to recover the original span
  // even inside lists/quotes or beside an identical literal code example.
  // A per-parse nonce prevents even entity-encoded lookalike URLs in the
  // original source from impersonating a candidate after normalization.
  const nonce = [...crypto.getRandomValues(new Uint32Array(4))].map(n=>n.toString(16).padStart(8,'0')).join('');
  const prefix = `openbear://reference-candidate-${nonce}-`;
  const parts = []; let offset = 0;
  for (const [index,match] of matches.entries()) {
    const start = match.index + match[0].length - match[2].length - 1;
    parts.push(text.slice(offset,start), `${prefix}${index}/`);
    offset = start + 'openbear://ref/'.length;
  }
  parts.push(text.slice(offset));
  const destinations = new Map();
  for (const block of referenceMarkdown.parse(parts.join(''), {})) {
    for (const token of block.children || []) {
      if (token.type !== 'link_open') continue; // In particular, not image alt text.
      const href = token.attrGet('href') || '';
      if (!href.startsWith(prefix)) continue;
      const tagged = /^([0-9]+)\//.exec(href.slice(prefix.length));
      if (tagged) destinations.set(Number(tagged[1]), 'openbear://ref/' + href.slice(prefix.length + tagged[0].length));
    }
  }
  return matches.flatMap((match,index) => destinations.has(index) ? [{match,href:destinations.get(index)}] : []);
}
// Legacy rich-HTML preprocessing must not decode a Markdown destination a
// second time. Preserve only destinations; Markdown still decides link syntax.
export function preserveReferenceDestinations(value, transform) {
  const text = String(value || '');
  if (!text.includes('openbear://ref/')) return transform(text);
  const destinations = [];
  const prefix = `OPENBEAR_REF_${[...crypto.getRandomValues(new Uint32Array(4))].join('_')}_`;
  const protectedText = text.replace(referencePattern, (whole, _label, href) => {
    const marker = `${prefix}${destinations.length}_END`;
    destinations.push(href);
    return whole.slice(0, whole.length - href.length - 1) + marker + ')';
  });
  return transform(protectedText).replace(new RegExp(`${prefix}(\\d+)_END`, 'g'), (_whole, index) => destinations[Number(index)]);
}
export function parseReferenceText(value) {
  const text = String(value || '');
  if (!text.includes('openbear://ref/')) return text ? [{type:'text',text}] : [];
  const parts = []; let offset = 0;
  for (const {match,href} of referenceMatches(text)) {
    const ref = referenceFromUrl(href,match[1].replace(/\\(.)/g,'$1'));
    if (!ref) continue;
    if (match.index > offset) parts.push({type:'text',text:text.slice(offset,match.index)});
    parts.push({type:'reference',attrs:ref}); offset = match.index + match[0].length;
  }
  if (offset < text.length) parts.push({type:'text',text:text.slice(offset)});
  return parts;
}
export function referenceDisplayText(value) { return parseReferenceText(value).map(part=>part.type==='reference'?part.attrs.label:part.text).join(''); }
export function referencesInText(value) {
  const unique = new Map();
  for (const part of parseReferenceText(value)) if (part.type === 'reference') unique.set(referenceKey(part.attrs),part.attrs);
  return [...unique.values()];
}
export function textToDocument(value) {
  const content = [];
  for (const part of parseReferenceText(value)) {
    if (part.type === 'reference') { content.push(part); continue; }
    part.text.split('\n').forEach((text,index) => {
      if (index) content.push({type:'hardBreak'});
      if (text) content.push({type:'text',text});
    });
  }
  return {type:'doc',content:[{type:'paragraph',content}]};
}
export function documentToText(doc) {
  function inline(node) {
    if (node.type === 'reference') return referenceToken(node.attrs);
    if (node.type === 'hardBreak') return '\n';
    if (node.type === 'text') return node.text || '';
    return (node.content || []).map(inline).join('');
  }
  return (doc?.content || []).map(inline).join('\n');
}
export function referenceErrorText(error) {
  const code = typeof error === 'string' ? error : error?.code || error?.error || '';
  return ({invalid_reference_mode:'引用模式无效，请重新选择引用内容或仅提及',reference_unavailable:'引用对象已删除、停用或无法访问，请检查胶囊',history_output_limit:'引用内容超过 History 单次输出上限，请缩小会话范围',reference_budget_exceeded:'引用材料超出当前模型输入预算，请缩小范围或移除部分引用',too_many_references:'单条消息最多引用 30 个不同对象',protected_storage_unavailable:'凭证保护存储不可用，请先检查服务端配置'})[code] || code || '引用内容暂时无法读取';
}
