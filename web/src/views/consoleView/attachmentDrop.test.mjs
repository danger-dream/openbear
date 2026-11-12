import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {compile, computed, nextTick, reactive, ref, watch} from 'vue';
import {parse} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import {createAttachmentDropHandlers, hasDraggedFiles} from './attachmentDrop.js';

const source = readFileSync(new URL('./ConsoleView.vue', import.meta.url), 'utf8');
const walk = nodes => (nodes || []).flatMap(node => [node, ...walk(Array.isArray(node.children) ? node.children : [])]);
const ast = walk(baseParse(parse(source).descriptor.template.content).children);
const column = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'class' && prop.value?.content.includes('conversation-column')));
const hint = column.children.find(node => node.type === 1 && node.props.some(prop => prop.name === 'class' && prop.value?.content === 'attachment-drop-hint'));
// Compile the real drop ancestor's attributes and hint, leaving the heavy
// transcript/composer contents out. The handlers are real ConsoleView code.
const render = compile(column.loc.source.slice(0, column.loc.source.indexOf('>') + 1) + hint.loc.source + '</div>');
function harness(t) {
	const warnings = [], previews = [], persisted = [], focuses = [];
	const props = reactive({conversationUuid: 'conv-a'});
	const state = {
		props, ref, computed,
		watch: (...args) => {const stop = watch(...args); t.after(stop); return stop;},
		createAttachmentDropHandlers,
		running: ref(false), contextEditorOpen: ref(false),
		pendingAttachments: ref([]), attachmentPreviews: ref({}),
		ElMessage: {warning: message => warnings.push(message)},
		URL: {createObjectURL(file) {previews.push(file); return `blob:${file.name}`;}},
		persistActiveAttachments() {persisted.push(state.pendingAttachments.value.map(item => item.file));},
		focusComposer: async () => focuses.push(true),
	};
	const ctx = vm.createContext(state);
	vm.runInContext(source.slice(source.indexOf('const attachmentDragActive ='), source.indexOf('const warnedMemoryOnlyDraftIds =')), ctx);
	const bindings = vm.runInContext('({attachmentDrop, attachmentDragActive, attachmentDropBlockedReason})', ctx);
	return {...state, ...bindings, warnings, previews, persisted, focuses,
		view: () => render({attachmentDrop: bindings.attachmentDrop, attachmentDragActive: bindings.attachmentDragActive.value, attachmentDropBlockedReason: bindings.attachmentDropBlockedReason.value}, []),
	};
}
function drag(transfer = {}) {
	return {
		dataTransfer: {types: [], items: [], files: [], ...transfer},
		preventDefault() {this.defaultPrevented = true;},
		stopPropagation() {this.stopped = true;},
	};
}
const fileItem = file => ({kind: 'file', getAsFile: () => file});
const photo = () => new File(['image'], 'photo.png', {type: 'image/png'});

test('chat content and composer share capture handlers, before the rich-text editor can consume native files', t => {
	const h = harness(t), node = h.view();
	assert.ok(walk(column.children).some(node => node.tag === 'ConsoleComposer'));
	assert.ok(walk(column.children).some(node => node.props?.some(prop => prop.name === 'ref' && prop.value?.content === 'scroller')));
	for (const name of ['Dragenter', 'Dragover', 'Dragleave', 'Drop', 'Dragend']) assert.equal(typeof node.props[`on${name}Capture`], 'function');
	const hover = drag({types: ['Files']}); // files remain protected until drop
	node.props.onDragenterCapture(hover);
	node.props.onDragoverCapture(hover);
	assert.equal(hover.defaultPrevented, true);
	assert.equal(hover.stopped, true);
	assert.equal(hover.dataTransfer.dropEffect, 'copy');
	assert.equal(h.attachmentDragActive.value, true);
	const visibleHint = h.view().children.find(node => node.type === 'div');
	assert.equal(visibleHint.props.role, 'status');
	assert.equal(visibleHint.children[0].children[0].children, '松开以添加附件');
	assert.equal(h.pendingAttachments.value.length, 0);
});

