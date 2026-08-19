import test from "node:test";
import assert from "node:assert/strict";
import {currentPageTitle, documentTitle} from "./pageTitle.js";

test("conversation tabs use the selected conversation title with the OpenBear suffix", () => {
  assert.equal(documentTitle({page: "console"}), "新会话 - OpenBear");
  assert.equal(documentTitle({page: "console", conversationTitle: "控申Demo4"}), "控申Demo4 - OpenBear");
  assert.equal(documentTitle({page: "console", conversationTitle: "  控申Demo4  "}), "控申Demo4 - OpenBear");
});

test("navigation and settings pages use their visible page titles", () => {
  assert.equal(documentTitle({page: "memory"}), "记忆管理 - OpenBear");
  assert.equal(documentTitle({page: "secrets"}), "凭证库 - OpenBear");
  assert.equal(documentTitle({page: "docs"}), "文档库 - OpenBear");
  assert.equal(documentTitle({page: "settings", settingsSection: "channels"}), "渠道管理 - OpenBear");
  assert.equal(documentTitle({page: "settings", settingsSection: "templates"}), "提示词模板 - OpenBear");
  assert.equal(documentTitle({page: "login"}), "登录 - OpenBear");
  assert.equal(currentPageTitle({page: "unknown"}), "OpenBear");
  assert.equal(documentTitle({page: "unknown"}), "OpenBear");
});
