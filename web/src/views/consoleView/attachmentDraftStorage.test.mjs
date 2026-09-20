import test from "node:test";
import assert from "node:assert/strict";
import {
	ATTACHMENT_DRAFT_MAX_AGE_MS,
	createAttachmentDraftStorage,
	planAttachmentDraftRecord,
	readAttachmentDraftRecord,
} from "./attachmentDraftStorage.js";
import {createMemoryAttachmentDraftDriver} from "./attachmentDraftMemoryDriver.mjs";

function attachment(id, name, {type = "text/plain", bytes = 8} = {}) {
	return {id, file: new File([new Uint8Array(bytes)], name, {type})};
}

function storage({records = new Map(), fail = null, now = () => 1000, ...limits} = {}) {
	const driver = createMemoryAttachmentDraftDriver({records, fail});
	return {records, store: createAttachmentDraftStorage({driver, now, ...limits})};
}

test("oversized files are reported as skipped instead of spending storage quota", () => {
	const record = planAttachmentDraftRecord("conv-a", [
		attachment("small", "small.txt", {bytes: 10}),
		attachment("huge", "huge.zip", {bytes: 300}),
		attachment("second", "second.txt", {bytes: 10}),
	], {now: 500, maxFileBytes: 100, maxTotalBytes: 15});
	assert.deepEqual(record.items.map((item) => item.id), ["small"]);
	assert.deepEqual(record.skipped.map((item) => [item.fileName, item.sizeBytes]), [["huge.zip", 300], ["second.txt", 10]]);
	assert.equal(record.updatedAt, 500);
});

test("a saved draft comes back as usable files with their name, type and bytes", async () => {
	const {store} = storage();
	await store.save("conv-a", [attachment("image", "shot.png", {type: "image/png", bytes: 4}), attachment("doc", "notes.txt")]);
	const loaded = await store.load("conv-a");
	assert.deepEqual(loaded.items.map((item) => [item.id, item.file.name, item.file.type, item.file.size]), [
		["image", "shot.png", "image/png", 4],
		["doc", "notes.txt", "text/plain", 8],
	]);
	assert.equal(new Uint8Array(await loaded.items[1].file.arrayBuffer()).length, 8);
});

test("a stored entry that lost its File wrapper is rebuilt from the record metadata", () => {
	const loaded = readAttachmentDraftRecord({
		key: "conv-a",
		items: [{id: "blob", fileName: "kept.png", mimeType: "image/png", sizeBytes: 3, file: new Blob([new Uint8Array(3)])}],
	});
	assert.equal(loaded.items[0].file.name, "kept.png");
	assert.equal(loaded.items[0].file.type, "image/png");
	assert.equal(loaded.items[0].file.size, 3);
});

test("sent files are dropped from the stored draft, and the record disappears once empty", async () => {
	const {records, store} = storage();
	await store.save("conv-a", [attachment("sent", "sent.txt"), attachment("kept", "kept.txt")]);
	await store.removeItems("conv-a", new Set(["sent"]));
	assert.deepEqual((await store.load("conv-a")).items.map((item) => item.id), ["kept"]);
	await store.removeItems("conv-a", new Set(["kept"]));
	assert.equal(records.has("conv-a"), false);
});

test("a draft follows its conversation to a new id, leaving nothing under the old one", async () => {
	const {records, store} = storage();
	await store.save("local:new", [attachment("only", "only.txt")]);
	await store.move("local:new", "conv-created");
	assert.equal(records.has("local:new"), false);
	assert.deepEqual((await store.load("conv-created")).items.map((item) => item.file.name), ["only.txt"]);
});

test("only stale drafts are pruned, since a conversation may vanish elsewhere", async () => {
	const now = 10 * ATTACHMENT_DRAFT_MAX_AGE_MS;
	const {records, store} = storage({now: () => now});
	await store.save("fresh", [attachment("a", "a.txt")]);
	records.set("stale", {key: "stale", updatedAt: now - ATTACHMENT_DRAFT_MAX_AGE_MS - 1, items: [], skipped: []});
	await store.prune();
	assert.deepEqual([...records.keys()], ["fresh"]);
});

test("unavailable or failing browser storage leaves the caller working without persistence", async () => {
	const unavailable = createAttachmentDraftStorage({driver: null});
	assert.equal(unavailable.available, false);
	await unavailable.save("conv-a", [attachment("a", "a.txt")]);
	assert.deepEqual(await unavailable.load("conv-a"), {items: [], skipped: []});

	const errors = [];
	const denied = createAttachmentDraftStorage({
		driver: createMemoryAttachmentDraftDriver({fail: new Error("quota_exceeded")}),
		onError: (error) => errors.push(error.message),
	});
	await denied.save("conv-a", [attachment("a", "a.txt")]);
	assert.deepEqual(await denied.load("conv-a"), {items: [], skipped: []});
	assert.deepEqual(errors, ["quota_exceeded", "quota_exceeded"]);
	assert.equal(await denied.removeItems("conv-a", ["a"]), true, "failed first write cannot leave this new ID on disk");
	assert.equal(await denied.removeItems("conv-a", ["unknown"]), false, "no successful cleanup can be claimed for an unknown ID");
});

