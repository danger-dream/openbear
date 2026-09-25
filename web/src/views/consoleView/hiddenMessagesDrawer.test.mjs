import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parse} from '@vue/compiler-sfc';
import {compile, computed, createSSRApp, effectScope, h, nextTick, ref, watch} from 'vue';
import {renderToString} from 'vue/server-renderer';

const {descriptor} = parse(fs.readFileSync(new URL('./HiddenMessagesDrawer.vue', import.meta.url), 'utf8'));
const script = descriptor.scriptSetup.content.replace(/^import .*;\n/gm, '');
function drawer({phone = false, previewRequest} = {}) {
  const scope = effectScope(), mounted = [], cleanup = [], calls = [];
  const visibility = {managing: ref(true), items: ref([{opId: 'u', type: 'user_message', createdAtMs: 1000}]), busy: ref(false), restoreAll: async () => calls.push('restore-all')};
  const context = vm.createContext({ref, computed, watch, defineProps: () => ({conversationUuid: 'A'}), useMessageVisibility: () => visibility,
    onMounted: fn => mounted.push(fn), onBeforeUnmount: fn => cleanup.push(fn),
    window: {matchMedia: () => ({matches: phone, addEventListener() {}, removeEventListener() {}})},
    Api: {hiddenMessagePreview: async (uuid, id) => {calls.push(['preview', uuid, id]); return previewRequest ? previewRequest() : {operation: {opType: 'user_message', payload: {text: 'private text', attachments: [{fileName: 'private.pdf', contentUrl: '/private.pdf'}]}}};}},
    apiError: String, ElMessage: {error: error => calls.push(error)}, ElMessageBox: {confirm: async () => {}},
  });
  scope.run(() => vm.runInContext(script, context));
  mounted.forEach(fn => fn());
  const bindings = vm.runInContext('({props,visibility,managing,items,busy,phone,previewId,preview,loadingId,labels,payload,previewText,files,fileUrl,time,showPreview,restoreAll})', context);
  async function html() {
    const app = createSSRApp({render: compile(descriptor.template.content), setup: () => bindings});
    app.component('ElDrawer', {props: ['modelValue', 'direction', 'size'], setup: (props, {slots}) => () => props.modelValue ? h('section', {'data-direction': props.direction, 'data-size': props.size}, slots.default?.()) : null});
    app.component('ConsoleMarkdown', {props: ['text'], setup: props => () => h('p', props.text)});
    for (const icon of ['Hide', 'View', 'RefreshLeft']) app.component(icon, {render: () => h('svg')});
    return renderToString(app);
  }
  return {visibility, calls, html, run: expression => vm.runInContext(expression, context), stop: () => {cleanup.forEach(fn => fn()); scope.stop();}};
}

test('hidden manager never previews implicitly; explicit preview stays local and close forgets content', async () => {
  const d = drawer();
  try {
    let html = await d.html();
    assert.match(html, /用户消息|查看原文/);
    assert.doesNotMatch(html, /private text|private.pdf/);
    assert.match(html, /data-direction="rtl"/);
    assert.deepEqual(d.calls, []);
    await d.run("showPreview('u')");
    html = await d.html();
    assert.match(html, /private text/); assert.match(html, /private.pdf/);
    assert.deepEqual(d.calls, [['preview', 'A', 'u']]);
    assert.equal(d.visibility.items.value.length, 1);
    d.visibility.managing.value = false;
    await nextTick();
    assert.equal(d.run('preview.value'), null);
    d.visibility.managing.value = true;
    assert.doesNotMatch(await d.html(), /private text|private.pdf/);
    await d.run('restoreAll()');
    assert.deepEqual(d.calls.at(-1), 'restore-all');
  } finally { d.stop(); }
});

test('phone uses bottom sheet; delayed preview cannot reopen after closing', async () => {
  let resolve;
  const d = drawer({phone: true, previewRequest: () => new Promise(r => {resolve = r;})});
  try {
    assert.match(await d.html(), /data-direction="btt"/);
    const pending = d.run("showPreview('u')");
    d.visibility.managing.value = false; await nextTick();
    resolve({operation: {payload: {text: 'late private text'}}});
    await pending;
    assert.equal(d.run('preview.value'), null);
  } finally { d.stop(); }
});
