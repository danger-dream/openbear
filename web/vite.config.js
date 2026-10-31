import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";
import { createBuildInfo } from "./buildIdentity.mjs";

const webRoot = fileURLToPath(new URL(".", import.meta.url));
const buildInfo = createBuildInfo(webRoot);

// Android OS and browser versions are independent. Chrome 87 is an explicit
// older-engine build target, not a claim about every Android 11 installation.
export const browserTargets = ["safari15", "chrome87"];

// Monaco has its own dynamic language imports. Do not let its manual chunk
// absorb Vite's shared preload helper: that would make every lazy page (and
// the chat entry itself) statically import the full editor again.
export function manualChunk(id) {
  // Keep API patches independent and ahead of vendor evaluation. Shared CJS
  // helpers must not pull vendor/Monaco back into this compatibility chunk.
  if (id.includes("/node_modules/core-js/") || id.endsWith("/src/browserCompatibility.js") || id === "\0commonjsHelpers.js") return "compatibility";
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

// A gateway serves each instance's build below its own /_i/<instance>/ prefix.
// Compiled JS and CSS locate their assets relative to their own URL, so they
// follow whichever prefix loaded the page; index.html keeps root URLs, which
// the serving process rewrites for a prefixed page.
export function renderBuiltUrl(filename, {hostType}) {
  if (hostType === "js" || hostType === "css") return {relative: true};
}

export default defineConfig({
  plugins: [vue(), buildIdentity],
  experimental: { renderBuiltUrl },
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
    target: browserTargets,
    cssTarget: browserTargets,
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
