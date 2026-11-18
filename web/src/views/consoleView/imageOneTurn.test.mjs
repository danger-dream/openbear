import test from "node:test";
import assert from "node:assert/strict";
import {IMAGE_ONE_TURN_DEFAULT, imageOneTurnLabel, normalizeImageOneTurn} from "./imageOneTurn.js";
import {normalizedRunDefaults, runDefaultOption, runDefaultSelection, updateRunDefault} from "../../components/folderRunDefaults.js";

const OFF = {enabled: false, includeUser: true, agents: true};

test("the setting fills defaults and ignores values that are not booleans", () => {
  assert.deepEqual(IMAGE_ONE_TURN_DEFAULT, OFF);
  assert.deepEqual(normalizeImageOneTurn(), OFF);
  assert.deepEqual(normalizeImageOneTurn({enabled: true, agents: "no", other: 1}), {...OFF, enabled: true});
  assert.deepEqual(Object.keys(normalizeImageOneTurn({agents: false, enabled: true})), ["enabled", "includeUser", "agents"]);
  assert.equal(imageOneTurnLabel({enabled: true}), "图片只看一轮：已开启");
  assert.equal(imageOneTurnLabel(null), "图片只看一轮：关闭");
});

test("folders inherit the whole setting and store it as one run default", () => {
  const on = {enabled: true, includeUser: false, agents: true};
  assert.deepEqual(normalizedRunDefaults({fallback: {mainModel: "m"}}, [{key: "m"}]).imageOneTurn, OFF);
  assert.deepEqual(normalizedRunDefaults({fallback: {mainModel: "m"}, inherited: {imageOneTurn: on}}, [{key: "m"}]).imageOneTurn, on);
  const stored = updateRunDefault({}, "imageOneTurn", runDefaultOption(on));
  assert.deepEqual(stored, {imageOneTurn: on});
  assert.equal(runDefaultSelection(stored, "imageOneTurn"), runDefaultOption(normalizeImageOneTurn(on)));
  assert.deepEqual(updateRunDefault(stored, "imageOneTurn", "inherit"), {});
});
