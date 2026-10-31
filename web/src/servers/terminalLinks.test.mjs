import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { addressParts, linkInfo, shownHost } from './terminalLinks.js';

test('a link whose text names another host than the one it opens is called out', () => {
  const fake = linkInfo('https://paypal.com', 'https://evil.example/login');
  assert.deepEqual(fake, {url: 'https://evil.example/login', href: 'https://evil.example/login', host: 'evil.example', shown: 'https://paypal.com', shownHost: 'paypal.com', mismatch: true});
  // Look-alikes: another script's letter, a longer host that starts the same, dots a browser reads as dots.
  assert.equal(linkInfo('p\u0430ypal.com', 'https://paypal.com/').mismatch, true);
  assert.equal(linkInfo('evil.com', 'https://evil.com.attacker.net/').mismatch, true);
  assert.equal(linkInfo('paypal\u3002com', 'https://evil.example/').shownHost, 'paypal.com');
  assert.equal(linkInfo('看 https://paypal.com@evil.example/', 'https://paypal.com@evil.example/').mismatch, true, 'userinfo that looks like a host');
});

test('text that names the same host, or no host at all, is not a mismatch', () => {
  assert.equal(linkInfo('example.com/docs', 'https://www.example.com/docs').mismatch, false);
  assert.equal(linkInfo('1.2.3.4', 'http://1.2.3.4:8080/').mismatch, false);
  assert.equal(linkInfo('例子.测试', 'https://例子.测试/').mismatch, false, 'IDN compared as the browser writes it');
  const words = linkInfo('使用说明', 'https://docs.example.com/guide');
  assert.equal(words.mismatch, false);
  assert.equal(words.shown, '使用说明');
  assert.equal(linkInfo('https://example.com', 'https://example.com').shown, '', 'text that is the address itself is not repeated');
});

test('what is shown can be trusted to read as it is: no hidden marks, escaped and bounded', () => {
  const info = linkInfo('ab\u202ecd.com\u0007', 'https://例子.测试/路径?q=\u202e');
  assert.equal(info.shown, 'abcd.com');
  assert.equal(info.host, 'xn--fsqu00a.xn--0zwm56d', 'the host as punycode');
  assert.doesNotMatch(info.href, /[\u202e\u0000-\u001f]/);
  assert.ok(linkInfo('x'.repeat(5000), 'https://a.example/' + 'p'.repeat(5000)).href.length <= 2001);
  assert.equal(linkInfo('x', 'javascript:alert(1)').url, '', 'only web addresses are ever opened');
  assert.equal(shownHost('no host here'), '');
  assert.deepEqual(addressParts(linkInfo('', 'https://paypal.com@evil.example/x')), ['https://paypal.com@', 'evil.example', '/x']);
  assert.deepEqual(addressParts(linkInfo('', 'http://1.2.3.4:8080/')), ['http://', '1.2.3.4', ':8080/']);
});

test('the terminal routes OSC 8 links through hover, leave and a confirmed activate', () => {
  const pane = fs.readFileSync(new URL('./TerminalPane.vue', import.meta.url), 'utf8');
  assert.match(pane, /linkHandler: \{activate: openLink, hover: showTip, leave: hideTip\}/);
  // The window opens inside the button's click (beforeClose), not after the box has gone.
  assert.match(pane, /beforeClose\(action, _, done\) \{ if \(action === 'confirm'\) window\.open\(info\.url, '_blank', 'noopener,noreferrer'\); done\(\); \}/);
  assert.doesNotMatch(pane, /innerHTML|dangerouslyUseHTMLString/);
});
