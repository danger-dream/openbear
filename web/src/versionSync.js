export const FRONTEND_BUILD = {
  buildId: typeof __OPENBEAR_BUILD_ID__ === "string" ? __OPENBEAR_BUILD_ID__ : "",
  version: typeof __OPENBEAR_VERSION__ === "string" ? __OPENBEAR_VERSION__ : "",
};

// A non-ready snapshot may show disk files before the paired update is committed.
// Result acknowledgement and which tab started the update are deliberately irrelevant.
export function frontendMismatch(snapshot, current = FRONTEND_BUILD) {
  if (!snapshot || !["idle", "done", "checking"].includes(snapshot.phase || "idle")) return null;
  const buildId = String(snapshot.frontend?.buildId || "");
  if (buildId && current.buildId) return buildId !== current.buildId ? buildId : "";
  const version = String(snapshot.version || "");
  return version && current.version && version !== current.version ? `version:${version}` : "";
}
