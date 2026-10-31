#!/usr/bin/env python3
"""Run the tests this checkout's changes reach, and nothing else.

Python tests are picked by pytest-testmon from the code each test executed; frontend
tests by the files they import or read; Go packages by their sources and the packages
importing them. What passed is not run again until something it depends on changes.
A failing test is rerun on a clean checkout of HEAD, to tell a failure this change made
from one that was already there.

Full suites are scripts/release.py's job; --full runs them here only when asked for.

    .venv/bin/python scripts/check.py           # test the changes
    .venv/bin/python scripts/check.py --list    # show what would run
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
STATE = ROOT / ".check"
PYTHON = ROOT / ".venv" / "bin" / "python"
REPORTER = ROOT / "scripts" / "check_reporter.mjs"
BUDGET = 540  # one shell command may run for ten minutes
CPUS = os.cpu_count() or 4
SCOPES = ("app", "prompts", "tests", "scripts", "web", "node", "gateway", "pyproject.toml", "uv.lock", "benchmark-package", ".githooks", ".gitattributes")
GO_MODULES = ("node", "gateway")
VERSION_FILES = {"node/VERSION", "gateway/VERSION"}  # raised by every build, never a reason to test
PY_DEPENDENCIES = {"pyproject.toml", "uv.lock"}
VERSIONED = ("web/package.json", "web/package-lock.json", *sorted(PY_DEPENDENCIES))
NODE_BUILD = ("node_binary", "newer_build", "_build_node", "./cmd/openbear-node")
WEB_TEST = re.compile(r"(?:^|/)(?:[^/]*[.\-_]test|test-[^/]*|test)\.[cm]?js$")
KEEP_LOGS = 15

IMPORTS = (
    re.compile(r"""\b(?:import|export)\b[^'"`;()]*?\bfrom\s*(['"])([^'"\n]+)\1"""),
    re.compile(r"""\bimport\s*(['"])([^'"\n]+)\1"""),
    re.compile(r"""\bimport\s*\(\s*(['"`])([^'"`$\n]+)\1\s*\)"""),
    re.compile(r"""@import\s+(?:url\(\s*)?(['"])([^'"\n]+)\1"""),
)
LITERAL = re.compile(r"""(['"`])([\w@./\-]*[\w\-]\.(?:vue|[cm]?js|ts|css|json|html|svg|md|txt|webmanifest))\1""")
TEMPLATE = re.compile(r"""`((?:\.{1,2}/|/src/|src/)[^`$]*)\$\{[^}`]*\}[^`$]*?(\.\w+)`""")
SCANS = re.compile(r"\b(?:readdirSync|opendirSync|readdir)\s*\(")
PY_VERSION = re.compile(rb"""^__version__\s*=\s*["'][^"'\n]*["']""", re.M)
LABELS = {"old": "HEAD 上同样失败", "new": "这次改出来的", "absent": "新加的测试，HEAD 上没有", "flaky": "单独重跑通过，负载下不稳定"}

_groups: set[int] = set()


# ---------------------------------------------------------------- files and state

def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def tracked() -> list[str]:
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *SCOPES).split("\0")
    return sorted({n for n in names if n and not {"node_modules", "__pycache__"} & set(n.split("/")) and n not in VERSION_FILES})


def changed_since(ref: str) -> set[str]:
    names = set(git("diff", "--name-only", "-z", ref, "--", *SCOPES).split("\0"))
    names |= set(git("ls-files", "-z", "--others", "--exclude-standard", "--", *SCOPES).split("\0"))
    names = {n for n in names if n and "__pycache__" not in n and n not in VERSION_FILES}
    for rel in [n for n in names if n.endswith(".py") or n in VERSIONED]:
        before = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=ROOT, capture_output=True)
        if before.returncode == 0 and _digest(rel, before.stdout) == digest(rel):
            names.discard(rel)
    return names


def digest(rel: str) -> str:
    try:
        return _digest(rel, (ROOT / rel).read_bytes())
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return ""


def _digest(rel: str, data: bytes) -> str:
    # A build raises the version in these; that alone changes nothing a test checks.
    if rel in PY_DEPENDENCIES:
        try:
            doc = tomllib.loads(data.decode())
            if rel == "pyproject.toml":
                doc.get("project", {}).pop("version", None)
            else:
                for package in doc.get("package", []):
                    if package.get("name") == "openbear" and package.get("source") in ({"virtual": "."}, {"editable": "."}):
                        package.pop("version", None)
            data = json.dumps(doc, sort_keys=True, default=str).encode()
        except (ValueError, UnicodeError):
            pass
    elif rel in VERSIONED:
        try:
            doc = json.loads(data)
            doc.pop("version", None)
            doc.get("packages", {}).get("", {}).pop("version", None)
            data = json.dumps(doc, sort_keys=True).encode()
        except ValueError:
            pass
    elif rel.endswith(".py"):
        data = PY_VERSION.sub(b"__version__", data)
    return hashlib.sha1(data).hexdigest()


