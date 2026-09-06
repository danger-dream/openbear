import test from "node:test";
import assert from "node:assert/strict";
import { frontendMismatch } from "./versionSync.js";

const current = {version: "0.1.2", buildId: "old"};
test("build handshake detects same-version frontend rebuilds in any tab", () => {
  for (const requiresRestart of [true, false]) {
    for (const lastResult of [null, {status: "success", requiresRestart, acked: true}]) {
      assert.equal(frontendMismatch({phase: "idle", frontend: {buildId: "new"}, lastResult}, current), "new");
    }
  }
});
test("version equality cannot clear the refresh requirement before result is written", () => {
  assert.equal(frontendMismatch({version: "0.1.2", phase: "idle", frontend: {buildId: "new"}, lastResult: null}, current), "new");
});
test("intermediate paired file replacement is not treated as ready", () => {
  for (const phase of ["starting", "downloading", "applying", "restarting", "healthcheck", "rollback"]) {
    assert.equal(frontendMismatch({phase, frontend: {buildId: "new"}}, current), null);
  }
});
test("current build and rollback require no refresh", () => {
  assert.equal(frontendMismatch({phase: "idle", frontend: {buildId: "old"}}, current), "");
  assert.equal(frontendMismatch(null, current), null);
});
test("legacy backend fallback compares embedded version, not freshly fetched baseline", () => {
  assert.equal(frontendMismatch({version: "0.1.3"}, current), "version:0.1.3");
  assert.equal(frontendMismatch({version: "0.1.2"}, current), "");
});
