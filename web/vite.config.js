import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
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
