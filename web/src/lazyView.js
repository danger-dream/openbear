import { defineComponent, h, onBeforeUnmount, shallowRef } from "vue";
import LazyViewState from "./components/LazyViewState.vue";

const LOADING_DELAY_MS = 300;

// Call once per view definition (never in a computed/render function). Successful
// modules and concurrent requests are shared; a failed request is NOT cached.
// No reload here: App's existing version/preload-error confirmation owns that.
export function defineLazyView(loader, label = "页面") {
  let resolved;
  let pending;
  function load() {
    if (resolved) return Promise.resolve(resolved);
    if (!pending) {
      pending = Promise.resolve().then(loader).then(module => {
        resolved = module.default || module;
        return resolved;
      }).finally(() => { pending = null; });
    }
    return pending;
  }
  return defineComponent({
    name: "LazyView",
    inheritAttrs: false,
    setup(_, { attrs, slots }) {
      const view = shallowRef(resolved);
      const error = shallowRef(null);
      const showLoading = shallowRef(false);
      let alive = true;
      let busy = false;
      let loadingTimer = null;

      function clearLoadingTimer() {
        if (loadingTimer === null) return;
        clearTimeout(loadingTimer);
        loadingTimer = null;
      }
      function delayLoadingState() {
        clearLoadingTimer();
        showLoading.value = false;
        loadingTimer = setTimeout(() => {
          loadingTimer = null;
          if (alive && !view.value && !error.value) showLoading.value = true;
        }, LOADING_DELAY_MS);
      }

      onBeforeUnmount(() => {
        alive = false;
        clearLoadingTimer();
      });
      async function retry() {
        if (busy || view.value) return;
        busy = true;
        error.value = null;
        delayLoadingState();
        try {
          const component = await load();
          if (alive) view.value = component;
        } catch (reason) {
          if (alive) error.value = reason;
        } finally {
          busy = false;
          clearLoadingTimer();
          if (alive) showLoading.value = false;
        }
      }
      if (!view.value) void retry();
      return () => view.value
        ? h(view.value, attrs, slots)
        : error.value || showLoading.value
          ? h(LazyViewState, { label, failed: Boolean(error.value), onRetry: retry })
          : null;
    },
  });
}
