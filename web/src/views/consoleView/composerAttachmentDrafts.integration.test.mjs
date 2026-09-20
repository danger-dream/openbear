import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {createOutboundSendTracker, restoreOutboundDraft} from "./outboundSend.js";
import {createAttachmentDraftStorage} from "./attachmentDraftStorage.js";
import {createMemoryAttachmentDraftDriver} from "./attachmentDraftMemoryDriver.mjs";

// Runs the real ConsoleView draft functions: the composer's files must belong to
// their own conversation exactly like its text draft, across switches, receipts
// and a reload. Vue rendering, HTTP and object URLs are the isolated seams.
const source = fs.readFileSync(process.env.CONSOLE_TEST_SOURCE || new URL("./ConsoleView.vue", import.meta.url), "utf8");
function between(start, end) {
	const a = source.indexOf(start);
	const b = source.indexOf(end, a + start.length);
	assert.ok(a >= 0 && b > a, `${start}..${end}`);
	return source.slice(a, b);
}
const actual = [
	between("function draftKey(", "defineExpose("),
	between("function addAttachment(", "function queueSentAttachmentPreviewRevokes("),
	between("function finishPendingOutboundSend(", "function handleWsMessage("),
	between("async function switchConversation(", "watch(() => props.conversationUuid"),
].join("\n");
const flush = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
// Small on purpose: the same threshold logic that keeps multi-GB uploads out of
// browser storage is exercised with a 100-byte "oversized" file.
const MAX_FILE_BYTES = 64;

function harness({conversationUuid = "conv-a", records = new Map(), fail = null} = {}) {
	const driver = createMemoryAttachmentDraftDriver({records, fail});
	const props = {conversationUuid, folderId: ""};
	const revoked = [];
	const warnings = [];
	let previewSeq = 0;
	const state = {
		props,
		draft: {value: ""},
		draftByConversation: {value: {}},
		restoringDraft: {value: false},
		attachmentRestoring: {value: false},
		pendingAttachments: {value: []},
		attachmentPreviews: {value: {}},
		messages: {value: []},
		status: {value: "就绪"},
		sendPending: {value: false},
		running: {value: false},
		chatState: {value: {}},
		runStartedAt: {value: 0},
		lastStats: {value: null},
		activeTurnIndex: {value: 0},
		runConfigOverride: {value: null},
		localToServerTransitionUuid: {value: ""},
		activeConversationUuid: {get value() {return props.conversationUuid;}},
		isLocalConversation: {get value() {return props.conversationUuid.startsWith("local:");}},
		attachmentDrafts: createAttachmentDraftStorage({
			driver,
			maxFileBytes: MAX_FILE_BYTES,
		}),
		URL: {
			createObjectURL: (file) => `blob:${file.name}#${++previewSeq}`,
			revokeObjectURL: (url) => revoked.push(url),
		},
		ElMessage: {warning: (payload) => warnings.push(payload?.message || payload)},
		ElMessageBox: {confirm: async () => true},
		createOutboundSendTracker,
		restoreOutboundDraft,
		saveDraftStore: (next) => {state.draftByConversation.value = next;},
		nextTick: (fn) => {fn?.(); return Promise.resolve();},
		queueSentAttachmentPreviewRevokes: (urls) => {revoked.push(...urls);},
		adjustComposerHeight() {},
		focusComposer: async () => {},
		orderedOperationsList: () => [],
		syncRunStateFromOperations() {},
		resetAgentAutoOpenBoundary() {},
		hasOptimisticLocalTurn: () => false,
		closeWs() {},
		connectWs: async () => {},
		clearUiCaches() {},
		resetOperationStore() {},
		load: async () => {},
		defineExpose() {},
	};
	const context = vm.createContext(state);
	vm.runInContext(`let pendingLoadBottomScroll=null; let runConfigInteractionGeneration=0;
		let agentAutoOpenBoundaryConversation=""; let pinnedActiveTurnIndex=null;
		let componentMounted=true; let sendAttemptGeneration=0;
		let attachmentsLoadedKey=""; let attachmentRestoreGeneration=0;
		let readingAnchor=null, conversationSwitchGeneration=0;
		const attachmentHydrations=new Map();
		const attachmentsByConversation=new Map();
		const outboundSends=createOutboundSendTracker({});
		${actual}`, context);
	const run = (code) => vm.runInContext(code, context);
	return {
		context, records, driver, revoked, warnings, run,
		// Values built inside the vm realm are copied out before comparison.
		names: () => Array.from(state.pendingAttachments.value, (item) => item.file.name),
		previews: () => Array.from(state.pendingAttachments.value, (item) => state.attachmentPreviews.value[item.id] || ""),
		stored: (key) => Array.from(records.get(key)?.items || [], (item) => item.fileName),
		add: async (name, {type = "text/plain", bytes = 8} = {}) => {
			state.incoming = new File([new Uint8Array(bytes)], name, {type});
			run("addAttachment(incoming)");
			await flush();
			return state.pendingAttachments.value.at(-1);
		},
		open: async (uuid = conversationUuid) => {
			run(`restoreDraftForConversation(${JSON.stringify(uuid)})`);
			await flush();
		},
		switchTo: async (next) => {
			const prev = props.conversationUuid;
			props.conversationUuid = next;
			await run(`switchConversation(${JSON.stringify(next)}, ${JSON.stringify(prev)})`);
			await flush();
		},
	};
}

