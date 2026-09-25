import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import postcss from "postcss";
import {compile, computed, createSSRApp, h, nextTick, reactive, ref, watch} from "vue";
import {renderToString} from "vue/server-renderer";
import {parse, compileTemplate, compileStyle} from "@vue/compiler-sfc";
import {activityInteractionTarget, activityLabel, activityState, activityReadRequests, applyActivityReadVersions, createActivityReadTracker, eligibleActivityRead, groupActivityItems, withActivityReadVersion} from "./conversationActivity.js";
import {acceptActivityReadReceipt, applyCatalogPacket, referenceCatalog, stopReferenceCatalog} from "./references/catalog.js";
import {treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop} from "./components/conversationTreeInteractions.js";
const read = file => fs.readFileSync(new URL(file, import.meta.url), "utf8");
const row = (id, extra = {}) => ({conversationUuid: id, kind: "conversation", title: id, folderId: "original-folder", activityVersion: 1, activityReadVersion: 0, activityUnread: true, activityState: "completed", running: false, activityAtMs: 10, activityResult: {opId: "run:" + id, opRevision: 2, status: "completed"}, ...extra});
const snapshot = () => ({item: row("a"), conversationUuid: "a", loadedConversationUuid: "a", visible: true, focused: true, ready: true, atLatest: true, running: false, operation: {opId: "run:a", revision: 2}});

test("virtual directory deduplicates, groups urgent/running/unread, and sorts by state transitions only", () => {
  const items = [row("older"), row("newer", {activityAtMs: 20}), row("run", {running: true}), row("urgent", {running: true, activityState: "waiting"}), row("fail", {activityState: "failed"}), row("older"), row("archive", {archived: true})];
  const before = JSON.stringify(items);
  const groups = groupActivityItems(items);
  assert.deepEqual(groups.map(group => group.key), ["attention", "running", "unread"]);
  assert.deepEqual(groups[2].items.map(item => item.conversationUuid), ["newer", "older"]);
  assert.equal(groups.flatMap(group => group.items).length, 5);
  assert.equal(JSON.stringify(items), before, "no original folder or item mutation");
  assert.equal(activityLabel(items[3]), "待处理");
});

test("waiting types and targets remain actionable after reading, sort ahead of errors and clear only with live state", () => {
  const waiting = row("b", {running: true, activityState: "waiting", activityAtMs: 1, activityPending: [{interactionId: "form-b", action: "questionnaire"}]});
  for (const [action, label] of Object.entries({confirm: "待确认", select: "待选择", prompt: "待填写", questionnaire: "待填写问卷"})) {
    assert.equal(activityLabel({...waiting, activityPending: [{interactionId: "form-b", action}]}), label);
  }
  const read = withActivityReadVersion(waiting, 1);
  assert.equal(read.activityUnread, false);
  assert.equal(activityLabel(read), "待填写问卷");
  assert.deepEqual(activityInteractionTarget(read), {conversationUuid: "b", interactionId: "form-b"});
  const groups = groupActivityItems([row("error", {activityState: "failed", activityAtMs: 100}), read]);
  assert.deepEqual(groups[0].items.map(item => item.conversationUuid), ["b", "error"]);
  assert.equal(activityLabel({...read, activityPending: [...read.activityPending, {interactionId: "form-c", action: "confirm"}]}), "待填写问卷 · 2项");
  assert.equal(activityInteractionTarget({...read, activityState: "running", activityPending: []}), null);
  assert.equal(activityInteractionTarget(row("completed")), null);
});

test("read receipts cannot resurrect unread through delayed packets or consume a newer completion", () => {
  const versions = new Map([["a", 2]]);
  const status = {items: [row("run", {running: true})], activityItems: [row("a"), row("run", {running: true})]};
  assert.deepEqual(applyActivityReadVersions(status, versions).activityItems.map(item => item.conversationUuid), ["run"]);
  assert.equal(withActivityReadVersion(row("a", {activityVersion: 3}), 2).activityUnread, true);
  assert.deepEqual(activityReadRequests([row("a", {activityVersion: 3}), row("read", {activityUnread: false})]), [{conversationUuid: "a", version: 3}]);
  assert.equal(status.activityItems.length, 2);
});

