import test from "node:test";
import assert from "node:assert/strict";
import {mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import {createBuildInfo} from "../buildIdentity.mjs";

function fixture(t) {
  const root = mkdtempSync(path.join(tmpdir(), "openbear-build-identity-"));
  t.after(() => rmSync(root, {recursive: true, force: true}));
  const write = (file, data) => {
    mkdirSync(path.dirname(path.join(root, file)), {recursive: true});
    writeFileSync(path.join(root, file), data);
  };
  for (const file of ["index.html", "package-lock.json", "vite.config.js", "buildIdentity.mjs"]) write(file, file);
  write("package.json", JSON.stringify({version: "0.5.0"}));
  write("src/App.vue", "original app");
  return {root, write, info: () => createBuildInfo(root)};
}

test("build identity stays deterministic and retains the version handshake", t => {
  const f = fixture(t);
  assert.deepEqual(f.info(), f.info());
  assert.equal(f.info().schema, 1);
  assert.equal(f.info().version, "0.5.0");
  assert.match(f.info().buildId, /^[a-f0-9]{16}$/);
});

test("public manifest and binary icon edits change the refresh identity", t => {
  const f = fixture(t);
  const initial = f.info().buildId;
  f.write("public/manifest.webmanifest", '{"start_url":"/"}');
  const manifest = f.info().buildId;
  assert.notEqual(manifest, initial);
  f.write("public/assets/pwa/icon-192.png", Buffer.from([137,80,78,71,1]));
  const icon = f.info().buildId;
  assert.notEqual(icon, manifest);
  f.write("public/assets/pwa/icon-192.png", Buffer.from([137,80,78,71,2]));
  assert.notEqual(f.info().buildId, icon);
  f.write("public/manifest.webmanifest", '{"start_url":"/chat"}');
  assert.notEqual(f.info().buildId, icon);
});

test("source changes count, outputs/runtime files and symlinks do not", t => {
  const f = fixture(t);
  const initial = f.info().buildId;
  f.write("dist/index.html", "previous deployment");
  f.write("node_modules/cache.js", "dependency output");
  f.write("src/App.test.mjs", "tests are not shipped");
  f.write("outside/private.txt", "not an asset");
  mkdirSync(path.join(f.root, "public"));
  symlinkSync(path.join(f.root, "outside/private.txt"), path.join(f.root, "public/link"));
  assert.equal(f.info().buildId, initial);
  f.write("src/App.vue", "updated app");
  assert.notEqual(f.info().buildId, initial);
});