test("text and files stay with their own conversation, and several drafts survive at once", async () => {
	const h = harness();
	await h.open();
	h.context.draft.value = "回到这里继续";
	const kept = await h.add("shot.png", {type: "image/png"});
	const preview = h.context.attachmentPreviews.value[kept.id];
	assert.ok(preview);

	await h.switchTo("conv-b");
	assert.deepEqual(h.names(), []);
	assert.equal(h.context.draft.value, "");
	await h.add("other.txt");
	assert.deepEqual(h.names(), ["other.txt"]);

	await h.switchTo("conv-a");
	assert.deepEqual(h.names(), ["shot.png"]);
	assert.equal(h.context.draft.value, "回到这里继续");
	// The same preview URL comes back: switching away must not revoke a thumbnail
	// the user will see again.
	assert.deepEqual(h.previews(), [preview]);
	assert.deepEqual(h.revoked, []);

	await h.switchTo("conv-b");
	assert.deepEqual(h.names(), ["other.txt"]);
});

test("a reload restores each conversation's files from browser storage", async () => {
	const records = new Map();
	const first = harness({records});
	await first.open();
	await first.add("shot.png", {type: "image/png"});
	await first.switchTo("conv-b");
	await first.add("other.txt");
	assert.deepEqual(first.stored("conv-a"), ["shot.png"]);

	const reloaded = harness({records, conversationUuid: "conv-a"});
	await reloaded.open();
	assert.deepEqual(reloaded.names(), ["shot.png"]);
	assert.equal(reloaded.context.pendingAttachments.value[0].file.type, "image/png");
	// The blob URL of the previous page is gone, so the thumbnail is recreated.
	assert.deepEqual(reloaded.previews(), ["blob:shot.png#1"]);
	await reloaded.switchTo("conv-b");
	assert.deepEqual(reloaded.names(), ["other.txt"]);
});

test("an accepted send releases only its own files and clears them from storage", async () => {
	const h = harness();
	await h.open();
	const sent = await h.add("sent.txt");
	await h.add("next.txt");
	h.run(`outboundSends.begin({requestId:"r1", conversationUuid:"conv-a", draftText:"hi",
		attachments:[pendingAttachments.value[0]], previewUrls:[]})`);
	assert.equal(h.run(`finishPendingOutboundSend("r1")`), true);
	await flush();
	assert.deepEqual(h.names(), ["next.txt"]);
	assert.equal(h.context.pendingAttachments.value.some((item) => item.id === sent.id), false);
	// A reload must not offer a file that was already sent.
	assert.deepEqual(h.stored("conv-a"), ["next.txt"]);
});

test("leaving a conversation mid-send returns its files to it, not to the one opened next", async () => {
	const h = harness();
	await h.open();
	h.context.draft.value = "待发送";
	await h.add("inflight.txt");
	h.run(`outboundSends.begin({requestId:"r1", conversationUuid:"conv-a", draftText:"待发送",
		attachments:[pendingAttachments.value[0]], previewUrls:[]})`);

	await h.switchTo("conv-b");
	assert.deepEqual(h.names(), []);
	assert.equal(h.context.draft.value, "");
	assert.equal(h.context.draftByConversation.value["conv-a"], "待发送");

	await h.switchTo("conv-a");
	assert.deepEqual(h.names(), ["inflight.txt"]);
	assert.equal(h.context.draft.value, "待发送");
});

