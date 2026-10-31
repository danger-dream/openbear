// npm "prebuild": every frontend build of a development version (X.Y.Z-dev.N)
// raises N by one in all five version files, so each local build is told apart.
// beta, rc and release versions (what the release container builds) stay as
// they are. Plain Node, so it also runs without the Python environment.
// OPENBEAR_SKIP_DEV_BUMP=1 keeps the number: installing from source
// (scripts/install.sh) builds the frontend without counting a development build.
//
// The format is app/versioning.py's; uv.lock holds uv's PEP 440 spelling
// (X.Y.Z.devN), like scripts/bump_version.py writes it.
import {existsSync, readFileSync, renameSync, writeFileSync} from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";

const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(dev|beta|rc)\.([1-9]\d*))?$/;
const INIT = /^__version__\s*=\s*["']([^"']+)["']/m;
const PYPROJECT = /^version\s*=\s*["']([^"']+)["']/m;
const UV_LOCK = /(name = "openbear"\nversion = ")([^"]+)(")/;

export function parse(value) {
  const match = VERSION.exec(String(value ?? ""));
  if (!match) return null;
  return {base: `${match[1]}.${match[2]}.${match[3]}`, stage: match[4] || "release", number: Number(match[5] || 0)};
}

export function pep440(value) {
  const version = parse(value);
  if (!version) throw new Error(`not a version: ${value}`);
  const suffix = {release: "", rc: `rc${version.number}`, beta: `b${version.number}`, dev: `.dev${version.number}`};
  return version.base + suffix[version.stage];
}

function files(root) {
  return {
    init: path.join(root, "app", "__init__.py"),
    pyproject: path.join(root, "pyproject.toml"),
    uvLock: path.join(root, "uv.lock"),
    pkg: path.join(root, "web", "package.json"),
    pkgLock: path.join(root, "web", "package-lock.json"),
  };
}

function write(file, text) {
  const pending = `${file}.bump-${process.pid}`;
  writeFileSync(pending, text);
  renameSync(pending, file);
}

const json = (value) => JSON.stringify(value, null, 2) + "\n";

export const SKIP_ENV = "OPENBEAR_SKIP_DEV_BUMP";

/** The version after this build: {from, to, bumped}; throws when the five files disagree. */
export function bumpDevVersion(root, env = process.env) {
  const paths = files(root);
  const pkg = JSON.parse(readFileSync(paths.pkg, "utf8"));
  const current = String(pkg.version ?? "");
  const version = parse(current);
  if (!version) throw new Error(`web/package.json version ${current} is not X.Y.Z, X.Y.Z-rc.N, X.Y.Z-beta.N or X.Y.Z-dev.N`);
  if (version.stage !== "dev") return {from: current, to: current, bumped: false};
  if (env[SKIP_ENV] === "1") return {from: current, to: current, bumped: false, reason: `${SKIP_ENV}=1`};
  // A partial tree (a Docker build stage copies only a few files) cannot be kept
  // in step, so its build keeps the number.
  const missing = Object.values(paths).filter((file) => !existsSync(file));
  if (missing.length) return {from: current, to: current, bumped: false, reason: "version files missing: " + missing.map((f) => path.relative(root, f)).join(", ")};
  const init = readFileSync(paths.init, "utf8");
  const pyproject = readFileSync(paths.pyproject, "utf8");
  const uvLock = readFileSync(paths.uvLock, "utf8");
  const lock = JSON.parse(readFileSync(paths.pkgLock, "utf8"));
  const found = {
    "app/__init__.py": INIT.exec(init)?.[1], "pyproject.toml": PYPROJECT.exec(pyproject)?.[1],
    "web/package-lock.json version": lock.version, 'web/package-lock.json packages[""].version': lock.packages?.[""]?.version,
  };
  const drift = Object.entries(found).filter(([, value]) => value !== current).map(([name, value]) => `${name}=${value}`);
  if (UV_LOCK.exec(uvLock)?.[2] !== pep440(current)) drift.push(`uv.lock=${UV_LOCK.exec(uvLock)?.[2]}`);
  if (drift.length) {
    throw new Error(`version files disagree with web/package.json ${current}: ${drift.join(", ")}; run scripts/bump_version.py --check`);
  }
  const next = `${version.base}-dev.${version.number + 1}`;
  pkg.version = next;
  lock.version = next;
  lock.packages[""].version = next;
  write(paths.init, init.replace(INIT, `__version__ = "${next}"`));
  write(paths.pyproject, pyproject.replace(PYPROJECT, `version = "${next}"`));
  write(paths.uvLock, uvLock.replace(UV_LOCK, (_all, head, _old, tail) => head + pep440(next) + tail));
  write(paths.pkgLock, json(lock));
  write(paths.pkg, json(pkg));
  return {from: current, to: next, bumped: true};
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = process.argv[2] ? path.resolve(process.argv[2]) : path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  try {
    const result = bumpDevVersion(root);
    if (result.bumped) console.log(`OpenBear development version ${result.from} -> ${result.to}`);
    else if (result.reason) console.log(`OpenBear version ${result.from} kept (${result.reason})`);
  } catch (error) {
    console.error(String(error.message || error));
    process.exit(1);
  }
}
