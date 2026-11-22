import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {nextTick, ref} from "vue";
import {activityInteractionTarget} from "./conversationActivity.js";
const read = file => fs.readFileSync(new URL(file, import.meta.url), "utf8");

test("tree opening closes navigation before asking the current conversation to locate a pending form", async () => {
  const source = read("./App.vue");
  const code = source.slice(source.indexOf("async function handleTreeOpen(row)"), source.indexOf("function currentDraftConversation()"));
  const events = [];
  const activeConversationUuid = ref("c");
  const ctx = vm.createContext({activityInteractionTarget, activeConversationUuid, nextTick,
    openConversation: async row => {events.push("open:" + row.conversationUuid); activeConversationUuid.value = row.conversationUuid;},
    consoleViewRef: ref({focusPendingInteraction: target => events.push("focus:" + target.interactionId)}),
  });
  vm.runInContext(code, ctx);
  ctx.row = {conversationUuid: "b", running: true, activityState: "waiting", activityPending: [{interactionId: "form-b"}]};
  await vm.runInContext("handleTreeOpen(row)", ctx);
  assert.deepEqual(events, ["open:b", "focus:form-b"]);
  ctx.row = {conversationUuid: "c", running: true, activityState: "running"};
  await vm.runInContext("handleTreeOpen(row)", ctx);
  assert.deepEqual(events, ["open:b", "focus:form-b", "open:c"]);
  ctx.openConversation = async () => {}; // Navigation was suppressed or superseded.
  ctx.row = {conversationUuid: "b", running: true, activityState: "waiting", activityPending: [{interactionId: "form-b"}]};
  await vm.runInContext("handleTreeOpen(row)", ctx);
  assert.equal(events.length, 3);
});

test("pending form navigation waits for matching conversation and authenticated forms, and cancels when switching away", async () => {
  const source = read("./views/consoleView/ConsoleView.vue");
  const code = source.slice(source.indexOf("function focusPendingInteraction(target)"), source.indexOf("function normalizeConfirmations("));
  const focused = [];
  const ctx = vm.createContext({props: {conversationUuid: "b"}, pendingInteractionFocus: ref(null), nextTick, watch() {}, loading: ref(true), chatState: ref({conversationUuid: "c"}),
    pendingConfirmations: ref([]), composer: ref({focusInteraction: id => {focused.push(id); return true;}}),
  });
  vm.runInContext(code, ctx);
  vm.runInContext("focusPendingInteraction({conversationUuid:'b', interactionId:'form-b'})", ctx);
  await nextTick(); assert.deepEqual(focused, []);
  ctx.loading.value = false;
  vm.runInContext("tryFocusPendingInteraction()", ctx); assert.deepEqual(focused, []);
  ctx.chatState.value = {conversationUuid: "b"};
  vm.runInContext("tryFocusPendingInteraction()", ctx); assert.deepEqual(focused, []);
  ctx.pendingConfirmations.value = [{confirmationId: "form-b"}];
  vm.runInContext("tryFocusPendingInteraction()", ctx);
  assert.deepEqual(focused, ["form-b"]);
  assert.equal(ctx.pendingInteractionFocus.value, null);
  vm.runInContext("focusPendingInteraction({conversationUuid:'b', interactionId:'later'})", ctx);
  ctx.props.conversationUuid = "c";
  vm.runInContext("tryFocusPendingInteraction()", ctx);
  assert.equal(ctx.pendingInteractionFocus.value, null);
  await nextTick(); assert.deepEqual(focused, ["form-b"]);
});

test("composer delegates focusInteraction to PendingInteractionSheet, focusing the exact live card without focusing inputs or expired requests", () => {
  const composerSource = read("./views/consoleView/ConsoleComposer.vue");
  const sheetSource = read("./views/consoleView/PendingInteractionSheet.vue");
  assert.match(composerSource, /function focusInteraction\(interactionId\)\s*\{\s*return Boolean\(interactionSheet\.value\?\.focusInteraction\?\.\(interactionId\)\);\s*\}/);
  const code = sheetSource.slice(sheetSource.indexOf("function focusInteraction(interactionId)"), sheetSource.indexOf("defineExpose("));
  const calls = [];
  let expanded = 0;
  const activeId = ref("");
  const card = id => ({dataset: {interactionId: id}, scrollIntoView: options => calls.push([id, "scroll", options.block]), focus: options => calls.push([id, "focus", options.preventScroll])});
  const props = {items: [{confirmationId: "target"}, {confirmationId: "expired", expired: true}]};
  const ctx = vm.createContext({
    props,
    activeId,
    nextTick,
    isExpired: item => Boolean(item.expired),
    expandPanel: () => { expanded++; },
    sheetEl: ref({querySelectorAll: selector => {assert.equal(selector, "[data-interaction-id]"); return [card("other"), card("target")];}}),
    rootEl: ref(null),
    document: undefined,
  });
  vm.runInContext(code, ctx);
  assert.equal(vm.runInContext("focusInteraction('target')", ctx), true);
  assert.equal(activeId.value, "target");
  assert.equal(expanded, 1);
  assert.deepEqual(calls, [["target", "scroll", "nearest"], ["target", "focus", true]]);
  assert.equal(vm.runInContext("focusInteraction('expired')", ctx), false);
  assert.equal(vm.runInContext("focusInteraction('gone')", ctx), false);
  assert.equal(calls.length, 2);
  assert.match(sheetSource, /:data-interaction-id="activeItem\.confirmationId"[\s\S]*?tabindex="-1"/);
});