test('mixed image and file drop queues the original files once, creates image preview and persists the draft without sending', t => {
	const h = harness(t), image = photo(), document = new File(['text'], 'notes.txt', {type: 'text/plain'});
	const drop = drag({types: ['Files', 'text/plain'], files: [image, document], items: [fileItem(image), fileItem(document)], getData: () => 'must not enter draft'});
	h.view().props.onDropCapture(drop);
	assert.equal(drop.defaultPrevented, true);
	assert.equal(drop.stopped, true);
	assert.deepEqual(h.pendingAttachments.value.map(item => item.file), [image, document]);
	assert.deepEqual(h.previews, [image]);
	assert.equal(h.attachmentPreviews.value[h.pendingAttachments.value[0].id], 'blob:photo.png');
	assert.deepEqual(h.persisted.at(-1), [image, document]);
	assert.equal(h.focuses.length, 1);
	assert.equal(h.attachmentDragActive.value, false);
	assert.deepEqual(h.warnings, []);
});

test('FileList-only and item-only browsers are both accepted, including an empty file', t => {
	for (const useItems of [false, true]) {
		const h = harness(t), file = new File([], 'empty.txt');
		const event = drag(useItems ? {items: [fileItem(file)]} : {files: [file]});
		h.view().props.onDropCapture(event);
		assert.equal(event.defaultPrevented, true);
		assert.equal(h.pendingAttachments.value[0].file, file);
	}
});

test('nested dragenter/dragleave does not flicker; leaving, cancelling and conversation switching clear the hint', async t => {
	const h = harness(t), events = h.view().props;
	events.onDragenterCapture(drag({types: ['Files']}));
	events.onDragenterCapture(drag({types: ['Files']}));
	events.onDragleaveCapture(drag({types: ['Files']}));
	assert.equal(h.attachmentDragActive.value, true);
	events.onDragleaveCapture(drag());
	assert.equal(h.attachmentDragActive.value, false);
	events.onDragenterCapture(drag({types: ['Files']}));
	events.onDragendCapture();
	assert.equal(h.attachmentDragActive.value, false);
	events.onDragenterCapture(drag({types: ['Files']}));
	h.props.conversationUuid = 'conv-b';
	await nextTick();
	assert.equal(h.attachmentDragActive.value, false);
});

test('plain text, links and internal reference/tree drags remain untouched', t => {
	for (const types of [['text/plain'], ['text/uri-list', 'text/html'], ['application/x-openbear-reference', 'application/x-openbear-tree', 'text/plain'], []]) {
		const h = harness(t), event = drag({types, items: [{kind: 'string'}]});
		for (const handler of ['onDragenterCapture', 'onDragoverCapture', 'onDragleaveCapture', 'onDropCapture']) h.view().props[handler](event);
		assert.equal(event.defaultPrevented, undefined);
		assert.equal(event.stopped, undefined);
		assert.equal(h.pendingAttachments.value.length, 0);
		assert.equal(h.attachmentDragActive.value, false);
		assert.deepEqual(h.warnings, []);
	}
	assert.equal(hasDraggedFiles(null), false);
});

test('context editing refuses file drops with a reason, even when it starts during the drag', t => {
	for (const [key, message] of [['contextEditorOpen', '请先关闭上下文编辑器再添加附件']]) {
		const h = harness(t), hover = drag({types: ['Files']});
		h.view().props.onDragenterCapture(hover);
		h[key].value = true;
		h.view().props.onDragoverCapture(hover);
		assert.equal(hover.dataTransfer.dropEffect, 'none');
		const hintNode = h.view().children.find(node => node.type === 'div');
		assert.match(hintNode.props.class, /is-blocked/);
		assert.equal(hintNode.children[0].children[0].children, message);
		const event = drag({files: [photo()]});
		h.view().props.onDropCapture(event);
		assert.equal(event.defaultPrevented, true);
		assert.equal(h.pendingAttachments.value.length, 0);
		assert.deepEqual(h.warnings, [message]);
		assert.equal(h.attachmentDragActive.value, false);
	}
});

test('directory entries are not uploaded as files and unreadable drops give feedback', t => {
	const h = harness(t), image = photo();
	h.view().props.onDropCapture(drag({items: [
		{kind: 'file', webkitGetAsEntry: () => ({isDirectory: true}), getAsFile: () => assert.fail('directory must not become a file')},
		fileItem(image),
	]}));
	assert.equal(h.pendingAttachments.value.length, 1);
	assert.equal(h.pendingAttachments.value[0].file, image);
	assert.deepEqual(h.warnings, ['暂不支持拖入文件夹，请选择其中的文件']);
	h.view().props.onDropCapture(drag({types: ['Files'], items: [fileItem(null)]}));
	assert.equal(h.pendingAttachments.value.length, 1);
	assert.equal(h.warnings.at(-1), '未能读取拖入的文件，请使用附件按钮选择文件');
});
