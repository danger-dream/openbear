import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";
import { createBuildInfo } from "./buildIdentity.mjs";

const webRoot = fileURLToPath(new URL(".", import.meta.url));
const buildInfo = createBuildInfo(webRoot);

// Monaco has its own dynamic language imports. Do not let its manual chunk
// absorb Vite's shared preload helper: that would make every lazy page (and
// the chat entry itself) statically import the full editor again.
export function manualChunk(id) {
  if (id === "\0vite/preload-helper.js") return "vendor";
  if (id.includes("/node_modules/monaco-editor/") && !id.includes("?worker")) return "monaco";
  if (/\/node_modules\/(?:vue|element-plus)\//.test(id)) return "vendor";
}

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
        manualChunks: manualChunk,
      },
    },
  },
  optimizeDeps: { include: ["monaco-editor"] },
});
