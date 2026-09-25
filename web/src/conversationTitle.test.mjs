import test from "node:test";
import assert from "node:assert/strict";
import {
  conversationTitleHalfUnits,
  initialConversationTitle,
  titleGraphemes,
  truncateConversationTitle,
} from "./conversationTitle.js";

test("conversation title uses twelve CJK display units and preserves graphemes", () => {
  assert.equal(conversationTitleHalfUnits("会话ABC123"), 10);
  assert.equal(conversationTitleHalfUnits("e\u0301"), 1);
  assert.equal(conversationTitleHalfUnits("👨‍👩‍👧‍👦"), 2);
  assert.deepEqual(titleGraphemes("A👨‍👩‍👧‍👦B"), ["A", "👨‍👩‍👧‍👦", "B"]);
  assert.equal(truncateConversationTitle("一二三四五六七八九十十一十二十三"), "一二三四五六七八九十十一");
  assert.equal(truncateConversationTitle("abcdefghijklmnopqrstuvwxy"), "abcdefghijklmnopqrstuvwx");
  assert.equal(truncateConversationTitle("一二三四五六七八九十甲👨‍👩‍👧‍👦尾"), "一二三四五六七八九十甲👨‍👩‍👧‍👦");
  assert.equal(initialConversationTitle("  OpenBear   会话命名功能需要调整  "), "OpenBear 会话命名功能需");
});
