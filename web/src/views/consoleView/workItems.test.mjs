import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import postcss from "postcss";
import {parse} from "@vue/compiler-sfc";
import {Api} from "../../api.js";
import {createWorkItemsStore} from "./workItems.js";
import {turnTaskNumbers, workItemCallNumber, workItemToolPreview} from "./workItemsPresentation.js";

const read = name => readFileSync(new URL(name, import.meta.url), "utf8");

function tree() {
	const leaf = (number, status, extra = {}) => ({number, title: `item ${number}`, status, effectiveStatus: status, children: [], ...extra});
	return [{
		number: "4", title: "修复安全问题", status: "in_progress", top: true, children: [
			{number: "4.1", title: "测试与网关", status: "todo", children: [leaf("4.1.1", "done"), leaf("4.1.2", "done")]},
			{number: "4.4", title: "组网", status: "todo", children: [leaf("4.4.1", "done"), leaf("4.4.2", "doing"), leaf("4.4.3", "todo"), leaf("4.4.4", "dropped")]},
			{number: "4.5", title: "动态域名", status: "todo", children: [leaf("4.5.1", "ask"), leaf("4.5.2", "blocked")]},
		],
	}, {number: "5", title: "旧任务", status: "done", top: true, children: [leaf("5.1", "ask")]}];
}

async function loadedStore(data) {
	const original = Api.workItems;
	Api.workItems = async () => data;
	try {
		const store = createWorkItemsStore();
		store.setConversation("c1");
		await store.refresh();
		return store;
	} finally {
		Api.workItems = original;
	}
}

test("tool rows say what changed, and only what the result confirms", () => {
	assert.equal(workItemToolPreview({action: "complete", entries: [{number: "4.3"}]}, {ok: true, items: [{number: "4.3", status: "已完成"}]}), "#4.3 → 已完成");
	assert.equal(workItemToolPreview({action: "focus", items: ["4.4"]}), "执行范围 #4.4");
	assert.equal(workItemToolPreview({action: "create", title: "修复安全问题"}, {ok: true, number: "4"}), "建任务 “修复安全问题” #4");
	assert.equal(workItemToolPreview({action: "drop", number: "4.4.4"}, {ok: false, message: "需要你本人的出处"}), "不做 · #4.4.4 · 需要你本人的出处");
	assert.equal(workItemToolPreview({action: "start", entries: [{number: "4.4.1"}, {number: "4.4.2"}]},
		{ok: true, items: [{number: "4.4.1", status: "进行中"}, {number: "4.4.2", status: "进行中"}], errors: [{number: "4.4.9"}]}), "2 项 → 进行中 · 1 项未成功");
	assert.equal(workItemToolPreview("not json", ""), "查看任务");
	assert.equal(workItemCallNumber({action: "add", number: "4.4"}, {ok: true, number: "4.4.5"}), "4.4.5");
});

test("a turn's task chips come from WorkItem calls, including those inside grouped tool runs", () => {
	const events = [
		{calls: [{name: "Read", arguments: "{}"}]},
		{events: [{calls: [{name: "WorkItem", arguments: JSON.stringify({action: "start", number: "4.4.3"})}]}]},
		{calls: [{name: "WorkItem", arguments: JSON.stringify({action: "complete", entries: [{number: "4.4.2"}]})}]},
		{calls: [{name: "WorkItem", arguments: JSON.stringify({action: "create", title: "x"})}], result: {content: JSON.stringify({ok: true, number: "6"})}},
	];
	assert.deepEqual(turnTaskNumbers(events), ["4", "6"]);
});

test("every item in progress in the running focus is marked, not only the last one started", async () => {
	const several = tree();
	several[0].children[1].children[2] = {...several[0].children[1].children[2], status: "doing", effectiveStatus: "doing"};
	const store = await loadedStore({tasks: several, running: true, focus: {numbers: ["4.4"], current: "4.4.3"}, turnFocuses: [], outline: {text: "", tokens: 0}});
	assert.deepEqual(store.currentItems().map(item => item.number), ["4.4.2", "4.4.3"]);
	assert.ok(store.isCurrent("4.4.2") && store.isCurrent("4.4.3") && !store.isCurrent("4.4.1"));
	assert.ok(store.inFocus("4.4.3") && !store.inFocus("4.5.1"));
	assert.deepEqual(store.focusItems().map(item => item.number), ["4.4.1", "4.4.2", "4.4.3"], "dropped items leave the scope's count");
	const idle = await loadedStore({tasks: several, running: false, focus: {numbers: ["4.4"], current: "4.4.2"}, turnFocuses: []});
	assert.deepEqual(idle.currentItems(), []);
	assert.deepEqual(idle.focusItems(), []);
	assert.ok(!idle.isCurrent("4.4.2") && !idle.inFocus("4.4.3"));
});

