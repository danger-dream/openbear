// In-memory stand-in for the IndexedDB driver, used by Node tests so the real
// storage and console logic run without a browser. Records are cloned on the way
// in and out to keep IndexedDB's copy semantics.
export function createMemoryAttachmentDraftDriver({records = new Map(), fail = null} = {}) {
	function clone(record) {
		return record ? structuredClone(record) : record;
	}
	async function guard(action) {
		if (fail) throw fail instanceof Error ? fail : new Error(String(fail));
		return action();
	}
	return {
		records,
		get: (key) => guard(() => clone(records.get(String(key)))),
		list: () => guard(() => Array.from(records.values(), clone)),
		put: (record) => guard(() => {
			records.set(String(record.key), clone(record));
		}),
		delete: (key) => guard(() => {
			records.delete(String(key));
		}),
	};
}
