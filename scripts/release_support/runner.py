"""Fixed stage graph. Normal releases run here without model interventions."""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import time
from pathlib import Path

from . import build, candidate, node_compat, scope, versions
from .common import Commands, ReleaseError, State, atomic_json, fingerprint, load, process_ticks, sha
from .github import GitHub
from .publication import asset_metadata, publish

TEST_STAGES = ("py311", "py312", "py313", "frontend")
DEFAULT_IMAGES = {"pythonImages": {v: f"openbear-release-python:{version}" for v, version in
                                  (("311", "3.11"), ("312", "3.12"), ("313", "3.13"))},
                  "systemdImage": "openbear-release-systemd:local"}


def execution_environment(environment, stage):
    """Bind evidence only to tools used by this stage, not sibling runtimes."""
    identity = {key: environment[key] for key in ("nodeImage", "nodeKey")}
    if stage.startswith("py") or stage == "frontend-checks":
        identity["pythonImage"] = environment["pythonImages"][stage[2:] if stage.startswith("py") else "312"]
    return identity


def acceptance_fingerprint(support):
    paths = [support / "acceptance.py", *sorted((support / "acceptance_helpers").rglob("*.py"))]
    return fingerprint({str(path.relative_to(support)): sha(path.read_bytes()) for path in paths})


def verified_build(result, root=None, identity=None):
    """Verify the entire compiled tree and immutable manifest, not just index.html."""
    try:
        outputs = result["outputs"]
        manifest = Path(outputs["manifest"])
        record = load(manifest)
        files = build.tree_files(Path(outputs["dist"]))
        if (not result.get("ok") or "index.html" not in files or files != record["files"]
                or build.digest_json(files) != outputs["distSha256"]
                or sha(manifest.read_bytes()) != outputs["manifestSha256"]):
            return False
        if identity is not None and record["identity"] != identity:
            return False
        if root is not None and (load(root / "manifest.json") != record or build.tree_files(root / "dist") != files):
            return False
        log = Path(outputs["log"])
        return log.is_file() and sha(log.read_bytes()) == outputs["logSha256"]
    except (OSError, ValueError, KeyError, build.ReleaseError):
        return False


# Tests that build the Go node, drive a real Chrome, run the built Go gateway or call
# ssh-keygen; the matrix images carry none of them, and gateway/bin is a build output
# outside the exported source. The node's own tests run in the node release's Go container.
NODE_TOOLCHAIN_SKIPS = (("tests.test_servers::", "Go toolchain is not available"),
                        ("tests.test_servers_transfers::", "Go toolchain is not available"),
                        ("tests.test_node_browser::", "needs Chrome"),
                        ("tests.test_browser_engine_parity::", "needs google-chrome and Go"),
                        ("tests.test_gateway_update::", "needs the built gateway program"),
                        ("tests.test_instance_hardening::", "gateway binary not built"),
                        ("tests.test_servers_hardening::", "ssh-keygen is not available"))


def matrix_contract(results):
    backends = [results[name] for name in TEST_STAGES[:3]]
    suites = []
    for result in backends:
        cases = result["testCases"]
        ids = [item["id"] for item in cases]
        if not ids or len(ids) != len(set(ids)):
            raise ReleaseError("Backend test collection is empty or contains duplicate IDs")
        for case in cases:
            if case["status"] == "skipped":
                name = case["id"]
                reason = case.get("skipReason", "")
                # Real-upstream tests, and real-systemd tests that need PID 1 systemd
                # (the matrix containers have none; A/B run the unit under systemd).
                approved = ((name.startswith(("tests.test_live_parrot::", "tests.test_live_read_images::")) and "--run-live" in reason)
                            or (name.startswith("tests.test_systemd_watchdog_live::") and "OPENBEAR_SYSTEMD_TEST=1" in reason)
                            or (name.startswith("tests.test_servers_bench_live::") and reason.endswith("set OPENBEAR_BENCH_LIVE=1 to run a real benchmark"))
                            or any(name.startswith(module) and why in reason for module, why in NODE_TOOLCHAIN_SKIPS))
                if not approved:
                    raise ReleaseError(f"Unexpected conditional skip requires review: {name}: {case.get('skipReason')}")
            elif case["status"] != "passed":
                raise ReleaseError("Backend test result was not passed: " + case["id"])
        suites.append({item["id"]: (item["status"], item.get("skipReason", "")) for item in cases})
    if suites[0] != suites[1] or suites[1] != suites[2]:
        raise ReleaseError("Python versions did not execute identical test collections/statuses")
    if not all(result.get("ok") for result in results.values()):
        raise ReleaseError("A matrix stage failed")
    return {"ok": True, "sameBackendTestCases": True, "casesPerPython": len(suites[0]), "results": results}


