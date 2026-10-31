import {reactive} from 'vue';

// Servers tool calls carry server ids; rows show names. The list is fetched
// when an unknown id is shown, at most every ten seconds, and rows re-render
// once it arrives.
const names = reactive({});
let pending = null, fetchedAt = 0;

export function rememberServerNames(items) {
	for (const item of items || []) if (item?.id && item.name) names[item.id] = item.name;
}

export function serverName(id) {
	if (typeof id !== 'string' || !id) return '';
	if (names[id]) return names[id];
	load();
	return id === 'local' ? '本机' : '';
}

function load() {
	if (pending || typeof window === 'undefined' || Date.now() - fetchedAt < 10000) return;
	pending = import('../../api.js')
		.then(({Api}) => Api.servers())
		.then(data => rememberServerNames(data?.items))
		.catch(() => { /* Rows keep the generic wording. */ })
		.finally(() => { pending = null; fetchedAt = Date.now(); });
}