def kind(rel: str) -> str:
    if rel.startswith("web/"):
        return "web"
    if rel.split("/", 1)[0] in GO_MODULES:
        return "go"
    return "py" if rel.endswith(".py") else "data"


def load_state() -> dict:
    try:
        return json.loads((STATE / "state.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    pending = STATE / "state.json.pending"
    pending.write_text(json.dumps(state))
    os.replace(pending, STATE / "state.json")


def base_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "TESTMON_DATAFILE")}
    home = env.setdefault("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    env.setdefault("GOPATH", str(Path(home) / "go"))
    return env


def go_env() -> dict[str, str]:
    return {**base_env(), "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOFLAGS": "-mod=readonly"}


def short(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


# ---------------------------------------------------------------- what reaches what

def _inside(path: Path, top: Path) -> str | None:
    path = Path(os.path.normpath(path))
    if "node_modules" in path.parts or not path.is_relative_to(top):
        return None
    return path.relative_to(ROOT).as_posix()


def _resolve(spec: str, origin: Path) -> str | None:
    spec = spec.split("?")[0]
    if spec.startswith("."):
        base = origin.parent / spec
    elif spec.startswith("/src/"):
        base = WEB / spec[1:]
    else:
        return None
    for ext in ("", ".js", ".mjs", ".vue", ".ts", ".css", ".json", "/index.js", "/index.vue"):
        candidate = Path(str(base) + ext)
        if candidate.is_file():
            return _inside(candidate, WEB)
    return None


def _resolve_literal(text: str, origin: Path) -> str | None:
    text = text.split("?")[0].lstrip("/")
    for base in (origin.parent, WEB, WEB / "src"):
        if (base / text).is_file():
            return _inside(base / text, WEB)
    return None


def _resolve_folder(prefix: str, origin: Path) -> str | None:
    stem = prefix if prefix.endswith("/") else prefix.rsplit("/", 1)[0] + "/"
    base = WEB / stem.lstrip("/") if stem.startswith(("/src/", "src/")) else origin.parent / stem
    folder = _inside(base, WEB)
    return folder + "/" if folder and (ROOT / folder).is_dir() else None


class WebGraph:
    """Which files each frontend test imports or reads, followed through imports."""

    def __init__(self, files: list[str], current: dict[str, str]):
        self.current = current
        self.files = [f for f in files if f.startswith("web/")]
        self.tests = sorted(f for f in self.files if f.startswith("web/src/") and WEB_TEST.search(f) and (ROOT / f).is_file())
        self._parsed: dict[str, tuple[set[str], set[str]]] = {}
        self._deps: dict[str, set[str]] = {}

    def _parse(self, rel: str) -> tuple[set[str], set[str]]:
        if rel in self._parsed:
            return self._parsed[rel]
        path, imports, reads = ROOT / rel, set(), set()
        if path.suffix in (".js", ".mjs", ".cjs", ".vue", ".ts", ".css"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for pattern in IMPORTS:
                imports.update(t for m in pattern.finditer(text) if (t := _resolve(m.group(2), path)))
            reads.update(t for m in LITERAL.finditer(text) if (t := _resolve_literal(m.group(2), path)))
            if WEB_TEST.search(rel):
                if SCANS.search(text):
                    reads.update(f for f in self.files if f.startswith("web/src/") and not WEB_TEST.search(f))
                for m in TEMPLATE.finditer(text):
                    if folder := _resolve_folder(m.group(1), path):
                        reads.update(f for f in self.files if f.startswith(folder) and f.endswith(m.group(2)))
        self._parsed[rel] = (imports - {rel}, reads - {rel})
        return self._parsed[rel]

    def deps(self, test: str) -> set[str]:
        if test not in self._deps:
            seen, reads, stack = set(), set(), [test]
            while stack:
                rel = stack.pop()
                if rel in seen:
                    continue
                seen.add(rel)
                imports, more = self._parse(rel)
                stack.extend(imports - seen)
                reads |= more
            self._deps[test] = seen | reads | {"web/package.json", "web/package-lock.json"}
        return self._deps[test]

    def fingerprint(self, test: str) -> str:
        """Everything the test depends on, as it is now; a pass is recorded against this."""
        lines = (f"{d}:{self.current.get(d) or digest(d)}" for d in sorted(self.deps(test)))
        return hashlib.sha1("\n".join(lines).encode()).hexdigest()


class PyIndex:
    """Python test files, their text and the modules they import directly."""

    def __init__(self):
        self.tests = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("test_*.py"))
        self._text: dict[str, str] = {}

    def text(self, rel: str) -> str:
        if rel not in self._text:
            try:
                self._text[rel] = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._text[rel] = ""
        return self._text[rel]

    def imports(self, rel: str) -> set[str]:
        try:
            tree = ast.parse(self.text(rel))
        except SyntaxError:
            return set()
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
                names.update(f"{node.module}.{a.name}" for a in node.names)
        return names

    def node_build_tests(self) -> list[str]:
        return [t for t in self.tests if any(marker in self.text(t) for marker in NODE_BUILD)]

    def tests_for_file(self, rel: str) -> list[str]:
        """Tests naming a non-Python file; else tests named after it (work_items.sql: test_work_items.py);
        else tests importing a module that names it. The last is broad, so it is only the fallback."""
        if rel.startswith("benchmark-package/"):
            return [t for t in self.tests if t in {"tests/test_servers_bench.py", "tests/test_bench_review.py", "tests/test_release_resources.py"}]
        name, stem = Path(rel).name, Path(rel).stem.replace("-", "_")
        hits = [t for t in self.tests if name in self.text(t)]
        hits = hits or [t for t in self.tests if stem in Path(t).stem]
        if hits:
            return hits
        modules = {p.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
                   for p in (ROOT / "app").rglob("*.py") if name in self.text(p.relative_to(ROOT).as_posix())}
        return [t for t in self.tests if any(i == m or i.startswith(m + ".") for i in self.imports(t) for m in modules)]


def go_packages(module: str) -> list[tuple[str, str, set[str]]]:
    out = subprocess.run(["go", "list", "-e", "-f", '{{.ImportPath}}\t{{.Dir}}\t{{join .Deps " "}}', "./..."],
                         cwd=ROOT / module, env=go_env(), capture_output=True, text=True, timeout=120).stdout
    packages = []
    for line in out.splitlines():
        path, folder, deps = (line.split("\t") + ["", ""])[:3]
        packages.append((path, Path(folder).relative_to(ROOT).as_posix(), set(deps.split())))
    return packages


# ---------------------------------------------------------------- the plan

@dataclass
class Plan:
    first: bool
    changed: set[str]
    python: bool = False
    testmon_new: bool = False
    extra: dict[str, str] = field(default_factory=dict)      # python test file -> why
    web: dict[str, str] = field(default_factory=dict)        # web test -> why
    web_done: int = 0                                         # reached, but already passed as things are
    go: dict[str, list[str]] = field(default_factory=dict)   # module -> import paths
    uncovered: list[str] = field(default_factory=list)
    data_tests: dict[str, list[str]] = field(default_factory=dict)

    def empty(self) -> bool:
        return not (self.python or self.extra or self.web or any(self.go.values()))


def make_plan(args, state: dict, current: dict[str, str], web: WebGraph, py: PyIndex) -> Plan:
    snapshot = state.get("snapshot")
    if snapshot is None:
        changed = changed_since(args.base)
    else:
        changed = {f for f in set(current) | set(snapshot) if current.get(f, "") != snapshot.get(f, "")}
    plan = Plan(first=snapshot is None, changed=changed)
    by_kind = {k: sorted(f for f in changed if kind(f) == k) for k in ("py", "data", "web", "go")}
    plan.testmon_new = not (STATE / "testmondata").exists()
    plan.python = bool(args.full or by_kind["py"] or changed & PY_DEPENDENCIES or plan.testmon_new)

    if args.full:
        plan.web = {t: "--full" for t in web.tests}
        for module in GO_MODULES:
            if (ROOT / module / "go.mod").is_file():
                plan.go[module] = ["./..."]
        return plan

    for rel in by_kind["data"]:
        if rel in PY_DEPENDENCIES:
            continue
        tests = py.tests_for_file(rel)
        plan.data_tests[rel] = tests
        if not tests:
            plan.uncovered.append(rel)
        for t in tests:
            plan.extra.setdefault(t, f"引用了 {rel}")

    changed_web, passed = set(by_kind["web"]), state.get("web_passed", {})
    if changed_web:
        reached = set()
        for test in web.tests:
            hit = sorted(web.deps(test) & changed_web)
            if not hit:
                continue
            reached.update(hit)
            if web.fingerprint(test) in passed.get(test, ()):
                plan.web_done += 1
            else:
                plan.web[test] = hit[0] if len(hit) == 1 else f"{hit[0]} 等 {len(hit)} 个"
        plan.uncovered += [f for f in sorted(changed_web - reached) if (ROOT / f).is_file()]

    for module in GO_MODULES:
        files = [f for f in by_kind["go"] if f.startswith(module + "/")]
        if not files or not (ROOT / module / "go.mod").is_file():
            continue
        packages = go_packages(module)
        hit = set()
        for rel in files:
            owners = [p for p in packages if (rel.rsplit("/", 1)[0] + "/").startswith(p[1] + "/")]
            whole = not owners or Path(rel).name in ("go.mod", "go.sum")
            hit |= {p[0] for p in packages} if whole else {max(owners, key=lambda p: len(p[1]))[0]}
        plan.go[module] = sorted(p[0] for p in packages if p[0] in hit or p[2] & hit)
        if module == "node":
            for t in py.node_build_tests():
                plan.extra.setdefault(t, "会编译节点（node/ 有改动）")
    return plan


# ---------------------------------------------------------------- running

@dataclass
class Failure:
    lane: str
    key: str
    title: str
    detail: str
    unit: str
    verdict: str = ""


@dataclass
class Lane:
    name: str
    complete: bool = True
    error: str = ""
    note: str = ""
    passed: int = 0
    skipped: int = 0
    left: int = 0
    seconds: float = 0.0
    failures: list[Failure] = field(default_factory=list)
    passed_keys: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)          # test files / packages that ran to the end
    failed_units: set[str] = field(default_factory=set)
    unit_seconds: dict[str, float] = field(default_factory=dict)
    alone: set[str] = field(default_factory=set)          # failed files that passed run by themselves


def run(cmd: list[str], cwd: Path, env: dict, log: Path, deadline: float, stdout: Path | None = None) -> int | None:
    with open(log, "ab") as err, open(stdout or log, "ab") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True)
        _groups.add(proc.pid)
        try:
            return proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill(proc.pid)
            proc.wait()
            return None
        finally:
            _groups.discard(proc.pid)