test("global receipt is cross-component and persists against stale WebSocket status but clears on logout", () => {
  stopReferenceCatalog({clear: true});
  applyCatalogPacket({type: "snapshot", epoch: "e", seq: 1, items: [], treeStatus: {items: [], activityItems: [row("a")]}});
  acceptActivityReadReceipt({items: [{conversationUuid: "a", activityReadVersion: 1}]});
  assert.equal(referenceCatalog.treeStatus.activityItems.length, 0);
  applyCatalogPacket({type: "patch", epoch: "e", previousSeq: 1, seq: 2, treeStatus: {items: [], activityItems: [row("a")]}});
  assert.equal(referenceCatalog.treeStatus.activityItems.length, 0);
  applyCatalogPacket({type: "patch", epoch: "e", previousSeq: 2, seq: 3, treeStatus: {items: [], activityItems: [row("a", {activityVersion: 2})]}});
  assert.equal(referenceCatalog.treeStatus.activityItems.length, 1);
  stopReferenceCatalog({clear: true});
  assert.equal(referenceCatalog.activityReadVersions.size, 0);
});

test("auto read requires actual visible/focused latest result with exact conversation and terminal revision", () => {
  assert.deepEqual(eligibleActivityRead(snapshot()), {conversationUuid: "a", version: 1});
  for (const change of [
    {visible: false}, {focused: false}, {ready: false}, {atLatest: false}, {running: true},
    {loadedConversationUuid: "previous"}, {conversationUuid: "other"}, {operation: undefined},
    {operation: {opId: "run:a", revision: 1}}, {operation: {opId: "other", revision: 2}},
    {item: row("a", {running: true})}, {item: row("a", {activityUnread: false})},
  ]) assert.equal(eligibleActivityRead({...snapshot(), ...change}), null, JSON.stringify(change));
});

function trackerHarness({send} = {}) {
  let current = snapshot(), pending = null, serial = 0;
  const calls = [], receipts = [];
  const tracker = createActivityReadTracker({snapshot: () => current,
    send: async items => { calls.push(items); return send ? send(items) : {items: [{conversationUuid: "a", activityReadVersion: items[0].version}]}; },
    accepted: receipt => receipts.push(receipt), setTimer(fn) { pending = {id: ++serial, fn}; return serial; },
    clearTimer(id) { if (pending?.id === id) pending = null; },
  });
  return {tracker, calls, receipts, set: value => {current = value;}, tick: async () => {const fn = pending?.fn; pending = null; if (fn) await fn();}, hasTimer: () => Boolean(pending)};
}

test("switching/backgrounding during dwell cancels read and opening again acknowledges once", async () => {
  const h = trackerHarness();
  h.tracker.schedule(); h.set({...snapshot(), focused: false}); await h.tick();
  assert.equal(h.calls.length, 0);
  h.set(snapshot()); h.tracker.schedule(); await h.tick();
  assert.deepEqual(h.calls, [[{conversationUuid: "a", version: 1}]]);
  h.tracker.schedule(); await h.tick(); assert.equal(h.calls.length, 1);
  h.tracker.dispose();
});

test("new completion arriving during read HTTP remains eligible and uses a fresh version", async () => {
  let resolve;
  const h = trackerHarness({send: items => items[0].version === 1 ? new Promise(r => {resolve = r;}) : Promise.resolve({items: []})});
  h.tracker.schedule(); const pending = h.tick();
  h.set({...snapshot(), item: row("a", {activityVersion: 2})});
  resolve({items: [{conversationUuid: "a", activityReadVersion: 1}]}); await pending;
  await h.tick();
  assert.deepEqual(h.calls.map(items => items[0].version), [1, 2]);
  h.tracker.dispose();
});

test("failed HTTP never consumes a completion and unmount clears the pending retry", async () => {
  const h = trackerHarness({send: async () => {throw Error("offline");}});
  h.tracker.schedule(); await h.tick();
  assert.equal(h.receipts.length, 0); assert.equal(h.hasTimer(), true);
  h.tracker.dispose(); await h.tick(); assert.equal(h.calls.length, 1);
});

