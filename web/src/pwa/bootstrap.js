import { createInstallController } from "./install.js";

// main.js imports this before App evaluation. The lazy settings view reuses it.
export const installController = createInstallController(typeof window === "undefined" ? undefined : window);