test("a new conversation keeps its files when it becomes a server conversation", async () => {
	const h = harness({conversationUuid: "local:new"});
	await h.open("local:new");
	await h.add("first.txt");
	h.run(`migrateAttachmentDraft("local:new", "conv-created")`);
	await flush();
	h.context.props.conversationUuid = "conv-created";
	assert.deepEqual(h.names(), ["first.txt"]);
	assert.deepEqual(h.stored("conv-created"), ["first.txt"]);
	assert.equal(h.records.has("local:new"), false);

	// Switching to another conversation and back now uses the server id.
	await h.switchTo("conv-b");
	await h.switchTo("conv-created");
	assert.deepEqual(h.names(), ["first.txt"]);
});

test("a file too large to store stays usable now and is named once after a reload", async () => {
	const records = new Map();
	const first = harness({records});
	await first.open();
	await first.add("small.txt");
	await first.add("big.zip", {bytes: 100});
	assert.deepEqual(first.names(), ["small.txt", "big.zip"]);
	assert.deepEqual(first.stored("conv-a"), ["small.txt"]);

	const reloaded = harness({records, conversationUuid: "conv-a"});
	await reloaded.open();
	assert.deepEqual(reloaded.names(), ["small.txt"]);
	assert.equal(reloaded.warnings.length, 1);
	assert.match(reloaded.warnings[0], /big\.zip/);
	// Reporting it once is enough; switching back does not repeat the warning.
	await reloaded.switchTo("conv-b");
	await reloaded.switchTo("conv-a");
	assert.equal(reloaded.warnings.length, 1);
});

test("deleting a conversation drops its parked files, previews and stored record", async () => {
	const h = harness();
	await h.open();
	const parked = await h.add("shot.png", {type: "image/png"});
	const preview = h.context.attachmentPreviews.value[parked.id];
	await h.switchTo("conv-b");
	h.run(`discardConversationDraft("conv-a")`);
	await flush();
	assert.deepEqual(h.revoked, [preview]);
	assert.equal(h.records.has("conv-a"), false);
	await h.switchTo("conv-a");
	assert.deepEqual(h.names(), []);
});

test("unavailable browser storage still keeps each conversation's files for this page", async () => {
	const h = harness({fail: new Error("quota_exceeded")});
	await h.open();
	await h.add("shot.png", {type: "image/png"});
	await h.switchTo("conv-b");
	assert.deepEqual(h.names(), []);
	await h.switchTo("conv-a");
	assert.deepEqual(h.names(), ["shot.png"]);
});

function deferred() {let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve};}
function holdNextRead(driver) {
	const entered = deferred(), gate = deferred(), get = driver.get;
	driver.get = async key => {
		driver.get = get;
		const snapshot = await get(key);
		entered.resolve();
		await gate.promise;
		return snapshot;
	};
	return {entered: entered.promise, release: () => gate.resolve()};
}
const oldFile = () => ({id: 'old', file: new File(['old'], 'old.txt', {type: 'text/plain'})});
async function durableNames(h, key = 'conv-a') {
	return (await h.context.attachmentDrafts.load(key)).items.map(item => item.file.name);
}

test('C01: late B HTTP completion never restores B files or text into C', async () => {
	const h = harness(); await h.open();
	await h.switchTo('conv-b'); await h.add('B-private.txt'); h.context.draft.value = 'B draft';
	await h.switchTo('conv-a');
	const b = deferred(), c = deferred();
	h.context.load = () => h.context.props.conversationUuid === 'conv-b' ? b.promise : c.promise;
	const switchingB = h.switchTo('conv-b');
	const switchingC = h.switchTo('conv-c');
	c.resolve(); await switchingC;
	await h.add('C-only.txt'); h.context.draft.value = 'C draft';
	b.resolve(); await switchingB;
	assert.equal(h.context.props.conversationUuid, 'conv-c');
	assert.deepEqual(h.names(), ['C-only.txt']);
	assert.equal(h.context.draft.value, 'C draft');
	assert.deepEqual(await durableNames(h, 'conv-b'), ['B-private.txt']);
});

test('C03: edits during a delayed hydration merge durably, including a second reload', async () => {
	const h = harness(); await h.context.attachmentDrafts.save('conv-a', [oldFile()]);
	const read = holdNextRead(h.driver); const opening = h.open(); await read.entered;
	await h.add('new.txt');
	read.release(); await opening; await flush();
	assert.deepEqual(h.names(), ['new.txt', 'old.txt']);
	assert.deepEqual(await durableNames(h), ['new.txt', 'old.txt']);
	const reload = harness({records: h.records}); await reload.open();
	assert.deepEqual(reload.names(), ['new.txt', 'old.txt']);
});