test("real recent folder renders one compact list and keeps just-read selected row until leaving", async t => {
  const source = read("./components/ConversationActivityFolder.vue");
  const {descriptor} = parse(source);
  assert.deepEqual(compileTemplate({source: descriptor.template.content, filename: "activity.vue", id: "activity"}).errors, []);
  const props = reactive({items: [row("completed", {activityAtMs: Date.now() - 180000}), row("working", {running: true, activityUnread: false})], activeConversationUuid: "completed", readVersions: new Map(), titleGenerating: new Set(), busy: false});
  const ctx = vm.createContext({computed, ref, onMounted() {}, onBeforeUnmount() {}, watch: (...args) => {const stop = watch(...args); t.after(stop); return stop;}, activityLabel, activityState, groupActivityItems,
    referenceItem: () => null, defineProps: () => props, defineEmits: () => () => {}});
  vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*?;\n/gm, ""), ctx);
  for (const icon of ["ArrowRight", "Folder", "FolderOpened", "Check", "MoreFilled"]) ctx[icon] = {render: () => h("svg")};
  ctx.AnimatedConversationTitle = {props: ["text"], render() {return h("span", this.text);}};
  // New SSR app per render, keeping the component's actual reactive state.
  const renderFolder = () => renderToString(createSSRApp({components: {ArrowRight: ctx.ArrowRight, Check: ctx.Check, MoreFilled: ctx.MoreFilled, AnimatedConversationTitle: ctx.AnimatedConversationTitle}, render: compile(descriptor.template.content), setup: () => vm.runInContext("({...props, props, emit, expanded, rows, unreadCount, waitingCount, rowState, rowLabel, rowTitle, liveTitle, isTitleGenerating, ArrowRight, Folder, FolderOpened, Check, MoreFilled, AnimatedConversationTitle})", ctx)}));
  let html = await renderFolder();
  assert.match(html, /最近会话/); assert.match(html, /完成待查看/); assert.match(html, /全部标为已读/);
  assert.doesNotMatch(html, /运行与未读|<h5|data-activity-group/);
  assert.doesNotMatch(html, /draggable=|data-tree-id=/);
  assert.doesNotMatch(html, /activity-unread-slot/, "no invisible unread slot on read or running rows");
  assert.equal((html.match(/class="activity-unread-dot"/g) || []).length, 1, "only the actual unread row has a dot");
  const unreadRow = html.match(/data-activity-id="completed"[\s\S]*?<\/div>/)[0];
  assert.ok(unreadRow.indexOf('class="activity-row-read"') < unreadRow.indexOf('class="activity-row-status activity-row-status-desktop"'), "the actual action is before the right-aligned status, never a blank column after it");
  assert.equal((html.match(/class="activity-row-read"/g) || []).length, 1, "only unread rows have an actual action, no invisible button for running rows");
  props.titleGenerating = new Set(["working"]); await nextTick();
  html = await renderFolder(); assert.match(html, /data-activity-id="working"[^>]*aria-busy="true"/); assert.match(html, /正在生成名称/);
  props.titleGenerating = new Set();
  props.readVersions = new Map([["completed", 1]]); props.items = [row("working", {running: true})]; await nextTick();
  html = await renderFolder(); assert.match(html, /3 分钟前/); assert.match(html, /data-activity-id="completed"/);
  const justRead = html.match(/data-activity-id="completed"[\s\S]*?<\/div>/)[0];
  assert.doesNotMatch(justRead, /activity-row-read|activity-unread-dot|activity-unread-slot/);
  assert.match(justRead, /class="activity-row-status activity-row-status-desktop"[^>]*>3 分钟前<\/span><\/div>$/, "read normal result uses time as the last content, with no trailing placeholder");
  props.activeConversationUuid = "working"; await nextTick();
  html = await renderFolder(); assert.doesNotMatch(html, /data-activity-id="completed"/);
  props.items = [row("wait", {running: true, activityState: "waiting", activityUnread: false, activityPending: [{interactionId: "form", action: "questionnaire"}]})];
  await nextTick();
  html = await renderFolder(); assert.match(html, /待处理 1/); assert.match(html, /待填写问卷/);
  vm.runInContext("expanded.value = false", ctx);
  html = await renderFolder(); assert.match(html, /待处理 1/); assert.doesNotMatch(html, /data-activity-id="wait"/);
  props.items = []; await nextTick();
  html = await renderFolder(); assert.doesNotMatch(html, /activity-waiting-count/);
  assert.match(source, /max-height: min\(32vh, 260px\)/);
  const tree = read("./components/ConversationTree.vue");
  assert.ok(tree.indexOf("<ConversationActivityFolder") < tree.indexOf('<div ref="listRef"'));
});

