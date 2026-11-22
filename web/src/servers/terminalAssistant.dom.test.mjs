// The terminal and the page assistant, in happy-dom with the xterm stand-in: what the user selected
// in the terminal and its screen reach the page snapshot, and the terminal's two page actions type a
// command into the open shell or read the screen back; a disconnected terminal types nothing.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mountPage} from './testing/pageDom.mjs';
import {network} from '../testing/fakeNetwork.mjs';

const OUTPUT = 'root         1  0.0  /sbin/init\r\nroot       812  2.1  /usr/bin/dockerd -H fd://\r\n$ ';

async function open(t, {state = 'open'} = {}) {
  let socket = null;
  const h = await mountPage(t, {width: 1280, height: 800, load: n => import(`./TerminalPane.vue?fresh=${n}`),
    root: (Pane, h) => ({render: () => h('div', {class: 'servers-ui instance-ui', style: 'position: fixed; inset: 0'}, [h('div', {class: 'tw-panes', style: 'position: absolute; inset: 0'}, [h(Pane, {serverId: 'srv', active: true})])])}),
    handle: async () => [404, {ok: false, error: 'not_found'}],
    before: () => {
      globalThis.__xtermFakes = [];
      network.onSocket = s => { if (/\/api\/servers\/srv\/terminal/.test(s.url)) { socket = s; s.receive(JSON.stringify({type: 'status', state})); s.receive(Buffer.from(OUTPUT)); } };
    }});
  await h.page.waitForFunction(() => document.querySelector('.xterm-rows')?.textContent.includes('dockerd'));
  // The page's own copy of the assistant modules, where the terminal registered itself.
  const {capturePageContext} = await import(`../assistant/pageContext.js?fresh=${h.fresh}`);
  const {runPageRequest} = await import(`../assistant/pageActions.js?fresh=${h.fresh}`);
  return {...h, term: globalThis.__xtermFakes.at(-1), socket: () => socket, capturePageContext, runPageRequest};
}

test('a terminal selection and its screen are in the snapshot; run types the command, read returns the screen', async t => {
  const {term, socket, close, capturePageContext, runPageRequest} = await open(t);
  term.selection = 'root       812  2.1  /usr/bin/dockerd -H fd://';
  const context = capturePageContext({visibleChars: 2000});
  assert.deepEqual(context.selection, {text: term.selection, source: 'terminal'});
  const subject = context.subjects.find(item => item.kind === 'terminal');
  assert.equal(subject.id, 'srv');
  assert.match(subject.details.screen, /dockerd -H fd:\/\//);
  assert.deepEqual(context.actions.map(item => item.name).sort(), ['terminal.read', 'terminal.run']);

  const before = socket().sent.length;
  const ran = await runPageRequest({action: 'invoke', params: {name: 'terminal.run', args: {command: 'ps -o pid,cmd -p 812'}}});
  assert.equal(ran.ok, true);
  assert.deepEqual(socket().sent.slice(before).map(String), ['ps -o pid,cmd -p 812\r']);
  const read = await runPageRequest({action: 'invoke', params: {name: 'terminal.read', args: {lines: 2}}});
  assert.equal(read.screen, 'root       812  2.1  /usr/bin/dockerd -H fd://\n$');
  await close();
});

test('a terminal that is not connected types nothing', async t => {
  const {socket, close, runPageRequest} = await open(t, {state: 'closed'});
  const before = socket().sent.length;
  const ran = await runPageRequest({action: 'invoke', params: {name: 'terminal.run', args: {command: 'uptime'}}});
  assert.equal(ran.error, 'terminal_not_connected');
  assert.equal(socket().sent.length, before);
  await close();
});
