import test from "node:test";
import assert from "node:assert/strict";
import {
  describeSessionUserAgent,
  formatSessionTime,
  relativeSessionTime,
} from "./sessionPresentation.js";

test("session user agents are reduced to useful device and browser labels", () => {
  assert.deepEqual(
    describeSessionUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153.0.0.0 Safari/537.36"),
    {os: "Windows", browser: "Chrome 153", deviceKind: "desktop", label: "Windows · Chrome 153"},
  );
  assert.deepEqual(
    describeSessionUserAgent("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/152.0.0.0 Mobile Safari/537.36"),
    {os: "Android", browser: "Chrome 152", deviceKind: "mobile", label: "Android · Chrome 152"},
  );
  assert.deepEqual(
    describeSessionUserAgent("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Version/18.0 Mobile/15E148 Safari/604.1"),
    {os: "iPhone", browser: "Safari 18", deviceKind: "mobile", label: "iPhone · Safari 18"},
  );
});

test("session activity times are concise and deterministic around the current moment", () => {
  const now = Date.UTC(2026, 8, 22, 4, 0, 0);
  assert.equal(relativeSessionTime(now / 1000 - 20, now), "刚刚");
  assert.equal(relativeSessionTime(now / 1000 - 5 * 60, now), "5 分钟前");
  assert.equal(relativeSessionTime(now / 1000 - 3 * 3600, now), "3 小时前");
  assert.equal(relativeSessionTime(now / 1000 - 2 * 86400, now), "2 天前");
  assert.equal(formatSessionTime(0), "—");
  assert.match(formatSessionTime(now / 1000), /2026/);
});
