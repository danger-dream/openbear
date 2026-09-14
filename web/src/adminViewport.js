import { ref, onMounted, onBeforeUnmount } from "vue";

// Phone editor selection tracks layout width, including rotation/resizing.
// Never base the editor mode on visualViewport width (pinch zoom).
export const ADMIN_PHONE_QUERY = "(max-width: 760px)";
export function observeAdminPhone(callback, win = globalThis.window) {
  const media = win?.matchMedia?.(ADMIN_PHONE_QUERY);
  if (!media) return () => {};
  const update = () => callback(media.matches);
  update();
  if (media.addEventListener) media.addEventListener("change", update);
  else media.addListener?.(update);
  return () => {
    if (media.removeEventListener) media.removeEventListener("change", update);
    else media.removeListener?.(update);
  };
}
export function useAdminPhone() {
  const isAdminPhone = ref(Boolean(globalThis.window?.matchMedia?.(ADMIN_PHONE_QUERY).matches));
  let stop;
  onMounted(() => { stop = observeAdminPhone(value => { isAdminPhone.value = value; }); });
  onBeforeUnmount(() => stop?.());
  return isAdminPhone;
}
