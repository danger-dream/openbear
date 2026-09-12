import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {retryStatusView, retryWaitLabel} from "./modelRetryPresentation.js";

test("retry waits display rounded seconds or whole minutes, never fractional seconds", () => {
  for (const [ms, label] of [[2100, "等待 3 秒"], [3200, "等待 4 秒"], [3000, "等待 3 秒"], [180000, "等待 3 分钟"], [300000, "等待 5 分钟"], [600000, "等待 10 分钟"], [181000, "等待 3 分钟 1 秒"]]) {
    assert.equal(retryWaitLabel({waitMs: ms}), label);
  }
  assert.equal(retryWaitLabel(), "");
});

test("resumed means request retry started, not recovered successfully", () => {
  assert.deepEqual(retryStatusView({status: "resumed"}), {label: "已发起重试", tone: "neutral"});
  assert.equal(retryStatusView({}).tone, "neutral");
  assert.deepEqual(retryStatusView({status: "completed"}), {label: "重试成功", tone: "success"});
  assert.equal(retryStatusView({status: "failed"}).tone, "failed");
  assert.equal(retryStatusView({status: "cancelled"}).label, "已取消");
  assert.equal(retryStatusView({active: true}).label, "等待重试");
});

test("timeline uses the shared retry presentation and no fractional formatter", async () => {
  const source = await readFile(new URL("./TurnEvent.vue", import.meta.url), "utf8");
  assert.ok(source.includes('from "./modelRetryPresentation.js"'));
  assert.ok(source.includes('tone === "neutral" ? Refresh'));
  assert.ok(!source.includes('return "已恢复"'));
});
