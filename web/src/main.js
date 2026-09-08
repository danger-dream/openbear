import "./monaco-worker.js"; // ⚠️ 必须第一个 import:配置 Monaco worker(早于 monaco-editor 求值)
import "./theme.js";
import { createApp } from "vue";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import "element-plus/theme-chalk/dark/css-vars.css";
import * as Icons from "@element-plus/icons-vue";
import App from "./App.vue";
import "./style.css";
import "./dark-theme.css";

const app = createApp(App);
for (const [name, comp] of Object.entries(Icons)) {
  app.component(name, comp);
}
app.use(ElementPlus);
app.mount("#app");
