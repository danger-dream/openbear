// Unsent attachments are real File objects. The text drafts live in
// localStorage, which only holds strings, so each conversation's draft files are
// kept in IndexedDB under the same conversation key and restored when that
// conversation is opened again.
export const ATTACHMENT_DRAFT_DB_NAME = "openbear.console.attachmentDrafts";
export const ATTACHMENT_DRAFT_DB_VERSION = 1;
export const ATTACHMENT_DRAFT_STORE_NAME = "drafts";
// uploads.js streams multi-GB files without ever reading them whole. Copying such
// a file into browser storage spends the origin's quota on a draft that may never
// be sent, so oversized files stay in memory only and are reported as skipped.
export const ATTACHMENT_DRAFT_MAX_FILE_BYTES = 32 * 1024 * 1024;
export const ATTACHMENT_DRAFT_MAX_TOTAL_BYTES = 96 * 1024 * 1024;
export const ATTACHMENT_DRAFT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

function attachmentMeta(id, file) {
	return {
		id: String(id || ""),
		fileName: file?.name || "attachment",
		mimeType: file?.type || "application/octet-stream",
		sizeBytes: Number(file?.size || 0),
	};
}

export function planAttachmentDraftRecord(key, attachments = [], {
	now = Date.now(),
	maxFileBytes = ATTACHMENT_DRAFT_MAX_FILE_BYTES,
	maxTotalBytes = ATTACHMENT_DRAFT_MAX_TOTAL_BYTES,
} = {}) {
	const items = [];
	const skipped = [];
	let stored = 0;
	for (const entry of attachments || []) {
		if (!entry?.file) continue;
		const meta = attachmentMeta(entry.id, entry.file);
		if (meta.sizeBytes > maxFileBytes || stored + meta.sizeBytes > maxTotalBytes) {
			skipped.push(meta);
			continue;
		}
		stored += meta.sizeBytes;
		items.push({...meta, file: entry.file});
	}
	return {key: String(key || ""), updatedAt: Number(now) || 0, items, skipped};
}

// A stored entry may come back as a bare Blob if the browser dropped the File
// wrapper. The record keeps the metadata, so the file is rebuilt from it.
function attachmentFromRecordItem(item) {
	const stored = item?.file;
	if (!stored) return null;
	if (typeof stored.name === "string" && stored.name) return stored;
	try {
		return new File([stored], item.fileName || "attachment", {type: item.mimeType || stored.type || ""});
	} catch {
		return stored;
	}
}

export function readAttachmentDraftRecord(record) {
	const items = [];
	for (const item of record?.items || []) {
		const file = attachmentFromRecordItem(item);
		if (!file || !item.id) continue;
		items.push({id: String(item.id), file});
	}
	return {items, skipped: [...(record?.skipped || [])]};
}

function requestResult(request) {
	return new Promise((resolve, reject) => {
		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error || new Error("attachment_draft_request_failed"));
	});
}

export function createIndexedDbAttachmentDraftDriver({indexedDB = globalThis.indexedDB} = {}) {
	if (!indexedDB) return null;
	let dbPromise = null;
	function open() {
		if (dbPromise) return dbPromise;
		dbPromise = new Promise((resolve, reject) => {
			const request = indexedDB.open(ATTACHMENT_DRAFT_DB_NAME, ATTACHMENT_DRAFT_DB_VERSION);
			request.onupgradeneeded = () => {
				const db = request.result;
				if (!db.objectStoreNames.contains(ATTACHMENT_DRAFT_STORE_NAME)) db.createObjectStore(ATTACHMENT_DRAFT_STORE_NAME, {keyPath: "key"});
			};
			request.onsuccess = () => resolve(request.result);
			request.onerror = () => reject(request.error || new Error("attachment_draft_open_failed"));
			request.onblocked = () => reject(new Error("attachment_draft_open_blocked"));
		}).catch((error) => {
			dbPromise = null;
			throw error;
		});
		return dbPromise;
	}
	// One request per transaction: awaiting anything else would let the browser
	// close the transaction before the request settles.
	async function run(mode, issue) {
		const db = await open();
		return await new Promise((resolve, reject) => {
			let request = null;
			const tx = db.transaction(ATTACHMENT_DRAFT_STORE_NAME, mode);
			tx.oncomplete = () => resolve(request ? request.result : undefined);
			tx.onerror = () => reject(tx.error || new Error("attachment_draft_transaction_failed"));
			tx.onabort = () => reject(tx.error || new Error("attachment_draft_transaction_aborted"));
			try {
				request = issue(tx.objectStore(ATTACHMENT_DRAFT_STORE_NAME));
			} catch (error) {
				try { tx.abort(); } catch { /* the transaction is already unusable */ }
				reject(error);
			}
		});
	}
	return {
		get: (key) => run("readonly", (store) => store.get(key)),
		list: () => run("readonly", (store) => store.getAll()),
		put: (record) => run("readwrite", (store) => store.put(record)),
		delete: (key) => run("readwrite", (store) => store.delete(key)),
	};
}

