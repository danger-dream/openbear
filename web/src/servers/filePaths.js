// Paths reach the node with forward slashes. On Windows they start with a
// drive ("C:/Users") and "/" lists the drives; they are shown and copied with
// backslashes, the way Windows writes them.
const DRIVE = /^[A-Za-z]:\/?$/;
const WINDOWS_BANNED = /[\\/:*?"<>|]/;

export function pathsFor(platform) {
  const windows = platform === 'windows';
  const split = p => p.split('/').filter(Boolean);
  return {
    windows,
    start: windows ? '/' : platform === 'darwin' ? '/Users' : '/root',
    normalize(input) {
      const p = String(input ?? '').trim();
      if (!windows) return p || '/';
      const slashed = p.replace(/\\/g, '/');
      return !slashed ? '/' : /^[A-Za-z]:$/.test(slashed) ? `${slashed}/` : slashed;
    },
    join(dir, name) {
      if (windows && dir === '/') return `${name}/`;
      return (dir.endsWith('/') ? dir : `${dir}/`) + name;
    },
    parent(p) {
      if (p === '/' || windows && DRIVE.test(p)) return '/';
      const up = p.replace(/\/[^/]+\/?$/, '');
      return windows && /^[A-Za-z]:$/.test(up) ? `${up}/` : up || '/';
    },
    crumbs(p) {
      const parts = split(p);
      if (!windows) return [{name: '/', path: '/'}, ...parts.map((name, i) => ({name, path: '/' + parts.slice(0, i + 1).join('/')}))];
      return [{name: '此电脑', path: '/'}, ...parts.map((name, i) => ({name, path: i === 0 ? `${name}/` : parts.slice(0, i + 1).join('/')}))];
    },
    show(p) { return windows ? (p === '/' ? '' : p.replace(/\//g, '\\')) : p; },
    leaf(p) { return split(p).pop()?.replace(/:$/, '') || ''; },
    absolute(value) { return windows ? /^[A-Za-z]:([\\/]|$)/.test(value.trim()) : value.trim().startsWith('/'); },
    validName(value) {
      const v = String(value ?? '');
      if (!v.trim() || v === '.' || v === '..' || (windows ? WINDOWS_BANNED.test(v) : v.includes('/'))) {
        return windows ? '名称不能为空，也不能包含 \\ / : * ? " < > | 这些字符' : '名称不能为空，也不能包含斜杠';
      }
      return true;
    },
  };
}
