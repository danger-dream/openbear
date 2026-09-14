import {createHash} from "node:crypto";
import {existsSync, readFileSync, readdirSync} from "node:fs";
import path from "node:path";

// Public installation metadata/icons are part of the UI delivery, not business
// data. A change there must participate in the existing manual-refresh handshake.
export function createBuildInfo(webRoot) {
  function files(dir, sourceOnly = false) {
    if (!existsSync(path.join(webRoot, dir))) return [];
    return readdirSync(path.join(webRoot, dir), {withFileTypes: true}).flatMap((entry) => {
      const name = `${dir}/${entry.name}`;
      if (entry.isDirectory()) return files(name, sourceOnly);
      return entry.isFile() && (!sourceOnly || /\.(vue|js|css)$/.test(name)) ? [name] : [];
    });
  }
  const inputs = [
    ...files("src", true), ...files("public"),
    "index.html", "package.json", "package-lock.json", "vite.config.js", "buildIdentity.mjs",
  ].sort();
  const hash = createHash("sha256");
  for (const name of inputs) {
    hash.update(name).update("\0").update(readFileSync(path.join(webRoot, name))).update("\0");
  }
  const {version} = JSON.parse(readFileSync(path.join(webRoot, "package.json"), "utf8"));
  return {schema: 1, version, buildId: hash.digest("hex").slice(0, 16)};
}