export function createAttachmentDraftStorage({
	driver = createIndexedDbAttachmentDraftDriver(),
	now = () => Date.now(),
	maxFileBytes = ATTACHMENT_DRAFT_MAX_FILE_BYTES,
	maxTotalBytes = ATTACHMENT_DRAFT_MAX_TOTAL_BYTES,
	maxAgeMs = ATTACHMENT_DRAFT_MAX_AGE_MS,
	onError = null,
} = {}) {
	const empty = {items: [], skipped: []};
	// Attachment IDs survive conversation migration. Only a failed first write
	// proves an ID is memory-only; loaded or successfully saved IDs stay guarded
	// if storage later becomes unreadable.
	const persistedIds = new Set();
	const memoryOnlyIds = new Set();
	function recordIds(record) {
		return [...(Array.isArray(record?.items) ? record.items : []),
			...(Array.isArray(record?.skipped) ? record.skipped : [])].map(item => String(item.id));
	}
	function rememberStored(record) {
		for (const id of recordIds(record)) { persistedIds.add(id); memoryOnlyIds.delete(id); }
	}
	// Queue whole operations, not individual driver requests. A read-modify-write,
	// move or prune must finish before a later save can change its snapshot.
	let tail = Promise.resolve();
	function enqueue(action, fallback) {
		const result = tail.then(() => guard(action, fallback));
		tail = result.catch(() => {});
		return result;
	}
	// Persistence is an enhancement: a private-mode or quota failure must never
	// break the in-memory draft the user is editing.
	async function guard(action, fallback) {
		if (!driver) return fallback;
		try {
			return await action();
		} catch (error) {
			onError?.(error);
			return fallback;
		}
	}
	function idSet(ids) {
		return new Set(Array.from(ids || []).map((id) => String(id)));
	}
	async function write(record) {
		if (!record.items.length && !record.skipped.length) {
			await driver.delete(record.key);
			return;
		}
		await driver.put(record);
	}
	return {
		available: Boolean(driver),
		load(key) {
			return enqueue(async () => {
				const record = await driver.get(String(key || ""));
				rememberStored(record);
				return readAttachmentDraftRecord(record);
			}, empty);
		},
		save(key, attachments) {
			// Capture at invocation: Vue may mutate the composer's array while queued.
			const record = planAttachmentDraftRecord(key, attachments, {now: now(), maxFileBytes, maxTotalBytes});
			return enqueue(async () => {
				try {
					await write(record);
					rememberStored(record);
				} catch (error) {
					for (const id of recordIds(record)) if (!persistedIds.has(id)) memoryOnlyIds.add(id);
					throw error;
				}
			}, undefined);
		},
		remove(key) {
			return enqueue(() => driver.delete(String(key || "")), undefined);
		},
		removeItems(key, ids) {
			const drop = idSet(ids);
			if (!drop.size || !driver) return Promise.resolve(true);
			return enqueue(async () => {
				const storageKey = String(key || "");
				let record;
				try {
					record = await driver.get(storageKey);
				} catch (error) {
					// Disabled/private storage must not prevent sending new files
					// whose attempted writes never succeeded. Unknown or previously
					// persisted IDs still require a successful cleanup fence.
					if ([...drop].every(id => memoryOnlyIds.has(id))) return true;
					throw error;
				}
				rememberStored(record);
				if (!record) return true;
				const items = (record.items || []).filter((item) => !drop.has(String(item.id)));
				const skipped = (record.skipped || []).filter((item) => !drop.has(String(item.id)));
				if (items.length === (record.items || []).length && skipped.length === (record.skipped || []).length) return true;
				if (!items.length && !skipped.length) {
					await driver.delete(storageKey);
					return true;
				}
				await driver.put({...record, items, skipped, updatedAt: now()});
				return true;
			}, false);
		},
		move(from, to) {
			return enqueue(async () => {
				const fromKey = String(from || "");
				const toKey = String(to || "");
				if (!fromKey || !toKey || fromKey === toKey) return;
				const record = await driver.get(fromKey);
				if (!record) return;
				const target = await driver.get(toKey);
				rememberStored(record);
				rememberStored(target);
				const targetIds = new Set([...(target?.items || []), ...(target?.skipped || [])].map(item => item.id));
				// Preserve anything already selected under the new conversation id.
				await driver.put({...record, key: toKey, updatedAt: now(),
					items: [...(record.items || []).filter(item => !targetIds.has(item.id)), ...(target?.items || [])],
					skipped: [...(record.skipped || []).filter(item => !targetIds.has(item.id)), ...(target?.skipped || [])],
				});
				await driver.delete(fromKey);
			}, undefined);
		},
		// Drafts are never explicitly discarded when a conversation disappears from
		// another tab or device, so age is the only available cleanup signal.
		prune() {
			return enqueue(async () => {
				const cutoff = now() - maxAgeMs;
				for (const record of (await driver.list()) || []) {
					if (Number(record?.updatedAt || 0) >= cutoff) continue;
					await driver.delete(String(record.key || ""));
				}
			}, undefined);
		},
	};
}
