import test from "node:test";
import assert from "node:assert/strict";
import {chooseActiveTurnIndex} from "./activeTurn.js";

test("a long turn containing the reading anchor remains active", () => {
	const rows = [
		{index: 1, top: -900, bottom: -120},
		{index: 2, top: -120, bottom: 980},
		{index: 3, top: 980, bottom: 1380},
	];
	assert.equal(chooseActiveTurnIndex(rows, 0, 800), 2);
});

test("the nearest turn boundary wins when the anchor falls in spacing", () => {
	const rows = [
		{index: 2, top: -300, bottom: 100},
		{index: 3, top: 260, bottom: 700},
	];
	assert.equal(chooseActiveTurnIndex(rows, 0, 800), 3);
});

test("the final turn wins when the conversation is scrolled to the bottom", () => {
	const rows = [
		{index: 2, top: -200, bottom: 620},
		{index: 3, top: 620, bottom: 790},
	];
	assert.equal(chooseActiveTurnIndex(rows, 0, 800, {atBottom: true}), 3);
});

test("an explicit short-turn selection survives bottom scroll clamping", () => {
	const rows = [
		{index: 5, top: 410, bottom: 520},
		{index: 6, top: 520, bottom: 640},
		{index: 7, top: 640, bottom: 790},
	];
	assert.equal(chooseActiveTurnIndex(rows, 0, 800, {atBottom: true, preferredIndex: 5}), 5);
	assert.equal(chooseActiveTurnIndex(rows, 0, 800, {atBottom: true, preferredIndex: 6}), 6);
});

test("a stale preferred index falls back to the viewport calculation", () => {
	const rows = [
		{index: 2, top: -120, bottom: 500},
		{index: 3, top: 500, bottom: 790},
	];
	assert.equal(chooseActiveTurnIndex(rows, 0, 800, {preferredIndex: 99}), 2);
});

test("an empty conversation resolves to the first index", () => {
	assert.equal(chooseActiveTurnIndex([], 0, 800), 0);
});