def _kill(pgid: int) -> None:
    for sig, pause in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        end = time.monotonic() + pause
        while time.monotonic() < end:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)


def _on_signal(signum, _frame):
    for pgid in list(_groups):
        _kill(pgid)
    sys.exit(128 + signum)


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def to_nodeid(classname: str, name: str, root: Path = ROOT) -> str:
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        file = "/".join(parts[:i]) + ".py"
        if (root / file).is_file():
            return "::".join([file, *parts[i:], name])
    return f"{classname}::{name}"


def _py_detail(element) -> str:
    text = element.text or ""
    message = (element.get("message") or "").strip().splitlines()
    lines = [message[0][:300]] if message else []
    lines += [line[1:].strip()[:300] for line in text.splitlines() if line.startswith("E ")][:5]
    at = [m.group(0).removeprefix(f"{ROOT}/") for line in text.splitlines() if (m := re.match(r"^[\w./\-]+\.py:\d+", line))]
    unique = list(dict.fromkeys(line for line in lines if line))
    return "\n".join(unique[:5] + ([f"位置 {at[-1]}"] if at else []))


def read_junit(path: Path, lane: Lane, root: Path = ROOT) -> bool:
    try:
        tree = ET.parse(path)
    except (FileNotFoundError, ET.ParseError):
        return False
    for case in tree.iter("testcase"):
        nodeid = to_nodeid(case.get("classname", ""), case.get("name", ""), root)
        unit = nodeid.split("::", 1)[0]
        lane.units.add(unit)
        lane.unit_seconds[unit] = lane.unit_seconds.get(unit, 0.0) + float(case.get("time") or 0)
        bad = case.find("failure")
        bad = bad if bad is not None else case.find("error")
        if bad is not None:
            lane.failures.append(Failure("py", nodeid, nodeid, _py_detail(bad), unit))
            lane.failed_units.add(unit)
        elif case.find("skipped") is not None:
            lane.skipped += 1
        else:
            lane.passed += 1
            lane.passed_keys.add(nodeid)
    return True


