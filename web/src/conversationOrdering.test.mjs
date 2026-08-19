import assert from "node:assert/strict";
import test from "node:test";

import {
  compareConversationCreatedAtDesc,
  mergeConversationRows,
  normalizeConversationRows,
} from "./conversationOrdering.js";


test("pinned conversations stay above ordinary rows and each group is newest-first", () => {
  const rows = [
    { conversationUuid: "same-id-20", createdAt: 100, updatedAt: 1, pinned: false },
    { conversationUuid: "old-pinned", createdAt: 50, updatedAt: 9999, pinnedAt: 9999, pinned: true },
    { conversationUuid: "new", createdAt: 200, updatedAt: 0, pinned: false },
    { conversationUuid: "same-id-10", createdAt: 100, updatedAt: 99999, pinned: true },
    { conversationUuid: "legacy-null", createdAt: null, updatedAt: 999999 },
  ];

  assert.deepEqual(
    normalizeConversationRows(rows).map((row) => row.conversationUuid),
    ["same-id-10", "old-pinned", "new", "same-id-20", "legacy-null"],
  );
  assert.equal(compareConversationCreatedAtDesc(rows[0], rows[3]), 0);
});


test("manual display ranks reorder only within existing pin groups", () => {
  const rows = [
    { conversationUuid: "normal-late", createdAt: 300, displayOrder: 30 },
    { conversationUuid: "pinned-late", createdAt: 400, pinned: true, displayOrder: 20 },
    { local: true, conversationUuid: "local:new", createdAt: 999999 },
    { conversationUuid: "normal-first", createdAt: 100, displayOrder: 10 },
    { conversationUuid: "archived-normal", createdAt: 200, archived: true, displayOrder: 20 },
    { conversationUuid: "pinned-first", createdAt: 10, pinned: true, displayOrder: 10 },
  ];

  assert.deepEqual(
    normalizeConversationRows(rows).map((row) => row.conversationUuid),
    ["pinned-first", "pinned-late", "local:new", "normal-first", "archived-normal", "normal-late"],
  );
});


test("local drafts remain explicit rows when no persisted conversation is pinned", () => {
  const rows = [
    { conversationUuid: "persisted-old", createdAt: 10 },
    { local: true, conversationUuid: "local:new", createdAt: 999999 },
    { conversationUuid: "persisted-new", createdAt: 20 },
  ];

  assert.deepEqual(
    normalizeConversationRows(rows).map((row) => row.conversationUuid),
    ["local:new", "persisted-new", "persisted-old"],
  );
});


test("API refresh moves a newly pinned row to the top without losing its state", () => {
  const draft = { local: true, conversationUuid: "local:new", status: "draft" };
  const current = [
    draft,
    { conversationUuid: "newer", createdAt: 200, updatedAt: 200, pinned: false, status: "idle" },
    { conversationUuid: "older", createdAt: 100, updatedAt: 100, pinned: false, status: "idle" },
  ];
  const refreshed = [
    { conversationUuid: "newer", createdAt: 200, updatedAt: 201, pinned: false, status: "idle" },
    { conversationUuid: "older", createdAt: 100, updatedAt: 999999, pinnedAt: 999999, pinned: true, status: "running" },
  ];

  const merged = mergeConversationRows(current, refreshed);

  assert.deepEqual(merged.map((row) => row.conversationUuid), ["older", "local:new", "newer"]);
  assert.equal(merged[1], draft);
  assert.equal(merged[0].status, "running");
  assert.equal(merged[0].pinned, true);
});


test("API refresh restores an unpinned row to its chronological position", () => {
  const current = [
    { conversationUuid: "older", createdAt: 100, pinned: true },
    { conversationUuid: "newer", createdAt: 200, pinned: false },
  ];
  const refreshed = [
    { conversationUuid: "newer", createdAt: 200, pinned: false },
    { conversationUuid: "older", createdAt: 100, pinned: false },
  ];

  assert.deepEqual(
    mergeConversationRows(current, refreshed).map((row) => row.conversationUuid),
    ["newer", "older"],
  );
});