test('C03/C04: clear while hydrating fences old files but retains later selections', async () => {
	const h = harness(); await h.context.attachmentDrafts.save('conv-a', [oldFile()]);
	const read = holdNextRead(h.driver); const opening = h.open(); await read.entered;
	await h.add('discard.txt'); h.run('clearDraftAndAttachments()');
	assert.deepEqual(h.names(), []);
	await h.add('after-clear.txt');
	read.release(); await opening; await flush();
	assert.deepEqual(h.names(), ['after-clear.txt']);
	assert.deepEqual(await durableNames(h), ['after-clear.txt']);
});

test('C04: a clear without further edits stays empty after the old read completes', async () => {
	const h = harness(); await h.context.attachmentDrafts.save('conv-a', [oldFile()]);
	const read = holdNextRead(h.driver); const opening = h.open(); await read.entered;
	await h.add('new.txt'); h.run('clearDraftAndAttachments()');
	read.release(); await opening; await flush();
	assert.deepEqual(h.names(), []); assert.deepEqual(await durableNames(h), []);
});

test('C03/C04: remove confirmation edits its owner even after a switch during hydration', async () => {
	const h = harness(); await h.context.attachmentDrafts.save('conv-a', [oldFile()]);
	const read = holdNextRead(h.driver); const opening = h.open(); await read.entered;
	const added = await h.add('removed.txt'); const confirm = deferred();
	h.context.ElMessageBox.confirm = () => confirm.promise;
	const removing = h.run(`removeAttachment(${JSON.stringify(added.id)})`);
	await h.switchTo('conv-b'); await h.add('B.txt');
	confirm.resolve(); await removing;
	read.release(); await opening; await flush();
	assert.deepEqual(await durableNames(h), ['old.txt']);
	assert.deepEqual(h.names(), ['B.txt']);
	await h.switchTo('conv-a'); assert.deepEqual(h.names(), ['old.txt']);
});

test('C04: discard an offscreen conversation invalidates its still pending hydration', async () => {
	const h = harness(); await h.context.attachmentDrafts.save('conv-a', [oldFile()]);
	const read = holdNextRead(h.driver); const opening = h.open(); await read.entered;
	await h.add('discard.txt'); await h.switchTo('conv-b');
	h.run('discardConversationDraft("conv-a")');
	read.release(); await opening; await flush();
	assert.deepEqual(await durableNames(h), []);
	await h.switchTo('conv-a'); assert.deepEqual(h.names(), []);
});

test('C03: A to B to A during one read keeps one overlay and creates no duplicate files', async () => {
	const h = harness(); await h.context.attachmentDrafts.save('conv-a', [oldFile()]);
	const read = holdNextRead(h.driver); const opening = h.open(); await read.entered;
	await h.add('new.txt'); await h.switchTo('conv-b'); await h.switchTo('conv-a');
	read.release(); await opening; await flush();
	assert.deepEqual(h.names(), ['new.txt', 'old.txt']);
	assert.deepEqual(await durableNames(h), ['new.txt', 'old.txt']);
});

test('C03: unmount during hydration finishes durable edits without resurrecting preview URLs', async () => {
	const h = harness(); await h.context.attachmentDrafts.save('conv-a', [oldFile()]);
	const read = holdNextRead(h.driver); const opening = h.open(); await read.entered;
	await h.add('new.png', {type: 'image/png'});
	h.run('detachAttachmentHydrations(); componentMounted=false; clearAttachments(); releaseAllStashedAttachments();');
	read.release(); await opening; await flush();
	assert.deepEqual(h.names(), []);
	assert.deepEqual(await durableNames(h), ['new.png', 'old.txt']);
	assert.equal(h.revoked.length, 1);
});

test('C05: text edits preserve the preparing draft but exclude it once send has crossed its fence', async () => {
	const h = harness(); await h.open();
	h.run(`outboundSends.begin({requestId:"r1", conversationUuid:"conv-a", draftText:"original", attachments:[], previewUrls:[]})`);
	h.run('persistComposerDraft("")');
	assert.equal(h.context.draftByConversation.value['conv-a'], 'original');
	h.run('persistComposerDraft("next")');
	assert.equal(h.context.draftByConversation.value['conv-a'], 'original\n\nnext');
	h.run('outboundSends.current.storageReleased=true; persistComposerDraft("next")');
	assert.equal(h.context.draftByConversation.value['conv-a'], 'next');
	h.run('finishPendingOutboundSend("r1")');
});