def pytest_cmd(junit: Path, workers: int, *more: str) -> list[str]:
    cmd = [str(PYTHON), "-m", "pytest", "-q", "-rfE", "-o", f"cache_dir={STATE / 'pytest-cache'}", f"--junitxml={junit}"]
    if workers > 1:
        cmd += ["-n", str(workers), "--dist=worksteal"]
    return cmd + list(more)


def run_testmon(logs: Path, deadline: float, full: bool) -> Lane:
    lane = Lane("python")
    env = {**base_env(), "TESTMON_DATAFILE": str(STATE / "testmondata"), "PYTHONPATH": str(ROOT)}
    log, junit = logs / "python.log", logs / "python.xml"
    start = time.monotonic()
    code = run(pytest_cmd(junit, CPUS, "-p", "scripts.check_testmon", "--testmon-noselect" if full else "--testmon"),
               ROOT, env, log, deadline)
    lane.seconds = time.monotonic() - start
    lane.complete = code is not None
    read_junit(junit, lane)
    try:
        head = log.read_text(errors="replace").splitlines()[:40]
    except OSError:
        head = []
    notes = [line.strip() for line in head if line.startswith("testmon:")]
    lane.note = notes[0][:300] if notes else ""
    if code not in (0, 1, 5, None):
        lane.error = f"pytest 退出码 {code}\n{_tail(log)}"
    return lane


def run_extra(tests: list[str], logs: Path, deadline: float) -> Lane:
    lane = Lane("python-extra")
    log, junit = logs / "python-extra.log", logs / "python-extra.xml"
    start = time.monotonic()
    code = run(pytest_cmd(junit, min(CPUS, len(tests) * 2), *tests), ROOT, base_env(), log, deadline)
    lane.seconds = time.monotonic() - start
    lane.complete = code is not None
    read_junit(junit, lane)
    if code not in (0, 1, 5, None):
        lane.error = f"pytest 退出码 {code}\n{_tail(log)}"
    return lane


