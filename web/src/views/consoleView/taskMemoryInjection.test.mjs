import test from "node:test";
import assert from "node:assert/strict";
import {taskMemoryInjectionPreview, taskMemoryInjectionUsage} from "./taskMemoryInjection.js";

test("preview carries the server-selected short-body policy and omission facts", () => {
  const data = taskMemoryInjectionPreview({catalogXml: "<content>macOS</content>", runtimeSnapshot: "<snapshot><content>macOS</content></snapshot>", estimatedRuntimeTokens: 612,
    maxRuntimeTokens: 1500, shortBodyMaxChars: 500, includedCount: 3, omittedCount: 2});
  assert.deepEqual(data, {text: "<snapshot><content>macOS</content></snapshot>", tokens: 612, maxTokens: 1500,
    shortBodyMaxChars: 500, includedCount: 3, omittedCount: 2});
});

test("old servers do not falsely advertise body injection or a complete empty catalog", () => {
  const data = taskMemoryInjectionPreview({catalogXml: "legacy"});
  assert.equal(data.text, "legacy");
  assert.equal(taskMemoryInjectionPreview({runtimeSnapshot: "", catalogXml: "stale"}).text, "");
  assert.equal(data.shortBodyMaxChars, 0);
  assert.equal(data.includedCount, null);
  assert.equal(data.omittedCount, 0);
  assert.match(taskMemoryInjectionUsage({autoReinjectCatalog: true, body: "macOS"}, data.shortBodyMaxChars), /正文按需读取/);
});

test("preview normalizes absent/invalid values and never leaves stale omission counts", () => {
  assert.deepEqual(taskMemoryInjectionPreview(null), {text: "", tokens: 0, maxTokens: 1500,
    shortBodyMaxChars: 0, includedCount: null, omittedCount: 0});
  const data = taskMemoryInjectionPreview({estimatedRuntimeTokens: -1, maxRuntimeTokens: "bad", omittedCount: -3,
    shortBodyMaxChars: "bad", includedCount: 0});
  assert.equal(data.tokens, 0);
  assert.equal(data.maxTokens, 1500);
  assert.equal(data.omittedCount, 0);
  assert.equal(data.includedCount, 0);
});

test("500/501 Unicode-character bodies match the backend boundary without UTF-16 overcount", () => {
  assert.match(taskMemoryInjectionUsage({autoReinjectCatalog: true, body: "🦜".repeat(500)}, 500), /^短正文自动提供/);
  assert.match(taskMemoryInjectionUsage({autoReinjectCatalog: true, body: "🦜".repeat(501)}, 500), /^正文较长/);
});

test("disabled injection and empty bodies have explicit independent labels", () => {
  assert.equal(taskMemoryInjectionUsage({autoReinjectCatalog: false, body: "macOS"}, 500), "名称与正文均按需读取");
  assert.equal(taskMemoryInjectionUsage({autoReinjectCatalog: true, body: ""}, 500), "自动提供名称与说明");
  assert.match(taskMemoryInjectionUsage({autoReinjectCatalog: true, body: "  "}, 500), /^短正文自动提供/);
  assert.match(taskMemoryInjectionUsage({autoReinjectCatalog: true}, 500), /以模型可见内容为准/);
});
