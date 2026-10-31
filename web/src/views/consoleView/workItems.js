import {inject, reactive} from "vue";
import {Api} from "../../api.js";
import {inSubtree, isClosed, leaves, walk} from "./workItemsPresentation.js";

export {STATUS_LABELS, ATTENTION, isClosed, walk, leaves, statusOf, topNumber, inSubtree, formatTime, workItemToolPreview, workItemCallNumber, turnTaskNumbers} from "./workItemsPresentation.js";

// Shared by the task drawer, the composer dock, the transcript and tool rows.
export const WORK_ITEMS_KEY = Symbol("openbear.workItems");

export function createWorkItemsStore() {
	const state = reactive({
		conversationUuid: "", tasks: [], focus: null, running: false, turnFocuses: [],
		outline: {text: "", tokens: 0}, loaded: false, error: "", revision: 0,
	});
	let generation = 0, timer = 0, opener = null;

	function reset() {
		Object.assign(state, {tasks: [], focus: null, running: false, turnFocuses: [], outline: {text: "", tokens: 0}, loaded: false, error: ""});
		state.revision++;
	}

	async function refresh() {
		const uuid = state.conversationUuid;
		if (!uuid || uuid.startsWith("local:")) return;
		const current = ++generation;
		try {
			const data = await Api.workItems(uuid);
			if (current !== generation || uuid !== state.conversationUuid) return;
			Object.assign(state, {
				tasks: Array.isArray(data?.tasks) ? data.tasks : [],
				focus: data?.focus || null,
				running: Boolean(data?.running),
				turnFocuses: Array.isArray(data?.turnFocuses) ? data.turnFocuses : [],
				outline: data?.outline || {text: "", tokens: 0},
				loaded: true, error: "",
			});
			state.revision++;
		} catch (error) {
			if (current === generation) state.error = String(error?.response?.data?.message || error?.message || "任务读取失败");
		}
	}

	function schedule(delay = 120) {
		clearTimeout(timer);
		timer = setTimeout(() => { void refresh(); }, delay);
	}

	function setConversation(uuid) {
		const value = String(uuid || "");
		if (value === state.conversationUuid) return;
		generation++;
		state.conversationUuid = value;
		reset();
		if (value && !value.startsWith("local:")) schedule(0);
	}

	function changed(event) {
		if (String(event?.conversationUuid || "") === state.conversationUuid) schedule();
	}

	function find(number) {
		const value = String(number || "");
		for (const node of walk(state.tasks)) if (node.number === value) return node;
		return null;
	}

	// Every open leaf of the running focus marked 进行中 is being worked on, several at
	// once when they are; a task without children carries the task's own status.
	function working(node) {
		if (!node || node.children?.length) return false;
		return node.status === "doing" || (Boolean(node.top) && node.status === "in_progress");
	}

	function currentItems() {
		return focusItems().filter(working);
	}

	function isCurrent(number) {
		return inFocus(number) && working(find(number));
	}

	function inFocus(number) {
		return Boolean(state.running && state.focus?.numbers?.some(root => inSubtree(number, root)));
	}

	function focusFor(rootTurnUuid) {
		return state.turnFocuses.find(item => item.rootTurnUuid === rootTurnUuid) || null;
	}

	function focusItems() {
		if (!state.running || !state.focus) return [];
		const out = [];
		for (const root of state.focus.numbers || []) {
			const node = find(root);
			if (node) for (const leaf of leaves(node)) if (leaf.status !== "dropped") out.push(leaf);
		}
		return out;
	}

	function attention() {
		let count = 0;
		for (const task of state.tasks) {
			if (isClosed(task.status)) continue;
			for (const node of walk([task])) if (!node.children?.length && node.status === "ask") count++;
		}
		return count;
	}

	function summary() {
		const open = state.tasks.filter(task => !isClosed(task.status));
		let done = 0, total = 0;
		for (const task of open) {
			const live = task.children?.length ? leaves(task).filter(leaf => leaf.status !== "dropped") : [task];
			total += live.length;
			done += live.filter(leaf => leaf.status === "done").length;
		}
		return {open: open.length, done, total, attention: attention()};
	}

	return {
		state, refresh, schedule, setConversation, changed, find, currentItems, isCurrent, inFocus, focusFor, focusItems,
		attention, summary,
		setOpener(fn) { opener = typeof fn === "function" ? fn : null; },
		open(number = "", options = {}) { opener?.(String(number || ""), options); },
	};
}

export function useWorkItems() {
	return inject(WORK_ITEMS_KEY, null);
}
