import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { apiError } from "../api";
import { useAdminPhone } from "../adminViewport.js";
import "../mobile-assets.css";

// Presentation state only. Reads and mutations stay with the owning asset page.
export function useMobileAssets({ items, loadDetail, isSelected, setSelected, clearSelection, actions }) {
  const isAdminPhone = useAdminPhone();
  const mobileMode = ref("browse");
  const mobileId = ref(null);
  const mobileOpen = ref(false);
  const mobileView = ref("detail");
  const mobileDetail = ref(null);
  const mobileLoading = ref(false);
  const mobileError = ref("");
  const mobileBusy = ref(false);
  const mobileItem = computed(() => items.value.find(row => row.id === mobileId.value) || null);
  let request = 0;
  let alive = true;
  function closeMobileAsset() {
    request++;
    mobileOpen.value = false;
    mobileDetail.value = null;
    mobileLoading.value = false;
    mobileError.value = "";
  }
  function setMobileMode(mode) {
    closeMobileAsset();
    clearSelection();
    mobileMode.value = mode;
  }
  async function reloadMobileDetail() {
    const item = mobileItem.value;
    if (!item || !mobileOpen.value) return;
    const current = ++request;
    mobileLoading.value = true;
    mobileError.value = "";
    try {
      const result = await loadDetail(item.id);
      if (result?.ok === false || !result?.item) throw new Error(result?.error || "条目不存在");
      if (alive && current === request) mobileDetail.value = result.item;
    } catch (error) {
      if (alive && current === request) mobileError.value = apiError(error);
    } finally {
      if (alive && current === request) mobileLoading.value = false;
    }
  }
  async function openMobileAsset(item, view = "detail") {
    if (!isAdminPhone.value || mobileBusy.value) return;
    if (mobileMode.value === "select") { setSelected(item.id, !isSelected(item.id)); return; }
    if (mobileMode.value === "sort") return;
    closeMobileAsset();
    mobileId.value = item.id;
    mobileView.value = view;
    mobileOpen.value = true;
    if (view === "detail") await reloadMobileDetail();
  }
  async function runMobileAction(action) {
    const item = mobileItem.value;
    if (!item || mobileBusy.value) return;
    if (action === "more") { mobileView.value = "actions"; return; }
    if (!actions[action]) return;
    mobileBusy.value = true;
    try {
      if (action === "edit") {
        closeMobileAsset();
        await nextTick();
        if (!alive || !isAdminPhone.value) return;
      }
      await actions[action](item);
      if (alive && ["archive", "remove"].includes(action)) closeMobileAsset();
    } catch (error) {
      if (alive && error !== "cancel" && error !== "close") ElMessage.error("操作失败: " + apiError(error));
    } finally { mobileBusy.value = false; }
  }
  watch(mobileOpen, open => { if (!open) closeMobileAsset(); });
  watch(mobileItem, item => { if (!item) closeMobileAsset(); });
  watch(isAdminPhone, () => setMobileMode("browse"));
  onBeforeUnmount(() => { alive = false; closeMobileAsset(); });
  return { isAdminPhone, mobileMode, mobileOpen, mobileView, mobileItem, mobileDetail, mobileLoading, mobileError, mobileBusy,
    openMobileAsset, closeMobileAsset, setMobileMode, runMobileAction, reloadMobileDetail };
}