test("phone and touch activity styles give separate 44px actions, wrapping titles and bounded scrolling without changing desktop rows", () => {
  const {descriptor} = parse(read("./components/ConversationActivityFolder.vue"));
  const css = postcss.parse(descriptor.styles[0].content);
  const mobile = css.nodes.find(node => node.type === "atrule" && node.params === "(max-width: 760px), (pointer: coarse)");
  assert.ok(mobile);
  const rule = (parent, selector) => Object.fromEntries(parent.nodes.filter(node => node.type === "rule" && node.selector === selector).flatMap(node => node.nodes.filter(decl => decl.type === "decl").map(decl => [decl.prop, decl.value])));
  assert.equal(rule(css, ".activity-row-open").height, "29px");
  assert.equal(rule(css, ".activity-row").display, "flex");
  assert.equal(rule(css, ".activity-row")["grid-template-columns"], undefined, "desktop must not reserve an absent read button");
  assert.equal(rule(mobile, ".activity-row")["grid-template-columns"], undefined, "touch must not reserve an absent 44px action either");
  assert.equal(rule(css, ".activity-row-status-desktop").margin, "0 5px 0 6px", "all desktop statuses end at the same small right inset");
  assert.equal(rule(css, ".activity-row-status-touch").display, "none");
  assert.equal(rule(mobile, ".activity-row-status-desktop").display, "none");
  assert.equal(rule(mobile, ".activity-row-status-touch").display, "block");
  assert.equal(rule(css, ".activity-row-read").opacity, "1", "read action is visible without hover on desktop too");
  css.walkRules(node => {
    if (node.selector.includes(".activity-row-read") && /:hover|:focus-within/.test(node.selector)) {
      assert.equal(node.nodes.some(decl => decl.prop === "opacity"), false, "hover must not control whether the action exists visually");
    }
  });
  assert.equal(rule(css, ".activity-row-copy").display, "contents");
  assert.equal(rule(css, ".activity-touch-label").display, "none");
  assert.equal(rule(mobile, ".activity-row-copy")["flex-direction"], "column");
  assert.equal(rule(mobile, ".activity-row-title")["-webkit-line-clamp"], "2");
  assert.equal(rule(mobile, ".activity-row-title")["overflow-wrap"], "anywhere");
  assert.equal(rule(mobile, ".activity-row-read").width, "44px");
  assert.equal(rule(mobile, ".activity-row-read").height, "44px");
  assert.equal(rule(mobile, ".activity-row-read").opacity, "1");
  assert.equal(rule(mobile, ".activity-read-all").height, "44px");
  assert.equal(rule(mobile, ".activity-row-open")["min-height"], "56px");
  assert.equal(rule(mobile, ".activity-folder")["max-height"], "50%");
  assert.equal(rule(mobile, ".activity-folder-content")["max-height"], "min(36dvh, 300px)");
  assert.equal(rule(css, ".activity-folder-content")["overflow-y"], "auto");
  const template = descriptor.template.content;
  assert.match(template, /emit\('read-all'\)[\s\S]*?全部已读/);
  assert.match(template, /emit\('read', item\)[\s\S]*?标已读/);
  assert.match(template, /<\/button>\s*<button v-if="item.activityUnread"/, "read action is not nested in the conversation-opening button; one merged row has one action");
});

test("compiled recent running indicator keeps a valid rotating animation with either motion preference", () => {
  const {descriptor} = parse(read("./components/ConversationActivityFolder.vue"));
  const compiled = compileStyle({source: descriptor.styles[0].content, filename: "activity.vue", id: "data-v-activity", scoped: true});
  assert.deepEqual(compiled.errors, []);
  const css = postcss.parse(compiled.code);
  for (const reduced of [false, true]) {
    let animation;
    css.walkRules(rule => {
      if (!rule.selector.includes(".activity-state-dot.is-running")) return;
      for (let parent = rule.parent; parent; parent = parent.parent) {
        if (parent.type === "atrule" && parent.name === "media" && parent.params.includes("prefers-reduced-motion")
          && parent.params.includes("reduce") && !reduced) return;
      }
      rule.walkDecls("animation", decl => {animation = decl.value;});
    });
    assert.match(animation, /^activity-spin-[\w-]+ 1s linear infinite$/, `running status must keep moving; reduced=${reduced}`);
    const name = animation.split(" ")[0];
    const frames = css.nodes.find(node => node.type === "atrule" && node.name === "keyframes" && node.params === name);
    assert.ok(frames, "scoped animation refers to the emitted keyframes");
    assert.ok(frames.nodes.some(rule => rule.nodes.some(decl => decl.prop === "transform" && decl.value === "rotate(360deg)")));
  }
});

