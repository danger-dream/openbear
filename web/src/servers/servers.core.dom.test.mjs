// CORE-C1: actual TrafficSection request/response and history disclosure, no real API.
import test from 'node:test';
import assert from 'node:assert/strict';
import {window, resize} from '../domTestEnv.mjs';
import './testing/serversStubs.mjs';
import {createApp, h, nextTick} from 'vue';
import ElementPlus from 'element-plus';
const {Api} = await import('../api.js');
const TrafficSection = (await import('./detail/TrafficSection.vue')).default;

async function settle() {
  await nextTick();
  await new Promise(resolve => setImmediate(resolve));
  await nextTick();
}

for (const phone of [false, true]) {
  test(`traffic history: retained basis is disclosed, not guessed (${phone ? 'phone' : 'desktop'})`, async t => {
    resize(phone ? 375 : 1280, 800);
    const calls = [];
    t.mock.method(Api, 'serverTrafficStats', async (sid, unit) => {
      calls.push([sid, unit]);
      return {ok: true, unit, timezone: 'America/Los_Angeles', currentLabel: unit === 'month' ? '2026-10' : '2026-10-31',
        points: [{t: 1793491200, label: unit === 'month' ? '2026-11' : '2026-11-01', rx: 300, tx: 30}],
        history: unit === 'hour' ? [] : [
          {day: '2026-11-01', timezone: 'Asia/Shanghai', rx: 200, tx: 20},
          {day: '2026-10-31', timezone: null, rx: 100, tx: 10},
        ]};
    });
    const host = document.createElement('div');
    document.body.append(host);
    const app = createApp({render: () => h(TrafficSection, {server: {id: 's'}, active: true,
      now: 1793491200000, timezone: 'America/Los_Angeles', phone})});
    const errors = [];
    app.config.errorHandler = e => errors.push(e);
    app.use(ElementPlus);
    app.mount(host);
    t.after(() => { app.unmount(); host.remove(); assert.deepEqual(errors, []); });
    await settle();
    assert.deepEqual(calls, [['s', 'day']]);
    const details = host.querySelector('[data-traffic-history]');
    assert.ok(details);
    assert.equal(details.open, false, 'long history is collapsed by default');
    details.querySelector('summary').click();
    await settle();
    assert.equal(details.open, true, 'the disclosure is touch/click accessible');
    assert.match(details.textContent, /Asia\/Shanghai/);
    assert.match(details.textContent, /历史口径未记录/);
    assert.match(details.textContent, /已包含在图表合计中/);
    assert.match(host.querySelector('.dt-total').textContent, /下行合计 300 B/);
    assert.equal(host.querySelector('.tc-bar.is-partial'), null, 'retained next-month label is not marked accumulating');
    const tabs = [...host.querySelectorAll('[aria-label="统计单位"] button')];
    tabs.find(el => el.textContent === '按月').click();
    await settle();
    assert.deepEqual(calls.at(-1), ['s', 'month']);
    assert.ok(host.querySelector('[data-traffic-history]'));
    tabs.find(el => el.textContent === '按小时').click();
    await settle();
    assert.deepEqual(calls.at(-1), ['s', 'hour']);
    assert.equal(host.querySelector('[data-traffic-history]'), null, 'hourly sources never inherit legacy daily labels');
    assert.equal(window.innerWidth, phone ? 375 : 1280);
  });
}
