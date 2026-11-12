// Sortable 1.14.0 loses scrollFn when a second event reuses the same scroll root.
// Keep the dependency/version; apply the same one-line fix to UMD and ESM builds.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

const marker = '/* OpenBear: refresh Sortable scrollFn on every autoScroll event. */';
export function patchScrollSource(source, filename) {
  if (source.includes(marker)) return source;
  let patched;
  if (filename === 'Sortable.min.js') {
    const assignment = /([\w$]+)=([\w$]+)\.scrollFn/.exec(source);
    if (!assignment) throw new Error(`Missing scrollFn in ${filename}`);
    const start = source.lastIndexOf('function(', assignment.index);
    const prefix = source.slice(start, assignment.index);
    const declaration = `var ${assignment[1]},`;
    if (prefix.split(declaration).length !== 2) throw new Error(`Unexpected autoScroll declaration in ${filename}`);
    patched = source.slice(0, start) + prefix.replace(declaration, `var ${assignment[1]}=${assignment[2]}.scrollFn,`) + source.slice(assignment.index);
  } else {
    const declaration = 'scrollCustomFn;';
    if (source.split(declaration).length !== 2) throw new Error(`Unexpected autoScroll declaration in ${filename}`);
    patched = source.replace(declaration, 'scrollCustomFn = options.scrollFn;');
  }
  return `${marker}\n${patched}`;
}

export function patchInstalledSortable(requireFrom = import.meta.url) {
  const require = createRequire(requireFrom);
  const draggableRequire = createRequire(require.resolve('vuedraggable'));
  const root = path.dirname(draggableRequire.resolve('sortablejs/package.json'));
  const version = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version;
  if (version !== '1.14.0') throw new Error(`Review the Sortable scrollFn patch before using ${version}`);
  const files = ['Sortable.js', 'Sortable.min.js', 'modular/sortable.esm.js', 'modular/sortable.complete.esm.js', 'modular/sortable.core.esm.js'];
  // Validate every entry before writing any, so signature drift cannot half-patch an install.
  const edits = files.map(filename => {
    const target = path.join(root, filename), source = fs.readFileSync(target, 'utf8');
    if (!source.includes('.scrollFn')) return [target, source, source];
    return [target, source, patchScrollSource(source, path.basename(filename))];
  });
  for (const [target, original, patched] of edits) if (patched !== original) fs.writeFileSync(target, patched);
  patchInstalledNoVnc(requireFrom);
}

const noVncMarker = '/* OpenBear: avoid top-level await in @novnc/novnc browser detection. */';
export function patchInstalledNoVnc(requireFrom = import.meta.url) {
  const require = createRequire(requireFrom);
  let rfbPath;
  try {
    rfbPath = require.resolve('@novnc/novnc');
  } catch {
    return;
  }
  const browserPath = path.join(path.dirname(rfbPath), 'util', 'browser.js');
  if (fs.existsSync(browserPath)) {
    const source = fs.readFileSync(browserPath, 'utf8');
    const needle = 'supportsWebCodecsH264Decode = await _checkWebCodecsH264DecodeSupport();';
    if (!source.includes(noVncMarker) && source.includes(needle)) {
      const patched = `${noVncMarker}\n${source.replace(needle, '_checkWebCodecsH264DecodeSupport().then(v => { supportsWebCodecsH264Decode = v; }).catch(() => {});')}`;
      fs.writeFileSync(browserPath, patched);
    }
  }
  const cursorPath = path.join(path.dirname(rfbPath), 'util', 'cursor.js');
  if (fs.existsSync(cursorPath)) {
    const source = fs.readFileSync(cursorPath, 'utf8');
    const needle = "this._target.style.cursor = 'none';";
    if (!source.includes(noVncMarker) && source.includes(needle)) {
      const patched = `${noVncMarker}\n${source.replace(needle, "this._target.style.cursor = 'default';")}`;
      fs.writeFileSync(cursorPath, patched);
    }
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) patchInstalledSortable();
