import {reactive, ref} from 'vue';

// SystemConfig setting calls carry config paths (e.g. assistant.enabled); rows show
// Chinese titles. Specs are fetched once on demand and rows re-render when they arrive.
const names = reactive({});
export const settingNamesVersion = ref(0);
let pending = null, fetchedAt = 0;

export function rememberSettingNames(specs) {
	let changed = false;
	const entries = Array.isArray(specs)
		? specs.map(item => [item?.path, item?.title])
		: Object.entries(specs || {}).map(([path, spec]) => [path, spec?.title || (typeof spec === 'string' ? spec : '')]);
	for (const [path, title] of entries) {
		if (typeof path === 'string' && path && typeof title === 'string' && title && names[path] !== title) {
			names[path] = title;
			changed = true;
		}
	}
	if (changed) settingNamesVersion.value += 1;
}

export function settingName(path) {
	if (typeof path !== 'string' || !path) return '';
	if (names[path]) return names[path];
	load();
	return '';
}

function load() {
	if (pending || typeof window === 'undefined' || Date.now() - fetchedAt < 10000) return;
	pending = import('../../api.js')
		.then(({Api}) => Api.settingsSpecs())
		.then(data => rememberSettingNames(data?.specs))
		.catch(() => { /* Rows fall back to the setting path. */ })
		.finally(() => { pending = null; fetchedAt = Date.now(); });
}