def read_node(jsonl: Path, lane: Lane, test: str) -> None:
    try:
        lines = jsonl.read_text(errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        key = f"{test} › {event.get('name') or ''}"
        if event.get("ok"):
            lane.passed += 1
            lane.passed_keys.add(key)
            continue
        detail = "\n".join((event.get("error") or "").strip().splitlines()[:6])
        if event.get("at"):
            detail += f"\n位置 {event['at']}"
        lane.failures.append(Failure("web", key, key, detail, test))


def run_web(tests: list[str], logs: Path, deadline: float, concurrency: int, root: Path = ROOT,
            tag: str = "web", longest: dict[str, float] | None = None) -> Lane:
    """One node process per test file, longest first, so each file's end and outcome are known."""
    lane = Lane("web")
    folder = logs / tag
    folder.mkdir(exist_ok=True)
    queue = sorted(tests, key=lambda t: -(longest or {}).get(t, 0.0))
    guard = threading.Lock()
    start = time.monotonic()

    def one(test: str) -> None:
        name = test.removeprefix("web/").replace("/", "_")
        jsonl, log, err = folder / f"{name}.jsonl", folder / f"{name}.log", folder / f"{name}.stderr.log"
        cmd = ["node", "--test", "--test-reporter=spec", f"--test-reporter-destination={log}",
               f"--test-reporter={REPORTER}", f"--test-reporter-destination={jsonl}", test.removeprefix("web/")]
        began = time.monotonic()
        code = run(cmd, root / "web", base_env(), err, deadline)
        part = Lane("web")
        read_node(jsonl, part, test)
        with guard:
            lane.passed += part.passed
            lane.passed_keys |= part.passed_keys
            lane.failures += part.failures
            if code is None:
                lane.complete = False
                return
            lane.units.add(test)
            lane.unit_seconds[test] = time.monotonic() - began
            if code != 0 or part.failures:
                lane.failed_units.add(test)
                if not part.failures:
                    detail = _tail(err, 8) or _tail(log, 8)
                    lane.failures.append(Failure("web", f"{test} › 整个文件", f"{test}（退出码 {code}，没有测试报告失败）", detail, test))

    def worker() -> None:
        while True:
            with guard:
                if not queue or time.monotonic() >= deadline - 1:
                    return
                test = queue.pop(0)
            one(test)

    threads = [threading.Thread(target=worker) for _ in range(max(1, min(concurrency, len(queue))))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lane.left = len(queue)
    lane.complete = lane.complete and not queue
    lane.seconds = time.monotonic() - start
    return lane


def run_go(module: str, packages: list[str], logs: Path, deadline: float, root: Path = ROOT, tag: str = "go",
           flags: tuple[str, ...] = ()) -> Lane:
    lane = Lane(f"go:{module}")
    raw, log = logs / f"{tag}-{module}.json", logs / f"{tag}-{module}.log"
    start = time.monotonic()
    code = run(["go", "test", "-json", *flags, *packages], root / module, go_env(), log, deadline, stdout=raw)
    lane.seconds = time.monotonic() - start
    lane.complete = code is not None
    output: dict[tuple[str, str], list[str]] = {}
    failed_tests: set[tuple[str, str]] = set()
    try:
        events = [json.loads(line) for line in raw.read_text(errors="replace").splitlines() if line.startswith("{")]
    except (OSError, ValueError):
        events = []
    with open(log, "a") as text:
        for e in events:
            # Build events name the test variant, "pkg [pkg.test]".
            package, test = (e.get("Package") or e.get("ImportPath") or "").split(" [")[0], e.get("Test") or ""
            if e.get("Output"):
                output.setdefault((package, test), []).append(e["Output"].rstrip("\n"))
                text.write(e["Output"])
            action = e.get("Action")
            if action in ("pass", "fail", "skip") and package and not test:
                lane.units.add(package)
                lane.unit_seconds[package] = float(e.get("Elapsed") or 0)
            if action == "pass" and test:
                lane.passed += 1
                lane.passed_keys.add(f"{module}:{package}:{test}")
            elif action == "skip" and test:
                lane.skipped += 1
            elif action in ("fail", "build-fail"):
                if test:
                    failed_tests.add((package, test))
                elif not any(p == package for p, _ in failed_tests):
                    failed_tests.add((package, ""))
                lane.failed_units.add(package)
    for package, test in sorted(failed_tests):
        lines = [line for line in output.get((package, test), []) if line.strip() and not line.startswith(("=== RUN", "=== PAUSE", "=== CONT"))]
        title = f"{package} {test}" if test else f"{package}（编译或包级失败）"
        lane.failures.append(Failure("go", f"{module}:{package}:{test}", title, "\n".join(lines[-6:]), package))
    if code not in (0, 1, None) or (code and not lane.failures):
        lane.error = f"go test 退出码 {code}\n{_tail(log)}"
    return lane


# ---------------------------------------------------------------- telling failures apart

def rerun_alone(lanes: list[Lane], logs: Path, deadline: float) -> str:
    """A failure that passes when its test runs by itself came from load, not from the change."""
    failing = [lane for lane in lanes if lane.failures]
    if not failing:
        return ""
    estimate = 10.0 + sum(min(lane.unit_seconds.get(u, 10.0), 300.0) for lane in failing for u in lane.failed_units)
    if time.monotonic() + estimate > deadline:
        return f"剩余时间不够（估计要 {short(estimate)}），失败的测试没有单独重跑"
    out = logs / "alone"
    out.mkdir(exist_ok=True)
    for lane in failing:
        if lane.name.startswith("python"):
            again = Lane("alone")
            ids = sorted({f.key for f in lane.failures})
            run([str(PYTHON), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={out / (lane.name + '.xml')}", *ids],
                ROOT, base_env(), out / f"{lane.name}.log", deadline)
            read_junit(out / f"{lane.name}.xml", again)
            for f in lane.failures:
                f.verdict = "flaky" if f.key in again.passed_keys else f.verdict
        elif lane.name == "web":
            again = run_web(sorted(lane.failed_units), out, deadline, 1)
            fine = again.units - again.failed_units
            lane.alone = fine
            for f in lane.failures:
                f.verdict = "flaky" if f.unit in fine else f.verdict
        else:
            module = lane.name.split(":", 1)[1]
            again = run_go(module, sorted(lane.failed_units), out, deadline, flags=("-count=1", "-p=1"))
            for f in lane.failures:
                f.verdict = "flaky" if f.key in again.passed_keys or (f.unit in again.units - again.failed_units) else f.verdict
    return ""


def compare_with_head(lanes: list[Lane], ref: str, logs: Path, deadline: float) -> str:
    failures = [f for lane in lanes for f in lane.failures if f.verdict != "flaky"]
    if not failures:
        return ""
    estimate = 20.0
    for lane in lanes:
        units = {f.unit for f in lane.failures if f.verdict != "flaky"}
        estimate += sum(min(lane.unit_seconds.get(u, 10.0), 300.0) for u in units) * 1.3
    if time.monotonic() + estimate > deadline:
        return f"剩余时间不够（估计要 {short(estimate)}），没和 {ref} 对比"
    base = Path(tempfile.mkdtemp(prefix="openbear-check-"))
    tree, out = base / "src", logs / "baseline"
    out.mkdir(exist_ok=True)
    try:
        git("worktree", "add", "--detach", str(tree), ref)
        (tree / "workspace").mkdir(exist_ok=True)
        if (WEB / "node_modules").exists():
            (tree / "web" / "node_modules").symlink_to(WEB / "node_modules")
        failing_there: set[str] = set()
        present: set[str] = set()

        ids = sorted({f.key for f in failures if f.lane == "py" and (tree / f.unit).is_file()})
        lane = Lane("head-python")
        while ids:
            log = out / "python.log"
            cmd = [str(PYTHON), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={out / 'python.xml'}", *ids]
            code = run(cmd, tree, base_env(), log, deadline)
            missing = set(re.findall(r"ERROR: not found: (\S+)", _tail(log, 200)))
            if code == 4 and missing:
                ids = [i for i in ids if i not in missing]
                continue
            if read_junit(out / "python.xml", lane, tree):
                present |= set(ids)
                failing_there |= {f.key for f in lane.failures}
            break

        web = sorted({f.unit for f in failures if f.lane == "web" and (tree / f.unit).is_file()})
        if web:
            lane = run_web(web, out, deadline, 2, root=tree)
            present |= {f.key for f in failures if f.lane == "web" and f.unit in lane.units}
            failing_there |= {f.key for f in lane.failures}

        for module in GO_MODULES:
            packages = sorted({f.unit for f in failures if f.lane == "go" and f.key.startswith(module + ":")})
            if packages and (tree / module / "go.mod").is_file():
                lane = run_go(module, packages, out, deadline, root=tree, tag="go")
                present |= {f.key for f in failures if f.lane == "go" and f.unit in lane.units}
                failing_there |= {f.key for f in lane.failures}

        for f in failures:
            f.verdict = "old" if f.key in failing_there else "new" if f.key in present else "absent"
        return ""
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"没能和 {ref} 对比：{exc}"
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- main

def _clean(lane: Lane | None) -> bool:
    return lane is None or (lane.complete and not lane.error and all(f.verdict == "flaky" for f in lane.failures))


def settle(plan: Plan, web: WebGraph, lanes: dict[str, Lane], passed: dict[str, str]) -> set[str]:
    """Changed files every test reaching them has now passed with."""
    settled = set()
    extra = lanes.get("python-extra")
    for rel in plan.changed:
        k = kind(rel)
        if k == "py" or rel in PY_DEPENDENCIES:
            ok = _clean(lanes.get("python"))
        elif k == "data":
            tests = set(plan.data_tests.get(rel, []))
            bad = {f.unit for f in extra.failures if f.verdict != "flaky"} if extra else set()
            ok = not tests or (extra is not None and extra.complete and not extra.error and not tests & bad)
        elif k == "web":
            ok = all(web.fingerprint(t) in passed.get(t, ()) for t in web.tests if rel in web.deps(t))
        else:
            module = rel.split("/", 1)[0]
            ok = _clean(lanes.get(f"go:{module}")) and (module != "node" or _clean(extra))
        if ok:
            settled.add(rel)
    return settled


def report(plan: Plan, lanes: dict[str, Lane], notes: list[str], logs: Path, ref: str) -> int:
    out = []
    counts = {k: sum(1 for f in plan.changed if kind(f) == k) for k in ("py", "web", "go", "data")}
    names = {"py": "Python", "web": "前端", "go": "Go", "data": "其他"}
    since = f"相对 {ref} 的未提交改动（首次运行）" if plan.first else "自上次通过后的改动"
    out.append(f"{since}：" + "，".join(f"{names[k]} {n}" for k, n in counts.items() if n) if plan.changed else f"{since}：无")
    if plan.testmon_new:
        out.append("testmon 还没有依赖记录：这次完整跑一遍 Python 测试来建立，之后只跑受影响的")

    def line(label: str, lane: Lane | None, what: str) -> None:
        if lane is None:
            return
        status = f"通过 {lane.passed}" + (f"，失败 {len(lane.failures)}" if lane.failures else "") + (f"，跳过 {lane.skipped}" if lane.skipped else "")
        if lane.left:
            status += f"，还有 {lane.left} 个文件没跑到"
        elif not lane.complete:
            status += "，超时被中止"
        out.append(f"  {label:<8}{what}：{status}  {short(lane.seconds)}")
        if lane.note:
            out.append(f"          {lane.note}")
        if lane.error:
            out.extend("          " + text for text in lane.error.splitlines()[:12])

    py = lanes.get("python")
    if py:
        line("Python", py, f"testmon 选中 {py.passed + len(py.failures) + py.skipped} 项")
    if lanes.get("python-extra"):
        reasons = "；".join(sorted(set(plan.extra.values())))
        line("Python", lanes["python-extra"], f"另跑 {len(plan.extra)} 个文件（{reasons[:120]}）")
    for module in GO_MODULES:
        line("Go", lanes.get(f"go:{module}"), f"{module} {len(plan.go.get(module, []))} 个包")
    if lanes.get("web"):
        done = f"，另有 {plan.web_done} 个已在当前内容下通过过" if plan.web_done else ""
        line("前端", lanes["web"], f"选中 {len(plan.web)} 个文件{done}")
        for test, why in sorted(plan.web.items())[:6]:
            out.append(f"          {Path(test).name} ← {why}")
        if len(plan.web) > 6:
            out.append(f"          …… 另有 {len(plan.web) - 6} 个")
    if plan.uncovered:
        out.append("没有测试覆盖到：" + "、".join(plan.uncovered[:8]) + (f" 等 {len(plan.uncovered)} 个" if len(plan.uncovered) > 8 else ""))

    failures = [f for lane in lanes.values() for f in lane.failures]
    if failures:
        out.append(f"失败 {len(failures)} 项：")
        for f in failures[:15]:
            verdict = LABELS.get(f.verdict, "")
            out.append(f"  [{verdict}] {f.title}" if verdict else f"  {f.title}")
            out.extend("      " + text[:240] for text in f.detail.splitlines()[:7])
        if len(failures) > 15:
            out.append(f"  …… 另有 {len(failures) - 15} 项，见日志")
    out.extend(note for note in notes if note)
    mine = [f for f in failures if f.verdict not in ("old", "flaky")]
    if mine:
        unknown = "" if all(f.verdict for f in mine) else "（没能和 HEAD 对比，不确定是不是这次改的）"
        verdict, code = f"结论：{len(mine)} 项失败需要处理{unknown}", 1
    elif any(lane.error for lane in lanes.values()):
        verdict, code = "结论：测试没能正常运行，见上面的报错", 2
    elif any(not lane.complete for lane in lanes.values()):
        verdict, code = "结论：时间用完，结果不完整；再运行一次会接着跑没跑到的", 3
    elif failures:
        verdict, code = "结论：没有这次改出来的失败（失败的都是负载下不稳定或 HEAD 上原本就失败的）", 0
    else:
        verdict, code = "结论：通过", 0
    out.append(verdict)
    out.append(f"日志：{logs.relative_to(ROOT)}/")
    text = "\n".join(out)
    (logs / "summary.txt").write_text(text + "\n")
    print(text)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="只列出会跑什么，不运行")
    parser.add_argument("--full", action="store_true", help="跑全部测试（只在明确要求时用）")
    parser.add_argument("--base", default="HEAD", help="对比失败、首次运行时作为基准的提交，默认 HEAD")
    parser.add_argument("--budget", type=int, default=BUDGET, help=f"总时长上限（秒），默认 {BUDGET}")
    parser.add_argument("--no-baseline", action="store_true", help="有失败时不单独重跑、不到基准提交上对比")
    args = parser.parse_args(argv)
    started = time.monotonic()
    deadline = started + args.budget
    STATE.mkdir(exist_ok=True)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    with open(STATE / "lock", "w") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started > 120:
                    print("另一个 check.py 正在运行，等了 2 分钟还没结束")
                    return 2
                time.sleep(2)

        state = load_state()
        files = tracked()
        current = {f: digest(f) for f in files}
        web, py = WebGraph(files, current), PyIndex()
        plan = make_plan(args, state, current, web, py)

        if args.list:
            print(f"改动 {len(plan.changed)} 个文件" + (f"（相对 {args.base}，首次运行）" if plan.first else "（自上次通过）"))
            if plan.python:
                print("Python：testmon 运行时按执行过的代码挑选" + ("（还没有依赖记录，会完整跑一遍）" if plan.testmon_new else ""))
            for test, why in sorted(plan.extra.items()):
                print(f"Python 另跑 {test} ← {why}")
            for module, packages in plan.go.items():
                print(f"Go {module}：{' '.join(packages)}")
            for test, why in sorted(plan.web.items()):
                print(f"前端 {test} ← {why}")
            if plan.web_done:
                print(f"前端另有 {plan.web_done} 个受影响的文件已在当前内容下通过过，不再跑")
            if plan.uncovered:
                print("没有测试覆盖到：" + "、".join(plan.uncovered))
            if plan.empty():
                print("没有需要跑的测试")
            return 0

        # A test keeps the last few states it passed in, so changing a file back needs no rerun.
        passed = {t: [h] if isinstance(h, str) else h for t, h in state.get("web_passed", {}).items() if t in set(web.tests)}
        if plan.empty():
            print(("自上次通过后没有改动" if not plan.changed else "改动碰到的测试都已在当前内容下通过过") + "，不需要跑测试")
            if plan.uncovered:
                print("没有测试覆盖到：" + "、".join(plan.uncovered))
            settled = settle(plan, web, {}, passed)
            save_state({**state, "snapshot": {f: sha for f, sha in current.items() if f not in plan.changed or f in settled}
                        | {f: (state.get("snapshot") or {}).get(f, "?") for f in plan.changed - settled}})
            return 0

        logs = STATE / "logs" / time.strftime("%Y%m%d-%H%M%S")
        logs.mkdir(parents=True)
        for old in sorted((STATE / "logs").iterdir())[:-KEEP_LOGS]:
            shutil.rmtree(old, ignore_errors=True)

        # One after another: run side by side they slow each other into timeouts.
        lanes: dict[str, Lane] = {}
        stop = deadline - 15
        steps = []
        if plan.python:
            steps.append(("python", lambda: run_testmon(logs, stop, args.full)))
        if plan.extra:
            steps.append(("python-extra", lambda: run_extra(sorted(plan.extra), logs, stop)))
        for module, packages in plan.go.items():
            if packages:
                steps.append((f"go:{module}", lambda m=module, p=packages: run_go(m, p, logs, stop)))
        if plan.web:
            # The frontend runs after the backend, alone, so it takes every core (measured: 8 beat 4 by ~20%).
            steps.append(("web", lambda: run_web(sorted(plan.web), logs, stop, CPUS,
                                                 longest=state.get("web_seconds"))))
        for name, step in steps:
            if time.monotonic() >= stop - 5:
                lanes[name] = Lane(name, complete=False, note="时间用完，这一项没来得及跑")
                if name == "web":
                    lanes[name].left = len(plan.web)
                continue
            lanes[name] = step()

        notes = []
        if not args.no_baseline:
            notes.append(rerun_alone(list(lanes.values()), logs, deadline))
            notes.append(compare_with_head(list(lanes.values()), args.base, logs, deadline))

        lane = lanes.get("web")
        if lane:
            for test in (lane.units - lane.failed_units) | lane.alone:
                now = web.fingerprint(test)
                passed[test] = [now, *(h for h in passed.get(test, []) if h != now)][:5]
            state["web_seconds"] = {**state.get("web_seconds", {}), **lane.unit_seconds}
        settled = settle(plan, web, lanes, passed)
        previous = state.get("snapshot") or {}
        snapshot = {f: sha for f, sha in current.items() if f not in plan.changed or f in settled}
        snapshot.update({f: previous.get(f, "?") for f in plan.changed - settled})
        save_state({**state, "snapshot": snapshot, "web_passed": passed})
        return report(plan, lanes, notes, logs, args.base)


if __name__ == "__main__":
    sys.exit(main())