test('failed rewrites never turn loaded or previously saved IDs into memory-only files', async () => {
	for (const observedBy of ['load', 'save']) {
		const driver = createMemoryAttachmentDraftDriver();
		const initial = createAttachmentDraftStorage({driver});
		await initial.save('a', [attachment('stored', 'stored.txt')]);
		const store = observedBy === 'save' ? initial : createAttachmentDraftStorage({driver});
		if (observedBy === 'load') await store.load('a');
		driver.put = driver.get = async () => {throw new Error('storage_disabled');};
		await store.save('a', [attachment('stored', 'stored.txt'), attachment('new', 'new.txt')]);
		assert.equal(await store.removeItems('a', ['new']), true);
		assert.equal(await store.removeItems('a', ['stored']), false, observedBy);
	}
});

test('a successful later write removes the failed-first-write exemption', async () => {
	const driver = createMemoryAttachmentDraftDriver();
	const store = createAttachmentDraftStorage({driver});
	const put = driver.put;
	driver.put = async () => {throw new Error('quota');};
	await store.save('a', [attachment('new', 'new.txt')]);
	driver.put = put;
	await store.save('a', [attachment('new', 'new.txt')]);
	driver.get = async () => {throw new Error('disabled');};
	assert.equal(await store.removeItems('a', ['new']), false);
});

function deferred() {let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve};}
function hold(driver, method) {
	const gate = deferred(), entered = deferred(), original = driver[method];
	driver[method] = async (...args) => {
		driver[method] = original;
		const snapshot = await original(...args);
		entered.resolve(); await gate.promise; return snapshot;
	};
	return {entered: entered.promise, release: () => gate.resolve()};
}
const ids = async (store, key) => (await store.load(key)).items.map(item => item.id);

test('C02: a delayed removeItems cannot delete a later recovery/save', async () => {
	const driver = createMemoryAttachmentDraftDriver();
	const store = createAttachmentDraftStorage({driver});
	await store.save('a', [attachment('sent', 'sent.txt')]);
	const read = hold(driver, 'get');
	const removing = store.removeItems('a', ['sent']); await read.entered;
	const saving = store.save('a', [attachment('sent', 'sent.txt'), attachment('next', 'next.txt')]);
	read.release(); await Promise.all([removing, saving]);
	assert.deepEqual(await ids(store, 'a'), ['sent', 'next']);
});

test('C02: delayed move keeps existing destination files and orders a later ACK cleanup', async () => {
	const driver = createMemoryAttachmentDraftDriver();
	const store = createAttachmentDraftStorage({driver});
	await store.save('local:new', [attachment('sent', 'sent.txt')]);
	await store.save('created', [attachment('next', 'next.txt')]);
	const read = hold(driver, 'get');
	const moving = store.move('local:new', 'created'); await read.entered;
	const cleaning = store.removeItems('created', ['sent']);
	read.release(); await Promise.all([moving, cleaning]);
	assert.deepEqual(await ids(store, 'local:new'), []);
	assert.deepEqual(await ids(store, 'created'), ['next']);
});

test('C02: prune and move cannot overwrite later replacement saves', async () => {
	for (const operation of ['prune', 'move']) {
		const driver = createMemoryAttachmentDraftDriver();
		const store = createAttachmentDraftStorage({driver, now: () => 100, maxAgeMs: 1});
		await store.save('old', [attachment('old', 'old.txt')]);
		driver.records.get('old').updatedAt = 1;
		const read = hold(driver, operation === 'prune' ? 'list' : 'get');
		const work = operation === 'prune' ? store.prune() : store.move('old', 'a');
		await read.entered;
		const key = operation === 'prune' ? 'old' : 'a';
		const saving = store.save(key, [attachment('new', 'new.txt')]);
		read.release(); await Promise.all([work, saving]);
		assert.deepEqual(await ids(store, key), ['new'], operation);
	}
});

test('C02: queued save snapshots its input at invocation and an error does not poison the queue', async () => {
	const driver = createMemoryAttachmentDraftDriver();
	const errors = [];
	const store = createAttachmentDraftStorage({driver, onError: error => errors.push(error.message)});
	const read = hold(driver, 'get'); const loading = store.load('a'); await read.entered;
	const files = [attachment('first', 'first.txt')];
	const saving = store.save('a', files); files.push(attachment('late', 'late.txt'));
	read.release(); await Promise.all([loading, saving]);
	assert.deepEqual(await ids(store, 'a'), ['first']);
	const remove = driver.delete; driver.delete = async () => {throw new Error('denied');};
	assert.equal(await store.removeItems('a', ['first']), false);
	driver.delete = remove;
	await store.save('a', [attachment('recovered', 'recovered.txt')]);
	assert.deepEqual(await ids(store, 'a'), ['recovered']); assert.deepEqual(errors, ['denied']);
});
