import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

// Drive the component's actual paint loop and text watcher with fake timers.
const source = readFileSync(new URL("./ConsoleMarkdown.vue", import.meta.url), "utf8");
const constants = source.slice(source.indexOf("const SOFT_PUNCT"), source.indexOf("const componentTag"));
const loop = source.slice(source.indexOf("let liveTimer"), source.indexOf("onBeforeUnmount(cancelLivePaint)"));

function harness(text) {
	const timers = new Map();
	let clock = 0, nextId = 0, wakeups = 0;
	const window = {
		setTimeout: (callback, delay) => { timers.set(++nextId, {callback, at: clock + delay}); return nextId; },
		clearTimeout: id => timers.delete(id),
		requestAnimationFrame: callback => window.setTimeout(callback, 0),
		cancelAnimationFrame: id => timers.delete(id),
	};
	const props = {text, live: true, liveIntervalMs: 32};
	const displayedText = {value: ""};
	let watcher;
	const watch = (_, callback) => { watcher = callback; callback(); };
	const performance = {now: () => clock};
	new Function("window", "props", "displayedText", "watch", "performance", `${constants}\n${loop}`)(window, props, displayedText, watch, performance);
	function advance(ms) {
		const end = clock + ms;
		for (;;) {
			const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
			if (!due) break;
			timers.delete(due[0]);
			clock = Math.max(clock, due[1].at);
			wakeups += 1;
			due[1].callback();
		}
		clock = end;
	}
	return {props, displayedText, advance, pending: () => timers.size, wakeups: () => wakeups, setText(value) { props.text = value; watcher(); }};
}

test("an unfinished link destination parks the paint loop until more text arrives", () => {
	const h = harness("看图：\n\n![预览](workspace/artifacts/x.pn");
	h.advance(2000);
	assert.equal(h.displayedText.value, "看图：\n\n![预览](");
	assert.equal(h.pending(), 0, "no idle timer while waiting for the destination");
	const parked = h.wakeups();
	h.advance(2000);
	assert.equal(h.wakeups(), parked);
	h.setText("看图：\n\n![预览](workspace/artifacts/x.png) 完成。");
	h.advance(2000);
	assert.equal(h.displayedText.value, h.props.text);
	assert.equal(h.pending(), 0);
});

test("ordinary text still types out to the end", () => {
	const h = harness("第一段文字，继续输出。\n\n第二段也会完整显示出来。");
	h.advance(5000);
	assert.equal(h.displayedText.value, h.props.text);
	assert.equal(h.pending(), 0);
});
