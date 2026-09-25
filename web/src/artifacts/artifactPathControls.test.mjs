import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {parse, compileTemplate} from "@vue/compiler-sfc";
import {baseParse} from "@vue/compiler-dom";

function nodes(input) {
	const {descriptor, errors} = parse(readFileSync(new URL(input, import.meta.url), "utf8"), {filename: input});
	assert.deepEqual(errors, []);
	assert.deepEqual(compileTemplate({source: descriptor.template.content, filename: input, id: input}).errors, []);
	const visit = list => list.flatMap(node => [node, ...(node.children ? visit(node.children) : [])]);
	return visit(baseParse(descriptor.template.content).children);
}
function prop(node, name) { return node.props?.find(item => item.name === name || item.arg?.content === name); }

test("file and image controls expose copy only for shared paths while retaining independent downloads", () => {
	for (const file of ["./ArtifactCard.vue", "./ArtifactImagePath.vue"]) {
		const elements = nodes(file).filter(node => node.type === 1);
		const copy = elements.find(node => node.tag === "button" && prop(node, "title")?.value?.content === "复制工作区路径");
		assert.ok(copy, file);
		assert.equal(prop(copy, "if")?.exp?.content, "sharedPath");
		assert.equal(prop(copy, "click")?.exp?.content, "copyPath");
		const download = elements.find(node => node.tag === "a" && prop(node, "download"));
		assert.ok(download && !prop(download, "if"), `${file}: download remains available without shared path`);
	}
});
