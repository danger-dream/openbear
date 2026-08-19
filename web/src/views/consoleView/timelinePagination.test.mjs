import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

import {
  capturePrependAnchor,
  findTurnIndexByIdentity,
  mergeOperationSnapshots,
  prependAnchoredScrollTop,
  sameTimelinePageRequest,
  settleTimelinePageRequest,
  shouldRequestEarlierPage,
  stableTurnIdentity,
  touchMovesTimelineUp,
} from "./timelinePagination.js";

const consoleSource = readFileSync(new URL("./ConsoleView.vue", import.meta.url), "utf8");
const workDetailSource = readFileSync(new URL("./TurnWorkDetailPanel.vue", import.meta.url), "utf8");

test("operation pages prepend in displaySeq order without stale revision overwrite or duplicates", () => {
  const current = [
    {opId: "tail-a", displaySeq: 300, revision: 3, payload: {text: "live"}},
    {opId: "tail-b", displaySeq: 400, revision: 1},
  ];
  const incoming = [
    {opId: "old-a", displaySeq: 100, revision: 1},
    {opId: "tail-a", displaySeq: 300, revision: 2, payload: {text: "stale page"}},
    {opId: "old-b", displaySeq: 200, revision: 1},
  ];

  const merged = mergeOperationSnapshots(current, incoming);

  assert.deepEqual(merged.map((operation) => operation.opId), ["old-a", "old-b", "tail-a", "tail-b"]);
  assert.equal(merged.find((operation) => operation.opId === "tail-a").payload.text, "live");
  assert.equal(new Set(merged.map((operation) => operation.opId)).size, merged.length);
});

test("older page keeps server tie order when the active extra has the higher id", () => {
  const current = [
    {opId: "terminal-lower-id", displaySeq: 100, operationOrder: 11, revision: 1},
    {opId: "active-higher-id", displaySeq: 100, operationOrder: 12, revision: 3, payload: {text: "live"}},
  ];
  const incoming = [
    {opId: "older-page-row", displaySeq: 90, operationOrder: 10, revision: 1},
    {opId: "active-higher-id", displaySeq: 100, operationOrder: 12, revision: 2, payload: {text: "stale snapshot"}},
  ];

  const merged = mergeOperationSnapshots(current, incoming);

  assert.deepEqual(merged.map((operation) => operation.opId), ["older-page-row", "terminal-lower-id", "active-higher-id"]);
  assert.equal(merged[2].payload.text, "live");
});

test("older-page gate requires real upward input and blocks prepend compensation", () => {
  const scrollbarUp = {
    userIntent: true,
    previousScrollTop: 160,
    scrollTop: 120,
    threshold: 180,
  };
  assert.equal(shouldRequestEarlierPage(scrollbarUp), true);
  assert.equal(shouldRequestEarlierPage({...scrollbarUp, previousScrollTop: 80, scrollTop: 120}), false);
  assert.equal(shouldRequestEarlierPage({...scrollbarUp, previousScrollTop: 120, scrollTop: 120}), false);
  assert.equal(shouldRequestEarlierPage({...scrollbarUp, userIntent: false}), false);

  assert.equal(shouldRequestEarlierPage({
    userIntent: true,
    explicitUpward: true,
    scrollTop: 0,
    threshold: 180,
  }), true);
  assert.equal(touchMovesTimelineUp(100, 130), true);
  assert.equal(touchMovesTimelineUp(130, 100), false);
  assert.equal(touchMovesTimelineUp(null, 100), false);

  // The exact same upward/top facts are rejected while prepend height
  // compensation is programmatic, so one page cannot trigger the next.
  assert.equal(shouldRequestEarlierPage({...scrollbarUp, programmaticScroll: true}), false);
  assert.equal(shouldRequestEarlierPage({
    programmaticScroll: true,
    userIntent: true,
    explicitUpward: true,
    scrollTop: 0,
    threshold: 180,
  }), false);
});

test("prepend anchor offsets scrollTop by exactly the newly inserted height", () => {
  const anchor = capturePrependAnchor({scrollTop: 72, scrollHeight: 900});
  assert.deepEqual(anchor, {scrollTop: 72, scrollHeight: 900});
  assert.equal(prependAnchoredScrollTop(anchor, 1235), 407);
  assert.equal(prependAnchoredScrollTop(anchor, 850), 72);
});

