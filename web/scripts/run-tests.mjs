// npm test runs every test except the real-browser ones (*.browser.test.mjs): they take most of
// the time and run with `npm run test:browser`; scripts/check.py still picks them when a page changes.
// Arguments other than --browser and --all go to node --test unchanged.
import {spawn} from 'node:child_process';
import {readdirSync} from 'node:fs';
import {availableParallelism} from 'node:os';
import {join, relative, sep} from 'node:path';
import {fileURLToPath} from 'node:url';

const web = fileURLToPath(new URL('..', import.meta.url));
const args = process.argv.slice(2);
const mode = args.includes('--all') ? 'all' : args.includes('--browser') ? 'browser' : 'unit';
const passed = args.filter(arg => arg !== '--all' && arg !== '--browser');

// The file names node --test finds on its own.
const isTest = path => /\.[cm]?js$/.test(path) && (/(^|[.\-_])test\.[cm]?js$/.test(path.split(sep).pop())
  || /^test-/.test(path.split(sep).pop()) || path.split(sep).includes('test'));
const isBrowser = path => /\.browser\.test\.[cm]?js$/.test(path);

function walk(dir, found = []) {
  for (const entry of readdirSync(dir, {withFileTypes: true})) {
    if (entry.name === 'node_modules' || entry.name === 'dist' || entry.name.startsWith('.')) continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) walk(path, found);
    else if (isTest(relative(web, path))) found.push(relative(web, path));
  }
  return found;
}

const files = walk(web).filter(path => mode === 'all' || (mode === 'browser') === isBrowser(path)).sort();
const child = spawn(process.execPath, ['--test', `--test-concurrency=${availableParallelism()}`, ...passed, ...files],
  {cwd: web, stdio: 'inherit'});
child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