test("a task without children is being worked on while it is in progress in the running focus", async () => {
	const lone = [{number: "4", title: "优化模型拦截配置界面", status: "in_progress", effectiveStatus: "in_progress", top: true, children: []}];
	const store = await loadedStore({tasks: lone, running: true, focus: {numbers: ["4"], current: ""}, turnFocuses: []});
	assert.deepEqual(store.currentItems().map(item => item.number), ["4"]);
	assert.ok(store.isCurrent("4"));
	const dock = read("./WorkItemsDock.vue");
	assert.match(dock, /\{\{ current \? '正在做' : '执行中' \}\}/, "a running scope always says it is running");
	assert.match(dock, /<span v-if="working\.length > 1" class="wi-dock-more">等 \{\{ working\.length \}\} 项<\/span>/);
});

test("the entry counts open tasks only, and the attention badge counts what waits for the user", async () => {
	const store = await loadedStore({tasks: tree(), running: false, focus: null, turnFocuses: []});
	assert.deepEqual(store.summary(), {open: 1, done: 3, total: 7, attention: 1}, "the dropped item and the finished task are left out; the badge counts only 等你定, not 受阻");
});

test("switching conversations drops the previous conversation's tasks and ignores its late answer", async () => {
	const original = Api.workItems;
	let release;
	Api.workItems = uuid => uuid === "a" ? new Promise(resolve => { release = () => resolve({tasks: tree()}); }) : Promise.resolve({tasks: []});
	try {
		const store = createWorkItemsStore();
		store.setConversation("a");
		const pending = store.refresh();
		store.setConversation("b");
		release();
		await pending;
		assert.equal(store.state.conversationUuid, "b");
		assert.deepEqual(store.state.tasks, []);
		store.setConversation("local:draft");
		await store.refresh();
		assert.equal(store.state.loaded, false);
	} finally {
		Api.workItems = original;
	}
});

function rules(source, selector) {
	const declarations = {};
	postcss.parse(parse(source).descriptor.styles.map(style => style.content).join("\n")).walkRules(rule => {
		if (rule.parent?.type === "atrule" || !rule.selectors.includes(selector)) return;
		rule.walkDecls(decl => { declarations[decl.prop] = decl.value; });
	});
	return declarations;
}

test("rows follow the agreed look: finished rows fade, the running row is green, no boxes around rows", () => {
	const treeSource = read("./WorkItemTree.vue"), dock = read("./WorkItemsDock.vue");
	for (const [source, prefix] of [[treeSource, ".wi-row"], [dock, ".wi-dock-row"]]) {
		assert.equal(rules(source, prefix)["border-radius"], undefined, `${prefix} has no rounded box`);
		assert.equal(rules(source, `${prefix}.is-current`)?.background, undefined, "the running row is not a card");
		const tx = prefix === ".wi-row" ? ".wi-tx" : ".wi-dock-tx";
		assert.equal(rules(source, `${prefix}.is-current ${tx}`).color, "var(--ob-success)");
		assert.equal(rules(source, `${prefix}.is-done ${tx}`).color, "var(--ob-text-muted)");
	}
	assert.equal(rules(treeSource, ".wi-row:hover")["border-radius"], undefined);
	assert.equal(rules(dock, ".wi-dock").border, undefined, "the progress line has no frame of its own");
	assert.equal(rules(dock, ".wi-dock")["border-bottom"], "1px solid var(--ob-chat-line)");
	assert.equal(rules(dock, ".wi-dock-open").border, "0");
});

