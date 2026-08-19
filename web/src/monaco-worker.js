// Monaco worker 配置(Vite ?worker)。必须作为第一个被 import 的模块,
// 保证 window.MonacoEnvironment 在 monaco-editor 模块求值前就绪。
// 不配会报 "Could not create web worker(s)" → worker 退化主线程 → 补全等异步功能失效。
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";

if (typeof window !== "undefined") {
  window.MonacoEnvironment = {
    getWorker() {
      return new EditorWorker();
    },
  };
}
