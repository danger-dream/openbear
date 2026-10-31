// How the file browser opens a file: as text in the editor, as an image, or
// not at all (its menu opens instead).
export const TEXT_LIMIT = 2 * 1024 * 1024;
export const IMAGE_LIMIT = 20 * 1024 * 1024;

const LANGUAGES = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript', vue: 'html',
  json: 'json', jsonc: 'json', py: 'python', sh: 'shell', bash: 'shell', zsh: 'shell', yml: 'yaml', yaml: 'yaml',
  md: 'markdown', markdown: 'markdown', html: 'html', htm: 'html', css: 'css', scss: 'scss', less: 'less', xml: 'xml', svg: 'xml',
  sql: 'sql', go: 'go', rs: 'rust', java: 'java', kt: 'kotlin', php: 'php', rb: 'ruby', lua: 'lua', pl: 'perl', c: 'c', h: 'c',
  cc: 'cpp', cpp: 'cpp', hpp: 'cpp', cs: 'csharp', swift: 'swift', ini: 'ini', conf: 'ini', cfg: 'ini', toml: 'ini', env: 'ini',
  properties: 'ini', service: 'ini', timer: 'ini', socket: 'ini', ps1: 'powershell', bat: 'bat', dockerfile: 'dockerfile',
};
const PLAIN = new Set(['txt', 'log', 'csv', 'tsv', 'list', 'lock', 'pem', 'crt', 'key', 'pub', 'rules', 'gitignore', 'dockerignore', 'editorconfig', 'npmrc']);
const BY_NAME = {dockerfile: 'dockerfile', makefile: 'plaintext', caddyfile: 'plaintext', jenkinsfile: 'plaintext', hosts: 'plaintext', crontab: 'plaintext',
  '.bashrc': 'shell', '.profile': 'shell', '.bash_profile': 'shell', '.zshrc': 'shell', 'authorized_keys': 'plaintext', 'known_hosts': 'plaintext'};
const IMAGES = {png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml',
  ico: 'image/x-icon', bmp: 'image/bmp', avif: 'image/avif'};
const ARCHIVES = ['.tar.gz', '.tgz', '.tar.xz', '.txz', '.tar.bz2', '.tbz2', '.tar.zst', '.tar', '.zip'];

function ext(name) {
  const lower = name.toLowerCase(), dot = lower.lastIndexOf('.');
  return dot > 0 ? lower.slice(dot + 1) : '';
}
export function language(name) {
  const lower = name.toLowerCase();
  if (BY_NAME[lower]) return BY_NAME[lower];
  const e = ext(name);
  if (LANGUAGES[e]) return LANGUAGES[e];
  if (lower.startsWith('dockerfile')) return 'dockerfile';
  return 'plaintext';
}
export function isArchive(name) { const lower = name.toLowerCase(); return ARCHIVES.some(a => lower.endsWith(a)); }
export function archiveBase(name) {
  const lower = name.toLowerCase(), hit = ARCHIVES.find(a => lower.endsWith(a));
  return hit ? name.slice(0, name.length - hit.length) || name : name;
}
export function imageType(name) { return IMAGES[ext(name)] || ''; }
// An SVG is a document that can run scripts: as a blob: URL (this page's origin) it would run them when opened in
// a new tab. A data: URL still shows it as an image, and a document opened from it gets an origin of its own.
export function imageUrl(buffer, type) {
  if (type !== IMAGES.svg) return URL.createObjectURL(new Blob([buffer], {type}));
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return `data:${type};base64,${btoa(binary)}`;
}
// Text: a known extension, or no extension at all (config files, scripts,
// dotfiles); the content is checked again when it is read.
export function kind(entry) {
  if (entry.dir) return 'dir';
  if (entry.type === 'link') return 'link';
  if (entry.type !== 'file') return 'other';
  if (imageType(entry.name)) return 'image';
  if (isArchive(entry.name)) return 'archive';
  const lower = entry.name.toLowerCase(), e = ext(entry.name);
  if (LANGUAGES[e] || PLAIN.has(e) || BY_NAME[lower] || !e || lower.startsWith('.') || /\.(d|conf|rc)$/.test(lower)) return 'text';
  return 'other';
}
// The body of a response, but null once more than limit bytes arrive: the size the node listed is only its word.
export async function readLimited(response, limit) {
  if (!response.body) return new ArrayBuffer(0);
  const reader = response.body.getReader(), parts = [];
  let total = 0;
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    total += value.length;
    if (total > limit) { await reader.cancel(); return null; }
    parts.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out.buffer;
}
// Read text strictly: a NUL byte or invalid UTF-8 means it is not text.
export function decodeText(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes.includes(0)) return null;
  try { return new TextDecoder('utf-8', {fatal: true}).decode(bytes); } catch { return null; }
}
export function modeBits(mode) {
  const value = parseInt(mode || '0', 8) & 0o777;
  return [0o400, 0o200, 0o100, 0o40, 0o20, 0o10, 0o4, 0o2, 0o1].map(bit => Boolean(value & bit));
}
export function modeFrom(bits, special = 0) {
  const value = [0o400, 0o200, 0o100, 0o40, 0o20, 0o10, 0o4, 0o2, 0o1].reduce((sum, bit, i) => sum + (bits[i] ? bit : 0), 0) | (special << 9);
  return value.toString(8).padStart(special ? 4 : 3, '0');
}
