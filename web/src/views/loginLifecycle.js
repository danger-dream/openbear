// Login-only lifecycle adapter; no credential/request persistence or global listeners.
export function createLoginLifecycle({
  page = globalThis.document,
  browser = globalThis.window,
  network = globalThis.navigator,
} = {}) {
  let pageActive = true;
  return {
    isVisible: () => pageActive && page?.visibilityState !== "hidden",
    isOnline: () => network?.onLine !== false,
    subscribe(notify) {
      const onPageHide = () => { pageActive = false; notify(); };
      const onPageShow = () => { pageActive = true; notify(); };
      const listeners = [
        [page, "visibilitychange", notify],
        [browser, "pagehide", onPageHide],
        [browser, "pageshow", onPageShow],
        [browser, "online", notify],
        [browser, "offline", notify],
      ];
      for (const [target, event, listener] of listeners) target?.addEventListener(event, listener);
      return () => {
        for (const [target, event, listener] of listeners) target?.removeEventListener(event, listener);
      };
    },
  };
}
