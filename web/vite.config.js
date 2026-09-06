import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const webRoot = fileURLToPath(new URL(".", import.meta.url));
const packageInfo = JSON.parse(readFileSync(path.join(webRoot, "package.json"), "utf8"));
function sourceFiles(dir) {
  return readdirSync(path.join(webRoot, dir), {withFileTypes: true}).flatMap((entry) => {
    const name = `${dir}/${entry.name}`;
    return entry.isDirectory() ? sourceFiles(name) : (/\.(vue|js|css)$/.test(name) ? [name] : []);
  });
}
const hash = createHash("sha256");
for (const name of [...sourceFiles("src"), "index.html", "package.json", "package-lock.json", "vite.config.js"].sort()) {
  hash.update(name).update("\0").update(readFileSync(path.join(webRoot, name))).update("\0");
}
const buildInfo = {schema: 1, version: packageInfo.version, buildId: hash.digest("hex").slice(0, 16)};
const buildIdentity = {
  name: "openbear-build-identity",
  transformIndexHtml() {
    return [{tag: "meta", attrs: {name: "openbear-build", content: buildInfo.buildId}, injectTo: "head"}];
  },
  generateBundle() {
    this.emitFile({type: "asset", fileName: "build-info.json", source: JSON.stringify(buildInfo) + "\n"});
  },
};

export default defineConfig({
  plugins: [vue(), buildIdentity],
  define: {
    __OPENBEAR_BUILD_ID__: JSON.stringify(buildInfo.buildId),
    __OPENBEAR_VERSION__: JSON.stringify(buildInfo.version),
  },
  server: {
    host: "0.0.0.0",
    port: 5273,
    proxy: { "/api": "http://127.0.0.1:8899" },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 40000,
    rollupOptions: {
      onwarn(warning, warn) {
        const id = String(warning.id || "");
        const message = String(warning.message || "");
        if (
          warning.code === "INVALID_ANNOTATION" &&
          id.includes("/node_modules/@vueuse/core/") &&
          message.includes("contains an annotation that Rollup cannot interpret")
        ) {
          return;
        }
        warn(warning);
      },
      output: {
        manualChunks: {
          monaco: ["monaco-editor"],
          vendor: ["vue", "element-plus"],
        },
      },
    },
  },
  optimizeDeps: { include: ["monaco-editor"] },
});
