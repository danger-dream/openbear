import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';
import {parse, compileStyleAsync} from '@vue/compiler-sfc';
import mobileCompat from '../postcss-mobile-compat.js';
import config from '../postcss.config.js';

const src = path.dirname(fileURLToPath(import.meta.url));
const read = name => fs.readFileSync(path.join(src, name), 'utf8');
function styles() {
  const out = [];
  function visit(dir) {
    for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
      const filename = path.join(dir, entry.name);
      if (entry.isDirectory()) visit(filename);
      else if (entry.name.endsWith('.css')) out.push([filename, fs.readFileSync(filename, 'utf8')]);
      else if (entry.name.endsWith('.vue')) {
        const source = fs.readFileSync(filename, 'utf8');
        for (const style of parse(source).descriptor.styles) out.push([filename, style.content]);
      }
    }
  }
  visit(src);
  return out;
}
const sources = styles();
const VIEWS = 'views/consoleView/';
function descriptor(name) {return parse(read(`${VIEWS}${name}.vue`), {filename: name}).descriptor;}

// These are source CSS contract tests, not an engine/rendering simulation.
test('all CSS color-mix and dvh sites retain modern values and add ordered, legacy-safe overrides', async () => {
  let mixes = 0, heights = 0, occurrences = 0;
  for (const [filename, css] of sources) {
    const root = postcss.parse(css, {from: filename});
    occurrences += (css.match(/color-mix\(/g) || []).length;
    const impacted = [];
    root.walkRules(rule => {
      for (const decl of rule.nodes.filter(n => n.type === 'decl')) {
        if (decl.value.includes('color-mix(') || decl.value.includes('dvh')) impacted.push({selector: rule.selector, prop: decl.prop, value: decl.value});
      }
    });
    if (!impacted.length) continue;
    const result = await postcss([mobileCompat()]).process(css, {from: filename});
    const output = result.root;
    const guarded = [];
    output.walkAtRules('supports', guard => {
      if (!guard.params.startsWith('not (color: color-mix(') && guard.params !== 'not (height: 100dvh)') return;
      for (const rule of guard.nodes.filter(n => n.type === 'rule')) {
        for (const decl of rule.nodes.filter(n => n.type === 'decl')) {
          assert.doesNotMatch(decl.value, /color-mix\(|\bdvh\b/, `${filename}: invalid legacy value`);
          const previous = guard.prev();
          assert.ok(previous?.type === 'rule' || previous?.name === 'supports', `${filename}: original cascade location`);
          guarded.push({selector: rule.selector, prop: decl.prop, guard: guard.params});
        }
      }
    });
    for (const {selector, prop, value} of impacted) {
      if (value.includes('color-mix(')) {
        mixes++;
        assert.ok(guarded.some(x => x.selector === selector && x.prop === prop && x.guard.startsWith('not (color: color-mix(')), `${filename}: ${selector} ${prop} color fallback`);
      }
      if (value.includes('dvh')) {
        heights++;
        assert.ok(guarded.some(x => x.selector === selector && x.prop === prop && x.guard === 'not (height: 100dvh)'), `${filename}: ${selector} ${prop} viewport fallback`);
      }
    }
    if (filename.endsWith('dark-theme.css')) {
      assert.match(result.css, /--el-color-primary-light-3:\s*rgb\(var\(--ob-blue-rgb\) \/ 0\.7\)/);
      assert.match(result.css, /--el-mask-color:\s*rgb\(var\(--ob-bg-rgb\) \/ 0\.82\)/);
    }
  }
  assert.equal(occurrences, 109, 'all 109 stylesheet color mixes are accounted for, two of them the server detail page\'s contents bar and map caption (the 110th is the heatmap inline style)');
  assert.equal(mixes, 106, `covered ${mixes} source CSS color-mix declarations, the server detail page\'s two among them`);
  assert.ok(heights >= 55, `covered ${heights} source CSS dvh declarations`);
});

test('existing media nesting, custom properties, specificity and dark/light theme channels survive compilation', () => {
  const input = '@media(max-width:760px){:root,html.dark{--shade:color-mix(in srgb,var(--ob-blue) 20%,var(--ob-surface));height:100dvh}.next{color:var(--ob-text)}}';
  const result = postcss([mobileCompat()]).process(input, {from: undefined}).root;
  const media = result.first;
  assert.equal(media.name, 'media');
  assert.deepEqual(media.nodes.map(n => n.type === 'atrule' ? n.params : n.selector), [':root,html.dark','not (color: color-mix(in srgb, red, blue))','not (height: 100dvh)','.next']);
  assert.equal(media.nodes[1].first.selector, ':root,html.dark');
  assert.equal(media.nodes[1].first.first.value, 'rgb(var(--ob-blue-rgb) / 0.2)');
  assert.equal(media.nodes[2].first.nodes.find(n => n.prop === 'height').value, '100vh');
  assert.equal(media.first.first.value, 'color-mix(in srgb,var(--ob-blue) 20%,var(--ob-surface))');
  assert.throws(() => postcss([mobileCompat()]).process('.a{--x:color-mix(in oklab,red,blue)}', {from:undefined}).css, /Unsupported color-mix fallback/);
});

test('light and dark semantic RGB fallbacks preserve readable foreground on tinted surfaces', async () => {
  const theme = postcss.parse(read('theme-tokens.css'));
  const rgb = (selector, role) => {
    let channels;
    theme.walkRules(rule => {
      if (rule.selector !== selector) return;
      rule.walkDecls(`--ob-${role}-rgb`, declaration => {
        channels = declaration.value.split(/\s+/).map(Number);
      });
    });
    assert.ok(channels?.length === 3 && channels.every(Number.isFinite), `${selector}: ${role}`);
    return channels;
  };
  const luminance = channels => channels.map(n => {
    const srgb = n / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
  }).reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0);
  const colorCss = (await postcss([mobileCompat()]).process(read('dark-theme.css'), {from:undefined})).root;
  const fallback = [];
  colorCss.walkAtRules('supports', guard => {
    if (guard.params !== 'not (color: color-mix(in srgb, red, blue))') return;
    guard.walkDecls('--el-color-primary-light-9', decl => fallback.push(decl.value));
  });
  assert.deepEqual(fallback, ['rgb(var(--ob-blue-rgb) / 0.1)']);
  for (const mode of [':root', 'html.dark']) {
    const surface = rgb(mode, 'surface');
    const blue = rgb(mode, 'blue');
    const ink = rgb(mode, 'text');
    const tinted = blue.map((n, i) => 0.1 * n + 0.9 * surface[i]);
    const [light, dark] = [luminance(tinted), luminance(ink)].sort((a, b) => b - a);
    assert.ok((light + 0.05) / (dark + 0.05) >= 4.5, `${mode}: semantic text on accent tint`);
  }
});

test('actual PostCSS plugin chain compiles scoped CSS and keeps guards in emitted CSS', async () => {
  assert.deepEqual(config.plugins.map(plugin => plugin.postcssPlugin), ['openbear-mobile-css-compat', 'tailwindcss', 'autoprefixer']);
  const theme = await postcss(config.plugins).process(read('dark-theme.css'), {from:path.join(src, 'dark-theme.css')});
  assert.match(theme.css, /@supports not \(color: color-mix\(in srgb, red, blue\)\)/);
  for (const name of ['views/StatisticsView.vue', `${VIEWS}ConsoleComposer.vue`]) {
    const descriptor = parse(read(name)).descriptor;
    const style = descriptor.styles[0];
    const result = await compileStyleAsync({source:style.content,filename:name,id:'data-v-compat',scoped:style.scoped,postcssPlugins:config.plugins});
    assert.deepEqual(result.errors, [], name);
    assert.match(result.code, /@supports not \((?:color: color-mix\(in srgb, red, blue\)|height: 100dvh)\)/);
  }
});

test('teleported drawer inline sizes use inherited visualViewport pixels and vh when unset; heatmap remains themed', async () => {
  const menu = descriptor('MobileConversationTools').template.content;
  const hidden = descriptor('HiddenMessagesDrawer').template.content;
  assert.match(menu, /size="min\(calc\(var\(--mobile-viewport-height, 100vh\) \* \.6\), 30rem\)"/);
  assert.match(hidden, /phone \? 'min\(calc\(var\(--mobile-viewport-height, 100vh\) \* \.78\), 44rem\)' : '420px'/);
  const stat = parse(read('views/StatisticsView.vue')).descriptor;
  assert.match(stat.scriptSetup.content, /'--heat-opacity': percent \/ 100, background: `color-mix\(/);
  assert.match(stat.styles[0].content, /\.heat-cell\{[^}]*background:linear-gradient\(rgb\(var\(--ob-blue-rgb\) \/ var\(--heat-opacity\)\)/);
  assert.match(stat.styles[0].content, /--stat-cyan:#0e98a7/);
  const css = await postcss([mobileCompat()]).process(stat.styles[0].content, {from:undefined});
  assert.match(css.css, /rgb\(14 152 167 \/ 0\.13\)/);
});