class Runner:
    versioning = candidate.APP_VERSIONING
    resume_command = "scripts/release.py resume"

    def __init__(self, repo: Path, directory: Path, value: dict, *, github=None, command=None):
        self.repo, self.directory = repo, directory
        self.state = State(directory, value)
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        self.github = github or GitHub(value["repository"], token)
        self.command = command or Commands(directory, token, value.get("timeout", 1800))
        self.support = Path(__file__).resolve().parent
        self.cache = repo / ".release-runs/cache"

    @property
    def value(self):
        return self.state.value

    def cleanup_owned(self):
        if not self.value.get("dockerReady"):
            return {"ok": True, "ownedResourcesRemaining": []}
        label = "openbear.release.run=" + self.value["runId"]
        removed = []
        # Labels are checked by Docker; never global prune or name substrings.
        for kind, list_args in (("container", ["ps", "-aq"]), ("network", ["network", "ls", "-q"]), ("volume", ["volume", "ls", "-q"])):
            ids = self.command(["docker", *list_args, "--filter", "label=" + label], "cleanup-list").splitlines()
            for object_id in ids:
                inspect = json.loads(self.command(["docker", kind, "inspect", object_id], "cleanup-inspect"))[0]
                labels = inspect.get("Labels") or inspect.get("Config", {}).get("Labels") or {}
                if labels.get("openbear.release.run") != self.value["runId"]:
                    raise ReleaseError("Cleanup object does not belong to this run", code=2)
                args = ["docker", kind, "rm"] + (["-f"] if kind == "container" else []) + [object_id]
                self.command(args, "cleanup-owned")
                removed.append({"kind": kind, "id": object_id})
        return {"ok": True, "removed": removed, "ownedResourcesRemaining": []}

    def preflight(self, approved: bool):
        complete = all(self.value.get("stages", {}).get("acceptance-" + case, {}).get("status") == "passed" for case in ("A", "B"))
        if not approved and not complete:
            raise ReleaseError("This release needs explicitly authorized private-container cgroup remounts; resume with --approve-container-cgroup after obtaining this run's authorization", code=2)
        if not self.github.token:
            raise ReleaseError("Provide GH_TOKEN (or GITHUB_TOKEN) in the environment; it is never read from application data")
        self.command(["docker", "info", "--format", "{{.ServerVersion}}"], "docker-info")
        self.value["dockerReady"] = True
        self.state.save()
        self.command.recover_children()
        self.cleanup_owned()
        if not self.value.get("previous"):
            permission = self.github.api("")
            if not permission.get("permissions", {}).get("push"):
                raise ReleaseError("GitHub credential lacks push access for the requested repository")
            version = versions.target(self.value["version"])
            tag = "v" + version
            if self.github.api("/releases/tags/" + tag, missing=True) or self.github.api("/git/ref/tags/" + tag, missing=True):
                raise ReleaseError("Version/tag already exists; do not start a duplicate release")
            published = versions.published(self.github, "v")
            versions.require_newer(version, published, "v")
            # Upgrade acceptance and the change scope start from the latest release
            # (/releases/latest never returns a pre-release): what users install.
            previous = self.github.api("/releases/latest")
            old = str(previous["tag_name"]).removeprefix("v")
            if previous["draft"] or previous["prerelease"] or not versions.written(old) or versions.prerelease(old):
                raise ReleaseError("Previous release is not a stable formal version")
            if versions.versioning.compare(old, version) >= 0:
                raise ReleaseError("Target version must exceed the actual previous formal release")
            self.value.update(previous=previous, previousVersion=old,
                              publicBase=self.github.api("/git/ref/heads/main")["object"]["sha"])
            base = versions.changelog_base(version, sorted({*published, old}, key=lambda v: versions.versioning.parse(v).key)) or old
            compare = f"https://github.com/{self.value['repository']}/compare/v{base}...{tag}"
            notes = self.value["notes"]
            if "Full Changelog" in notes and compare not in notes:
                raise ReleaseError("Release notes have an incorrect Full Changelog range")
            if "Full Changelog" not in notes:
                self.value["notes"] = notes.rstrip() + f"\n\n**Full Changelog**: {compare}\n"
            (self.directory / "notes.md").write_text(self.value["notes"], encoding="utf-8")
            self.state.save()
        if "nodeCompatibility" not in self.value:
            # Advisory only: the node is optional and released on its own line.
            self.value["nodeCompatibility"] = node_compat.check(self.repo, self.github, versions.prerelease(self.value["version"]))
            self.state.save()
            if not self.value["nodeCompatibility"]["ok"]:
                print("Warning: " + self.value["nodeCompatibility"]["warning"], flush=True)
        if not self.value.get("previousSourceCommit"):
            ref = self.github.api("/git/ref/tags/v" + self.value["previousVersion"])["object"]
            if ref["type"] == "tag":
                ref = self.github.api("/git/tags/" + ref["sha"])["object"]
            if ref["type"] != "commit":
                raise ReleaseError("Previous formal tag does not resolve to a commit")
            self.value["previousSourceCommit"] = ref["sha"]
            self.state.save()
        resolved = {}
        for name, image in {**self.value["environment"]["pythonImages"], "systemd": self.value["environment"]["systemdImage"]}.items():
            metadata = json.loads(self.command(["docker", "image", "inspect", image], "image-inspect"))[0]
            resolved[name] = metadata["Id"]
        self.value["images"] = resolved
        self.state.save()

    def previous_assets(self):
        previous = self.value["previous"]
        root = self.directory / "previous-assets"
        root.mkdir(exist_ok=True)
        required = {"install.sh", f"openbear-{self.value['previousVersion']}.zip", "SHA256SUMS", "release-meta.json"}
        assets = {item["name"]: item for item in previous["assets"] if item["name"] in required}
        if set(assets) != required:
            raise ReleaseError("Previous formal release does not have the four required assets")
        expected = {}
        for name, item in assets.items():
            target = root / name
            saved = self.value.get("previousAssetsMetadata", {}).get(name)
            if saved and asset_metadata(root, {name: saved}):
                expected[name] = saved
                continue
            data = self.github.download_asset(item)
            if len(data) != item["size"] or (item.get("digest") and item["digest"] != "sha256:" + sha(data)):
                raise ReleaseError("Previous asset size/digest mismatch: " + name)
            temporary = root / (name + ".part")
            temporary.write_bytes(data)
            temporary.replace(target)
            expected[name] = {"bytes": len(data), "sha256": sha(data)}
            self.value.setdefault("previousAssetsMetadata", {})[name] = expected[name]
            self.state.save()
        archive = f"openbear-{self.value['previousVersion']}.zip"
        if f"{expected[archive]['sha256']}  {archive}" not in (root / "SHA256SUMS").read_text().splitlines():
            raise ReleaseError("Previous release SHA256SUMS does not match its ZIP")
        if load(root / "release-meta.json")["version"] != self.value["previousVersion"]:
            raise ReleaseError("Previous release metadata version mismatch")
        return {"ok": True, "directory": str(root), "assets": expected}

    def classify_scope(self):
        source = self.directory / "source"
        commit = self.value["previousSourceCommit"]
        self.command(["git", "fetch", "--depth", "1", "origin", commit], "previous-source", cwd=source)
        result = scope.from_checkout(source, self.value["candidate"], commit)
        atomic_json(self.directory / "scope.json", result)
        return result

    def build_config(self):
        return {"schema": 1, "runId": self.value["runId"], "version": self.value["version"],
                "previousVersion": self.value["previousVersion"], "sourceDir": str(self.directory / "source"),
                "runDir": str(self.directory), "cacheDir": str(self.cache), "previousAssets": str(self.directory / "previous-assets"),
                "pythonImages": {k: self.value["images"][k] for k in ("311", "312", "313")},
                "systemdImage": self.value["images"]["systemd"], "releaseScope": self.value.get("scope")}

    def build_stage(self, name):
        self.command([self.value["python"], str(self.support / "build.py"), "--config", str(self.directory / "build-config.json"), "--stage", name], name)
        result = load(self.directory / "results" / name / "result.json")
        if not result.get("ok"):
            raise ReleaseError(name + " failed; see its result/log")
        return result

    def acceptance(self, case, approved):
        if not approved:
            raise ReleaseError("Container cgroup approval is required for this invocation", code=2)
        package = self.value["stages"]["package"]["result"]
        cfg = {"schema": 1, "runId": self.value["runId"], "version": self.value["version"],
               "previousVersion": self.value["previousVersion"], "currentAssets": str(self.directory / "assets"),
               "previousAssets": str(self.directory / "previous-assets"), "assets": package["assets"],
               "previousAssetsMetadata": self.value["previousAssetsMetadata"], "image": self.value["images"]["systemd"],
               "outputDir": str(self.directory / "acceptance"), "cacheDir": str(self.cache / "uv")}
        path = self.directory / ("acceptance-config-" + case + ".json")
        atomic_json(path, cfg)
        self.command([self.value["python"], str(self.support / "acceptance.py"), "--config", str(path), "--case", case, "--approve-container-cgroup"], "acceptance-" + case)
        result = load(self.directory / "acceptance" / ("case-" + case.lower()) / "report.json")
        expected = package["assets"][f"openbear-{self.value['version']}.zip"]["sha256"]
        if not result.get("ok") or not result["cleanup"]["ok"] or result["zipSha256"] != expected:
            raise ReleaseError("Real installation/upgrade was not cleanly verified: " + case)
        return result

    def test_cache_key(self, name, environment):
        release_scope = self.value.get("scope")
        if not release_scope:
            return None
        component = "backend" if name.startswith("py") else "frontend"
        source = release_scope.get(component + "StageFingerprint")
        if name == "frontend-checks":
            backend, frontend = release_scope.get("backendStageFingerprint"), release_scope.get("frontendFingerprint")
            if not backend or not frontend:
                return None
            # Unlike the reusable main matrix, these checks cover both sides.
            source = {"backend": backend, "frontend": frontend, "checks": list(scope.FRONTEND_CHECKS)}
        if not source:
            return None  # Old scope schemas cannot establish the new input contract.
        return {"policy": scope.POLICY, "stage": name,
                "source": source,
                "environment": execution_environment(environment, name)}

    def cached_test(self, name, environment):
        key = self.test_cache_key(name, environment)
        if not key:
            return None
        path = self.cache / "tests" / (fingerprint(key) + ".json")
        try:
            record = load(path)
            if record["key"] == key and scope.verified_result(record["result"]):
                origin = record["result"].get("evidenceReuse", {}).get("runId", record["runId"])
                return {**record["result"], "evidenceReuse": {"runId": origin, "key": key}}
        except (OSError, ValueError, KeyError):
            pass
        return None

    def build_once(self, environment):
        inputs = scope.build_identity(self.value.get("scope", {}).get("frontendStageFingerprint")
            or self.value["candidate"]["sourceFingerprint"], environment, self.value["version"])

        def operation():
            root = self.directory / "build"
            # Different inputs need a fresh canonical build, never an overwrite
            # of its immutable attempt evidence. Same-input corruption fails in
            # build.py instead of silently trusting an index.html existence test.
            if root.exists() and load(root / "manifest.json").get("identity") != inputs:
                archive = self.directory / "superseded" / ("build-" + str(time.time_ns()))
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(root), archive)
            return self.build_stage("build")

        return self.state.execute("build", inputs, operation,
            valid=lambda result: verified_build(result, self.directory / "build", inputs))

    def parallel(self, operations):
        """Join every owned job before cleanup; one failure blocks the next gate."""
        results, errors = {}, []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            futures = {pool.submit(operation): name for name, operation in operations.items()}
            for future in concurrent.futures.as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise ReleaseError("Parallel stage failed: " + "; ".join(errors))
        except BaseException:
            self.command.cancel_all()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return results

    def run_tests(self, environment, *, with_build=False):
        built = {}
        cached = {name: self.cached_test(name, environment) for name in TEST_STAGES}

        def stage_inputs(name):
            return self.test_cache_key(name, environment) or {
                "candidate": self.value["candidate"]["sourceFingerprint"],
                "environment": execution_environment(environment, name), "policy": scope.POLICY}

        def same_candidate_reuse(name):
            prior = self.value.get("stages", {}).get(name, {})
            return (prior.get("status") == "passed" and prior.get("fingerprint") == fingerprint(stage_inputs(name))
                    and scope.verified_result(prior["result"]))

        def checks_passed(result):
            cases = result.get("testCases", [])
            return bool(cases) and all(case["status"] == "passed" for case in cases)

        def execute(name):
            key = self.test_cache_key(name, environment)

            def operation():
                result = cached.get(name) or self.build_stage(name)
                if name == "frontend-checks" and not checks_passed(result):
                    raise ReleaseError("Frontend Python checks must all pass without skips")
                return result

            result = self.state.execute(name, stage_inputs(name), operation,
                valid=lambda result: scope.verified_result(result) and (name != "frontend-checks" or checks_passed(result)))
            # Persist each success before starting dependent work or waiting for
            # siblings. A failed Python job/build cannot swallow this evidence.
            if key and scope.verified_result(result):
                atomic_json(self.cache / "tests" / (fingerprint(key) + ".json"),
                            {"key": key, "runId": self.value["runId"], "result": result})
            if name == "frontend" and with_build:
                # Frontend test completion gates compilation, not sibling Python jobs.
                built.update(self.build_once(environment))
            return result

        operations = {name: lambda name=name: execute(name) for name in TEST_STAGES}
        if self.value.get("scope", {}).get("frontendChanged") and any(
                cached[name] or same_candidate_reuse(name) for name in TEST_STAGES[:3]):
            cached["frontend-checks"] = self.cached_test("frontend-checks", environment)
            operations["frontend-checks"] = lambda: execute("frontend-checks")
        results = self.parallel(operations)
        matrix = matrix_contract(results)
        matrix["reusedStages"] = [name for name, result in results.items()
                                  if result.get("evidenceReuse") or self.value["stages"][name].get("lastReusedAt")]
        return (matrix, built) if with_build else matrix

    def report(self):
        value = self.value
        stages = value.get("stages", {})
        times = [{"stage": name, "status": result["status"],
                  "seconds": round(result.get("finishedAt", time.time()) - result["startedAt"], 3)} for name, result in stages.items()]
        active = sum(invocation.get("finishedAt", time.time()) - invocation["startedAt"] for invocation in value.get("invocations", []))
        elapsed = value.get("completedAt", time.time()) - value["createdAt"]
        result = {"runId": value["runId"], "version": value["version"], "completed": bool(value.get("completed")),
                  "candidate": {k: v for k, v in value.get("candidate", {}).items() if k != "files"}, "stages": times,
                  "wallSeconds": round(elapsed, 3), "scriptActiveSeconds": round(active, 3),
                  "outsideScriptSeconds": round(max(0, elapsed - active), 3), "lastError": value.get("lastError"),
                  "publication": stages.get("publication", {}).get("result"), "cleanup": value.get("cleanup"),
                  "scope": value.get("scope"), "nodeCompatibility": value.get("nodeCompatibility"),
                  "development": value.get("development"),
                  "developmentInstance": "No online dist update or service restart is performed by this entrypoint."}
        atomic_json(self.directory / "report.json", result)
        print(json.dumps({"runId": value["runId"], "completed": result["completed"], "report": str(self.directory / "report.json"),
                          "url": (result.get("publication") or {}).get("url"), "lastError": result["lastError"]}, ensure_ascii=False), flush=True)

    def finish_cleanup(self):
        self.value["cleanup"] = self.cleanup_owned()
        if self.value.get("published"):
            for name in ("source", "build", "package", "previous-assets"):
                path = self.directory / name
                if path.exists() and not path.is_symlink():
                    shutil.rmtree(path)
            # Keep result/log evidence, not duplicate package/previous extractions.
            for attempt in (self.directory / "results/package/attempts").glob("*"):
                for name in ("package", "previous", "assets"):
                    path = attempt / name
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
            self.value["completed"] = True
            self.value.setdefault("completedAt", time.time())

    def develop(self):
        """After publication: the local version becomes the next minor's dev.1 (one local commit, never pushed).
        Runs started before this rule (no startsDevelopment) are left as they finished."""
        if not self.value.get("startsDevelopment"):
            return None
        if not self.value.get("development"):
            try:
                result = candidate.start_development(self.repo, self.state, self.command, self.versioning)
            except BaseException as exc:
                self.value["lastError"] = {"type": type(exc).__name__, "message": self.command.redact(str(exc)),
                    "exitCode": getattr(exc, "code", 1), "published": True,
                    "resume": f"{self.value['python']} {self.resume_command} {self.value['runId']}"}
                self.state.save()
                raise
            print(f"Local version is now {result['version']} (local commit, not pushed)", flush=True)
        self.value.pop("lastError", None)
        self.state.save()
        return self.value["development"]

    def finish_published(self):
        # A cleanup failure never invalidates successful publication.
        self.command.recover_children()
        self.finish_cleanup()
        self.value.pop("lastError", None)
        self.state.save()
        try:
            self.develop()
        finally:
            self.report()

    def run(self, *, approved=False):
        if self.value.get("completed") and (self.value.get("development") or not self.value.get("startsDevelopment")):
            self.report()
            return
        if self.value.get("published"):
            self.finish_published()
            return
        invocation = {"startedAt": time.time(), "pid": os.getpid(), "startTicks": process_ticks(os.getpid())}
        self.value.setdefault("invocations", []).append(invocation)
        self.state.save()
        try:
            self.preflight(approved)
            candidate.freeze(self.repo, self.directory, self.state, self.command)
            identity = self.value["candidate"]
            self.state.execute("source", identity["sourceFingerprint"], lambda: candidate.export(self.repo, self.directory, self.state, self.command),
                valid=lambda result: candidate.source_matches(self.directory / "source", identity))
            previous = self.state.execute("previous-assets", self.value["previous"]["id"], self.previous_assets,
                valid=lambda result: asset_metadata(self.directory / "previous-assets", result["assets"]))
            self.value["scope"] = self.state.execute("scope", {"candidate": identity["sourceFingerprint"],
                "previousCommit": self.value["previousSourceCommit"], "policy": scope.POLICY}, self.classify_scope)
            self.state.save()
            print("Release scope: " + self.value["scope"]["kind"], flush=True)
            atomic_json(self.directory / "build-config.json", self.build_config())
            self.state.execute("env", {"images": {k: self.value["images"][k] for k in ("311", "312", "313")},
                                     "dependencies": build.normalize_dependencies(self.directory / "source")},
                                     lambda: self.build_stage("env"), valid=lambda _: (self.directory / "environment.json").is_file())
            environment = load(self.directory / "environment.json")
            matrix, built = self.run_tests(environment, with_build=True)
            atomic_json(self.directory / "matrix-summary.json", matrix)
            candidate.assert_local(self.repo, self.value, self.command)
            package = self.state.execute("package", {"candidate": identity["sourceFingerprint"], "build": built, "previous": previous}, lambda: self.build_stage("package"),
                                         valid=lambda result: asset_metadata(self.directory / "assets", result["assets"]))
            operations = {"smoke": lambda: self.state.execute("smoke",
                {"assets": package["assets"], "validator": sha((self.support / "build.py").read_bytes()),
                 "image": environment["pythonImages"]["312"]}, lambda: self.build_stage("smoke"))}
            harness = acceptance_fingerprint(self.support)
            for case in ("A", "B"):
                operations["acceptance-" + case] = lambda case=case: self.state.execute("acceptance-" + case,
                    {"assets": package["assets"], "previous": previous, "image": self.value["images"]["systemd"], "harness": harness},
                    lambda: self.acceptance(case, approved))
            self.parallel(operations)
            candidate.assert_local(self.repo, self.value, self.command)
            prerelease = versions.prerelease(self.value["version"])
            self.state.execute("publication", {"candidate": identity, "notes": self.value["notes"], "assets": package["assets"]},
                               lambda: publish(self.directory, self.state, self.command, self.github, latest=not prerelease, prerelease=prerelease))
            self.value.pop("lastError", None)
            self.value["published"] = True
        except BaseException as exc:
            self.value["lastError"] = {"type": type(exc).__name__, "message": self.command.redact(str(exc)),
                "exitCode": getattr(exc, "code", 130 if isinstance(exc, KeyboardInterrupt) else 1),
                "resume": f"{self.value['python']} {self.resume_command} {self.value['runId']}"}
            raise
        finally:
            self.command.cancel_all()
            try:
                self.finish_cleanup()
            except Exception as exc:
                self.value["cleanup"] = {"ok": False, "error": str(exc)}
                self.value["lastError"] = {"message": "Cleanup failed: " + str(exc)}
                raise
            finally:
                invocation["finishedAt"] = time.time()
                self.state.save()
                self.report()
        try:
            self.develop()
        finally:
            self.report()
