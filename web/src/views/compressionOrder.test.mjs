import test from "node:test";
import assert from "node:assert/strict";

import {
  buildCompressionOrderItems,
  compressionOrderFullnames,
} from "./compressionOrder.js";

test("buildCompressionOrderItems preserves configured order and display metadata", () => {
  const items = buildCompressionOrderItems(
    ["provider/b", "provider/a", "provider/b", "other/c"],
    [
      { fullname: "provider/a", provider: "provider", id: "a", name: "Model A" },
      { fullname: "provider/b", provider: "provider", id: "b", name: "Model B" },
    ],
  );

  assert.deepEqual(items, [
    { fullname: "provider/b", provider: "provider", id: "b", name: "Model B" },
    { fullname: "provider/a", provider: "provider", id: "a", name: "Model A" },
    { fullname: "other/c", provider: "other", id: "c", name: "c" },
  ]);
});

test("compressionOrderFullnames returns only usable ordered identities", () => {
  assert.deepEqual(compressionOrderFullnames([
    { fullname: "provider/a" },
    null,
    { fullname: "" },
    { fullname: "provider/b" },
  ]), ["provider/a", "provider/b"]);
});