test("an old generation cannot settle a newer conversation page request", () => {
  const oldRequest = {generation: 7, token: 11, conversationUuid: "conversation-a"};
  const newRequest = {generation: 8, token: 12, conversationUuid: "conversation-b"};

  assert.equal(sameTimelinePageRequest(newRequest, oldRequest), false);
  assert.equal(sameTimelinePageRequest(newRequest, {...newRequest, token: 11}), false);
  assert.equal(sameTimelinePageRequest(newRequest, {...newRequest, generation: 7}), false);
  assert.strictEqual(settleTimelinePageRequest(newRequest, oldRequest), newRequest);
  assert.equal(settleTimelinePageRequest(newRequest, {...newRequest}), null);
});

test("earlier-page loading is observable, accessible, and outside scroll layout flow", () => {
  assert.match(consoleSource, /const timelinePageInFlight = ref\(null\);/);
  const statusMarkup = consoleSource.match(/<div\s+v-if="timelinePageInFlight"[\s\S]*?<\/div>/)?.[0] || "";
  assert.match(statusMarkup, /role="status"/);
  assert.match(statusMarkup, /aria-live="polite"/);
  assert.match(statusMarkup, /<Loading\/>/);
  assert.match(statusMarkup, /正在加载更早内容…/);
  assert.doesNotMatch(statusMarkup, /<button/);

  const statusStyles = consoleSource.match(/\.timeline-page-loading\s*\{[^}]*\}/)?.[0] || "";
  assert.match(statusStyles, /position:\s*absolute/);
  assert.match(statusStyles, /pointer-events:\s*none/);
  assert.ok(consoleSource.indexOf('v-if="timelinePageInFlight"') < consoleSource.indexOf('ref="scroller"'));
  assert.match(consoleSource, /function resetTimelinePagination[\s\S]*?timelinePageInFlight\.value = null;/);
  assert.match(consoleSource, /finally\s*\{\s*timelinePageInFlight\.value = settleTimelinePageRequest/);
});

test("prepend remaps the work-detail turn by UUID, then id, without remounting the panel", () => {
  const selected = {turnUuid: "stable-turn", id: "old-render-id"};
  const identity = stableTurnIdentity(selected);
  const afterPrepend = [
    {turnUuid: "older-a", id: "older-a"},
    {turnUuid: "different-turn", id: "old-render-id"},
    {turnUuid: "stable-turn", id: "new-render-id"},
  ];
  const remappedIndex = findTurnIndexByIdentity(afterPrepend, identity);
  assert.equal(remappedIndex, 2);
  assert.deepEqual(stableTurnIdentity(afterPrepend[remappedIndex]), identity);

  const legacyIdentity = stableTurnIdentity({id: "legacy-turn"});
  assert.deepEqual(legacyIdentity, {field: "id", value: "legacy-turn"});
  assert.equal(findTurnIndexByIdentity([{id: "older"}, {id: "legacy-turn"}], legacyIdentity), 1);

  const pageLoader = consoleSource.slice(
    consoleSource.indexOf("async function loadEarlierOperations"),
    consoleSource.indexOf("function requestEarlierPageFromUser"),
  );
  assert.ok(pageLoader.indexOf("stableTurnIdentity(activeTurn.value)") < pageLoader.indexOf("messages.value = projectOperationMessages(merged)"));
  assert.ok(pageLoader.indexOf("activeTurnIndex.value = remappedActiveTurnIndex") < pageLoader.indexOf("await nextTick()"));

  const panelMarkup = consoleSource.match(/<TurnWorkDetailPanel[\s\S]*?\/>/)?.[0] || "";
  assert.ok(panelMarkup);
  assert.doesNotMatch(panelMarkup, /\bv-if=|(?::|\s)key=/);
  assert.equal((consoleSource.match(/<TurnWorkDetailPanel\b/g) || []).length, 1);
  assert.match(workDetailSource, /transition:\s*flex-basis \.24s[^;]*;?/);
  assert.match(workDetailSource, /transition:\s*transform \.24s[^;]*;?/);
});