test("real tree alias opens the same row without moving/expanding folders; stale read cannot clear a new completion", async () => {
  const source = parse(read("./components/ConversationTree.vue")).descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, "");
  const emitted = [], sent = [];
  let resolve;
  const catalog = {connected: false, ready: false, activityReadVersions: new Map()};
  const ctx = vm.createContext({computed, nextTick, reactive, ref, rowId, treeItemParent, compareTreeItems, resolveTreeDrop, activityLabel, activityReadRequests,
    // UI import seam only; alias navigation and read-receipt behavior remain real.
    defineLazyView: () => ({}),
    clearTimeout, defineProps: () => ({activeConversationUuid: "", draftConversation: null}), defineEmits: () => (...args) => emitted.push(args), defineExpose() {},
    watch() {}, onMounted() {}, onBeforeUnmount() {}, referenceCatalog: catalog,
    acceptActivityReadReceipt(receipt) {catalog.activityReadVersions = new Map(receipt.items.map(item => [item.conversationUuid, item.activityReadVersion]));},
    Api: {readConversationActivity: items => {sent.push(items); return new Promise(r => {resolve = r;});}}, apiError: String, ElMessage: {error(error) {throw Error(error);}},
  });
  vm.runInContext(source, ctx);
  ctx.initial = {items: [], activityItems: [row("a")]};
  vm.runInContext("selectedFolderId.value = 'keep-selected'; applyStatus(initial); openActivityConversation(activityItems.value[0])", ctx);
  const opened = emitted.find(item => item[0] === "open");
  assert.equal(opened[1].conversationUuid, "a");
  assert.equal(vm.runInContext("selectedFolderId.value", ctx), "keep-selected");
  assert.equal(vm.runInContext("expanded.value.size", ctx), 0);
  assert.equal(vm.runInContext("stateFor('original-folder').items[0].activityUnread", ctx), true);
  const pending = vm.runInContext("markActivityRead(activityItems.value)", ctx);
  ctx.newer = {items: [], activityItems: [row("a", {activityVersion: 2})]};
  vm.runInContext("applyStatus(newer)", ctx);
  resolve({items: [{conversationUuid: "a", activityReadVersion: 1}]}); await pending;
  assert.equal(sent[0][0].version, 1);
  assert.equal(vm.runInContext("activityItems.value[0].activityUnread", ctx), true);
  assert.equal(vm.runInContext("stateFor('original-folder').items[0].activityUnread", ctx), true);
});

test("ConsoleView snapshot rejects stale global status until matching rendered operation is in the focused viewport", () => {
  const source = read("./views/consoleView/ConsoleView.vue");
  const code = source.slice(source.indexOf("function activityReadSnapshot()"), source.indexOf("function scheduleActivityRead()"));
  const ctx = vm.createContext({withActivityReadVersion, props: {navigationObscured: false}, window: {matchMedia: () => ({matches: true})}, activeConversationUuid: ref("a"), referenceCatalog: {treeStatus: {activityItems: [row("a")]}, activityReadVersions: new Map()},
    chatState: ref({conversationUuid: "a"}), scroller: ref({clientHeight: 500}), scrollerDistanceFromBottom: () => 0,
    document: {visibilityState: "visible", hasFocus: () => true}, componentMounted: true, loading: ref(false), streamFlushPending: false,
    turns: ref([{stats: {live: false}}]), running: ref(false), sendPending: ref(false), operationsById: ref(new Map()),
  });
  vm.runInContext(code, ctx);
  assert.equal(eligibleActivityRead(vm.runInContext("activityReadSnapshot()", ctx)), null);
  ctx.operationsById.value.set("run:a", {opId: "run:a", revision: 2});
  assert.deepEqual(eligibleActivityRead(vm.runInContext("activityReadSnapshot()", ctx)), {conversationUuid: "a", version: 1});
  ctx.streamFlushPending = true; assert.equal(eligibleActivityRead(vm.runInContext("activityReadSnapshot()", ctx)), null);
  ctx.streamFlushPending = false;
  ctx.props.navigationObscured = true;
  for (const width of [320, 375, 414, 760, 768, 1024]) {
    ctx.window.matchMedia = query => {assert.equal(query, "(max-width: 760px)"); return {matches: width <= 760};};
    assert.equal(Boolean(eligibleActivityRead(vm.runInContext("activityReadSnapshot()", ctx))), width > 760, `drawer occlusion at ${width}px`);
  }
  ctx.window.matchMedia = () => ({matches: true});
  ctx.props.navigationObscured = false;
  assert.ok(eligibleActivityRead(vm.runInContext("activityReadSnapshot()", ctx)), "closing phone drawer permits viewing again");
  assert.match(read("./App.vue"), /:navigation-obscured="sidebarOpen"/);
  assert.match(source, /watch\(\(\) => \[props\.conversationUuid, props\.navigationObscured/);
  assert.match(source, /window\.addEventListener\("resize", scheduleActivityRead\)/);
  assert.match(source, /window\.removeEventListener\("resize", scheduleActivityRead\)/);
  assert.match(source, /activityReadTracker\?\.dispose\(\)/);
});