test("the progress line lives inside the composer box, so new output never scrolls it away", () => {
	const composer = parse(read("./ConsoleComposer.vue")).descriptor.template.content;
	const box = composer.indexOf('<div class="composer-box">');
	assert.ok(box > 0 && composer.indexOf("<WorkItemsDock/>") > box);
	assert.ok(composer.indexOf("<WorkItemsDock/>") < composer.indexOf('class="attachment-strip"'));
	const dock = read("./WorkItemsDock.vue");
	assert.match(dock, /<div v-if="focus" ref="root" class="wi-dock"/);
	assert.match(dock, /max-height: 212px; overflow: auto;/);
	assert.match(dock, /\.wi-dock:not\(\.open\) \.wi-dock-open \{ display: none; \}/, "on a phone the folded line keeps only the essentials");
});

test("the entry sits at the top of the desktop rail and in the phone's conversation tools", () => {
	const drawer = read("./WorkItemsDrawer.vue"), consoleSource = read("./ConsoleView.vue"), tools = read("./MobileConversationTools.vue");
	assert.match(rules(drawer, ".work-items-entry-wrap").top, /- 3 \* \(var\(--console-float-control-size/);
	const template = parse(consoleSource).descriptor.template.content;
	const rail = template.indexOf('class="console-controls"');
	assert.ok(template.indexOf("<WorkItemsDrawer") > rail, "the desktop entry is part of the rail hidden on phones");
	assert.match(consoleSource, /@open-work-items="workItems\.open\(\)"/);
	assert.match(tools, /chooseAction\('open-work-items'\)"><ListChecks/);
	assert.ok(tools.indexOf("open-work-items')") < tools.indexOf("open-hidden')\">"), "tasks come first in the tools sheet");
	assert.match(consoleSource, /<button v-if="workItemReturn" type="button" @click="returnToWorkItem">返回任务<\/button>/);
});

test("files named in a task open in the read-only viewer, with the item's change one switch away", async () => {
	const {artifactFromUrl, workItemFileIdentity} = await import("../../artifacts/artifactFiles.js");
	const identity = workItemFileIdentity("0b8c2c47-1e43-4b38-9d1b-7b41f4d8f9a1", "4.1.1", "item:0");
	assert.equal(identity.workItem, true);
	assert.equal(identity.diffUrl, "/api/conversations/0b8c2c47-1e43-4b38-9d1b-7b41f4d8f9a1/work-items/4.1.1/file/diff?ref=item%3A0");
	const parsed = artifactFromUrl("/api/conversations/0b8c2c47-1e43-4b38-9d1b-7b41f4d8f9a1/work-items/4.1.1/file/content?ref=item%3A0", "https://console.test");
	assert.equal(parsed?.key, identity.key);
	const preview = read("../../artifacts/ArtifactPreview.vue");
	assert.match(preview, />当前内容<\/button><button type="button" :class="\{active: mode === 'diff'\}"/);
	assert.match(preview, /这一项的改动/);
});

test("the rail entry has no frame or ring; a running scope spins like a conversation in the tree", () => {
	const drawer = read("./WorkItemsDrawer.vue");
	assert.doesNotMatch(drawer, /work-items-ring|populated/);
	assert.match(drawer, /class="work-items-entry" :class="\{active: drawerOpen, 'is-working': focus\}"/);
	const spin = rules(drawer, ".work-items-entry.is-working .work-items-entry-icon::before");
	assert.equal(spin["border-top-color"], "var(--ob-blue)");
	assert.equal(spin["border-radius"], "50%");
	assert.match(spin.animation, /linear infinite/);
	assert.equal(rules(drawer, ".work-items-entry.is-working")["border-radius"], "50%", "hover and selection are round while it spins");
	assert.doesNotMatch(drawer, /开始做/, "work starts when the user says so in the conversation");
});

test("the open progress list folds when attention moves away", () => {
	const dock = read("./WorkItemsDock.vue");
	assert.match(dock, /document\.addEventListener\("pointerdown", onPointerDown, true\)/);
	assert.match(dock, /if \(open\.value && outside\(event\.target\)\) open\.value = false;/);
	assert.match(dock, /event\.key === "Escape"/);
	assert.match(dock, /@touchstart\.passive="onTouchStart" @touchmove\.passive="onTouchMove"/);
});

test("a file on another server names the server and offers no open action", () => {
	const drawer = read("./WorkItemsDrawer.vue");
	assert.match(drawer, /if \(entry\.server\) return false;/);
	assert.equal((drawer.match(/<span v-if="entry\.server" class="wi-server">服务器 \{\{ entry\.server \}\}<\/span>/g) || []).length, 2);
});
